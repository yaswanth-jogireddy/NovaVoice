"""Smallest AI (Waves, Lightning v3.1) speech streaming over SSE: raw PCM, cancellation, send-ahead.

POST https://api.smallest.ai/waves/v1/tts/live with Accept: text/event-stream. Each frame is
`data: {"audio": "<base64 PCM>", "done": false, ...}`; the last has "done": true (docs.smallest.ai,
models/text-to-speech/streaming, checked 31 Oct 2026). The voice must speak the requested language: the
Indic voices (e.g. sunidhi, devansh) cover Hindi and Indian English; other Indian languages use the voice
in SMALLEST_LANGUAGE_VOICES.
"""
import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator

import httpx

from utils.speech_text import phone_numbers_for_speech

# Our language codes -> Smallest's (Odia is "or" there).
_LANGUAGE = {"od": "or", "hinglish": "hi"}


class SmallestTTSProcessor:
    def __init__(self, settings):
        self._settings = settings
        self._client = None
        self._cancelled = False
        self._synthesizing = False
        # Each phrase is its own request; the next one starts while this one plays (no gap between sentences).
        self._ahead = None  # (phrase, language, chunk queue, task)

    async def initialize(self):
        if not self._settings.smallest_api_key:
            raise ValueError('Smallest AI speech credentials are not configured (SMALLEST_API_KEY)')
        self._client = httpx.AsyncClient(
            headers={'Authorization': 'Bearer ' + self._settings.smallest_api_key,
                     'Accept': 'text/event-stream'},
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
            self._ahead[3].cancel()
            self._ahead = None

    def voice_for(self, language: str) -> str:
        """The call's voice for English and Hindi; the configured voice for another Indian language."""
        code = _LANGUAGE.get(language, language) or 'en'
        return self._settings.smallest_language_voices.get(code) or self._settings.smallest_voice_id

    async def send_ahead(self, phrase: str, language='en') -> bool:
        """Start the next phrase's request now; synthesize() of the same phrase then plays its buffered audio."""
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
        language = language or 'en'
        phrase = phone_numbers_for_speech(phrase, language)
        tracker = getattr(self, 'cost_tracker', None)
        event = tracker.begin('tts', 'smallest', cfg.smallest_model, {'characters': len(phrase)}, 'estimated') if tracker else None
        outcome, request_id, size = 'uncertain', None, 0
        try:
            async with self._client.stream('POST', cfg.smallest_tts_url, json={
                'text': phrase, 'voice_id': self.voice_for(language), 'model': cfg.smallest_model,
                'sample_rate': cfg.smallest_sample_rate, 'speed': cfg.smallest_speed,
                'language': _LANGUAGE.get(language, language), 'output_format': 'pcm',
            }) as response:
                if response.status_code in {400, 401, 403, 404, 422, 429}: outcome = 'rejected'
                if response.status_code >= 400:
                    detail = (await response.aread())[:200].decode('utf-8', 'replace')
                    raise RuntimeError(f'Smallest AI speech HTTP {response.status_code}: {detail}')
                request_id = response.headers.get('x-request-id')
                pending = b''
                async for line in response.aiter_lines():
                    if self._cancelled: return
                    if not line.startswith('data:'):
                        continue   # "event: audio", blank separators, comments
                    frame = json.loads(line[5:].strip() or '{}')
                    if frame.get('audio'):
                        pending += base64.b64decode(frame['audio'])
                        even = len(pending) // 2 * 2   # 16-bit samples: never split one
                        if even:
                            size += even
                            yield pending[:even]
                            pending = pending[even:]
                    if frame.get('done') is True:
                        break
                if pending: raise RuntimeError('Speech returned a partial PCM sample')
                if not size: raise RuntimeError('Speech provider returned empty audio')
                if not self._cancelled: outcome = 'completed'
        finally:
            if tracker:
                tracker.finish(event, outcome, request_id=request_id,
                               units={'characters': len(phrase), 'output_audio_seconds': size / (2 * cfg.smallest_sample_rate)})

    async def synthesize_stream(self, phrases, language='en'):
        async for phrase in phrases:
            if self._cancelled: break
            async for chunk in self.synthesize(phrase, language): yield chunk
