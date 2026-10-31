"""Stream signed PCM through a persistent HTTP pool; retain the voice across languages."""
from collections.abc import AsyncIterator
from urllib.parse import quote
import httpx
from config import Settings
from utils.speech_text import phone_numbers_for_speech


class ElevenLabsTTSProcessor:
    def __init__(self, settings: Settings):
        self._settings = settings
        self._client = None
        self._cancelled = False
        self._synthesizing = False
        self._previous_text = ""

    async def initialize(self):
        if not self._settings.elevenlabs_api_key:
            raise ValueError("Set ELEVENLABS_API_KEY in voice_module/.env")
        if not self._settings.elevenlabs_voice_id:
            raise ValueError("Set ELEVENLABS_VOICE_ID to a stock or authorized cloned voice")
        self._client = httpx.AsyncClient(
            base_url="https://api.elevenlabs.io/v1/",
            headers={"xi-api-key": self._settings.elevenlabs_api_key},
            timeout=httpx.Timeout(12, connect=5),
        )

    async def close(self):
        self.cancel()
        if self._client:
            await self._client.aclose()
            self._client = None

    def cancel(self):
        self._cancelled = True
        self._previous_text = ""

    def reset(self):
        self._cancelled = False
        self._previous_text = ""

    @property
    def is_synthesizing(self):
        return self._synthesizing

    async def synthesize(self, phrase: str, language: str = "en") -> AsyncIterator[bytes]:
        if not phrase.strip() or self._cancelled:
            return
        if self._client is None:
            raise RuntimeError("TTS has not been initialized")
        cfg = self._settings
        phrase = phone_numbers_for_speech(phrase, language or ("hi" if any("\u0900" <= c <= "\u097f" for c in phrase) else "en"))
        payload = {
            "text": phrase, "model_id": cfg.elevenlabs_model_id,
            "voice_settings": {"stability": cfg.elevenlabs_stability,
                               "similarity_boost": cfg.elevenlabs_similarity_boost,
                               "speed": cfg.elevenlabs_speed},
        }
        # Carry only successfully synthesized text within this turn. No extra
        # request or next-sentence wait is needed for prosody continuity.
        if self._previous_text:
            payload["previous_text"] = self._previous_text
        if language and cfg.elevenlabs_model_id != "eleven_multilingual_v2":
            payload["language_code"] = language.split("-")[0]
        self._synthesizing = True
        tracker = getattr(self, "cost_tracker", None)
        event = tracker.begin("tts", "elevenlabs", cfg.elevenlabs_model_id,
                              {"characters": len(phrase)}, "submitted_text") if tracker else None
        outcome, request_id = "uncertain", None
        try:
            # output_format belongs in the query, not in the JSON payload.
            async with self._client.stream(
                "POST", f"text-to-speech/{quote(cfg.elevenlabs_voice_id, safe='')}/stream",
                params={"output_format": f"pcm_{cfg.elevenlabs_tts_sample_rate}"},
                json=payload, headers={"Accept": "application/octet-stream"},
            ) as response:
                if response.status_code in {400, 401, 403, 404, 422, 429}:
                    outcome = "rejected"
                response.raise_for_status()
                request_id = response.headers.get("request-id") or response.headers.get("x-request-id")
                pending = b""
                async for chunk in response.aiter_bytes():
                    if self._cancelled:
                        return
                    pending += chunk
                    even = len(pending) // 2 * 2
                    if even:
                        yield pending[:even]
                        pending = pending[even:]
                if pending:
                    raise RuntimeError("TTS returned an incomplete PCM sample")
                if not self._cancelled:
                    outcome = "completed"
                    self._previous_text = (self._previous_text + " " + phrase).strip()[-1000:]
        finally:
            if tracker:
                tracker.finish(event, outcome, request_id=request_id)
            self._synthesizing = False

    async def synthesize_stream(self, phrases, language="en"):
        async for phrase in phrases:
            if self._cancelled:
                break
            async for chunk in self.synthesize(phrase, language):
                yield chunk
