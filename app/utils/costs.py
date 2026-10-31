"""Local usage accounting. Estimates are not invoices; never store conversation text."""
import json
import logging
import math
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

RATE_VERSION = "2026-10-09-luna-fast"
# Native currency per unit. Pin rates to models, never guess a changed model's price.
RATES = {
    ("sarvam", "saaras:v3"): ("INR", {"audio_seconds": 30 / 3600}),
    # Official pricing page (2026-10-09): speech to text ₹30/hour, not split by model.
    ("sarvam", "saaras:v4"): ("INR", {"audio_seconds": 30 / 3600}),
    ("sarvam", "bulbul:v3"): ("INR", {"characters": 30 / 10000}),
    # Not on the official pricing page (2026-10-09); a secondary source reports the
    # v3 rate. Priced so budgeted calls admit it; confirm with Sarvam before relying on it.
    ("sarvam", "bulbul:v4"): ("INR", {"characters": 30 / 10000}),
    # Sarvam dashboard model card shown by the owner 2026-10-10: ₹2 per 1K characters
    # for REST, HTTP and WebSocket streaming.
    ("sarvam", "bulbul:v4-flash"): ("INR", {"characters": 2 / 1000}),
    ("groq", "openai/gpt-oss-120b"): ("USD", {"input_tokens": .15e-6, "cached_tokens": .075e-6, "output_tokens": .60e-6}),
    ("openai", "gpt-4o-mini"): ("USD", {"input_tokens": .15e-6, "cached_tokens": .075e-6, "output_tokens": .60e-6}),
    # Standard tier, verified against the official model page 2026-10-09.
    ("openai", "gpt-6-luna"): ("USD", {"input_tokens": .10e-6, "cached_tokens": .01e-6,
                                       "cache_write_tokens": .125e-6, "output_tokens": .50e-6}),
    ("openai", "gpt-4o-mini-2024-07-18"): ("USD", {"input_tokens": .15e-6, "cached_tokens": .075e-6, "output_tokens": .60e-6}),
    ("openai", "text-embedding-3-small"): ("USD", {"input_tokens": .02e-6}),
    ("elevenlabs", "scribe_v2_realtime"): ("USD", {"audio_seconds": .39 / 3600}),
    ("elevenlabs", "eleven_flash_v2_5"): ("USD", {"characters": .05 / 1000}),
    ("elevenlabs", "eleven_turbo_v2_5"): ("USD", {"characters": .05 / 1000}),
    ("elevenlabs", "eleven_multilingual_v2"): ("USD", {"characters": .10 / 1000}),
}


