"""Caller silence after the agent spoke (demo_session.arm_silence_check): one "are you still there?" from the
model, then the closing line and the end of the call; never during the "anything else?" countdown."""
import asyncio
from types import SimpleNamespace

from demo_session import SILENCE_INSTRUCTION, DemoSession


class Stub:
    arm_silence_check = DemoSession.arm_silence_check
    cancel_silence_check = DemoSession.cancel_silence_check

    def __init__(self, **state):
        self.settings = SimpleNamespace(silence_check_s=0.05)
        self.mode, self.closed, self.finished, self.completion_pending, self.end_after_playback = "voice", False, False, False, False
        self.caller_speaking, self.playing, self.turn, self.filler_task = False, False, None, None
        self.history, self.lock, self.events, self.replies = [{"role": "assistant", "content": "How can I help?"}], asyncio.Lock(), [], []
        self._silence_task, self._silence_checks = None, 0
        self.__dict__.update(state)

    async def trace_event(self, kind, **data):
        self.events.append(data["action"])

    async def respond(self, **kwargs):
        self.replies.append(kwargs)

    def silence_warning(self):
        return "I haven't heard from you, so I'll end the call in a few seconds."


def run(stub, rounds=1):
    async def go():
        for _ in range(rounds):
            stub.arm_silence_check()
            await asyncio.sleep(0.12)
    asyncio.run(go())
    return stub


def test_one_check_in_then_the_closing_line():
    s = run(Stub(), rounds=2)
    assert s.events == ["check_in", "close"]
    assert s.replies[0] == {"silence": True}                                # the model asks "are you still there?"
    assert s.replies[1]["script"].startswith("I haven't heard") and s.end_after_playback


def test_the_model_is_told_the_caller_was_silent():
    note = SILENCE_INSTRUCTION.format(seconds=7)
    assert "CALLER SILENT" in note and "said nothing for 7 seconds" in note and "still there" in note


def test_no_check_when_the_caller_speaks_or_the_closing_countdown_runs():
    s = Stub()

    async def go():
        s.arm_silence_check()
        await asyncio.sleep(0.01)
        s.history.append({"role": "user", "content": "my balance"})          # the caller spoke in time
        await asyncio.sleep(0.1)
    asyncio.run(go())
    assert s.events == [] and s.replies == []
    assert run(Stub(completion_pending=True)).events == []                   # "anything else?" owns the silence
    assert run(Stub(mode="text")).events == []                               # voice calls only
