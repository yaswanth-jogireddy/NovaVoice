"""Small per-browser demo session reusing the repository's speech providers.

No Redis, Kafka, SIP or CRM dependency. Servers attach durable call storage. The browser
acknowledges audio playback so interruption remains possible after synthesis ends.
"""
import asyncio
import contextlib
import json
import re
import time

from pipeline.llm import LLMProcessor
from config import validate_browser_settings
from pipeline.providers import create_stt_processor
from pipeline.tts import create_tts_processor
from prompts.international_demo import LANGUAGE_ALIASES, LANGUAGE_NAMES
from utils.speech_text import to_spoken_text
from utils.conversation_turn import ConversationTurn
from utils.costs import CostTracker, attach_tracker
from prompts.playground import closing_intent, final_help_offer
from prompts.listening import generic_closer, listening_question
from utils.call_closing import CallClosing, CLOSING, final_refusal
from csm import persona, tiercost
from csm.guardrails import redact
from utils.logger import get_logger

logger = get_logger("demo_session")
# Characters before the first comma that may be spoken on its own (voice only).
FIRST_CLAUSE_CHARS = 24
# Item 5: caller audio kept for the end-of-turn model (16 kHz PCM16).
TURN_AUDIO_BYTES = 8 * 16000 * 2
TURN_PREROLL_BYTES = 16000
# A queue item taken early for TTS send-ahead (F4); None is the end marker.
_NOTHING = object()
# Tool routing slower than this plays the spoken filler line (only when no ack clips are ready).
CSM_FILLER_AFTER_S = 0.9
# No agent audio this long after the caller STOPPED SPEAKING: play a pre-recorded acknowledgement
# ("Sure."). Measured from the caller's last sound, not from when the reply starts, so it never lands
# right on top of their last word. CSM_ACK_DELAY_MS overrides it (500-2000).
ACK_DELAY_DEFAULT_MS = 1000
# Same phrases as scripts/make_ack_clips.py, which pre-records them per voice into app/acks/.
ACK_PHRASES = {"en": ("Sure, one moment.", "Okay, let me check.", "Thanks, one moment."),
               "hi": ("जी, एक पल।", "ठीक है, देखती हूँ।", "धन्यवाद, एक पल।")}
# Every model reply starts with <lang:xx-IN>: it picks the TTS language and follows the caller between languages.
LANG_TAG = re.compile(r"<\s*lang\s*:\s*([a-z]{2,3})(?:[-_][a-z]{2})?\s*>", re.I)
# English and Hindi for now. Sarvam and Smallest also speak bn, ta, te, kn, ml, mr, gu, pa and od: add them
# here and to prompt.md's Language section together.
TAG_LANGUAGES = ("en", "hi")
# Call control from the model: <HANGUP> ends the call after the reply, <OFFTOPIC> marks an unrelated caller turn.
CONTROL_TAG = re.compile(r"<\s*(HANGUP|OFF[\s_-]?TOPIC)\s*/?\s*>", re.I)
OFFTOPIC_LIMIT = 3   # unrelated caller turns in one call before it ends
# Sent to the model when the caller says nothing for silence_check_s after the agent spoke (arm_silence_check).
SILENCE_INSTRUCTION = (
    "\nCALLER SILENT: the caller has said nothing for {seconds} seconds since your last words. In the current "
    "language and in a few words, ask whether they are still there (for example \"Are you still there?\" or "
    "\"क्या आप लाइन पर हैं?\"). If your last question is still open, repeat it briefly. Say nothing else.")
LANGUAGE_SECTION = re.compile(r"^## Language\b.*?(?=^## |\Z)", re.S | re.M)   # prompt.md's switching rules
CONTROL_PROMPT = (
    "\n\n## Ending the call\n"
    "- If the caller asks something that has nothing to do with banking, loans or this call, say in one sentence "
    "that you can only help with Nova Trust banking and loan questions, and ask what they need.\n"
    "- Put <HANGUP> right after the language tag only when the call should end now: the caller said goodbye or that "
    "they need nothing else. Then say a short goodbye and ask nothing. Never use it while a request is still open.\n"
    "- Tags are never spoken.")


def intent_line(item):
    """One request and its result for the call summary: "Account balance: Solved · Jev", "Other: Not solved"."""
    return (f"{item['intent_label']}: {item['resolution_label']}" + (f" ({item['ticket_id']})" if item["ticket_id"] else "")
            + (" · Jev" if item.get("confidence") is not None else ""))


def answered_enquiry(facts):
    """True when this turn's facts answer an enquiry (a tool result or policy), not only identity or an OTP."""
    return any(f.startswith("policy ") or (" result: " in f and not f.startswith(("verify_identity", "check_otp")))
               for f in facts)


