"""OpenAI speech streaming: native 24 kHz PCM, cancellation, no provider fallback."""
import asyncio
from collections.abc import AsyncIterator
import contextlib
import httpx
from utils.speech_text import phone_numbers_for_speech

US_DELIVERY = (
    'Speak with a clear, natural General American accent. Be warm, attentive and '
    'professional, like a helpful receptionist. Use conversational pacing, subtle '
    'intonation and short natural pauses. Avoid a sales-pitch or theatrical delivery. '
    'Read the supplied text faithfully; add no words. Pronounce names carefully and '
    'read phone digits distinctly. For explicitly requested non-English speech, '
    'use natural pronunciation in that language rather than an American caricature.'
)


class OpenAITTSProcessor:
    def __init__(self, settings):
        self._settings = settings
        self._client = None
        self._cancelled = False
        self._synthesizing = False
        # Each phrase is its own HTTP request with ~1 s before first audio. The next
        # phrase's request starts while this one plays, so sentences follow with no gap.
        self._ahead = None  # (phrase, language, chunk queue, task)

    async def initialize(self):
        if not self._settings.openai_api_key:
            raise ValueError('OpenAI speech credentials are not configured')
        self._client = httpx.AsyncClient(
            base_url=self._settings.openai_base_url.rstrip('/')+'/',
            headers={'Authorization':'Bearer '+self._settings.openai_api_key},
            timeout=httpx.Timeout(20, connect=5))

    async def close(self):
        self.cancel()
        if self._client:
            await self._client.aclose()
            self._client = None

    def cancel(self):
        self._cancelled = True
        self._drop_ahead()

    def reset(self): self._cancelled = False
    @property
    def is_synthesizing(self): return self._synthesizing

    def _drop_ahead(self):
        if self._ahead:
            self._ahead[3].cancel()  # the request's own cost row records it as uncertain
            self._ahead = None

    async def send_ahead(self, phrase: str, language='en') -> bool:
        """Start the next phrase's request now; synthesize() of the same phrase then
        plays its buffered audio. False when not possible; synthesize() then works as usual."""
        if (not getattr(self._settings, 'tts_send_ahead', True) or self._cancelled
                or self._client is None or self._ahead or not phrase.strip()):
            return False
        chunks = asyncio.Queue()

        async def fetch():
            try:
                async for chunk in self._request(phrase, language):
                    chunks.put_nowait(chunk)
                chunks.put_nowait(None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                chunks.put_nowait(exc)
        self._ahead = (phrase, language, chunks, asyncio.create_task(fetch()))
        return True

    async def synthesize(self, phrase: str, language='en') -> AsyncIterator[bytes]:
        if self._cancelled or not phrase.strip(): return
        if self._client is None: raise RuntimeError('Speech has not been initialized')
        ahead, self._ahead = self._ahead, None
        if ahead and (ahead[0], ahead[1]) != (phrase, language):
            ahead[3].cancel()  # a different phrase came next; never play stale audio
            ahead = None
        self._synthesizing = True
        if not ahead:
            try:
                async for chunk in self._request(phrase, language):
                    yield chunk
            finally:
                self._synthesizing = False
            return
        chunks, task = ahead[2], ahead[3]
        try:
            while True:
                item = await chunks.get()
                if item is None: return
                if isinstance(item, Exception): raise item
                if self._cancelled: return
                yield item
        finally:
            self._synthesizing = False
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _request(self, phrase, language):
        if self._cancelled: return
        cfg = self._settings
        phrase = phone_numbers_for_speech(phrase, language)
        tracker = getattr(self, 'cost_tracker', None)
        # PCM streaming returns no token usage. Keep the request explicitly
        # unpriced, rather than pretending a character rate is a token invoice.
        event = tracker.begin('tts', 'openai', cfg.openai_tts_model,
                              {'characters':len(phrase)}, 'estimated') if tracker else None
        outcome, request_id, size = 'uncertain', None, 0
        try:
            async with self._client.stream('POST', 'audio/speech', json={
                'model':cfg.openai_tts_model, 'voice':cfg.openai_tts_voice,
                'input':phrase, 'instructions':US_DELIVERY,
                'response_format':'pcm', 'speed':1.0,
            }) as response:
                if response.status_code in {400,401,403,404,422,429}: outcome = 'rejected'
                response.raise_for_status()
                request_id = response.headers.get('x-request-id')
                pending = b''
                async for chunk in response.aiter_bytes():
                    if self._cancelled: return
                    pending += chunk
                    even = len(pending)//2*2
                    if even:
                        size += even
                        yield pending[:even]
                        pending = pending[even:]
                if pending: raise RuntimeError('Speech returned a partial PCM sample')
                if not size: raise RuntimeError('Speech provider returned empty audio')
                if not self._cancelled: outcome = 'completed'
        finally:
            if tracker:
                tracker.finish(event, outcome, units={'characters':len(phrase),'output_audio_seconds':size/48000},
                               request_id=request_id)

    async def synthesize_stream(self, phrases, language='en'):
        async for phrase in phrases:
            if self._cancelled: break
            async for chunk in self.synthesize(phrase, language): yield chunk
