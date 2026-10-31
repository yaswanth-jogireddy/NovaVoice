import asyncio
import base64
import contextlib
import io
from collections.abc import Callable, Awaitable
from typing import Optional
import wave

import httpx
import websockets

from config import Settings
from utils.logger import get_logger

logger = get_logger("stt")


class AudioBacklogError(RuntimeError):
    """Recognition cannot keep up; ending the call avoids silent speech loss."""


def _emit_transcript_event(kind: str, text: str) -> None:
    try:
        from solar.events import bus as _eb
        _eb.emit(kind, text=text, speaker="caller")
    except Exception:
        pass


def _is_valid_transcript(text: str) -> bool:
    """Accept any non-empty transcript. The LLM/graph handles noise filtering
    better than a regex — rejecting short utterances here was dropping legitimate
    user replies like 'Hello', 'Haan', '2000 ke aaspaas', etc."""
    return bool(text and text.strip())


class SarvamSTTStreaming:
    """Sarvam AI STT via WebSocket streaming (Saaras v3, codemix mode).

    Handles Hindi, English, and mixed Hinglish in a single stream.
    Falls back to batch REST API if WebSocket connection fails.
    """

    def __init__(self, settings: Settings) -> None:
        self._api_key = settings.sarvam_api_key
        self._api_url = settings.sarvam_api_url
        self._model = settings.sarvam_model
        self._mode = settings.sarvam_mode
        self._language = settings.sarvam_language
        self._sample_rate = settings.audio_sample_rate
        self.detected_language: Optional[str] = "hi"

        # WebSocket streaming state
        self._ws_context = None
        self._ws = None
        self._listen_task: Optional[asyncio.Task] = None
        self._is_connected = False
        self._expected_close = False
        self._disconnect_callback = None
        self._transcript_callback: Optional[
            Callable[[str, bool], Awaitable[None]]
        ] = None
        self._feed_count = 0

        # Sarvam server-side VAD signal callbacks
        self._on_sarvam_speech_start: Optional[Callable[[], Awaitable[None]]] = None
        self._on_sarvam_speech_end: Optional[Callable[[], Awaitable[None]]] = None

        # Batch REST fallback (used if WebSocket fails)
        # Multipart requests set their own Content-Type; a JSON default broke them.
        self._http_client = httpx.AsyncClient(
            headers={"api-subscription-key": self._api_key},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
        self._flush_enabled = bool(getattr(settings, "stt_flush_silence_ms", 0))
        self._audio_buffer = bytearray()
        self._buffer_flush_size = self._sample_rate * 2 * 1  # 1 second of 16-bit audio
        self._use_batch_fallback = False

    async def start_stream(self, session_id: str) -> None:
        """Connect to Sarvam WebSocket streaming endpoint."""
        self._expected_close = False
        try:
            self._ws_context = self._open_stream()
            self._ws = await self._ws_context.__aenter__()
            self._is_connected = True
            self._listen_task = asyncio.create_task(self._listen_loop())
            logger.info("sarvam_ws_stream_started", session_id=session_id)
            print(f"[STT] Sarvam WebSocket connected (model={self._model}, mode={self._mode})")
        except Exception as e:
            logger.warning("sarvam_ws_connect_failed", code=type(e).__name__,
                           status=getattr(getattr(e, "response", None), "status_code", None))
            logger.warning('speech_provider_issue', code='provider_failure')
            self._use_batch_fallback = True

    @contextlib.asynccontextmanager
    async def _open_stream(self):
        """The same socket the SDK's connect() opens, with Happy Eyeballs.

        The SDK accepts no socket options, and on a NAT64 network its IPv6-first
        connect timed out. Same URL, query and auth header; the SDK's own client
        wraps the socket, so transcribe()/recv() behave as before.
        """
        from sarvamai.environment import SarvamAIEnvironment
        from sarvamai.speech_to_text_streaming.socket_client import AsyncSpeechToTextStreamingSocketClient
        from utils.network import HAPPY_EYEBALLS
        # "unknown" lets Sarvam detect the language of every segment; the
        # returned language_code is stored in self.detected_language.
        params = {"language-code": "unknown", "model": self._model, "mode": self._mode,
                  "sample_rate": str(self._sample_rate), "input_audio_codec": "pcm_s16le",
                  "vad_signals": "true"}
        if self._flush_enabled:
            params["flush_signal"] = "true"  # F3: allow finalize() on caller silence
        query = httpx.QueryParams(params)
        url = f"{SarvamAIEnvironment.PRODUCTION.production}/speech-to-text/ws?{query}"
        async with websockets.connect(url, additional_headers={"Api-Subscription-Key": self._api_key},
                                      open_timeout=10, **HAPPY_EYEBALLS) as ws:
            yield AsyncSpeechToTextStreamingSocketClient(websocket=ws)

    async def finalize(self) -> bool:
        """F3: ask Sarvam to finalise buffered speech now (documented flush
        signal), instead of waiting for its own end-of-speech detection."""
        if not (self._flush_enabled and self._is_connected and self._ws is not None):
            return False
        try:
            await self._ws.flush()
            return True
        except Exception as exc:
            logger.warning("sarvam_ws_flush_failed", code=type(exc).__name__)
            return False

    async def _listen_loop(self) -> None:
        """Listen for transcript messages from Sarvam WebSocket via recv()."""
        try:
            while self._is_connected:
                try:
                    message = await self._ws.recv()
                except Exception:
                    break

                msg_type = getattr(message, "type", None)

                if msg_type == "data":
                    data = message.data
                    transcript = getattr(data, "transcript", "")
                    if transcript and transcript.strip():
                        lang = getattr(data, "language_code", None)
                        if lang:
                            self.detected_language = lang.split("-")[0]

                        # Sarvam marks final segments with is_final=True; partials arrive
                        # with is_final=False (or the field absent). We pass the flag through
                        # so the agent can show live text without triggering LLM processing.
                        is_final = bool(getattr(data, "is_final", True))

                        if self._transcript_callback is not None:
                            clean = transcript.strip()
                            if is_final:
                                if not _is_valid_transcript(clean):
                                    safe = clean[:40].encode("ascii", errors="replace").decode("ascii")
                                    logger.warning('speech_provider_issue', code='provider_failure')
                                    continue
                                _emit_transcript_event("transcript_final", clean)
                            else:
                                _emit_transcript_event("transcript_partial", clean)
                            await self._transcript_callback(clean, is_final)

                elif msg_type == "events":
                    data = message.data
                    signal = getattr(data, "signal_type", None)
                    if signal == "START_SPEECH":
                        logger.debug("sarvam_speech_start")
                        if self._on_sarvam_speech_start is not None:
                            await self._on_sarvam_speech_start()
                    elif signal == "END_SPEECH":
                        logger.debug("sarvam_speech_end")
                        # Sarvam's server-side endpointing fires here — use it as a
                        # fast-path signal that the utterance is complete, letting the
                        # agent process without waiting for the full Silero silence timeout.
                        if self._on_sarvam_speech_end is not None:
                            await self._on_sarvam_speech_end()

                elif msg_type == "error":
                    data = message.data
                    logger.warning(
                        "sarvam_ws_error_message",
                        error=getattr(data, "error", ""),
                        code=getattr(data, "code", ""),
                    )

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("sarvam_ws_listen_error", code=type(exc).__name__)
        finally:
            self._is_connected = False
            logger.info("sarvam_ws_listen_loop_exited")
            if not self._expected_close and self._disconnect_callback is not None:
                await self._disconnect_callback()

    async def feed_audio(
        self,
        audio_chunk: bytes,
        callback: Optional[Callable[[str, bool], Awaitable[None]]],
    ) -> None:
        self._transcript_callback = callback
        # At most ten seconds while disconnected. Do not silently discard speech
        # or accumulate an unbounded call recording during an outage.
        if len(self._audio_buffer) + len(audio_chunk) > self._sample_rate * 2 * 10:
            raise AudioBacklogError("STT audio backlog exceeded ten seconds")

        if self._use_batch_fallback:
            self._audio_buffer.extend(audio_chunk)
            if len(self._audio_buffer) >= self._buffer_flush_size:
                await self._transcribe_buffer_rest(callback)
            return

        if not self._is_connected:
            self._audio_buffer.extend(audio_chunk)
            return

        try:
            self._feed_count += 1
            if self._feed_count == 1:
                print(f"[STT] First audio chunk sent to Sarvam ({len(audio_chunk)} bytes)")
            if self._feed_count % 500 == 0:
                print(f"[STT] {self._feed_count} chunks sent to Sarvam")

            audio_b64 = base64.b64encode(audio_chunk).decode("ascii")
            tracker = getattr(self, 'cost_tracker', None)
            if tracker:
                tracker.reserve_request('sarvam', self._model, {'audio_seconds': len(audio_chunk)/(self._sample_rate*2)})
            await self._ws.transcribe(audio=audio_b64)
            tracker = getattr(self, "cost_tracker", None)
            if tracker:
                tracker.audio("sarvam", self._model, len(audio_chunk), self._sample_rate)

        except Exception as e:
            from utils.costs import ProviderBudgetExceeded
            if isinstance(e, ProviderBudgetExceeded): raise
            logger.warning("sarvam_ws_send_error", code=type(e).__name__)
            logger.warning('speech_provider_issue', code='provider_failure')
            self._use_batch_fallback = True
            self._audio_buffer.extend(audio_chunk)

    async def _transcribe_buffer_rest(
        self,
        callback: Optional[Callable[[str, bool], Awaitable[None]]],
    ) -> None:
        """Batch REST fallback for when WebSocket is unavailable."""
        if not self._audio_buffer:
            return

        byte_count = len(self._audio_buffer)
        # Sarvam REST takes a multipart audio file at the unversioned path
        # (docs.sarvam.ai speech-to-text/transcribe), not base64 JSON under /v1.
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self._sample_rate)
            wav.writeframes(bytes(self._audio_buffer))
        self._audio_buffer.clear()

        tracker = getattr(self, "cost_tracker", None)
        event = tracker.begin("stt", "sarvam", self._model,
                              {"audio_seconds": byte_count / (self._sample_rate * 2)}, "sent_pcm") if tracker else None
        try:
            from sarvamai.environment import SarvamAIEnvironment
            response = await self._http_client.post(
                f"{SarvamAIEnvironment.PRODUCTION.base}/speech-to-text",
                files={"file": ("speech.wav", audio.getvalue(), "audio/wav")},
                data={"model": self._model, "language_code": self._language, "mode": self._mode},
            )
            response.raise_for_status()
            if tracker:
                tracker.finish(event, "completed", request_id=response.headers.get("x-request-id"))
            data = response.json()
            transcript = data.get("transcript", "")

            lang_code = data.get("language_code")
            if lang_code:
                self.detected_language = lang_code.split("-")[0]

            if transcript.strip() and callback is not None:
                clean = transcript.strip()
                if not _is_valid_transcript(clean):
                    safe = clean[:40].encode("ascii", errors="replace").decode("ascii")
                    logger.warning('speech_provider_issue', code='provider_failure')
                    return
                _emit_transcript_event("transcript_final", clean)
                await callback(clean, True)

        except Exception as exc:
            logger.warning("sarvam_rest_stt_error", code=type(exc).__name__,
                           status=getattr(getattr(exc, "response", None), "status_code", None))

    async def stop_stream(self) -> None:
        self._expected_close = True
        if self._listen_task is not None:
            if self._listen_task is not asyncio.current_task():
                self._listen_task.cancel()
                await asyncio.gather(self._listen_task, return_exceptions=True)
            self._listen_task = None

        if self._ws_context is not None:
            try:
                await self._ws_context.__aexit__(None, None, None)
            except Exception:
                pass
            self._ws_context = None
            self._ws = None

        self._is_connected = False

    async def flush(
        self,
        callback: Optional[Callable[[str, bool], Awaitable[None]]],
    ) -> None:
        if self._audio_buffer:
            await self._transcribe_buffer_rest(callback)

    async def close(self) -> None:
        await self.stop_stream()
        await self._http_client.aclose()