class DemoSession(CallClosing):
    def __init__(self, settings, ws, language="en", mode="voice", scenario="sales", context="", regional_style=None, trace=None, agent=None):
        from demo_languages import validate_hinglish, speech_settings
        validate_hinglish(settings, language, scenario, context, regional_style or settings.agent_accent)
        if language == 'hinglish':
            settings = settings.model_copy(update={**speech_settings(language), 'caller_language_switch': False})
        elif language in TAG_LANGUAGES:
            # No Hindi default: speech starts in the call's own language (SPEECH_DEFAULT_LANGUAGE was "hi").
            settings = settings.model_copy(update={'speech_default_language': language})
        validate_browser_settings(settings)
        # Facts come from the CSM orchestrator (bank tools + policy KB), not the old demo presets.
        self.knowledge = None
        self.settings, self.ws = settings, ws
        self.language, self.mode = language, mode
        self.scenario, self.context = scenario, context
        self.regional_style = regional_style or settings.agent_accent
        self.llm = LLMProcessor(settings)
        self.stt = create_stt_processor(settings) if mode == "voice" else None
        self.tts = create_tts_processor(settings, settings.tts_provider) if mode == "voice" else None
        self.costs = CostTracker(settings, mode="studio-" + mode)
        attach_tracker(self.costs, self.llm, self.stt, self.tts)
        # The CSM orchestrator: the model proposes tool calls, policy code decides (csm/orchestrator.py).
        from csm.orchestrator import Orchestrator, services
        from features import flags
        self.csm = Orchestrator(services(), flags(), self.costs.session_id, language, llm=self.llm, agent=agent)
        self._persona_prompt = persona.system_prompt(self.csm.agent["name"], self.csm.agent["gender"])
        self._trusted = set()   # fixed lines (greeting, refusals): spoken as written, no outbound rewrite
        self._reply_lang = None   # the current reply's <lang:..> tag (TTS language for Latin-script sentences)
        self._reply_tags = set()  # control tags seen in the current reply: "hangup", "offtopic"
        self._silence_task, self._silence_checks = None, 0   # "are you still there?" after the caller goes quiet
        self.offtopic_turns = 0   # caller turns the model marked <OFFTOPIC>; the call ends at OFFTOPIC_LIMIT
        self._language_pinned = False   # picked on the call page: caller speech and reply tags don't move it
        self._acks, self._ack_inflight, self._turn_scripted, self._last_ack = {}, False, False, None
        self._diagnostics = set()
        if self.tts is not None and hasattr(self.tts, 'on_stall'):
            self.tts.on_stall = self._tts_stalled
        self._cost_task = None
        self._flush_task = None
        self.history = []
        self.turn = None
        self.generation = 0
        self.playing = False
        self.closed = False
        self.last_spoken = ""
        self.last_audio_text = ""  # speech-only form, for echo detection
        self.latencies = []
        self.lock = asyncio.Lock()
        self.end_after_playback = False
        self.end_task = None
        self.finished = False
        self.finalized = False
        self._finish_task = None
        self._finish_caller = None
        self.interruptions = 0
        self.final_help_offered = False
        self.trace = trace
        self.trace_failed = False
        self.user_turn = 0
        # Turn completion: a "hmm"/"okay" heard while Maya speaks, and the timer
        # that nudges once after a thinking sound is followed by silence.
        self.pending_backchannel = None
        self.filler_task = None
        self.caller_speaking = False
        self._caller_quiet = asyncio.Event()
        self._caller_quiet.set()
        self._waiting_for_completion = False
        self._awaiting_caller_final = False
        # Speech-trained provider VAD (Sarvam START/END_SPEECH), separate from the
        # browser's energy gate, plus end-of-speech timing for endpointing work.
        self._stt_speech_active = False
        self._stt_speech_at = 0.0
        self._stt_vad_seen = False
        self._onset_at = 0.0
        self._released_at = None
        self._final_timing = {}
        # Item 5: optional end-of-turn model and the caller's recent audio for it.
        self._turn_model = None
        self._turn_audio = bytearray()
        self._turn_hold_until = 0.0
        self._turn_task = None
        self.init_closing()

    @property
    def turn_completion_enabled(self):
        return self.settings.turn_completion and self.mode == 'voice'

    async def caller_activity(self):
        """Microphone onset gates new replies; playing audio keeps its barge-in path.

        Called under the session lock by either server. Do not treat a pause in
        the caller's own thought as an interruption of Maya's audible speech.
        """
        if self.closed or self.finished:
            return
        if self.turn_completion_enabled:
            if self._turn_model is not None and (self._released_at is None or time.monotonic() - self._released_at > 2):
                # A new turn: score this turn's audio, with 0.5 s of lead-in.
                del self._turn_audio[:-TURN_PREROLL_BYTES]
            self._turn_hold_until = 0.0
            self.caller_speaking = True
            self._onset_at = time.monotonic()
            self._released_at = None
            self._cancel_flush()
            self._caller_quiet.clear()
            self._awaiting_caller_final = True
            self.cancel_filler_nudge()
            self.cancel_silence_check()
            # A reply still being prepared is not cancelled on onset any more (background noise dropped
            # replies live): it holds before its first audio until the caller is quiet, and a real caller
            # turn replaces it when its transcript arrives.
            if not self.playing and self.turn and not self.turn.done():
                await self.trace_event('turn_completion', action='hold_unspoken_reply')
        await super().caller_activity()

    async def caller_speech_end(self):
        if self.closed or self.finished or not self.turn_completion_enabled:
            return
        self.caller_speaking = False
        self._released_at = time.monotonic()
        self._caller_quiet.set()
        self._schedule_flush()
        if self._turn_model is not None:
            self._judge_turn_soon()
        if self._awaiting_caller_final and not self.playing:
            # No final recognition: recover once with fixed speech, never replay
            # a cancelled answer based on the earlier incomplete fragment.
            self.arm_filler_nudge(repeat=True)

    async def _respond_when_complete(self, reason, script=None):
        generation = self.generation
        self._waiting_for_completion = True
        try:
            # Item 5: the end-of-turn model heard an unfinished turn; wait longer
            # than the text hold would. New caller speech cancels this task.
            acoustic_ms = round((self._turn_hold_until - time.monotonic()) * 1000)
            if acoustic_ms > (self.settings.turn_hold_ms if reason else 0):
                await self.trace_event('turn_completion', action='hold', reason='acoustic_incomplete', hold_ms=acoustic_ms)
                await asyncio.sleep(acoustic_ms / 1000)
            elif reason and self.settings.turn_hold_ms:
                await self.trace_event('turn_completion', action='hold', reason=reason,
                                       hold_ms=self.settings.turn_hold_ms)
                await asyncio.sleep(self.settings.turn_hold_ms / 1000)
            # End-of-speech from the browser is independent of STT final order.
            # A missing end event must not hold a task forever or start speech.
            if not await self._wait_for_caller_quiet():
                await self.trace_event('turn_completion', action='speech_end_timeout')
                await self.send('state', state='listening')
                return
            if generation == self.generation and not self.closed and not self.finished:
                self._waiting_for_completion = False
                await self.respond(script=script)
        finally:
            self._waiting_for_completion = False

    async def _wait_for_caller_quiet(self):
        """True once the caller is quiet; False if the existing playback timeout passes.

        A fan, traffic or clatter can hold the browser's energy gate open after
        the caller has finished: Maya then stayed silent for up to 20 seconds.
        The provider's speech-trained VAD tells a voice from that. Browser sound
        with no provider speech for the release wait is background, so answer.
        A voice (caller or TV) still waits as before.
        """
        wait = self.settings.release_wait_ms / 1000
        started = time.monotonic()
        deadline = started + self.settings.studio_playback_timeout_s
        while not self._caller_quiet.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self._caller_quiet.wait(), min(wait, remaining))
            except asyncio.TimeoutError:
                now = time.monotonic()
                if now >= deadline:
                    return False
                # Noise only after a full wait with no provider speech in it.
                if (not self._stt_speech_active and now - started >= wait
                        and now - self._stt_speech_at >= wait):
                    await self.trace_event('turn_completion', action='background_sound')
                    self.caller_speaking = False
                    self._caller_quiet.set()
                    # The browser holds first PCM while its own gate is open.
                    await self.send('speak_over_noise')
        return True

    def _schedule_flush(self, delay_ms=None):
        """F3: once the caller has been quiet for stt_flush_silence_ms (counting the
        browser's own release delay), ask Sarvam to finalise now. Any new onset
        cancels it; the caller's fragments are joined as before."""
        self._cancel_flush()
        silence = self.settings.stt_flush_silence_ms
        finalize = getattr(self.stt, 'finalize', None)
        if not ((silence or delay_ms is not None) and finalize and self._awaiting_caller_final):
            return
        delay = (delay_ms if delay_ms is not None else max(0, silence - self.settings.studio_barge_in_release_ms)) / 1000

        async def flush():
            await asyncio.sleep(delay)
            if not (self.caller_speaking or self.closed or self.finished) and self._awaiting_caller_final:
                await finalize()
        self._flush_task = asyncio.create_task(flush())

    def caller_audio(self, pcm):
        """Item 5: the caller's last 8 s of 16 kHz audio, for the end-of-turn model."""
        if self._turn_model is None:
            return
        self._turn_audio += pcm
        if len(self._turn_audio) > TURN_AUDIO_BYTES:
            del self._turn_audio[:-TURN_AUDIO_BYTES]

    def _judge_turn_soon(self):
        task = self._turn_task
        if task is not None and not task.done():
            task.cancel()
        self._turn_task = asyncio.create_task(self._judge_turn(self._released_at, bytes(self._turn_audio)))

    async def _judge_turn(self, released_at, pcm):
        """Item 5: finished -> finalise now; unfinished -> keep listening a while;
        unsure, slow (over budget) or failed -> the previous timing, unchanged."""
        started = time.monotonic()
        try:
            probability = await asyncio.wait_for(asyncio.to_thread(self._turn_model.predict, pcm),
                                                 self.settings.turn_detector_budget_ms / 1000)
        except asyncio.TimeoutError:
            await self.trace_event('turn_detector', action='fallback', reason='over_budget',
                                   ms=round((time.monotonic() - started) * 1000))
            return
        except Exception as exc:
            await self.trace_event('turn_detector', action='fallback', reason=type(exc).__name__,
                                   ms=round((time.monotonic() - started) * 1000))
            return
        ms = round((time.monotonic() - started) * 1000)
        if self.caller_speaking or self._released_at != released_at or self.closed or self.finished:
            await self.trace_event('turn_detector', action='stale', probability=round(probability, 3), ms=ms)
            return
        finalize = getattr(self.stt, 'finalize', None)
        flush_pending = self._flush_task is not None and not self._flush_task.done()
        if probability >= self.settings.turn_complete_threshold:
            action = 'complete'
            if finalize and self._awaiting_caller_final and (flush_pending or not self.settings.stt_flush_silence_ms):
                self._cancel_flush()
                await finalize()
        elif probability <= self.settings.turn_incomplete_threshold:
            action = 'incomplete'
            wait = self.settings.turn_incomplete_wait_ms
            self._turn_hold_until = time.monotonic() + wait / 1000
            if flush_pending or not self.settings.stt_flush_silence_ms:
                self._schedule_flush(delay_ms=wait)
        else:
            action = 'default'
        await self.trace_event('turn_detector', action=action, probability=round(probability, 3), ms=ms)

    def _cancel_flush(self):
        task, self._flush_task = self._flush_task, None
        if task and not task.done():
            task.cancel()

    async def _stt_speech_start(self):
        self._stt_speech_active, self._stt_speech_at, self._stt_vad_seen = True, time.monotonic(), True

    async def _stt_speech_end(self):
        self._stt_speech_active, self._stt_speech_at, self._stt_vad_seen = False, time.monotonic(), True

    async def trace_event(self, kind, **data):
        if self.trace is None:
            return
        try:
            if self.trace_failed:
                raise RuntimeError("Conversation storage unavailable")
            await self.trace.acall("record", self.costs.session_id, kind, data)
        except Exception:
            self.trace_failed = True
            with contextlib.suppress(Exception):
                await self.ws.send_json({"type": "error", "message": ('Conversation save नहीं हो पा रही। Call रुक गई है; पहले की saved बातें history में हैं।' if self.language == 'hinglish' else "Conversation storage is unavailable. The call has stopped; earlier saved turns remain in history.")})
                await self.ws.close()
            raise

    def _tts_stalled(self, stall):
        """Store a speech-provider stall with the call (timings only, no text)."""
        async def record():
            with contextlib.suppress(Exception):
                await self.trace_event('tts_stall', generation=self.generation, **stall)
        task = asyncio.create_task(record())
        self._diagnostics.add(task)
        task.add_done_callback(self._diagnostics.discard)

    async def playback_stats(self, data):
        """Browser-measured delivery for one reply: heard latency and playback gaps."""
        def number(key, limit):
            value = data.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= limit:
                return None
            return round(value)
        generation = number('generation', 10**6)
        if generation is None:
            return
        await self.trace_event('playback_stats', generation=generation,
                               heard_ms=number('heard_ms', 120000), gated=data.get('gated') is True,
                               cut=data.get('cut') is True, gaps=number('gaps', 10000) or 0,
                               gap_ms=number('gap_ms', 600000) or 0, max_gap_ms=number('max_gap_ms', 600000) or 0)

    async def playback_received(self, generation):
        await self.trace_event("delivery", turn_key=f"assistant:{generation}", state="browser_playback_acknowledged" if self.tts else 'text_display_acknowledged')
        if generation == self.playback_generation:
            self.playback_event.set()

    async def send(self, kind, **data):
        if self.finished and kind == 'state' and data.get('state') != 'summarizing':
            return
        if kind in {"transcript", "agent_text", "brief", "cost", "error", "latency", "interrupted", "ready"}:
            record = dict(data)
            # PII masking at rest is a security basic: the stored trace gets a redacted copy;
            # the caller's own screen still shows what they said.
            if isinstance(record.get("text"), str):
                record["text"] = redact(record["text"])
            if isinstance(record.get("transcript"), list):
                record["transcript"] = [{**m, "content": redact(m.get("content", ""))} for m in record["transcript"]]
            if kind == "transcript":
                self.user_turn += 1
                record['turn_key'] = f"user:{self.user_turn}"
            elif kind == "agent_text":
                record['turn_key'] = f"assistant:{data.get('generation', self.generation)}"
            await self.trace_event(kind, **record)
        if not self.closed and not self.ws.closed:
            await self.ws.send_json({"type": kind, **data})

    async def _publish_costs(self):
        while not self.closed:
            await self.send("cost", usage=self.costs.snapshot())
            await asyncio.sleep(2)

    async def start(self):
        started = time.monotonic()
        self._cost_task = asyncio.create_task(self._publish_costs())

        async def start_stt():
            if self.stt:
                await self.stt.initialize()
                self.stt.on_transcript(self.transcript)
                self.stt.on_disconnect(self.disconnected)
                if hasattr(self.stt, 'on_sarvam_speech_start'):
                    self.stt.on_sarvam_speech_start(self._stt_speech_start)
                    self.stt.on_sarvam_speech_end(self._stt_speech_end)
                await self.stt.start_stream(self.costs.session_id)
                if not getattr(self.stt, 'streaming', True):
                    # The 1-second REST fallback cannot hold a conversation. A call
                    # that looks connected but never hears the caller is worse than
                    # a visible startup error ("Check provider access").
                    raise RuntimeError('Speech recognition stream unavailable')

        # Independent provider handshakes overlap. Both must finish before ready
        # or the greeting, so the caller can still interrupt the opening line.
        startup = [asyncio.create_task(start_stt())]
        if self.tts:
            startup.append(asyncio.create_task(self.tts.initialize()))
        if self.turn_completion_enabled and self.settings.turn_detector == 'smart_turn':
            async def load_turn_model():
                from utils.turn_detector import SmartTurn
                self._turn_model = await asyncio.to_thread(
                    SmartTurn.load, self.settings.turn_detector_model or None, self.settings.turn_detector_threads)
                await self.trace_event('turn_detector', action='loaded' if self._turn_model else 'unavailable')
            startup.append(asyncio.create_task(load_turn_model()))
        try:
            await asyncio.gather(*startup)
        finally:
            # gather alone leaves the sibling running after a provider failure.
            # Join it before the server closes providers, including on cancellation.
            for task in startup:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*startup, return_exceptions=True)
        await self.trace_event('startup', stage='providers_ready',
                               ms=round((time.monotonic() - started) * 1000))
        await self.send("ready", sample_rate=self.settings.tts_output_sample_rate, mode=self.mode,
                        call_id=self.costs.session_id,
                        interruption={
                            "min_speech_ms": self.settings.studio_barge_in_min_speech_ms,
                            "min_rms": self.settings.studio_barge_in_min_rms,
                            "noise_ratio": self.settings.studio_barge_in_noise_ratio,
                            "release_ms": self.settings.studio_barge_in_release_ms,
                            "resume_ms": self.settings.studio_false_interrupt_resume_ms,
                        })
        self.turn = asyncio.create_task(self.respond(greeting=True))
        # The greeting is a fixed line, so no model call has opened the connection yet: warm it and
        # the provider's prompt cache while the greeting plays (the first routed turn was 2.4 s cold).
        for job in (self._prewarm(), self._prepare_acks()):
            warm = asyncio.create_task(job)
            self._diagnostics.add(warm)
            warm.add_done_callback(self._diagnostics.discard)

    async def _prepare_acks(self):
        """Synthesise the acknowledgement clips once per call, on a separate speech connection so they
        never interleave with the greeting's audio. Off with CSM_ACK_CLIPS=false."""
        import os
        if (not self.tts or self.language == "ar"
                or os.environ.get("CSM_ACK_CLIPS", "true").lower() in {"0", "false", "off", "no"}):
            return   # (no Arabic clips: an English "Sure" would sound wrong on an Arabic call)
        language = self.language
        recorded = self._recorded_acks()
        if recorded:
            self._acks = recorded
            return   # pre-recorded for this voice: identical every call, no speech cost
        phrases = ACK_PHRASES["hi" if language in {"hi", "hinglish"} else "en"]
        if self.csm.agent["gender"] == "male":
            phrases = tuple(persona.masculine(p) for p in phrases)
        tts = create_tts_processor(self.settings, self.settings.tts_provider)
        attach_tracker(self.costs, tts)
        acks = {}   # swapped in whole: a reply never sees a half-built or empty set after a language switch
        try:
            await tts.initialize()
            for phrase in phrases:
                tts.reset()
                lang = "" if self.settings.tts_provider == "elevenlabs" else output_language(phrase, language)
                pcm = b"".join([chunk async for chunk in tts.synthesize(phrase, language=lang)])
                if pcm:
                    acks[phrase] = pcm
            if acks and language == self.language:
                self._acks = acks
        except Exception as exc:
            await self.trace_event("error", stage="ack_clips", code=type(exc).__name__)
        finally:
            with contextlib.suppress(Exception):
                await tts.close()

    def _recorded_acks(self):
        """Clips pre-recorded for this call's voice and language (scripts/make_ack_clips.py), or {}."""
        import json
        import wave
        from pathlib import Path
        folder = Path(__file__).resolve().parent / "acks"
        key = f"{self.csm.agent.get('voice', '')}|{'hi' if self.language in {'hi', 'hinglish'} else 'en'}"
        try:
            entry = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))[key]
            if entry["rate"] != self.settings.tts_output_sample_rate:
                return {}
            clips = {}
            for clip in entry["clips"]:
                with wave.open(str(folder / clip["file"]), "rb") as w:
                    clips[clip["phrase"]] = w.readframes(w.getnframes())
            return clips
        except (OSError, ValueError, KeyError, wave.Error):
            return {}

    def _pick_ack(self):
        """'Thanks.' after the caller gave details (digits), otherwise rotate without repeating."""
        phrases = [p for p in self._acks]
        last_user = next((m["content"] for m in reversed(self.history) if m["role"] == "user"), "")
        if re.search(r"\d", last_user) and phrases[-1] != self._last_ack:
            choice = phrases[-1]
        else:
            choice = next(p for p in phrases[:-1] + phrases[-1:] if p != self._last_ack)
        self._last_ack = choice
        return self._acks[choice]

    async def _ack_if_silent(self, generation, no_audio_yet):
        """A long silence after the caller finishes feels like lag: fill it with 'Sure.' But wait a
        natural beat after their last sound first, and stay quiet if the thought sounds unfinished."""
        import os
        from utils.turn_completion import incomplete_reason
        try:
            delay = min(max(int(os.environ.get("CSM_ACK_DELAY_MS", ACK_DELAY_DEFAULT_MS)), 500), 2000) / 1000
        except ValueError:
            delay = ACK_DELAY_DEFAULT_MS / 1000
        stopped = self._released_at or time.monotonic()
        await asyncio.sleep(max(0.3, stopped + delay - time.monotonic()))
        last_user = next((m["content"] for m in reversed(self.history) if m["role"] == "user"), "")
        if (not no_audio_yet() or generation != self.generation or self.closed or self.caller_speaking
                or self._released_at != stopped and self._released_at is not None
                or self._turn_scripted or not self._acks or incomplete_reason(last_user)):
            return
        self._ack_inflight = True
        try:
            await self.ws.send_bytes(self._pick_ack())
        finally:
            self._ack_inflight = False
        await self.trace_event("ack_clip", generation=generation)

    async def _prewarm(self):
        with contextlib.suppress(Exception):
            await self.llm.prewarm_prompt(self._static_system())

    async def disconnected(self):
        message = {'hinglish': 'Speech connection टूट गया। Call बंद करके फिर connect कीजिए।', 'hi': 'आवाज़ पहचानने का कनेक्शन टूट गया। कॉल समाप्त करके दोबारा जुड़ें।',
                   'mr': 'आवाज ओळखण्याचे कनेक्शन तुटले. कॉल संपवून पुन्हा जोडा.'}.get(self.language, 'Speech recognition disconnected. End this call and reconnect.')
        await self.send("error", message=message)
        await self.ws.close()

    async def interrupt(self):
        if self.playing:
            await self.trace_event("delivery", turn_key=f"assistant:{self.generation}", state="interrupted_may_not_have_been_heard")
        self.generation += 1
        self.playing = False
        self.llm.cancel()
        if self.tts:
            self.tts.cancel()
        if self.turn and not self.turn.done():
            self.turn.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.turn
        self._waiting_for_completion = False
        await self.send("clear_audio", generation=self.generation)
        await self.send("state", state="listening")

    async def transcript(self, text, final):
        # The terminal transition and accepted caller facts share one owner.
        # A callback queued before finish must recheck after taking this lock.
        async with self.lock:
            await self._accept_transcript(text, final)

    async def _accept_transcript(self, text, final):
        if self.closed or self.finished or not text.strip():
            return
        text = text.strip()[:2000]
        if final and (sum(m['role'] == 'user' for m in self.history) >= self.settings.browser_max_turns
                or len(json.dumps(self.history, ensure_ascii=False).encode('utf-8')) + len(text.encode('utf-8')) > self.settings.browser_context_bytes):
            await self.send('error', message=('Call की limit पूरी हो गई है। पहले मिली details summary में रहेंगी।' if self.language == 'hinglish' else 'Conversation limit reached. Only previously accepted details will be included in the brief.'))
            async def end_for_limit():
                await self.finish()
                await self.ws.close()
            if not self.end_task:
                self.end_task = asyncio.create_task(end_for_limit())
            return
        if not final:
            await self.send("partial", text=text)
            # Pause rather than discard queued audio until a final transcript.
            # If recognition produces no final utterance, the browser resumes.
            heard = set(text.lower().split())
            said = set(self.last_spoken.lower().split()) | set(self.last_audio_text.lower().split())
            echo = bool(heard) and len(heard & said) / len(heard) > 0.8
            if self.turn_completion_enabled and heard and not echo and not self.playing:
                # A partial may arrive before local onset (short words) or after
                # local release. It is evidence of continuation, never new facts.
                if self.turn and not self.turn.done():
                    await self.interrupt()
                self._awaiting_caller_final = True
                if not self.caller_speaking:
                    self.arm_filler_nudge(repeat=True)
            if heard and not echo and self.completion_pending:
                await self.cancel_closing()
                if self.playing: await self.interrupt()
                # An unfinished recognition segment resets the waiting period;
                # there is still a bounded close if no final transcript arrives.
                self.arm_closing()
            if self.playing and heard and not echo:
                explicit = bool(re.match(
                    r"^(?:stop|wait|hold on|रुको|रुकिए|एक मिनट|रुकिये|थांबा|थांब|एक मिनिट|espera|warte|aspetta)(?:\W|$)",
                    text, re.IGNORECASE))
                await self.send("pause_audio", explicit_interrupt=explicit)
            # The caller is still talking after a "hmm": push the nudge back.
            if heard and not echo and self.filler_task and not self.filler_task.done():
                self.arm_filler_nudge(repeat=self._awaiting_caller_final)
            return
        if final:
            self._awaiting_caller_final = False
            # Measured before any endpointing change: how long recognition takes
            # after the browser released and after the provider's own VAD end.
            now = time.monotonic()
            self._final_timing = {
                'stt_final_after_release_ms': round((now - self._released_at) * 1000) if self._released_at else None,
                'stt_final_after_vad_end_ms': (round((now - self._stt_speech_at) * 1000)
                                               if self._stt_speech_at and not self._stt_speech_active else None)}
            if self.turn_completion_enabled and await self._backchannel(text):
                return
            self.cancel_filler_nudge()
            self.cancel_silence_check()
            self._silence_checks = 0   # the caller spoke: a later silence gets a fresh check-in
            self.pending_backchannel = None
            interrupted = self.playing
            await self.cancel_closing()
            await self.interrupt()
            if self.end_task:
                self.end_task.cancel()
                self.end_task = None
            previous = next((m['content'] for m in reversed(self.history) if m['role'] == 'assistant'), '')
            final_answer = self.completion_pending and (final_help_offer(previous) or not re.search(r'[?？]', previous))
            self.end_after_playback = closing_intent(text, self.history, self.final_help_offered) or (final_answer and final_refusal(text))
            if self.language == 'hinglish':
                from utils.backchannel import hinglish_stop
                self.end_after_playback = self.end_after_playback or hinglish_stop(text)
            if interrupted:
                self.interruptions += 1
                await self.send("interrupted", count=self.interruptions)
            # An explicit request is the ONLY thing that changes the call's
            # language. Inference cannot: that is what let one Devanagari name
            # flip the rest of the conversation.
            self.switch_language(text)
            self.history.append({"role": "user", "content": text})
            await self.send("transcript", role="user", text=text)
            if self.end_after_playback:
                goodbye = self.csm.lines["goodbye"]
                if self.turn_completion_enabled:
                    self._waiting_for_completion = True
                    self.turn = asyncio.create_task(self._respond_when_complete(None, script=goodbye))
                else:
                    self.turn = asyncio.create_task(self.respond(script=goodbye))
            else:
                if self.turn_completion_enabled:
                    from utils.turn_completion import incomplete_reason
                    # Original transcript fragments stay intact in history.
                    fragments = []
                    for message in reversed(self.history):
                        if message['role'] != 'user':
                            break
                        fragments.append(message['content'])
                    reason = incomplete_reason(' '.join(reversed(fragments)), previous)
                    self._waiting_for_completion = True
                    self.turn = asyncio.create_task(self._respond_when_complete(reason))
                else:
                    self.turn = asyncio.create_task(self.respond())

    async def _backchannel(self, text):
        """Handle a whole-message "hmm"/"okay" without a model call. True if handled.

        While Maya is speaking or preparing a reply, it is listening feedback:
        she keeps going (the browser resumes its provisional pause by itself)
        and the sound is held until her playback ends. When she is quiet, a
        thinking sound means the caller is deciding: record it, stay silent and
        nudge once if the silence continues. An "okay" said to a quiet Maya is
        an ordinary turn (it may answer her question), so it is not handled here.
        """
        from utils.backchannel import backchannel
        kind = backchannel(text, hinglish=self.language == "hinglish")
        if not kind:
            return False
        if self.playing or (self.turn and not self.turn.done() and not self._waiting_for_completion):
            self.pending_backchannel = text
            await self.trace_event('backchannel', sound=kind, action='kept_speaking')
            return True
        if kind != 'thinking':
            return False
        await self.cancel_closing()
        if self.end_task:
            self.end_task.cancel()
            self.end_task = None
        self.pending_backchannel = None
        if self._waiting_for_completion:
            await self.interrupt()
        self.history.append({"role": "user", "content": text})
        await self.send("transcript", role="user", text=text)
        await self.trace_event('backchannel', sound=kind, action='waiting')
        self.arm_filler_nudge()
        return True

    async def playback_finished(self):
        """A sound held during Maya's speech counts only if her reply ended in a
        question: then "okay" is an answer and "hmm" means the caller is deciding."""
        text, self.pending_backchannel = self.pending_backchannel, None
        # Speech-time acknowledgments are not an answer to a later question.
        if self.language == 'hinglish' and text:
            return
        last = self.history[-1] if self.history else {}
        if text and last.get('role') == 'assistant' and re.search(r'[?？]\s*$', last['content']):
            await self.transcript(text, True)
            return
        self.arm_silence_check()

    def arm_silence_check(self):
        """The caller says nothing for silence_check_s after the agent spoke: the model is told so and asks whether
        they are still there (once per silence). Silent again after that: the closing line, and the call ends.
        After "anything else?" the closing countdown owns silence instead (completion_pending)."""
        self.cancel_silence_check()
        if self.mode != "voice" or self.closed or self.finished or self.completion_pending or self.end_after_playback:
            return
        turns = len(self.history)

        async def check():
            await asyncio.sleep(self.settings.silence_check_s)
            async with self.lock:
                if (self.closed or self.finished or len(self.history) != turns or self.caller_speaking or self.playing
                        or (self.turn and not self.turn.done()) or (self.filler_task and not self.filler_task.done())
                        or self.completion_pending):
                    return
                self._silence_task = None
                if self._silence_checks >= 1:
                    await self.trace_event('silence', action='close', seconds=self.settings.silence_check_s)
                    self.end_after_playback = True
                    self.turn = asyncio.create_task(self.respond(script=self.silence_warning()))
                    return
                self._silence_checks += 1
                await self.trace_event('silence', action='check_in', seconds=self.settings.silence_check_s)
                self.turn = asyncio.create_task(self.respond(silence=True))
        self._silence_task = asyncio.create_task(check())

    def cancel_silence_check(self):
        task, self._silence_task = self._silence_task, None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def arm_filler_nudge(self, repeat=False):
        self.cancel_filler_nudge()
        turns = len(self.history)

        async def nudge():
            await asyncio.sleep(self.settings.filler_nudge_s)
            async with self.lock:
                # Anything said since the "hmm" (or a call ending) cancels the nudge.
                if (self.closed or self.finished or len(self.history) != turns
                        or self.caller_speaking
                        or (self.turn and not self.turn.done())):
                    return
                self.filler_task = None
                if repeat and self._stt_vad_seen and self._stt_speech_at < self._onset_at:
                    # Reported call: "Sorry, I missed that" right after Maya's own
                    # question. The browser heard echo/noise, but Sarvam's speech
                    # VAD heard no voice, so there was nothing to miss. Keep listening.
                    await self.trace_event('turn_completion', action='repeat_skipped_no_speech')
                    return
                await self.trace_event('backchannel', sound='thinking', action='nudge')
                if repeat:
                    from utils.backchannel import REPEAT
                    self.turn = asyncio.create_task(self.respond(script=REPEAT.get(self.language, REPEAT['en'])))
                else:
                    self.turn = asyncio.create_task(self.respond(nudge=True))
        self.filler_task = asyncio.create_task(nudge())

    def cancel_filler_nudge(self):
        task, self.filler_task = self.filler_task, None
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _language_lock(self):
        """State the established language explicitly, every turn.

        The prompt's KEEP THE LANGUAGE STABLE section describes the rule in the
        abstract but never says which language is actually established, so the
        model re-derives it from the visible history — and a caller who answers
        one question in another script (a name in Devanagari) shifts that guess.
        Measured live against this exact history: ~20-30% of replies to a
        Devanagari name came back in Hindi, which is what made Maya switch
        mid-call and never announce it.

        `self.language` is the authoritative answer: it is chosen at connect and
        changes only through `switch_language`, never by inference. Naming it on
        every turn removes the guess. This narrows the failure rate but cannot
        reach zero on its own — sampling still rules — so `switch_language`
        below is what actually holds the line across a call.
        """
        name = LANGUAGE_NAMES[self.language]
        if self.language == 'hinglish' or not self.settings.caller_language_switch:
            return (
                f"\n\nACTIVE LANGUAGE: {name}. The language of this call is fixed by its settings and "
                f"cannot change. Understand the caller in any language, but reply ONLY in {name}, even "
                f"if they speak another language or ask you to switch. If they ask to switch, say briefly "
                f"in {name} that this call continues in {name}, then carry on helping."
            )
        # Static on purpose (no current language here): it stays in the cached prefix across a switch.
        # The current language goes in each turn's notes (_language_note).
        return (
            "\n\n## Language\n"
            "- Reply in the language the caller is speaking now. If they switch between English, Hindi or "
            "another Indian language, or ask you to, switch with them from that reply on. A single name, "
            "place or quoted word in another script is not a switch.\n"
            "- Start EVERY reply with a language tag for the speech engine, then your words, e.g. "
            "\"<lang:hi-IN> आपकी अगली EMI ...\". Tags: " + ", ".join(f"<lang:{c}-IN> {LANGUAGE_NAMES[c]}"
                                                                for c in TAG_LANGUAGES)
            + ". Use <lang:hi-IN> for Hinglish too. The tag is never spoken.\n"
            "- Write Hindi and other Indian languages in their own script."
        )

    def silence_warning(self):
        """The bank's own line, in the call's current language (the default is the sales demo's wording)."""
        return self.csm.lines["silence_close"]

    def _language_note(self):
        """Per-turn reminder of the current language and its tag (empty when the call language is fixed)."""
        if self._language_pinned:
            return (f"\nCURRENT LANGUAGE: {LANGUAGE_NAMES[self.language]}, chosen by the caller on screen. Reply only in "
                    f"{LANGUAGE_NAMES[self.language]}" + (f", starting with <lang:{self.language}-IN>"
                    if self.language in TAG_LANGUAGES else "") + ", even if they speak another language.")
        if self.language == 'hinglish' or not self.settings.caller_language_switch:
            return ""
        return (f"\nCURRENT LANGUAGE: {LANGUAGE_NAMES[self.language]}. Start your reply with "
                f"<lang:{self.language}-IN>, or with the tag of the language the caller has just switched to.")

    async def _hang_up(self, reason, queue=None):
        """End the call after this reply plays. With a queue, the fixed off-topic closing line is spoken first."""
        if queue is not None:
            line = self.csm.lines["offtopic_hangup"]
            self._trusted.add(line)
            await queue.put(line)
        self.end_after_playback = True
        self.csm.svc.audit.record(self.csm.state.call_id, "hangup", reason=reason, offtopic_turns=self.offtopic_turns)
        await self.trace_event("hangup", reason=reason, offtopic_turns=self.offtopic_turns)

    def _take_tags(self, sentence):
        """Strip control and language tags from a model sentence, noting each control tag once per reply."""
        for tag in CONTROL_TAG.findall(sentence):
            kind = "hangup" if tag.upper().startswith("HANG") else "offtopic"
            if kind not in self._reply_tags:
                self._reply_tags.add(kind)
                if kind == "offtopic":
                    self.offtopic_turns += 1
        return self._take_lang_tag(CONTROL_TAG.sub("", sentence)).strip()

    def _take_lang_tag(self, sentence):
        """Strip <lang:xx-IN> tags from a model sentence. The last one sets this reply's speech language and,
        when switching is on, the call's language (fixed lines, acks, the next turn's note)."""
        tags = LANG_TAG.findall(sentence)
        if not tags:
            return sentence
        code = {"or": "od"}.get(tags[-1].lower(), tags[-1].lower())
        if code in TAG_LANGUAGES:
            self._reply_lang = code
            if self.settings.caller_language_switch and self.language != 'hinglish' and not self._language_pinned:
                self._set_language(code)
        return LANG_TAG.sub("", sentence).strip()

    async def set_language_from_ui(self, code):
        """The call page's language picker during a call. The choice holds (the caller's speech no longer moves it)
        until they pick another or ask aloud. Hinglish is spoken as Hindi; a Hinglish call keeps its fixed setup."""
        code = "hi" if code == "hinglish" else code
        if self.language == "hinglish" or code not in TAG_LANGUAGES:
            await self.send("language", language=self.language, accepted=False)
            return
        self._language_pinned = True
        if not self._set_language(code):
            await self.send("language", language=code)   # already speaking it: confirm on screen
        await self.trace_event("language", source="ui", language=code)

    def _set_language(self, code):
        if code == self.language or code not in LANGUAGE_NAMES:
            return False
        self.language = code
        self.csm.state.language = code
        if not self.closed and not self.ws.closed:
            note = asyncio.create_task(self.send("language", language=code))   # the call page's picker follows
            self._diagnostics.add(note)
            note.add_done_callback(self._diagnostics.discard)
        if self.tts and code in ("en", "hi"):
            # Rebuild the "one moment" clips in the new language; the old ones play until they are ready.
            job = asyncio.create_task(self._prepare_acks())
            self._diagnostics.add(job)
            job.add_done_callback(self._diagnostics.discard)
        return True

    def _follow_script(self, text):
        """Follow a caller who switches language mid-call, before the orchestrator picks a fixed line (the
        transfer notice is spoken in the caller's language). Counted in words, so a Devanagari name inside
        an English sentence ("my name is प्रिया शर्मा") does not switch; the reply's tag can still switch later."""
        from csm.router import normalise_digits
        from csm.spoken import spoken_numbers
        indic = [w for w in text.split() if re.search(r"[ऀ-෿]", w)]
        # Seen live: "Double zero one." (a customer ID) flipped a Hindi call to English. Number words and
        # anything with a digit (C1001) are not language; an English sentence needs five real words.
        words = normalise_digits(spoken_numbers(text)).split()
        latin = [w for w in words if re.search(r"[A-Za-z]", w) and not re.search(r"[\dऀ-෿]", w)]
        if len(indic) >= 2 and len(indic) > len(latin):
            code = output_language(" ".join(indic), self.language)
            return code in TAG_LANGUAGES and self._set_language(code)   # only the languages the call offers
        if self.mode == "voice" and not indic and len(latin) >= 5 and self.language not in ("en", "hinglish"):
            return self._set_language("en")   # speech recognition writes Hindi in Devanagari, English in Latin
        return False

    def switch_language(self, text):
        """Adopt a new language only on an explicit request from the caller.

        This is the deterministic half of the fix. Without it the established
        language lives only in the prompt, so one unlucky sample flips the call
        and every later turn inherits the flip from history. Here the language
        is session state: an unexpected Devanagari reply cannot change it, and
        the next turn's lock pulls the model straight back to the caller's
        chosen language.

        A language is recognised by what callers actually CALL it — native script
        and romanisation, not only the English name (see LANGUAGE_ALIASES). The
        first version matched an English verb plus the English name, so a Hindi
        speaker asking in Hindi ("हिंदी में बात करें") could not switch at all —
        the group most likely to ask.

        A bare mention is not a request: "my name is Hindi Kumar" must not
        switch. So an alias only counts alongside a switch cue — an English verb,
        or a native postposition/verb such as "में"/"mein"/"bolo"/"en".
        """
        if self.language == 'hinglish' or not self.settings.caller_language_switch:
            return False
        lowered = text.lower()
        cue = re.compile(
            r"\b(?:speak|talk|say|reply|answer|respond|switch|change|use|in|into)\b"
            r"|में|मे\b|\bmein\b|\bme\b|\bbol|\bbaat\b|बोल|बात|\bhabla|\bhablar\b"
            r"|\bsprich|\bparla|\bpor favor\b",
            re.IGNORECASE,
        )
        if cue.search(lowered) or cue.search(text):
            for code, aliases in LANGUAGE_ALIASES.items():
                for alias in aliases:
                    needle = alias.lower()
                    # Word boundaries only help ASCII; non-Latin scripts match plainly.
                    found = (re.search(rf"\b{re.escape(needle)}\b", lowered)
                             if needle.isascii() else needle in text.lower())
                    if found:
                        if code not in TAG_LANGUAGES:
                            return False   # not offered yet: the reply says English or Hindi (prompt.md)
                        self._language_pinned = False   # a spoken request overrides the screen's choice
                        self._set_language(code)
                        return True
        return False if self._language_pinned else self._follow_script(text)

    def _static_system(self):
        """The system message, byte-identical every turn so the provider prompt cache holds.

        Per-turn guidance (listening, closing, call state) goes in a final system
        message after the dialogue instead: when it was appended here, every turn
        rewrote the cached prompt (measured 1.8-5 s per miss vs 1.1-1.4 s per hit).
        """
        from utils.backchannel import HINGLISH_STYLE
        prompt = self._persona_prompt
        fixed = self.language == 'hinglish' or not self.settings.caller_language_switch
        if fixed:
            # prompt.md's "## Language" section is the switching rule; a fixed-language call replaces it.
            prompt = LANGUAGE_SECTION.sub("", prompt)
        # The language and call-control rules live in prompt.md; these copies are only for a prompt.md without them.
        system = (prompt + (self._language_lock() if fixed or "<lang:" not in prompt else "")
                  + ("" if "<HANGUP>" in prompt else CONTROL_PROMPT))
        if self.settings.demo_expand_contractions:
            system += "\nFor this voice, expand English contractions into full words."
        if self.language == 'hinglish':
            system += HINGLISH_STYLE
        return system

    def _speech_parts(self, phrase):
        """(display text, TTS language, speech text) for one queued phrase, or None.

        One place for both the spoken sentence and its send-ahead copy (F4):
        the text sent ahead must be byte-identical to what is synthesised.
        """
        text = to_spoken_text(phrase)
        if not text:
            return None
        # Model sentences pass the CSM outbound guard (no unbacked action claims, no full
        # numbers, never an OTP, figures traced to tool facts). Fixed lines are trusted.
        if phrase not in self._trusted:
            text = self.csm.outbound(text)
        # Detect the output script, not the language of the request.
        # An English request can explicitly ask for a Hindi reply.
        script = output_language(text, self.language)
        # The reply's <lang:..> tag sets the voice language. One guard: Indian-script words under an English
        # tag are spoken by their script (an English voice can't read Devanagari). No tag (fixed lines, the
        # greeting): the script decides, as before.
        current = self.language if self.language in TAG_LANGUAGES else "en"   # the last language spoken on the call
        lang = (self._reply_lang if self._reply_lang and not (self._reply_lang == "en" and script != "en")
                else script if script != "en" else current)
        audio_text = text
        if self.settings.tts_provider == "elevenlabs":
            lang = ""
        elif self.settings.tts_provider == "sarvam":
            # Speech-only: "2.07 crore" -> "दो करोड़ सात लाख", Indic
            # names in Devanagari. Display/history keep the original.
            from utils.indic_speech import speakable
            audio_text = speakable(text, self.language if self.language == "hinglish" else lang)
        return text, lang, audio_text

    async def _csm_plan(self, queue):
        """Run the orchestrator on the caller's latest words. If routing is slow, a short filler
        line plays first so the caller isn't left in silence."""
        fragments = []
        for message in reversed(self.history):
            if message["role"] != "user":
                break
            fragments.append(message["content"])
        self.csm.state.language = self.language
        task = asyncio.create_task(self.csm.handle_turn(" ".join(reversed(fragments)), self.history))
        try:
            done, _ = await asyncio.wait({task}, timeout=CSM_FILLER_AFTER_S)
            if not done and self.tts and not self._acks:
                filler = self.csm.lines["filler"]
                self._trusted.add(filler)
                await queue.put(filler)
            plan = await task
        finally:
            if not task.done():
                task.cancel()
        self._turn_scripted = plan.script is not None
        await self.trace_event("csm_turn", source=plan.source, ms=plan.ms, scripted=plan.script is not None,
                               identity=self.csm.state.identity)
        if plan.otp_code and not self.closed and not self.ws.closed:
            # Simulated SMS to the registered mobile: its own channel, never spoken, traced or logged.
            await self.ws.send_json({"type": "otp_sms", "code": plan.otp_code})
        if plan.end_call:
            self.end_after_playback = True
        await self.send("csm_state", **self.csm.public_state())
        return plan

    async def respond(self, greeting=None, script=None, nudge=False, silence=False):
        if greeting:
            # Fixed greeting: the AI disclosure and recording notice never depend on sampling.
            script = self.csm.greeting()
        if script is not None:
            self._trusted.add(script)
        generation = self.generation
        self.reset_playback(generation)
        started = time.monotonic()
        spoken = []
        delivered = []
        self.last_audio_text = ""
        first = True
        suppressed_question = False
        offered_more = False
        tts_languages = []   # the speech language of each sentence in this reply, in order (shown on the call page)
        self._reply_lang = None   # set by this reply's <lang:..> tag
        self._reply_tags = set()
        queue = asyncio.Queue(maxsize=4)

        async def produce():
            nonlocal suppressed_question, offered_more
            if script is not None:
                await queue.put(script)
                await queue.put(None)
                return
            system = self._static_system()
            # Every caller turn goes through the CSM orchestrator first (a nudge after a "hmm" does not).
            plan = None if nudge or silence else await self._csm_plan(queue)
            if plan is not None and plan.script:
                # Refusals, lockout, handoff and OTP prompts: fixed wording, no model.
                self._trusted.add(plan.script)
                await queue.put(plan.script)
                await queue.put(None)
                return
            # Keep one chronological source of dialogue. Duplicating caller
            # turns and assistant summaries in the system prompt encourages
            # readbacks and makes the current exchange harder to distinguish.
            messages = [{"role": "system", "content": system}] + self.history
            # Per-turn guidance: a final system message, so the static prefix stays cached.
            notes = (plan.notes(self.csm.state) if plan is not None else "") + self._language_note()
            if self.history and re.fullmatch(r"(?:yes|yeah|yep|okay|ok|correct|right|sure)[.!\s]*",
                                             self.history[-1]["content"], re.I):
                # A bare affirmative is not a hang-up signal; the preceding question gives it meaning.
                notes += ("\nCURRENT TURN: The caller only gave an affirmative, not a goodbye. If it answers "
                          "your question, continue; don't say goodbye or offer more help.")
            if self.end_after_playback:
                notes += "\nThe caller has finished. Say a short polite farewell only. Do not ask another question."
            if nudge:
                from utils.backchannel import NUDGE_INSTRUCTION
                notes += NUDGE_INSTRUCTION
            if silence:
                notes += SILENCE_INSTRUCTION.format(seconds=round(self.settings.silence_check_s))
            if notes.strip():
                messages.append({"role": "system", "content": notes.strip()})
            # Voice: speak the opening clause as soon as it reaches a comma, rather
            # than waiting for the model to finish the whole first sentence.
            first_clause = FIRST_CLAUSE_CHARS if self.tts and not greeting else 0
            self.llm.first_clause_chars = first_clause
            guard = ConversationTurn(expand_contractions=self.settings.demo_expand_contractions,
                                     first_clause_chars=first_clause)
            import hashlib
            # The ~10 ms storage write overlaps the model request; awaited before speech.
            # Turn notes carry bank facts, so the stored copy is redacted.
            traced = asyncio.create_task(self.trace_event("prompt", generation=generation, language=self.language,
                prompt_sha256=hashlib.sha256(system.encode()).hexdigest(),
                turn_guidance=redact(system[len(self._persona_prompt):] + ("\n" + notes.strip() if notes.strip() else "")),
                message_count=len(messages), **self._final_timing))
            self._final_timing = {}

            facts = None   # the old preset FactGuard; the CSM outbound guard runs in _speech_parts
            spoken_any, blocked, last_put = False, None, ""

            async def put(sentence):
                nonlocal spoken_any, blocked, last_put
                # The prompt record is stored before anything is spoken.
                if not traced.done():
                    await traced
                problem = facts.problem(sentence) if facts else None
                if problem:
                    blocked = problem
                    await self.trace_event('knowledge_guard', generation=generation, reason=problem,
                                           sentence=sentence[:300])
                    return
                if not spoken_any and self.tts and self._acks:
                    # The "Sure, one moment." clip may already have played: never say it twice.
                    sentence = strip_ack(sentence)
                    if not sentence:
                        return
                spoken_any, last_put = True, sentence
                await queue.put(sentence)

            stream = self.llm.generate(messages)
            try:
                async for phrase in stream:
                    for sentence in guard.feed(phrase):
                        sentence = self._take_tags(sentence)
                        if self.offtopic_turns >= OFFTOPIC_LIMIT and "offtopic" in self._reply_tags:
                            break   # the third unrelated turn: this reply is replaced by the fixed closing line
                        if not sentence:
                            continue
                        if not greeting:
                            sentence = listening_question(sentence, self.history, self.language)
                            if not sentence:
                                suppressed_question = True
                                continue
                        if not greeting and generic_closer(sentence):
                            suppressed_question = True
                            continue
                        if self.final_help_offered and final_help_offer(sentence):
                            suppressed_question = True
                            continue
                        await put(sentence)
                        if blocked:
                            break
                    offtopic_limit = self.offtopic_turns >= OFFTOPIC_LIMIT and "offtopic" in self._reply_tags
                    if guard.done or blocked or offtopic_limit:
                        self.llm.cancel()
                        break
                offtopic_limit = self.offtopic_turns >= OFFTOPIC_LIMIT and "offtopic" in self._reply_tags
                for sentence in ([] if blocked or offtopic_limit else guard.feed("", final=True)):
                    sentence = self._take_tags(sentence)
                    if not sentence:
                        continue
                    if not greeting:
                        sentence = listening_question(sentence, self.history, self.language)
                        if not sentence:
                            suppressed_question = True
                            continue
                    if not greeting and generic_closer(sentence):
                        suppressed_question = True
                        continue
                    if self.final_help_offered and final_help_offer(sentence):
                        suppressed_question = True
                        continue
                    await put(sentence)
            finally:
                await stream.aclose()
                await traced
            if self.offtopic_turns >= OFFTOPIC_LIMIT and "offtopic" in self._reply_tags:
                # Third unrelated turn in this call: a fixed line, then the call ends.
                await self._hang_up("off_topic_limit", queue)
            elif "hangup" in self._reply_tags and spoken_any and not re.search(r"[?？]", last_put):
                # The model ended the call (<HANGUP>) after a goodbye; never after a question it asked.
                await self._hang_up("model_hangup")
            elif blocked:
                # An unsupported figure ends the turn with an honest line instead.
                await queue.put(self.knowledge.fallback(self.language))
            elif (spoken_any and plan is not None and answered_enquiry(plan.facts) and not self.end_after_playback
                    and not re.search(r"[?？]", last_put)):
                # An enquiry was answered and the reply asked nothing: close it with the fixed offer, in the
                # call's current language (persona LINES "anything_else").
                more = self.csm.lines["anything_else"]
                self._trusted.add(more)
                offered_more = True
                await queue.put(more)
            await queue.put(None)

        self.llm.reset()
        if self.tts:
            self.tts.reset()
        producer = asyncio.create_task(produce())
        self._turn_scripted = False
        ack_task = (asyncio.create_task(self._ack_if_silent(generation, lambda: first))
                    if self.tts and self._acks and not greeting and script is None and not nudge and not silence else None)
        try:
            await self.send("state", state="thinking")
            ahead = _NOTHING
            # Sarvam queues on its open socket; OpenAI starts the next request early.
            send_ahead = getattr(self.tts, 'send_ahead', None) if self.settings.tts_provider in {"sarvam", "openai", "smallest"} else None
            # Item 3: a queued item not joined into the current group (next to speak).
            carry = _NOTHING
            fewer_cuts = self.tts is not None and self.settings.tts_fewer_cuts
            def grouped(phrase):
                """Join sentences already waiting (same speech language) into one TTS
                request up to tts_group_chars. Never waits for more text."""
                nonlocal carry
                parts = self._speech_parts(phrase) if fewer_cuts and phrase is not None else None
                while parts and carry is _NOTHING and not queue.empty():
                    following = queue.get_nowait()
                    more = self._speech_parts(following) if following is not None else None
                    if (not more or more[1] != parts[1]
                            or len(phrase) + 1 + len(following) > self.settings.tts_group_chars):
                        carry = following
                        break
                    phrase = phrase.rstrip() + ' ' + following.lstrip()
                return phrase
            async with asyncio.timeout(35):
                while True:
                    if ahead is not _NOTHING:
                        phrase, ahead = ahead, _NOTHING  # already taken for send-ahead
                    elif carry is not _NOTHING:
                        phrase, carry = carry, _NOTHING
                        phrase = grouped(phrase) if spoken else phrase
                    else:
                        # Propagate producer failures rather than hanging on queue.get().
                        get = asyncio.create_task(queue.get())
                        try:
                            done, _ = await asyncio.wait({get, producer}, return_when=asyncio.FIRST_COMPLETED)
                            if producer in done and producer.exception():
                                raise producer.exception()
                            phrase = await get
                        finally:
                            if not get.done():
                                get.cancel()
                                with contextlib.suppress(asyncio.CancelledError):
                                    await get
                        # The first phrase was cut early for speed; later ones join up.
                        phrase = grouped(phrase) if spoken else phrase
                    if phrase is None:
                        break
                    parts = self._speech_parts(phrase)
                    if not parts:
                        continue
                    text, lang, audio_text = parts
                    if lang and lang not in tts_languages:
                        tts_languages.append(lang)
                    spoken.append(text + " ")
                    self.last_spoken = "".join(spoken)
                    # Voice: the ~10 ms storage write of the on-screen text runs
                    # while speech is synthesised; it completes before audio.
                    if self.tts:
                        shown = asyncio.create_task(self.send("agent_text", text=self.last_spoken, generation=generation))
                    else:
                        await self.send("agent_text", text=self.last_spoken, generation=generation)
                    if self.tts:
                        if self.settings.tts_provider == "sarvam":
                            self.last_audio_text = (self.last_audio_text + " " + audio_text).strip()
                        phrase_audio = False
                        try:
                            async for chunk in self.tts.synthesize(audio_text, language=lang):
                                if not chunk:
                                    continue
                                phrase_audio = True
                                if generation != self.generation:
                                    return
                                if first and self.turn_completion_enabled and self.caller_speaking:
                                    # Seen live: background noise looked like the caller starting to speak and
                                    # dropped this reply before a word played, twice in a row. Hold it instead:
                                    # real speech ends in a transcript that replaces this reply; noise goes quiet
                                    # (or the speech service hears no voice in it) and the reply plays.
                                    await self.trace_event('turn_completion', action='hold_reply_for_quiet')
                                    if not await self._wait_for_caller_quiet() or generation != self.generation:
                                        return
                                if not shown.done():
                                    await shown
                                if first:
                                    latency = round((time.monotonic() - started) * 1000)
                                    await self.send("state", state="speaking")
                                # send()/trace writes yield: a new caller turn can replace this one
                                # between synthesis and transmission of its first PCM.
                                if generation != self.generation:
                                    return
                                while first and self._ack_inflight:   # an ack clip goes out whole, before the reply
                                    await asyncio.sleep(0.005)
                                await self.ws.send_bytes(chunk)
                                was_first, first = first, False
                                self.playing = True
                                delivered = list(spoken)
                                if was_first and not greeting:
                                    # Recorded after the audio leaves, not before it.
                                    self.latencies.append(latency)
                                    await self.send("latency", ms=latency)
                                if send_ahead and ahead is _NOTHING and (carry is not _NOTHING or not queue.empty()):
                                    # F4: the next sentence is ready while this one is still
                                    # arriving. Queue it on the same socket so its audio
                                    # follows with no gap; it is spoken next, unchanged.
                                    if carry is not _NOTHING:
                                        ahead, carry = carry, _NOTHING
                                    else:
                                        ahead = queue.get_nowait()
                                    ahead = grouped(ahead)
                                    following = self._speech_parts(ahead) if ahead is not None else None
                                    if following:
                                        await send_ahead(following[2], language=following[1])
                        finally:
                            if not shown.done():
                                await shown
                        if not phrase_audio:
                            raise RuntimeError("Speech provider returned no audio for a sentence")
                    else:
                        delivered = list(spoken)
                        await self.send("state", state="thinking")
                await producer
                if self.tts and first and not suppressed_question:
                    raise RuntimeError("Speech provider returned no audio")
            if spoken:
                self.history.append({"role": "assistant", "content": "".join(spoken).strip()})
                # A caller turn arriving between here and the end of this task cancels it; the reply
                # is already in history, so the cancel path must not append a second copy.
                spoken, delivered = [], []
                if offered_more or final_help_offer(self.history[-1]["content"]):
                    self.final_help_offered = True
                    self.completion_pending = True
                elif script is None and not nudge and not silence:
                    # The caller took up "anything else?" with a new request: no silence countdown to hang-up
                    # until the next offer (it was never reset, so every later reply could end the call).
                    self.completion_pending = False
            # Which language the model tagged this reply with, and what the speech engine was told: the
            # tag itself is stripped before speaking and saving, so this is where it can be seen.
            await self.trace_event("reply_language", generation=generation, tag=self._reply_lang,
                                   call_language=self.language, tts=tts_languages)
            await self.send("agent_done", generation=generation, tag=self._reply_lang, tts=tts_languages)
            if not self.tts:
                await self.trace_event("delivery", turn_key=f"assistant:{generation}", state="text_generated")
            if not self.tts or (first and suppressed_question):
                await self.send("state", state="listening")
            if script is None or self.end_after_playback:
                if self.end_after_playback:
                    self.arm_closing(farewell=True)
                elif self.completion_pending:
                    self.arm_closing()
        except asyncio.CancelledError:
            if delivered:
                self.history.append({"role": "assistant", "content": "".join(delivered).strip() + " [interrupted; may not have been fully heard]"})
                if not self.trace_failed:
                    await self.trace_event("delivery", turn_key=f"assistant:{generation}", state="interrupted_may_not_have_been_heard")
            elif spoken and not self.trace_failed:
                await self.trace_event("delivery", turn_key=f"assistant:{generation}", state="cancelled_before_audio")
            raise
        except ConnectionResetError:
            # Browser refresh/disconnect: stop generation without writing an
            # error message back to the connection that just disappeared.
            self.closed = True
        except Exception as exc:
            # A later sentence failure must not erase earlier audio already
            # queued in the browser. Let that audio finish, and report the miss.
            if spoken:
                self.history.append({"role": "assistant", "content": "".join(spoken).strip()
                                     + " [speech failed; may not have been fully heard]"})
            if self.trace_failed:
                return
            from utils.costs import ProviderBudgetExceeded
            if isinstance(exc, ProviderBudgetExceeded):
                await self.end_for_provider_allowance()
                return
            await self.trace_event("error", stage="response", code=type(exc).__name__, generation=generation)
            await self.trace_event("delivery", turn_key=f"assistant:{generation}", state="response_failed_may_not_have_been_heard")
            try:
                await self.send("agent_done", generation=generation)
                await self.send("error", message={'hinglish': 'Sorry, reply पूरी नहीं हो पाई। एक बार फिर try कीजिए।',
                                                   'hi': 'माफ़ कीजिए, जवाब पूरा नहीं हो पाया। कृपया फिर से कोशिश करें।',
                                                   'mr': 'माफ करा, उत्तर पूर्ण झाले नाही. कृपया पुन्हा प्रयत्न करा.'}.get(
                    self.language, "The response could not finish. Check provider access or try again."))
                if first:
                    await self.send("state", state="listening")
            except ConnectionResetError:
                self.closed = True
        finally:
            if ack_task:
                ack_task.cancel()
            producer.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await producer

    async def end_for_provider_allowance(self):
        await self.send('error', message={'hinglish': 'इस call की limit पूरी हो गई है। Summary review के लिए save होगी; कोई booking नहीं हुई है।', 'hi': 'इस डेमो की सीमा पूरी हो गई है। बातचीत समीक्षा के लिए सहेजी जाएगी; कोई बाहरी कार्रवाई नहीं हुई है।',
                                         'mr': 'या डेमोची मर्यादा संपली आहे. संभाषण आढाव्यासाठी जतन केले जाईल; बाह्य कृती झालेली नाही.'}.get(self.language, 'This demo session has reached its limit. We will save the accepted conversation for review; no external action has been taken.'))
        async def end():
            await self.finish()
            await self.ws.close()
        if not self.end_task or self.end_task.done():
            self.end_task = asyncio.create_task(end())

    async def warn_trial(self):
        if self.finished: return
        async with self.lock:
            if self.finished or self.closed: return
            await self.cancel_closing()
            await self.interrupt()
            self.end_after_playback = False
            text = {'hi': 'आपका मुफ्त डेमो जल्द समाप्त होगा। बातचीत का सारांश सहेज दिया जाएगा।',
                    'mr': 'तुमचा मोफत डेमो लवकरच संपेल. संभाषणाचा सारांश जतन केला जाईल.'}.get(self.language, 'Your free demo will end shortly. Your conversation brief will be saved.')
            if self.language == 'hinglish':
                text = 'आपका free demo जल्द खत्म होगा। आपकी conversation की summary save हो जाएगी।'
            await self.send("agent_text", text=text, generation=self.generation)
            if self.tts:
                self.tts.reset()
                try:
                    async with asyncio.timeout(8):
                        async for chunk in self.tts.synthesize(text, language=output_language(text, self.language)):
                            if chunk: await self.ws.send_bytes(chunk)
                except Exception:
                    await self.send("error", message={'hinglish': 'Call जल्द खत्म होगी। Voice में यह notice नहीं सुनाया जा सका।', 'hi': 'डेमो जल्द समाप्त होगा। आवाज़ में सूचना उपलब्ध नहीं है।',
                                                       'mr': 'डेमो लवकरच संपेल. आवाजातील सूचना उपलब्ध नाही.'}.get(self.language, 'Trial ending soon. Spoken warning unavailable.'))
            await self.send("agent_done", generation=self.generation)

    async def finish(self):
        async with self.lock:
            if self._finish_task is None:
                if self.closed: return
                self.finished = True  # terminal input cutoff, not completion
                self._finish_caller = asyncio.current_task()
                self._finish_task = asyncio.create_task(self._finalize())
            task = self._finish_task
        # Disconnect, duplicate finish and cancelled timers do not own extraction.
        return await asyncio.shield(task)

    async def _finalize(self):
        try:
            async with asyncio.timeout(30):
                # A closing timer that requested finish still owns closing the
                # socket after the shared brief arrives. Do not cancel its wait.
                if self.closing_task is not self._finish_caller:
                    await self.cancel_closing()
                self.closing_phase = 'finished'
                await self.interrupt()
                if self.stt:
                    stt, self.stt = self.stt, None
                    try:
                        await asyncio.wait_for(stt.close(), 5)
                    except Exception as exc:
                        await self.trace_event('error', stage='stt_close', code=type(exc).__name__)
                await self.send('state', state='summarizing')
                fields = await self.brief()
                usage, tiers = self.tier_costs()
                await self.send('brief', fields=fields, transcript=self.history,
                                latencies=self.latencies, usage={**usage, "tiers": tiers})
        except TimeoutError:
            await self.send('error', message=('Summary पूरी नहीं हो पाई। Saved conversation review के लिए मौजूद है।' if self.language == 'hinglish' else 'Finalization timed out. Saved turns remain available for review.'))
        finally:
            self.finalized = True

    async def brief(self):
        await self.interrupt()
        # The call summary comes from orchestrator state (code, not a model), so nothing in it is invented.
        fields = self.csm.summary()
        # The outcome and the requests come from the disposition below (Jev); the code's own versions of those two
        # rows only repeated them. The rest of the summary (customer, actions, tickets, handoff, risk) stays.
        fields.pop("outcome", None)
        fields.pop("requests", None)
        # The disposition: each request and its result, labelled by Jev from the transcript (csm/disposition.py).
        from csm.disposition import classify
        d = (await classify(self.history, self.csm.state, self.settings)).public()
        self.csm.disposition = d
        fields.update(disposition=d["label"], labelled_by=d["note"] or d["source"],
                      customer_query=d["customer_query"], follow_up=d["follow_up"] or "None", sentiment=d["sentiment"],
                      intents=[intent_line(i) for i in d["intents"]] or ["No request"])
        return fields


    def tier_costs(self):
        """This call's usage, plus the same call priced on every voice tier (csm/tiercost.py)."""
        usage = self.costs.snapshot()
        return usage, tiercost.compare(usage, tiercost.tier_of(self.settings), tiercost.tts_models(self.settings),
                                       self.settings.cost_usd_to_inr)

    async def close(self):
        self.closed = True
        with contextlib.suppress(Exception):   # outcome, AHT and cost (per voice tier) for the supervisor KPIs
            usage, tiers = self.tier_costs()
            self.csm.end(usage.get('estimated_inr'), tiers=tiers, voice=self.mode == "voice")
        # Join an already accepted finish before timer/LLM cleanup. The bounded
        # owner task can still persist the brief when the browser has left.
        if self._finish_task and self._finish_task is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(self._finish_task), 35)
            if not self._finish_task.done():
                self._finish_task.cancel()
                await asyncio.gather(self._finish_task, return_exceptions=True)
        await self.cancel_closing()
        self.cancel_filler_nudge()
        self.cancel_silence_check()
        if self.end_task and self.end_task is not asyncio.current_task():
            self.end_task.cancel()
            await asyncio.gather(self.end_task, return_exceptions=True)
        if self._cost_task:
            self._cost_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._cost_task
        self._cancel_flush()
        for task in (self._turn_task, *self._diagnostics):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        # Run every provider cleanup even if cancelling a turn or closing one
        # provider fails. closed also prevents interrupt() from sending to UI.
        async with contextlib.AsyncExitStack() as cleanup:
            if self.settings.cost_async_write:
                cleanup.push_async_callback(asyncio.to_thread, self.costs.close)
            else:
                cleanup.callback(self.costs.close)
            cleanup.push_async_callback(self.llm.close)
            if self.tts:
                cleanup.push_async_callback(self.tts.close)
            if self.stt:
                cleanup.push_async_callback(self.stt.close)
            await self.interrupt()
        if not self.trace_failed:
            await self.trace_event("cost", usage=self.costs.snapshot())


