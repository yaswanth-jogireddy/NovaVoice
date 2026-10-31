"""
Sarvam AI Bulbul TTS — persistent WebSocket streaming.

Architecture
------------
One WebSocket connection is kept alive for the entire session (configured once,
reused for every phrase).  For each LLM phrase the flow is:

    send text msg  →  send flush msg  →  receive audio chunks  →  yield PCM

This eliminates the HTTP round-trip + full-synthesis wait that the old REST
approach had per phrase.  First audio bytes now arrive in ~80-150 ms instead
of 200-400 ms.

Audio format
------------
We request linear16 (raw PCM) at 16 kHz / mono — the same format LiveKit
expects — so no decoding or resampling is needed.

Connection lifecycle
--------------------
- connect() on initialize()
- Keepalive ping every 20 s (server auto-closes after 60 s idle)
- Auto-reconnects on drop with exponential backoff (max 3 attempts)
- cancel() sets a flag; the receive loop checks it between chunks
"""

import asyncio
import base64
import collections
import contextlib
import json
import struct
import time
from collections.abc import AsyncIterator
from typing import Optional

import websockets
import websockets.exceptions
from websockets.asyncio.client import ClientConnection
from websockets.connection import State as _WsState

def _ws_open(ws: Optional[ClientConnection]) -> bool:
    return ws is not None and ws.state is _WsState.OPEN

from config import Settings
from utils.logger import get_logger
from utils.speech_text import phone_numbers_for_speech

logger = get_logger("tts")

_WS_BASE = "wss://api.sarvam.ai/text-to-speech/ws"
_PING_INTERVAL_S = 20       # keepalive — server closes after 60 s idle
_RECONNECT_MAX = 3          # max reconnect attempts before giving up
_RECONNECT_DELAY_S = 0.5    # base delay for reconnect backoff
_COMPLETION_EVENT = "final" # event_type that signals synthesis is done
_FALLBACK_MODEL = "bulbul:v3"  # used when a configured newer model is rejected
# Max seconds to wait per WS message for a phrase (reset on every audio chunk).
# Was 15s — on a live call that is 15s of dead air before we even notice the
# server went quiet (observed 2026-08-22: a wedged phrase held SPEAKING ~30s
# until an abnormal close). First audio for a phrase normally lands <1s; 6s is
# already generous, and the zero-audio REST fallback below covers the miss.
_WS_PHRASE_TIMEOUT_S = 6
# The FIRST audio of a phrase gets a shorter wait. A kept-open socket can go
# silently dead between turns (2026-10-09 live call: no audio, error or close
# for 6.3 s, then REST, 10 s of dead air). First audio normally lands well under
# 1 s, so 2 s still leaves room before REST takes over.
_WS_FIRST_AUDIO_TIMEOUT_S = 2.0
_WS_OPEN_TIMEOUT_S = 5
_WS_RETRY_COOLDOWN_S = 60

# Maps detected language code → Sarvam BCP-47 target_language_code
_LANG_TO_SARVAM: dict[str, str] = {
    "hi": "hi-IN",
    "en": "en-IN",
    "bn": "bn-IN",
    "ta": "ta-IN",
    "te": "te-IN",
    "gu": "gu-IN",
    "kn": "kn-IN",
    "ml": "ml-IN",
    "mr": "mr-IN",
    "pa": "pa-IN",
    "od": "od-IN",
}
_DEFAULT_LANG = "hi-IN"



def _split_breath_groups(text: str, max_chars: Optional[int] = None) -> list[str]:
    """
    Split at sentence boundaries and merge tiny chunks to avoid mid-phrase restarts.
    With max_chars (item 3), whole sentences are joined up to that length instead:
    one Sarvam generation, so no seam between them.
    """
    import re

    # "।" (Devanagari danda) included: the agent's Hindi replies end sentences
    # with it, so without it a multi-sentence Hindi reply was ONE giant group —
    # slower first audio and flatter prosody than per-sentence synthesis.
    raw = re.split(r"(?<=[.?!।])\s+", text.strip())
    merged: list[str] = []
    buf = ""
    for chunk in raw:
        if not chunk:
            continue
        if max_chars:
            if buf and len(buf) + 1 + len(chunk) > max_chars:
                merged.append(buf)
                buf = ""
            buf = (buf + " " + chunk).strip() if buf else chunk
            continue
        buf = (buf + " " + chunk).strip() if buf else chunk
        if len(buf.split()) >= 5:
            merged.append(buf)
            buf = ""
    if buf:
        merged.append(buf)
    return merged or [text]