class STTProcessor:
    """STT processor — Sarvam AI (primary) with batch REST fallback."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._transcript_callback: Optional[
            Callable[[str, bool], Awaitable[None]]
        ] = None
        self._disconnect_callback: Optional[Callable[[], Awaitable[None]]] = None
        self._disconnect_notified = False
        self._sarvam: Optional[SarvamSTTStreaming] = None
        # Last language code detected (e.g. "en", "hi")
        self.detected_language: Optional[str] = None

    async def initialize(self) -> None:
        if self._settings.sarvam_api_key:
            self._sarvam = SarvamSTTStreaming(self._settings)
            self._sarvam._disconnect_callback = self._notify_disconnect
            self._sarvam.cost_tracker = getattr(self, "cost_tracker", None)
            self.detected_language = "hi"
            logger.info("stt_sarvam_initialized")
        else:
            logger.error("stt_no_sarvam_api_key_configured")

    def on_transcript(
        self, callback: Callable[[str, bool], Awaitable[None]]
    ) -> None:
        self._transcript_callback = callback

    def on_disconnect(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register callback fired when STT WebSocket disconnects unexpectedly."""
        self._disconnect_callback = callback

    def on_sarvam_speech_start(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register callback fired when Sarvam's server-side VAD detects speech start."""
        if self._sarvam is not None:
            self._sarvam._on_sarvam_speech_start = callback

    def on_sarvam_speech_end(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register callback fired when Sarvam's server-side VAD detects speech end.

        This fires faster than Silero VAD's silence timeout and can be used to
        trigger processing as soon as Sarvam confirms the utterance is complete.
        """
        if self._sarvam is not None:
            self._sarvam._on_sarvam_speech_end = callback

    async def finalize(self) -> bool:
        return await self._sarvam.finalize() if self._sarvam is not None else False

    @property
    def streaming(self) -> bool:
        """True while the live recognition socket is open (not the REST fallback)."""
        return self._sarvam is not None and self._sarvam._is_connected

    async def start_stream(self, session_id: str) -> None:
        self._disconnect_notified = False
        if self._sarvam is not None:
            await self._sarvam.start_stream(session_id)

    async def _notify_disconnect(self):
        if self._disconnect_notified: return
        self._disconnect_notified = True
        if self._disconnect_callback is not None:
            try:
                await self._disconnect_callback()
            except Exception as exc:
                logger.warning('stt_disconnect_callback_failed', code=type(exc).__name__)

    async def feed_audio(self, audio_chunk: bytes) -> None:
        if self._sarvam is not None:
            was_connected = self._sarvam._is_connected
            await self._sarvam.feed_audio(audio_chunk, self._transcript_callback)
            if self._sarvam.detected_language:
                self.detected_language = self._sarvam.detected_language
            # Fire disconnect callback once when connection drops
            if was_connected and not self._sarvam._is_connected:
                await self._notify_disconnect()

    async def stop_stream(self) -> None:
        if self._sarvam is not None:
            await self._sarvam.stop_stream()

    async def close(self) -> None:
        if self._sarvam is not None:
            await self._sarvam.close()
