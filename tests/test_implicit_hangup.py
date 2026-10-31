"""Exercise real transcript, reply and closing code with local provider doubles."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import demo_session
from config import Settings
from csm import orchestrator, persona


class Socket:
    def __init__(self):
        self.closed = False
        self.messages = []
        self.audio = []

    async def send_json(self, message):
        self.messages.append(message)

    async def send_bytes(self, chunk):
        self.audio.append(chunk)

    async def close(self):
        self.closed = True


class Speech:
    def reset(self):
        pass

    def cancel(self):
        pass

    async def synthesize(self, text, language):
        yield b"\x00\x00" * 240


class Model:
    reply = "<lang:en-IN><HANGUP> Thank you for calling. Goodbye."

    def reset(self):
        pass

    def cancel(self):
        pass

    async def generate(self, messages):
        yield self.reply


@pytest.fixture
def make_session(monkeypatch):
    monkeypatch.setattr(demo_session, "LLMProcessor", lambda settings: Model())
    monkeypatch.setattr(demo_session, "create_stt_processor", lambda settings: None)
    monkeypatch.setattr(demo_session, "create_tts_processor", lambda *args: Speech())
    monkeypatch.setattr(orchestrator, "services", lambda: None)

    def make():
        csm = SimpleNamespace(
            agent={"name": "Aria", "gender": "female"},
            lines=persona.lines("en"),
            state=SimpleNamespace(call_id="hangup-test"),
            svc=SimpleNamespace(audit=SimpleNamespace(record=Mock())),
            outbound=lambda text: text,
        )
        monkeypatch.setattr(orchestrator, "Orchestrator", lambda *args, **kwargs: csm)
        session = demo_session.DemoSession(
            Settings(_env_file=None, cost_tracking_enabled=False,
                     turn_completion=False, tts_provider="smallest",
                     studio_playback_timeout_s=2),
            Socket(), mode="voice",
        )
        session._csm_plan = AsyncMock(return_value=None)
        # Summary generation is outside this test; the real closing flow must
        # invoke it and close the socket only after playback acknowledgement.
        session.finish = AsyncMock()
        return session

    return make


async def acknowledge_and_check_close(session):
    assert session.ws.audio
    assert not session.ws.closed
    session.finish.assert_not_awaited()
    closing = session.closing_task
    assert closing is not None
    await session.playback_received(session.generation)
    await asyncio.wait_for(closing, 1)
    session.finish.assert_awaited_once()
    assert session.ws.closed
    assert not any(m["type"] == "error" for m in session.ws.messages)


@pytest.mark.parametrize("text", ["Goodbye", "Thanks, bye", "Nothing.", "nothing else", "That's it.", "कुछ नहीं"])
def test_caller_finishes_then_farewell_plays_before_socket_closes(make_session, text):
    async def run():
        session = make_session()
        session.history = [{"role": "assistant", "content": "Is there anything else I can help you with?"}]
        session.final_help_offered = session.completion_pending = True
        await session.transcript(text, True)
        await session.turn
        assert session.end_after_playback
        assert session.history[-1]["content"] == session.csm.lines["goodbye"]
        await acknowledge_and_check_close(session)
    asyncio.run(run())


@pytest.mark.parametrize("reply,ends", [
    ("<lang:en-IN><HANGUP> Thank you for calling. Goodbye.", True),
    ("<lang:en-IN><HANGUP> What else do you need?", False),
    ("<lang:en-IN> What else do you need?", False),
])
def test_model_control_tag_through_real_reply_pipeline(make_session, reply, ends):
    async def run():
        session = make_session()
        session.llm.reply = reply
        session.history = [{"role": "user", "content": "I am all sorted for today."}]
        await session.respond()
        assert session.end_after_playback is ends
        assert "<HANGUP>" not in session.history[-1]["content"]
        if ends:
            await acknowledge_and_check_close(session)
        else:
            assert session.closing_task is None and not session.ws.closed
            session.finish.assert_not_awaited()
    asyncio.run(run())


def test_new_request_cancels_pending_closure(make_session):
    async def run():
        session = make_session()
        session.history = [{"role": "assistant", "content": "Is there anything else I can help you with?"}]
        session.completion_pending = session.final_help_offered = True
        session.arm_closing()
        old_closing = session.closing_task
        session.llm.reply = "<lang:en-IN> Which account do you mean?"
        await session.transcript("Wait, I also need my balance.", True)
        await session.turn
        assert old_closing.cancelled()
        assert not session.end_after_playback and not session.completion_pending
        assert session.closing_task is None and not session.ws.closed
        session.finish.assert_not_awaited()
    asyncio.run(run())


def test_continued_silence_closes_after_warning_playback(make_session):
    async def run():
        session = make_session()
        session.settings = session.settings.model_copy(update={"silence_check_s": 0.01})
        session.llm.reply = "<lang:en-IN> Are you still there?"
        session.history = [{"role": "assistant", "content": "How can I help?"}]
        session.arm_silence_check()
        await asyncio.wait_for(session._silence_task, 1)
        await session.turn
        assert session._silence_checks == 1 and not session.end_after_playback
        await session.playback_received(session.generation)
        session.playing = False
        await session.playback_finished()
        await asyncio.wait_for(session._silence_task, 1)
        await session.turn
        assert session.end_after_playback
        assert session.history[-1]["content"] == session.csm.lines["silence_close"]
        await acknowledge_and_check_close(session)
    asyncio.run(run())
