import asyncio
import inspect
import contextlib
import random
import re
import time
from collections.abc import AsyncIterator
from typing import Optional

import groq
import openai

from config import Settings
from utils.costs import approximate_tokens, token_units, value
from utils.logger import get_logger

logger = get_logger("llm")

# Phrase boundary characters — TTS receives complete phrases, not single tokens
_PHRASE_BOUNDARIES = frozenset(".?!,;:")


async def _await_quietly(awaitable) -> None:
    """Await a close-coroutine, swallowing the errors a torn-down stream throws."""
    try:
        await awaitable
    except Exception:
        pass


PREWARM_TOKENS = 200


class LLMProcessor:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

        # Both clients share the configured primary/fallback order below.
        self._groq_client: Optional[groq.AsyncGroq] = None
        if settings.groq_api_key:
            self._groq_client = groq.AsyncGroq(api_key=settings.groq_api_key, max_retries=0, timeout=12)

        self._openai_client: Optional[openai.AsyncOpenAI] = None
        if settings.openai_api_key:
            self._openai_client = openai.AsyncOpenAI(api_key=settings.openai_api_key, max_retries=0, timeout=12,
                                                   base_url=settings.openai_base_url)

        # Phrase boundary buffering
        self._phrase_buffer_max_ms = settings.llm_phrase_buffer_max_ms

        # Fallback/backoff state
        self._using_fallback = False
        self._primary_retry_count = 0
        self._max_retries = settings.llm_backoff_max_retries
        self._backoff_base_s = settings.llm_backoff_base_s
        self._backoff_max_s = settings.llm_backoff_max_s
        self._primary_available_at: float = 0.0

        # Active stream reference for cancellation
        self._active_stream = None
        self._cancelled = False
        self._response_format = None
        # Per-session override of groq_temperature (None = configured value).
        self.temperature = None
        # Per-session: release the reply's first clause at a comma once it has
        # this many characters, so speech starts before the sentence is written.
        self.first_clause_chars = 0
        # Strong refs to in-flight stream-close tasks (asyncio only holds weak ones)
        self._close_tasks: set[asyncio.Task] = set()

    async def initialize(self) -> None:
        # Existing LiveKit warm-up uses the same routing/parameters as generation.
        # Browser DemoSession does not call this paid warm-up.
        for provider in self._provider_order():
            client = getattr(self, f'_{provider}_client')
            if client is None:
                continue
            model = self._settings.groq_model if provider == 'groq' else self._settings.effective_openai_model
            try:
                # Reuse metering (including budget checks) and stream cleanup.
                async for _ in self._metered_stream(self._provider_label(provider), client, model, 1,
                                                     [{"role": "user", "content": "hi"}], prewarm=True):
                    pass
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                from utils.costs import ProviderBudgetExceeded
                if isinstance(exc, ProviderBudgetExceeded): raise
                logger.warning('llm_prewarm_failed', provider=provider, code=type(exc).__name__)

    async def prewarm_prompt(self, system: str) -> None:
        """Write the static system prompt into the provider's prompt cache.

        Measured on live Luna calls: turns that missed the cache rewrote ~7k tokens
        (1.8-5 s per call); turns that read it took 1.1-1.4 s. The first reply
        always missed. A one-token request with the system message alone, sent
        while the fixed greeting plays, leaves an entry ending exactly at that
        message, which every later request in the call starts with.
        """
        for index, provider in enumerate(self._provider_order()):
            client = getattr(self, f'_{provider}_client')
            if client is None or (index == 0 and self._using_fallback):
                continue
            model = self._settings.groq_model if provider == 'groq' else self._settings.effective_openai_model
            # F2: Luna rejects a one-token limit ("could not finish", HTTP 400);
            # 200 lets it finish (about 40 tokens observed) so the cache is written.
            async for _ in self._metered_stream(self._provider_label(provider), client, model, PREWARM_TOKENS,
                                                 [{'role': 'system', 'content': system}], prewarm=True):
                pass
            return

    def _provider_order(self):
        primary = self._settings.llm_primary_provider
        return (primary, 'groq' if primary == 'openai' else 'openai')

    def _provider_label(self, provider):
        if provider == 'openai' and self._settings.openai_base_url.rstrip('/') != 'https://api.openai.com/v1':
            return 'openai-compatible'
        return provider

    def cancel(self) -> None:
        """Stop generating. Safe to call from a sync context (state-machine callback).

        `AsyncStream.close()` is a coroutine: calling it without awaiting produced
        `RuntimeWarning: coroutine 'AsyncStream.close' was never awaited` and closed
        nothing, so every barge-in left the upstream generation running to completion
        — billed tokens we discard, plus a connection never returned to the pool.
        We schedule the close and hold the task reference so it is not garbage
        collected mid-flight.
        """
        self._cancelled = True
        stream, self._active_stream = self._active_stream, None
        if stream is None:
            return

        closer = getattr(stream, "aclose", None) or getattr(stream, "close", None)
        if closer is None:
            return
        try:
            result = closer()
        except Exception:
            return
        if inspect.isawaitable(result):
            try:
                task = asyncio.get_running_loop().create_task(_await_quietly(result))
                self._close_tasks.add(task)
                task.add_done_callback(self._close_tasks.discard)
            except RuntimeError:
                # No running loop (shutdown path) — close it synchronously.
                try:
                    asyncio.run(_await_quietly(result))
                except Exception:
                    pass

    def reset(self) -> None:
        self._cancelled = False
        self._active_stream = None

    async def close(self) -> None:
        self.cancel()
        if self._close_tasks:
            await asyncio.gather(*self._close_tasks, return_exceptions=True)
        for client in (self._groq_client, self._openai_client):
            if client is not None:
                await client.close()

    async def generate_json(self, messages) -> AsyncIterator[str]:
        """Use provider JSON mode only for callers expecting a structured turn.

        Share existing metering, fallback and cancellation. No retry/extra call;
        ordinary voice and summary callers keep their existing request format.
        """
        previous = self._response_format
        self._response_format = {"type": "json_object"}
        try:
            async with contextlib.aclosing(self.generate(messages)) as stream:
                async for part in stream:
                    yield part
        finally:
            self._response_format = previous

    async def generate(
        self,
        messages: list[dict[str, str]],
    ) -> AsyncIterator[str]:
        self._cancelled = False

        if self._using_fallback and time.monotonic() >= self._primary_available_at:
            self._using_fallback = False
            self._primary_retry_count = 0

        for index, provider in enumerate(self._provider_order()):
            if getattr(self, f'_{provider}_client') is None:
                continue
            if index == 0 and self._using_fallback:
                continue
            emitted = False
            try:
                async with contextlib.aclosing(getattr(self, f'_stream_{provider}')(messages)) as stream:
                    async for phrase in stream:
                        emitted = True
                        yield phrase
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                from utils.costs import ProviderBudgetExceeded
                if isinstance(exc, ProviderBudgetExceeded): raise
                if self._cancelled:
                    # We closed this stream ourselves (caller interrupted). Not a
                    # provider failure: counting it put Luna in cooldown and sent the
                    # next reply to the fallback (seen on a live call, 2026-10-10).
                    logger.debug('llm_stream_closed_by_cancel', provider=provider, code=type(exc).__name__)
                    return
                logger.warning('llm_stream_error', provider=provider, code=type(exc).__name__)
                if index == 0:
                    await self._handle_primary_failure()
                if emitted:
                    # Never replay speech already yielded on a different provider.
                    raise RuntimeError('LLM stream interrupted after partial response') from exc

        logger.error("llm_all_providers_failed")
        raise RuntimeError("No language model could complete the response")

    async def _stream_groq(
        self, messages: list[dict[str, str]]
    ) -> AsyncIterator[str]:
        async with contextlib.aclosing(self._metered_stream("groq", self._groq_client,
                self._settings.groq_model, self._settings.groq_max_tokens, messages)) as stream:
            async for phrase in stream:
                yield phrase

    async def _stream_openai(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        # A custom compatible endpoint must not inherit OpenAI's prices.
        provider = self._provider_label('openai')
        async with contextlib.aclosing(self._metered_stream(provider, self._openai_client,
                self._settings.effective_openai_model, self._settings.openai_max_tokens, messages)) as stream:
            async for phrase in stream:
                yield phrase

    async def _metered_stream(self, provider, client, model, max_tokens, messages, prewarm=False):
        tracker = getattr(self, "cost_tracker", None)
        tier = self._settings.openai_service_tier if provider == 'openai' else None
        estimate = {"input_tokens": approximate_tokens(str(messages)), "output_tokens": 0}
        event = tracker.begin("llm", provider, model, estimate.copy(), budget_units={
            'input_tokens': len(str(messages).encode('utf-8')) + 1024 + 64 * len(messages),
            'output_tokens': max_tokens}, **({'service_tier': tier} if tier else {})) if tracker else None
        usage, request_id, output, status, stream, served = None, None, "", "uncertain", None, None
        async def observed():
            nonlocal usage, request_id, output, served
            async for chunk in stream:
                request_id = getattr(chunk, "id", None) or request_id
                served = getattr(chunk, "service_tier", None) or served
                reported = getattr(chunk, "usage", None) or value(getattr(chunk, "x_groq", None), "usage")
                usage = token_units(reported) or usage
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    output += (getattr(delta, "content", None) or "") + (getattr(delta, "reasoning", None) or "")
                yield chunk
        try:
            kwargs = dict(model=model, messages=messages, max_tokens=max_tokens,
                          temperature=self._settings.groq_temperature if self.temperature is None else self.temperature,
                          stream=True)
            if self._response_format is not None:
                kwargs['response_format'] = self._response_format
            if provider != "groq":
                kwargs["stream_options"] = {"include_usage": True}
            if provider == 'groq' and 'gpt-oss' in model:
                kwargs['reasoning_effort'] = self._settings.groq_reasoning_effort
            if tier:
                kwargs['service_tier'] = tier
            if provider != 'groq' and (model == 'gpt-6-luna' or model.startswith('gpt-6-luna-')):
                kwargs['max_completion_tokens'] = kwargs.pop('max_tokens')
                kwargs['reasoning_effort'] = self._settings.openai_reasoning_effort
                if self._settings.openai_reasoning_effort != 'none':
                    kwargs.pop('temperature')
            stream = await client.chat.completions.create(**kwargs)
            if not prewarm:
                # A cache warm-up finishing late must never become the stream
                # that barge-in cancels in place of the caller's reply.
                self._active_stream = stream
            if prewarm:
                # Only establish the pool and meter usage. A one-token limit is
                # intentional here; it must not trigger incomplete-reply fallback.
                async for _ in observed():
                    pass
            else:
                async with contextlib.aclosing(self._extract_phrases(observed())) as phrases:
                    async for phrase in phrases:
                        yield phrase
            status = "completed" if prewarm or not self._cancelled else "uncertain"
        except BaseException as exc:
            if getattr(exc, "status_code", None) in {400, 401, 403, 404, 422, 429}:
                status = "rejected"
            raise
        finally:
            if tracker:
                estimate["output_tokens"] = approximate_tokens(output)
                tracker.finish(event, status, usage or estimate,
                               "provider_reported" if usage else "estimated", request_id,
                               service_tier=served if isinstance(served, str) else None)
            if stream:
                with contextlib.suppress(Exception):
                    await stream.close()
            if self._active_stream is stream:
                self._active_stream = None

    async def _extract_phrases(self, stream) -> AsyncIterator[str]:
        """Yield sentence boundaries, with a word-safe fallback for long clauses.

        Preserve model whitespace so concatenation reconstructs the token stream.
        Decimal punctuation never splits a phrase; timeout cuts occur only at
        whitespace after a useful amount of text has accumulated.
        """
        buffer = ""
        buffer_start = time.monotonic()
        released = False
        first_clause = self.first_clause_chars if self._response_format is None else 0

        try:
            async for chunk in stream:
                if self._cancelled:
                    return

                choice = chunk.choices[0] if chunk.choices else None
                if choice is not None and getattr(choice, "finish_reason", None) in {"length", "content_filter"}:
                    # Do not flush a dangling final fragment as a completed reply.
                    raise RuntimeError("Language model ended before completing the response")
                delta = choice.delta if choice is not None else None
                if delta is None or delta.content is None:
                    continue

                token = delta.content
                buffer += token

                buffer_age_ms = (time.monotonic() - buffer_start) * 1000
                # Wait for whitespace after punctuation: decimals and partial
                # words must never become separate TTS requests. Preserve bytes.
                # A provider chunk may contain several complete sentences.
                # Drain all of them now, not one per future network chunk.
                while True:
                    boundary = re.search(r"[.!?\u0964](?:[\"'”’])?\s+", buffer)
                    if not boundary:
                        break
                    cut = boundary.end()
                    released = True
                    yield buffer[:cut]
                    if self._cancelled:
                        return
                    buffer = buffer[cut:]
                    buffer_start = time.monotonic()
                cut = 0
                if not released and first_clause:
                    # Comma plus whitespace only: "₹2,00,00,000" and "2.07" never split.
                    clause = re.compile(r'[,،]\s+').search(buffer, first_clause)
                    if clause:
                        cut = clause.end()
                # Item 3: after the first phrase, later text is synthesised while
                # earlier audio plays; cutting it mid-sentence only adds a seam.
                long_enough = 400 if released and getattr(self._settings, 'tts_fewer_cuts', False) else 100
                if not cut and buffer_age_ms >= max(350, self._phrase_buffer_max_ms) and len(buffer) >= long_enough:
                    cut = buffer.rfind(" ") + 1
                if cut:
                    released = True
                    yield buffer[:cut]
                    buffer = buffer[cut:]
                    buffer_start = time.monotonic()

            # Flush remaining buffer
            if buffer.strip() and not self._cancelled:
                yield buffer

        except asyncio.CancelledError:
            logger.debug("llm_stream_cancelled")
            raise

    async def _handle_primary_failure(self) -> None:
        self._primary_retry_count += 1

        if self._primary_retry_count >= self._max_retries:
            # Schedule primary retry after backoff without delaying fallback.
            backoff = min(
                self._backoff_base_s * (2 ** (self._primary_retry_count - 1)),
                self._backoff_max_s,
            )
            # Add jitter: +/- 25%
            jitter = backoff * 0.25 * (2 * random.random() - 1)
            wait_time = backoff + jitter

            self._primary_available_at = time.monotonic() + wait_time
            self._using_fallback = True

            logger.warning(
                "llm_switching_to_fallback", primary=self._settings.llm_primary_provider,
                retry_count=self._primary_retry_count,
                backoff_s=round(wait_time, 2),
                primary_retry_at=round(self._primary_available_at, 2),
            )
        else:
            # Exponential backoff before retrying the primary
            backoff = min(
                self._backoff_base_s * (2 ** (self._primary_retry_count - 1)),
                self._backoff_max_s,
            )
            jitter = backoff * 0.25 * random.random()
            wait_time = backoff + jitter

            logger.warning(
                "llm_primary_cooldown", primary=self._settings.llm_primary_provider,
                retry_count=self._primary_retry_count,
                backoff_s=round(wait_time, 2),
            )
            # Cool down the primary in the background; never sleep before fallback.
            self._primary_available_at = time.monotonic() + wait_time
            self._using_fallback = True

    @property
    def is_using_fallback(self) -> bool:
        return self._using_fallback