def value(obj, key, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def token_units(usage):
    if usage is None:
        return None
    prompt = value(usage, "prompt_tokens")
    if prompt is None:
        return None
    cached = min(prompt, value(value(usage, "prompt_tokens_details", {}), "cached_tokens", 0) or 0)
    writes = min(prompt - cached, value(value(usage, 'prompt_tokens_details', {}), 'cache_write_tokens', 0) or 0)
    units = {"input_tokens": prompt - cached - writes, "cached_tokens": cached,
             "output_tokens": value(usage, "completion_tokens", 0) or 0}
    if writes:
        units['cache_write_tokens'] = writes
    return units


def unit_rates(provider, model, units, service_tier=None):
    rate = RATES.get((provider, model))
    if rate and (provider, model) == ('openai', 'gpt-6-luna'):
        prompt = sum(units.get(k, 0) for k in ('input_tokens', 'cached_tokens', 'cache_write_tokens'))
        if prompt > 272000:
            rate = rate[0], {k: v * (1.5 if k == 'output_tokens' else 2) for k, v in rate[1].items()}
    if rate and provider == 'openai' and service_tier in FAST_TIERS:
        # OpenAI Fast mode (requested "priority" or "fast"): 2x the applicable
        # rates, per the official gpt-6-luna model page checked 2026-10-09.
        rate = rate[0], {k: v * 2 for k, v in rate[1].items()}
    return rate


FAST_TIERS = {'priority', 'fast'}


def approximate_tokens(text):
    # Heuristic only, especially uncertain for Indic text and hidden reasoning.
    return math.ceil(len(text.encode("utf-8")) / 4)


class ProviderBudgetExceeded(RuntimeError):
    """Estimated provider allowance cannot safely admit another request."""


class CostTracker:
    def __init__(self, settings, mode="studio", session_id=None):
        self.settings = settings
        self.synthetic = settings.cost_synthetic
        self.session_id = session_id or settings.managed_call_id or uuid.uuid4().hex
        self.mode = mode
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.started = time.monotonic()
        self.ended = None
        self.events = {}
        self._audio = {}
        self._last_flush = self.started
        self.persistence_error = False
        self.path = Path(settings.cost_log_dir) / (self.session_id + ".jsonl")
        self._writer = None
        self.budget_ceiling = settings.browser_session_budget_inr if mode.startswith('studio') else 0
        self.budget_reserved = 0.0
        if settings.cost_tracking_enabled and settings.cost_async_write:
            from utils.usage_writer import UsageWriter
            self._writer = UsageWriter(self.path)
        self._write({"type": "session", "session_id": self.session_id, "mode": mode,
                     "synthetic": self.synthetic,
                     "started_at": self.started_at, "rate_version": RATE_VERSION,
                     "usd_to_inr": settings.cost_usd_to_inr})

    def _write(self, data):
        if not self.settings.cost_tracking_enabled:
            return
        if self._writer:
            try:
                self._writer.append(json.dumps(data, ensure_ascii=True) + "\n")
            except RuntimeError:
                self.persistence_error = True
                raise
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as out:
                out.write(json.dumps(data, ensure_ascii=True) + "\n")
        except OSError:
            self.persistence_error = True
            logging.getLogger(__name__).warning("Cost ledger write failed; in-memory usage remains available")

    def reserve_request(self, provider, model, units, service_tier=None):
        if self.synthetic or not self.budget_ceiling: return
        rate = unit_rates(provider, model, units, service_tier)
        if not rate or rate[0] not in {'INR', 'USD'} or any(unit not in rate[1] for unit in units):
            raise ProviderBudgetExceeded('Unpriced provider request denied')
        if any(not math.isfinite(float(n)) or float(n) < 0 for n in units.values()):
            raise ProviderBudgetExceeded('Invalid provider request units')
        currency, prices = rate
        # Never rely on an undocumented cached-input discount for admission.
        input_ceiling = max(prices.get('input_tokens', 0), prices.get('cache_write_tokens', 0))
        native = sum(float(n) * (input_ceiling if unit in {'input_tokens', 'cached_tokens'} else prices[unit])
                     for unit, n in units.items())
        amount = native * (self.settings.cost_usd_to_inr if currency == 'USD' else 1)
        if not math.isfinite(amount) or amount < 0 or self.budget_reserved + amount > self.budget_ceiling:
            raise ProviderBudgetExceeded('Provider session allowance reached')
        # Retain the full request reservation on errors/cancellation/unknown use.
        self.budget_reserved += amount

    def begin(self, component, provider, model, units, basis="estimated", budget_units=None, **metadata):
        self.reserve_request(provider, model, units if budget_units is None else budget_units,
                             metadata.get('service_tier'))
        event_id = uuid.uuid4().hex
        if self.synthetic: basis = 'synthetic'
        self.events[event_id] = dict(event_id=event_id, component=component, provider=provider,
                                    synthetic=self.synthetic,
                                    model=model, units=units, basis=basis, status="pending", **metadata)
        self._persist_event(event_id)
        return event_id

    def _persist_event(self, event_id):
        event = self.events[event_id]
        rate = unit_rates(event['provider'], event['model'], event['units'], event.get('service_tier'))
        self._write({"type": "usage", "session_id": self.session_id, "at": datetime.now(timezone.utc).isoformat(),
                     "rate_currency": rate[0] if rate and not self.synthetic else None,
                     "unit_rates": {} if self.synthetic else rate[1] if rate else None, **event})

    def finish(self, event_id, status="completed", units=None, basis=None, request_id=None, service_tier=None):
        event = self.events[event_id]
        event["status"] = status
        if service_tier:
            event["service_tier"] = service_tier  # tier the provider reports it actually used
        if units is not None:
            event["units"] = units
        if basis and not self.synthetic:
            event["basis"] = basis
        if request_id:
            event["request_id"] = request_id
        self._persist_event(event_id)

    def audio(self, provider, model, byte_count, sample_rate):
        key = (provider, model)
        if key not in self._audio:
            self._audio[key] = self.begin("stt", provider, model, {"audio_seconds": 0}, "sent_pcm")
        self.events[self._audio[key]]["units"]["audio_seconds"] += byte_count / (sample_rate * 2)
        if time.monotonic() - self._last_flush >= 5:
            self.flush_audio()

    def flush_audio(self):
        for event_id in self._audio.values():
            self.finish(event_id, "submitted")
        self._last_flush = time.monotonic()

    def snapshot(self):
        if self._writer and self._writer.failed.is_set():
            self.persistence_error = True
        rows, total, unpriced, uncertain = [], 0.0, 0, 0
        for event in self.events.values():
            rate = unit_rates(event['provider'], event['model'], event['units'], event.get('service_tier'))
            currency, prices = rate if rate else (None, {})
            if self.synthetic: currency, prices = None, {}
            native = sum(n * prices.get(unit, 0) for unit, n in event["units"].items()) if rate else None
            if self.synthetic or event["status"] == "rejected":
                native = 0.0
            cost = native * (self.settings.cost_usd_to_inr if currency == "USD" else 1) if native is not None else None
            total += cost or 0
            unpriced += cost is None
            uncertain += not self.synthetic and (event["basis"] == "estimated" or event["status"] in {"pending", "uncertain"})
            rows.append({**event, "units": dict(event["units"]), "currency": currency,
                         "unit_rates": dict(prices), "native_cost": native, "estimated_inr": cost})
        return {"session_id": self.session_id, "mode": self.mode, "started_at": self.started_at,
                "synthetic": self.synthetic,
                "duration_seconds": round((self.ended or time.monotonic()) - self.started, 2),
                "rate_version": RATE_VERSION, "usd_to_inr": self.settings.cost_usd_to_inr,
                "estimated_inr": round(total, 6), "unpriced_events": unpriced,
                "uncertain_events": uncertain, "persistence_error": self.persistence_error,
                "is_invoice": False, "events": rows,
                "budget_ceiling_inr": self.budget_ceiling, "budget_reserved_inr": round(self.budget_reserved, 6),
                "excluded": (["taxes", "credits/discounts", "hosting", "subscriptions"] +
                             (["LiveKit", "telephony", "external orchestrator", "async safety audits"] if self.mode == "livekit" else []))}

    def close(self):
        if self.ended is None:
            self.flush_audio()
            self.ended = time.monotonic()
            self._write({"type": "summary", **self.snapshot()})
        if self._writer and not self._writer.close():
            self.persistence_error = True
        return self.snapshot()


def attach_tracker(tracker, *processors):
    for processor in processors:
        if processor is not None:
            processor.cost_tracker = tracker