class TTSProcessor:
    """Sarvam AI Bulbul TTS with persistent WebSocket streaming."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cancelled = False
        self._is_synthesizing = False

        self._gender = (settings.sarvam_tts_voice_gender or "male").strip().lower()
        # One speaker for the whole call, in every language
        self._speaker = (
            settings.sarvam_tts_voice_female
            if self._gender == "female"
            else settings.sarvam_tts_voice_male
        )
        self._model = settings.sarvam_tts_model
        self._pace = settings.sarvam_tts_pace
        # v3 introduces temperature (0.01–2.0) as the only expressivity knob;
        # v2 ignores this field. Safe to send to both — Sarvam silently drops
        # unknown keys on v2. Higher values = more prosodic variation but >1.0
        # risks mispronunciations.
        self._temperature = getattr(settings, "sarvam_tts_temperature", 0.7)
        self._sample_rate = settings.sarvam_tts_sample_rate
        self._api_key = settings.sarvam_api_key

        # Accent layer (accents/) — optional override on top of the
        # gender-based defaults above. "neutral"/unset AGENT_ACCENT never
        # enters this block, so _accent_speaker/_accent_dict_id stay None and
        # _pace/_temperature stay exactly what they were before this feature
        # existed — today's behavior, byte-for-byte, no hot-path cost.
        self._accent_speaker: Optional[str] = None
        self._accent_dict_id: Optional[str] = None
        accent_name = (getattr(settings, "agent_accent", "") or "neutral").strip().lower()
        if accent_name and accent_name != "neutral":
            try:
                from accents import load_accent
                accent = load_accent(accent_name)
                self._accent_speaker = accent.get("speaker") or None
                self._accent_dict_id = accent.get("dict_id") or None
                if accent.get("pace") is not None:
                    self._pace = accent["pace"]
                if accent.get("temperature") is not None:
                    self._temperature = accent["temperature"]
                logger.info(
                    "tts_accent_loaded",
                    accent=accent_name,
                    speaker=self._accent_speaker,
                    has_dict_id=bool(self._accent_dict_id),
                )
            except Exception as e:
                logger.warning("tts_accent_load_failed", accent=accent_name, code=type(e).__name__)

        # Bulbul v4 voices are named per style ("ishita_enhi_customer"); v3 uses
        # plain names ("ishita"). A mismatch fails every sentence with a 400, so
        # keep the proven v3 model rather than start a call that cannot speak.
        if self._model.startswith('bulbul:v4') and '_' not in self._speaker:
            logger.warning("tts_v4_needs_v4_voice", model=self._model, speaker=self._speaker,
                           fallback=_FALLBACK_MODEL)
            self._model = _FALLBACK_MODEL

        self._ws: Optional[ClientConnection] = None
        self._ws_lock = asyncio.Lock()
        # Serializes the send+recv loop so two overlapping synthesis calls never both
        # await recv() on the same WebSocket (that raises "ConcurrencyError: cannot call
        # recv while another coroutine is already running recv"). This happened on the
        # very first real call when the greet and the reply to the prospect's "Hello"
        # synthesised at once and collided, killing the greet. Distinct from _ws_lock
        # (which guards connect/close) to avoid re-entrant deadlock via _ensure_connected.
        self._synth_lock = asyncio.Lock()
        self._ping_task: Optional[asyncio.Task] = None
        self._close_tasks = set()
        # Track the (language, speaker) the WS was configured with so we reconnect on switch
        self._connected_lang: Optional[str] = None
        self._connected_speaker: Optional[str] = None
        self._ws_retry_after = 0.0
        # F4: phrases already sent on the current socket while the previous one
        # was still being received: (socket, text, cost event). Sarvam answers
        # flushes in order, so the next synthesis only receives.
        self._sent_ahead = collections.deque()
        self._send_lock = asyncio.Lock()
        # Diagnostics only: called with one dict per stall (timings and codes,
        # never text). The session stores it with the call.
        self.on_stall = None
        self._stall: Optional[dict] = None
        # Item 1: background reopen after a barge-in; last requested (lang, speaker).
        self._reconnect_task: Optional[asyncio.Task] = None
        self._wanted: Optional[tuple] = None
        self._closing = False

    @property
    def _group_chars(self) -> Optional[int]:
        return self._settings.tts_group_chars if getattr(self._settings, 'tts_fewer_cuts', False) else None

    @property
    def _fast(self) -> bool:
        return bool(getattr(self._settings, 'tts_fast_recovery', False))

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def initialize(self) -> None:
        default_language = self._settings.speech_default_language
        default_speaker = self._speaker_for_lang(default_language)
        try:
            await self._connect(lang=_LANG_TO_SARVAM.get(default_language, _DEFAULT_LANG), speaker=default_speaker)
        except (OSError, TimeoutError, websockets.exceptions.WebSocketException) as exc:
            # Startup is only a warm-up. A failed WS handshake must not prevent
            # the existing REST synthesis path from serving the first turn.
            self._ws_retry_after = time.monotonic() + _WS_RETRY_COOLDOWN_S
            await self._disconnect()
            logger.warning("tts_ws_startup_using_rest", code=type(exc).__name__)
            print("[TTS] WebSocket unavailable; using Sarvam REST speech fallback")
            return
        logger.info(
            "tts_ws_initialized",
            model=self._model,
            speaker=default_speaker,
            sample_rate=self._sample_rate,
        )
        print(
            f"[TTS] Sarvam WebSocket ready - speaker={default_speaker}, "
            f"model={self._model}, rate={self._sample_rate}Hz"
        )

    async def close(self) -> None:
        self._closing = True
        task = self._reconnect_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._disconnect()
        if self._close_tasks:
            await asyncio.gather(*tuple(self._close_tasks), return_exceptions=True)

    def cancel(self) -> None:
        """Hard-cancel any in-flight synthesis. Called on barge-in.

        Sets the flag (the recv loop checks it between chunks) AND closes the
        WebSocket — this kills any audio Sarvam has already buffered server-side
        and is about to stream. Without the close(), the recv loop sees
        _cancelled=True only between chunks, so we'd keep emitting 100-300ms
        of stale audio after the user has already started speaking. The next
        synthesize() call will reopen the WS via _ensure_connected.
        """
        self._cancelled = True
        in_flight = self._is_synthesizing or bool(self._sent_ahead)
        self._is_synthesizing = False
        if not in_flight:
            # F1: an idle socket carries no audio. Closing it here (every caller
            # turn calls interrupt()) forced a TLS + WebSocket reconnect before
            # each reply's first word, about 0.5-0.7 s in production.
            logger.debug("tts_cancelled_idle")
            return
        self._discard_sent_ahead()
        try:
            ws = self._ws
            if ws is not None:
                # Schedule the close — we're called from a sync context (state
                # machine transition) and cannot await here.
                task = asyncio.create_task(self._force_close_ws(ws))
                self._close_tasks.add(task)
                task.add_done_callback(self._close_tasks.discard)
                self._ws = None
                self._connected_lang = None
                self._connected_speaker = None
                if self._fast:
                    # Item 1: the next reply should not pay the reconnect (~0.8 s).
                    self._reconnect_soon(after=task)
        except Exception:
            pass
        logger.debug("tts_cancelled")

    def _close_in_background(self, ws) -> None:
        task = asyncio.create_task(self._force_close_ws(ws))
        self._close_tasks.add(task)
        task.add_done_callback(self._close_tasks.discard)

    def _reconnect_soon(self, after=None) -> None:
        """Item 1: reopen the socket in the background for the next reply."""
        if self._closing or self._wanted is None or (self._reconnect_task and not self._reconnect_task.done()):
            return
        lang, speaker = self._wanted
        async def reconnect():
            if after is not None:
                with contextlib.suppress(Exception):
                    await after
            if self._closing or _ws_open(self._ws) or time.monotonic() < self._ws_retry_after:
                return
            try:
                await self._connect(lang, speaker, only_if_closed=True)
            except Exception as exc:
                logger.warning("tts_ws_background_reconnect_failed", code=type(exc).__name__)
        self._reconnect_task = asyncio.create_task(reconnect())

    async def _await_reconnect(self) -> None:
        task = self._reconnect_task
        if task is not None and not task.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(task), _WS_OPEN_TIMEOUT_S + 1)

    def _drop_sent_ahead_on(self, ws) -> None:
        """Phrases sent ahead on a dead socket will never be answered there."""
        tracker = getattr(self, "cost_tracker", None)
        kept = collections.deque()
        while self._sent_ahead:
            entry = self._sent_ahead.popleft()
            if entry[0] is ws:
                if tracker and entry[2]:
                    tracker.finish(entry[2], "uncertain")
            else:
                kept.append(entry)
        self._sent_ahead = kept

    def _discard_sent_ahead(self) -> None:
        tracker = getattr(self, "cost_tracker", None)
        while self._sent_ahead:
            _, _, event = self._sent_ahead.popleft()
            if tracker and event:
                tracker.finish(event, "uncertain")  # sent; synthesis may have run

    async def send_ahead(self, phrase: str, language: Optional[str] = None) -> bool:
        """F4: queue the next phrase on the open socket while the current one is
        still being received, so its audio starts with no gap. False when not
        possible (closed socket, cancelled, other language/speaker); the normal
        synthesize() path then sends it as before."""
        if not self._settings.tts_send_ahead or self._cancelled or not _ws_open(self._ws):
            return False
        lang_code = language or ""
        target_lang = _LANG_TO_SARVAM.get(lang_code, _DEFAULT_LANG)
        if self._connected_lang != target_lang or self._connected_speaker != self._speaker_for_lang(lang_code):
            return False
        phrase = phone_numbers_for_speech(phrase.strip(), lang_code or self._settings.speech_default_language)
        if not phrase:
            return False
        ws = self._ws
        tracker = getattr(self, "cost_tracker", None)
        try:
            async with self._send_lock:
                for group in _split_breath_groups(phrase, self._group_chars):
                    event = tracker.begin("tts", "sarvam", self._model, {"characters": len(group)},
                                          "submitted_text", transport="websocket") if tracker else None
                    self._sent_ahead.append((ws, group, event))
                    await ws.send(json.dumps({"type": "text", "data": {"text": group}}))
                    await ws.send(json.dumps({"type": "flush"}))
        except Exception as exc:
            from utils.costs import ProviderBudgetExceeded
            if isinstance(exc, ProviderBudgetExceeded):
                raise
            logger.warning("tts_send_ahead_failed", code=type(exc).__name__)
            # Partly queued text would arrive before the next phrase: drop the socket.
            self.cancel()
            return False
        return True

    async def _force_close_ws(self, ws: Optional[ClientConnection]) -> None:
        """Close the WebSocket without taking the lock — called from cancel()."""
        if ws is None:
            return
        try:
            await asyncio.wait_for(ws.close(), 3)
        except Exception:
            pass

    def reset(self) -> None:
        self._cancelled = False
        self._is_synthesizing = False

    # ------------------------------------------------------------------ #
    # Speaker selection                                                    #
    # ------------------------------------------------------------------ #

    def _speaker_for_lang(self, lang_code: str) -> str:
        """Return the Sarvam speaker for the call, whatever the language.

        An accent override (accents/, via AGENT_ACCENT) takes precedence when
        set. Otherwise the env-configured voice for the chosen gender is used
        for every language: switching language mid-call must not change who is
        speaking. Bulbul v3 speakers are multilingual; the language code only
        drives text normalisation.
        """
        if self._accent_speaker:
            return self._accent_speaker
        return self._speaker

    # ------------------------------------------------------------------ #
    # Public synthesis API (same interface as old REST TTSProcessor)       #
    # ------------------------------------------------------------------ #

    async def synthesize(
        self, phrase: str, language: Optional[str] = None
    ) -> AsyncIterator[bytes]:
        phrase = phrase.strip()
        if not phrase:
            return
        if self._cancelled:
            # Loud, because a latched _cancelled flag (cancel() without a
            # later reset()) makes the agent silently mute — if this ever
            # logs outside a barge-in window, that's the bug to chase.
            logger.warning("tts_synthesize_skipped_cancelled_flag", phrase_characters=len(phrase))
            return

        self._is_synthesizing = True
        lang_code = language or ""
        target_lang = _LANG_TO_SARVAM.get(lang_code, _DEFAULT_LANG)
        speaker = self._speaker_for_lang(lang_code)
        phrase = phone_numbers_for_speech(phrase, lang_code or self._settings.speech_default_language)

        try:
            for group in _split_breath_groups(phrase, self._group_chars):
                async for chunk in self._ws_synthesize(group, target_lang, speaker):
                    if self._cancelled:
                        return
                    yield chunk
        finally:
            self._is_synthesizing = False

    async def synthesize_stream(
        self,
        phrase_iterator: AsyncIterator[str],
        language: Optional[str] = None,
    ) -> AsyncIterator[bytes]:
        """Consume LLM phrase-boundary chunks and yield audio bytes."""
        self._cancelled = False

        try:
            async for phrase in phrase_iterator:
                if self._cancelled:
                    return

                logger.debug(
                    "tts_synthesizing_phrase",
                    phrase_characters=len(phrase),
                    phrase_len=len(phrase),
                )

                async for chunk in self.synthesize(phrase, language=language):
                    if self._cancelled:
                        return
                    yield chunk

        except asyncio.CancelledError:
            logger.debug("tts_stream_cancelled")
            raise

    @property
    def is_synthesizing(self) -> bool:
        return self._is_synthesizing

    # ------------------------------------------------------------------ #
    # WebSocket connection management                                       #
    # ------------------------------------------------------------------ #

    def _build_config_data(self, lang: str, speaker: str) -> dict:
        """Build the `data` object for a Sarvam WS "config" message.

        Shared by _connect() (new connection) and _update_config() (in-place
        update on an already-open connection) so the two can never drift.
        """
        config_data = {
            "target_language_code": lang,
            "speaker": speaker,
            "pace": self._pace,
            "temperature": self._temperature,
            "speech_sample_rate": self._sample_rate,
            "enable_preprocessing": True,
            "output_audio_codec": "linear16",
            "min_buffer_size": 30,
            "max_chunk_length": 200,
        }
        # dict_id is a bulbul:v3-only config field (confirmed against Sarvam's
        # WS streaming reference) — same place speaker/pace/temperature live.
        # Omitted entirely (not sent as null) when no accent dict is set, so
        # the neutral path's config message is unchanged from before this
        # feature existed.
        if self._accent_dict_id:
            config_data["dict_id"] = self._accent_dict_id
        return config_data

    async def _connect(self, lang: str, speaker: str, only_if_closed: bool = False) -> None:
        """Open a new WebSocket and send the config message."""
        async with self._ws_lock:
            if only_if_closed and _ws_open(self._ws):
                return  # a synthesis connected first; never replace a live socket
            await self._close_ws()

            headers = {"api-subscription-key": self._api_key}
            from utils.network import HAPPY_EYEBALLS
            while True:
                url = f"{_WS_BASE}?model={self._model}&send_completion_event=true"
                try:
                    self._ws = await websockets.connect(
                        url, additional_headers=headers,
                        open_timeout=_WS_OPEN_TIMEOUT_S, close_timeout=2, **HAPPY_EYEBALLS,
                    )
                    break
                except websockets.exceptions.InvalidStatus as exc:
                    # F6: a newer model the key cannot use (bulbul:v4 returned 403)
                    # must not silence the call: fall back to the proven v3 model.
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    if status in (401, 403) and self._model != _FALLBACK_MODEL:
                        logger.warning("tts_model_rejected_fallback", model=self._model, status=status,
                                       fallback=_FALLBACK_MODEL)
                        self._model = _FALLBACK_MODEL
                        # v3 knows only the base voice: "ishita_enhi_customer" -> "ishita".
                        self._speaker = self._speaker.split('_')[0]
                        speaker = speaker.split('_')[0]
                        continue
                    raise
            self._ws_retry_after = 0.0
            self._connected_lang = lang
            self._connected_speaker = speaker

            config_msg = json.dumps({"type": "config", "data": self._build_config_data(lang, speaker)})
            await self._ws.send(config_msg)

            # Start keepalive
            if self._ping_task is not None:
                self._ping_task.cancel()
            self._ping_task = asyncio.create_task(self._keepalive())
            logger.info("tts_ws_connected", lang=lang, speaker=speaker)

    async def _update_config(self, lang: str, speaker: str) -> bool:
        """Push a new config on the ALREADY-OPEN WebSocket instead of a full
        reconnect (close + new TLS/WS handshake + new config message).

        Sarvam's WS config message can be updated at any point during the
        connection lifecycle (confirmed against Sarvam's streaming API
        reference, 2026-08-19) — every prior version of this code paid a full
        reconnect for every language/speaker switch, which happens on nearly
        every turn once a caller code-switches between Hindi and English.

        Never raises — returns False on any failure so the caller falls back
        to the existing full-reconnect path in _ensure_connected(). An update
        that silently fails must never be reported as a success.
        """
        ws = self._ws
        if not _ws_open(ws):
            return False
        try:
            config_msg = json.dumps({"type": "config", "data": self._build_config_data(lang, speaker)})
            await ws.send(config_msg)
        except Exception as e:
            logger.warning("tts_ws_config_update_failed", lang=lang, speaker=speaker, code=type(e).__name__)
            return False
        self._connected_lang = lang
        self._connected_speaker = speaker
        logger.info("tts_ws_config_updated", lang=lang, speaker=speaker)
        return True

    async def _close_ws(self) -> None:
        """Close the WebSocket without holding the lock (called from within lock)."""
        if self._ping_task is not None:
            task, self._ping_task = self._ping_task, None
            task.cancel()
            if task is not asyncio.current_task():
                await asyncio.gather(task, return_exceptions=True)
        ws, self._ws = self._ws, None
        await self._force_close_ws(ws)
        self._connected_lang = None
        self._connected_speaker = None

    async def _disconnect(self) -> None:
        async with self._ws_lock:
            await self._close_ws()

    async def _ensure_connected(self, lang: str, speaker: str) -> bool:
        """Return True if we have a live connection configured for the given language+speaker."""
        self._wanted = (lang, speaker)
        if (
            _ws_open(self._ws)
            and self._connected_lang == lang
            and self._connected_speaker == speaker
        ):
            return True

        if time.monotonic() < self._ws_retry_after:
            return False

        # True only on an actual language/speaker switch mid-session, never on
        # the first-ever connect (_connected_lang is None then, and there's no
        # open socket yet to update in place).
        switching = self._connected_lang is not None and (
            self._connected_lang != lang or self._connected_speaker != speaker
        )
        if switching:
            logger.info("tts_ws_switching", new_lang=lang, new_speaker=speaker)
            print(f"[TTS] Switching to lang={lang}, speaker={speaker}")

            # Connection is already open — push the new config onto it instead
            # of tearing down and reopening (saves a TLS+WS handshake, ~100-
            # 300ms, on every switch). Falls through to the full reconnect
            # below if this fails for any reason (e.g. the socket died between
            # the open-check and the send).
            if await self._update_config(lang, speaker):
                return True

        for attempt in range(1, _RECONNECT_MAX + 1):
            try:
                await self._connect(lang, speaker)
                return True
            except Exception as e:
                logger.warning(
                    "tts_ws_reconnect_failed",
                    attempt=attempt,
                    code=type(e).__name__,
                )
                if isinstance(e, (TimeoutError, OSError)):
                    # Do not burn the entire demo turn on repeated handshakes.
                    # Use REST now, then try streaming again after a cooldown.
                    self._ws_retry_after = time.monotonic() + _WS_RETRY_COOLDOWN_S
                    await self._disconnect()
                    return False
                if attempt < _RECONNECT_MAX:
                    await asyncio.sleep(_RECONNECT_DELAY_S * attempt)

        logger.error("tts_ws_reconnect_exhausted")
        self._ws_retry_after = time.monotonic() + _WS_RETRY_COOLDOWN_S
        return False

    async def _keepalive(self) -> None:
        """Send a ping every 20 s to prevent the server from closing the connection."""
        try:
            while True:
                await asyncio.sleep(_PING_INTERVAL_S)
                if _ws_open(self._ws):
                    try:
                        await self._ws.send(json.dumps({"type": "ping"}))
                    except Exception:
                        pass  # will reconnect on next synthesis call
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------ #
    # Core streaming synthesis                                             #
    # ------------------------------------------------------------------ #

    async def _ws_synthesize(
        self, phrase: str, lang: str, speaker: str = ""
    ) -> AsyncIterator[bytes]:
        """Send one phrase over the WebSocket and yield PCM chunks as they arrive.

        The whole send+recv runs under _synth_lock so two overlapping synthesis calls
        (e.g. the opening greet and the first reply) queue instead of both calling
        recv() on the same socket — which throws ConcurrencyError and kills the audio.
        """
        async with self._synth_lock:
            effective_speaker = speaker or self._speaker
            if self._sent_ahead and not (self._sent_ahead[0][0] is self._ws and self._sent_ahead[0][1] == phrase):
                # Something else was sent ahead (e.g. a guard replaced the next
                # sentence). Its audio would play first: never reuse that socket.
                logger.warning("tts_send_ahead_mismatch", phrase_characters=len(phrase))
                stale = self._ws
                self._discard_sent_ahead()
                self._ws = None
                self._connected_lang = None
                self._connected_speaker = None
                await self._force_close_ws(stale)
            if self._fast:
                await self._await_reconnect()
            if not await self._ensure_connected(lang, effective_speaker):
                # Could not connect — fall back to REST for this phrase
                stall = {'stage': 'connect', 'reason': 'no_socket', 'waited_ms': 0, 'sent_ahead': False,
                         'phrase_characters': len(phrase), 'audio_ms': 0, 'close_ms': 0}
                async for chunk in self._rest_recovery(stall, phrase, lang, effective_speaker, stop_on_cancel=False):
                    yield chunk
                return

            ws = self._ws  # capture reference — keepalive may update self._ws

            got_audio = False
            delivered = 0
            async for chunk in self._recv_phrase_audio(ws, phrase):
                got_audio = True
                delivered += len(chunk)
                yield chunk
            stall, self._stall = self._stall, None

            # A phrase that produced ZERO audio and wasn't cancelled means the
            # WS exchange failed silently (timeout, server error, stale
            # completion event). Without a fallback the agent "speaks" nothing
            # and the caller repeats themselves (2026-08-22: a 185ms silent
            # SPEAKING turn made the user re-ask the same question). One REST
            # attempt guarantees every phrase gets a voice through some path.
            if not got_audio and not self._cancelled:
                logger.warning("tts_ws_zero_audio_using_rest", phrase_characters=len(phrase))
                logger.warning('speech_provider_issue', code='provider_failure')
                stall = stall or {'stage': 'first_audio', 'reason': 'empty', 'waited_ms': 0, 'sent_ahead': False,
                                  'phrase_characters': len(phrase), 'audio_ms': 0, 'close_ms': 0}
                recovery = (self._fresh_recovery(stall, phrase, lang, effective_speaker, skip=0) if self._fast
                            else self._rest_recovery(stall, phrase, lang, effective_speaker))
                async for chunk in recovery:
                    yield chunk
            elif stall and not self._cancelled:
                if self._fast:
                    # Audio started, then stopped: speak the rest, not the whole phrase again.
                    async for chunk in self._fresh_recovery(stall, phrase, lang, effective_speaker, skip=delivered):
                        yield chunk
                else:
                    # Audio started, then stopped: the rest of this phrase is not spoken.
                    self._report_stall({**stall, 'recovery': 'none', 'recovered': False, 'recovery_ms': 0})

    async def _fresh_recovery(self, stall, phrase, lang, speaker, skip):
        """Item 1: speak a stalled phrase on a new socket (REST if that fails too).

        The dead socket is already abandoned, so only one copy can ever play.
        `skip` bytes of PCM were already delivered; a mid-phrase recovery resumes
        after them (a slight seam is possible; losing half a sentence is worse).
        """
        started = time.monotonic()
        first = None
        path = 'fresh_ws'
        remaining = skip
        received = 0
        def unspoken(chunk):
            nonlocal remaining
            if remaining >= len(chunk):
                remaining -= len(chunk)
                return b''
            chunk, remaining = chunk[remaining:], 0
            return chunk
        try:
            if await self._ensure_connected(lang, speaker):
                async for chunk in self._recv_phrase_audio(self._ws, phrase):
                    received += len(chunk)
                    out = unspoken(chunk)
                    if out:
                        if first is None:
                            first = round((time.monotonic() - started) * 1000)
                        yield out
                self._stall = None  # a second failure is part of this recovery
            else:
                path = 'rest'
            if received == 0 and not self._cancelled:
                path = 'fresh_ws+rest' if path == 'fresh_ws' else path
                remaining = skip
                async for chunk in self._rest_fallback(phrase, lang, speaker):
                    if self._cancelled:
                        return
                    out = unspoken(chunk)
                    if out:
                        if first is None:
                            first = round((time.monotonic() - started) * 1000)
                        yield out
        finally:
            if not self._cancelled:
                self._report_stall({**stall, 'recovery': path, 'recovered': first is not None,
                                    'recovery_ms': first if first is not None else round((time.monotonic() - started) * 1000)})

    async def _rest_recovery(self, stall, phrase, lang, speaker, stop_on_cancel=True):
        """REST for a phrase the socket did not speak; report the stall once."""
        started = time.monotonic()
        first = None
        try:
            async for chunk in self._rest_fallback(phrase, lang, speaker):
                if stop_on_cancel and self._cancelled:
                    return
                if first is None:
                    first = round((time.monotonic() - started) * 1000)
                yield chunk
        finally:
            if not self._cancelled:
                self._report_stall({**stall, 'recovery': 'rest', 'recovered': first is not None,
                                    'recovery_ms': first if first is not None else round((time.monotonic() - started) * 1000)})

    def _report_stall(self, stall):
        # Dead air the caller heard from this phrase: wait, socket close, recovery.
        stall['dead_air_ms'] = stall.get('waited_ms', 0) + stall.get('close_ms', 0) + stall.get('recovery_ms', 0)
        logger.warning("tts_stall", **stall)
        if self.on_stall is not None:
            try:
                self.on_stall(dict(stall))
            except Exception:
                logger.warning("tts_stall_report_failed")

    async def _recv_phrase_audio(self, ws, phrase: str) -> AsyncIterator[bytes]:
        """Inner send+recv for one phrase. Always called while holding _synth_lock."""
        tracker = getattr(self, "cost_tracker", None)
        outcome = "uncertain"
        if self._sent_ahead and self._sent_ahead[0][0] is ws and self._sent_ahead[0][1] == phrase:
            _, _, event = self._sent_ahead.popleft()  # text already sent ahead (F4)
            sent = True
        else:
            event = tracker.begin("tts", "sarvam", self._model, {"characters": len(phrase)}, "submitted_text", transport="websocket") if tracker else None
            sent = False
        sent_ahead = sent
        self._stall = None
        got_audio = False
        waiting_since = time.monotonic()
        audio_bytes = 0

        def stalled(reason):
            self._stall = {'stage': 'mid_phrase' if got_audio else 'first_audio', 'reason': reason,
                           'waited_ms': round((time.monotonic() - waiting_since) * 1000),
                           'sent_ahead': sent_ahead, 'phrase_characters': len(phrase),
                           'audio_ms': round(audio_bytes / 2 / self._sample_rate * 1000)}
        try:
            if not sent:
                # Send text then flush so the server starts synthesising immediately
                async with self._send_lock:
                    await ws.send(json.dumps({"type": "text", "data": {"text": phrase}}))
                    await ws.send(json.dumps({"type": "flush"}))

            # Receive audio chunks until the "final" completion event.
            # Per-message timeout guards against Sarvam never sending "final".
            first_wait = (self._settings.tts_recovery_first_audio_ms / 1000 if self._fast
                          else _WS_FIRST_AUDIO_TIMEOUT_S)
            chunk_wait = (self._settings.tts_recovery_chunk_gap_ms / 1000 if self._fast
                          else _WS_PHRASE_TIMEOUT_S)
            deadline = asyncio.get_event_loop().time() + first_wait
            waiting_since = time.monotonic()
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                timed_out = remaining <= 0
                if not timed_out:
                    try:
                        raw_msg = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        timed_out = True
                if timed_out:
                    # No first audio: the socket is likely dead; REST takes over at once.
                    event_name = "tts_ws_phrase_timeout" if got_audio else "tts_ws_first_audio_timeout"
                    logger.warning(event_name, phrase_characters=len(phrase))
                    print(f"[TTS] {event_name}: no audio/'final' in time; using fallback")
                    stalled('timeout')
                    break

                if self._cancelled:
                    return

                try:
                    msg = json.loads(raw_msg)
                except Exception:
                    continue

                msg_type = msg.get("type")

                if msg_type == "audio":
                    b64 = msg.get("data", {}).get("audio", "")
                    if b64:
                        got_audio = True
                        pcm = base64.b64decode(b64)
                        audio_bytes += len(pcm)
                        yield pcm
                    # Each audio chunk resets the deadline — server is still active
                    deadline = asyncio.get_event_loop().time() + chunk_wait
                    waiting_since = time.monotonic()

                elif msg_type == "event":
                    if msg.get("data", {}).get("event_type") == _COMPLETION_EVENT:
                        outcome = "completed"
                        break  # this phrase is done; ready for the next one

                elif msg_type == "error":
                    err = msg.get("data", {}).get("message", "unknown error")
                    logger.error("tts_ws_server_error", code='provider_error', phrase_characters=len(phrase))
                    logger.warning('speech_provider_issue', code='provider_failure')
                    stalled('server_error')
                    break

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning("tts_ws_connection_closed", code=e.code, phrase_characters=len(phrase))
            if not self._cancelled:
                stalled('closed')
        except Exception as e:
            logger.warning("tts_ws_error", code=type(e).__name__, phrase_characters=len(phrase))
            logger.warning('speech_provider_issue', code='provider_failure')
            if not self._cancelled:
                stalled('error')

        finally:
            # Timeout/error paths may have invalidated the current connection.
            # Close that captured socket without clearing a newer replacement.
            if outcome != 'completed':
                if self._ws is ws:
                    self._ws = None
                    self._connected_lang = None
                    self._connected_speaker = None
                if self._fast:
                    # Item 1: a dead socket can take its full close timeout; never wait for it.
                    self._close_in_background(ws)
                    self._drop_sent_ahead_on(ws)
                    if self._stall is not None:
                        self._stall['close_ms'] = 0
                else:
                    closing = time.monotonic()
                    await self._force_close_ws(ws)
                    if self._stall is not None:
                        self._stall['close_ms'] = round((time.monotonic() - closing) * 1000)
            if tracker:
                tracker.finish(event, outcome)

    # ------------------------------------------------------------------ #
    # REST fallback (if WebSocket cannot be established)                   #
    # ------------------------------------------------------------------ #

    async def _rest_fallback(self, phrase: str, lang: str, speaker: str = "") -> AsyncIterator[bytes]:
        """One-shot REST call used only when WebSocket is unavailable."""
        import httpx

        logger.warning("tts_using_rest_fallback", phrase_characters=len(phrase))
        logger.warning('speech_provider_issue', code='provider_failure')

        payload = {
            "inputs": [phrase],
            "target_language_code": lang,
            "speaker": speaker or self._speaker,
            "model": self._model,
            "pace": self._pace,
            "speech_sample_rate": self._sample_rate,
            "output_audio_codec": "wav",
            "enable_preprocessing": True,
        }
        # Same accent dict_id used on the WS path — omitted when unset so the
        # neutral path's payload is unchanged from before this feature existed.
        if self._accent_dict_id:
            payload["dict_id"] = self._accent_dict_id
        tracker = getattr(self, "cost_tracker", None)
        event = tracker.begin("tts", "sarvam", self._model, {"characters": len(phrase)}, "submitted_text", transport="rest") if tracker else None
        outcome = "uncertain"
        request_id = None
        try:
            async with httpx.AsyncClient(
                headers={
                    "api-subscription-key": self._api_key,
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(15.0, connect=5.0),
            ) as client:
                resp = await client.post("https://api.sarvam.ai/text-to-speech", json=payload)
                if resp.status_code in {400, 401, 403, 404, 422, 429}:
                    outcome = "rejected"
                resp.raise_for_status()
                request_id = resp.headers.get("x-request-id")
                outcome = "completed"
                data = resp.json()
                audios = data.get("audios", [])
                if audios:
                    yield _strip_wav_header(base64.b64decode(audios[0]))
        except Exception as e:
            logger.warning("tts_rest_fallback_error", code=type(e).__name__, phrase_characters=len(phrase))
            logger.warning('speech_provider_issue', code='provider_failure')

        finally:
            if tracker:
                tracker.finish(event, outcome, request_id=request_id)


# ------------------------------------------------------------------ #
# WAV header stripper (used only by the REST fallback path)           #
# ------------------------------------------------------------------ #

def _strip_wav_header(wav_bytes: bytes) -> bytes:
    """Remove the RIFF/WAV header and return raw PCM bytes."""
    if len(wav_bytes) < 44 or wav_bytes[:4] != b"RIFF":
        return wav_bytes

    offset = 12  # skip "RIFF" (4) + file_size (4) + "WAVE" (4)
    while offset + 8 <= len(wav_bytes):
        chunk_id = wav_bytes[offset : offset + 4]
        chunk_size = struct.unpack_from("<I", wav_bytes, offset + 4)[0]
        if chunk_id == b"data":
            return wav_bytes[offset + 8 :]
        offset += 8 + chunk_size

    return wav_bytes[44:]


def create_tts_processor(settings: Settings, provider: str = "sarvam") -> "TTSProcessor":
    """Factory — returns the correct TTS processor for the given provider.

    provider="sarvam"  → Sarvam Bulbul v2 (default, India-first)
    provider="elevenlabs" → ElevenLabs (higher quality, higher latency)
    """
    if provider == "elevenlabs":
        from pipeline.tts_elevenlabs import ElevenLabsTTSProcessor
        return ElevenLabsTTSProcessor(settings)
    if provider == 'openai':
        from pipeline.tts_openai import OpenAITTSProcessor
        return OpenAITTSProcessor(settings)
    if provider == 'smallest':
        from pipeline.tts_smallest import SmallestTTSProcessor
        return SmallestTTSProcessor(settings)
    return TTSProcessor(settings)
