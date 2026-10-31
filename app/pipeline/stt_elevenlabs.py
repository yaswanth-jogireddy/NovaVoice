"""ElevenLabs Scribe realtime adapter with the existing STTProcessor interface."""
import asyncio
import base64
import contextlib
import json
from urllib.parse import urlencode

from websockets.asyncio.client import connect


class ElevenLabsSTTProcessor:
    def __init__(self, settings):
        self.settings = settings
        self.detected_language = settings.speech_default_language
        self._ws = None
        self._task = None
        self._callback = None
        self._disconnect = None
        self._speech_end = None
        self._closing = False

    async def initialize(self):
        if not self.settings.elevenlabs_api_key:
            raise ValueError("Set ELEVENLABS_API_KEY in voice_module/.env")

    def on_transcript(self, callback):
        self._callback = callback

    def on_disconnect(self, callback):
        self._disconnect = callback

    def on_sarvam_speech_start(self, callback):
        pass  # Local VAD or transcript evidence handles interruptions.

    def on_sarvam_speech_end(self, callback):
        self._speech_end = callback

    async def start_stream(self, session_id):
        self._closing = False
        query = urlencode({
            "model_id": "scribe_v2_realtime",
            "audio_format": f"pcm_{self.settings.audio_sample_rate}",
            "commit_strategy": "vad",
            "vad_silence_threshold_secs": self.settings.stt_endpoint_s,
            "include_language_detection": "true",
        })
        self._ws = await connect(
            f"wss://api.elevenlabs.io/v1/speech-to-text/realtime?{query}",
            additional_headers={"xi-api-key": self.settings.elevenlabs_api_key},
            open_timeout=10, close_timeout=3, max_size=1024 * 1024,
        )
        try:
            first = json.loads(await asyncio.wait_for(self._ws.recv(), 10))
            if first.get("message_type") != "session_started":
                raise RuntimeError("Speech provider rejected session; check account access and quota")
        except BaseException:
            await self._ws.close()
            raise
        self._task = asyncio.create_task(self._listen())

    async def _listen(self):
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                kind = msg.get("message_type", "")
                if kind in {"partial_transcript", "committed_transcript", "committed_transcript_with_timestamps"}:
                    text = msg.get("text", "").strip()
                    lang = msg.get("language_code")
                    if lang:
                        self.detected_language = {"eng": "en", "spa": "es", "deu": "de", "ita": "it", "ara": "ar"}.get(lang, lang)
                    if text and self._callback:
                        final = kind != "partial_transcript"
                        if final and self._speech_end:
                            await self._speech_end()
                        await self._callback(text, final)
                elif "error" in kind or kind in {"auth_error", "quota_exceeded", "rate_limited", "unaccepted_terms"}:
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            if not self._closing and self._disconnect:
                await self._disconnect()

    async def feed_audio(self, audio_chunk):
        if not self._ws or self._closing:
            return
        await self._ws.send(json.dumps({
            "message_type": "input_audio_chunk",
            "audio_base_64": base64.b64encode(audio_chunk).decode("ascii"),
            "sample_rate": self.settings.audio_sample_rate,
        }))

        tracker = getattr(self, "cost_tracker", None)
        if tracker:
            tracker.audio("elevenlabs", "scribe_v2_realtime", len(audio_chunk), self.settings.audio_sample_rate)

    async def stop_stream(self):
        self._closing = True
        if self._ws:
            await self._ws.close()
        if self._task and self._task is not asyncio.current_task():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = self._ws = None

    async def close(self):
        await self.stop_stream()