_ACK_OPENING = re.compile(
    r"^\s*(?:(?:sure|okay|ok|alright|got it|of course|certainly|absolutely|thanks|thank you|one moment|"
    r"let me (?:check|see)(?: that)?|just a moment|जी|ठीक है|धन्यवाद|एक पल)(?=[\s,.!।-]|$)[\s,.!।-]*)+", re.I)


def strip_ack(sentence):
    """Drop an acknowledgement the model opened with ("Sure! Your balance..." -> "Your balance...").
    A sentence that was only an acknowledgement becomes empty. Names after "Thanks," are kept."""
    if re.match(r"^\s*(?i:thanks|thank you)\s*,?\s+[A-Z][a-z]+[.!]", sentence):
        return sentence   # "Thanks, Priya." addresses the caller; leave it
    rest = _ACK_OPENING.sub("", sentence, count=1)
    if rest == sentence:
        return sentence
    rest = rest.strip()
    return (rest[:1].upper() + rest[1:]) if rest else ""


def output_language(text, selected):
    if selected == 'hinglish':
        return 'hi'
    scripts = [(0x0900, 0x097F, "mr" if selected == "mr" else "hi"),
               (0x0980, 0x09FF, "bn"), (0x0A00, 0x0A7F, "pa"),
               (0x0A80, 0x0AFF, "gu"), (0x0B00, 0x0B7F, "od"),
               (0x0B80, 0x0BFF, "ta"), (0x0C00, 0x0C7F, "te"),
               (0x0C80, 0x0CFF, "kn"), (0x0D00, 0x0D7F, "ml")]
    scores = [(sum(start <= ord(c) <= end for c in text), lang) for start, end, lang in scripts]
    count, lang = max(scores)
    return lang if count else "en"
