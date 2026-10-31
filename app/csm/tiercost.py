"""One call priced on both voice tiers (Cost-effective vs Standard), from the call's own usage ledger.

The tiers differ only in the speaking voice: Sarvam or ElevenLabs. Listening (Sarvam) and the language
model are the same on both. So "the same call on the other tier" is the same spoken characters at the
other provider's rate, plus the same listening and model cost; no per-minute guess is involved. The tier
the call actually used keeps its measured speaking cost. Rates come from utils.costs.RATES (dated);
estimates, not invoices, and excluding tax.
"""
from __future__ import annotations

from csm import voices
from utils.costs import RATE_VERSION, unit_rates

COMPONENTS = ("stt", "tts", "llm")


def tier_of(settings) -> str:
    """The voices.json tier whose speaking provider this call's settings use."""
    for key, tier in voices.config()["tiers"].items():
        if tier["tts_provider"] == settings.tts_provider:
            return key
    return voices.config()["default_tier"]


def tts_models(settings) -> dict:
    """Speaking (provider, model) per tier, as this server is configured."""
    models = {"sarvam": settings.sarvam_tts_model, "elevenlabs": settings.elevenlabs_model_id,
              "smallest": getattr(settings, "smallest_model", "")}
    return {key: (tier["tts_provider"], models.get(tier["tts_provider"], ""))
            for key, tier in voices.config()["tiers"].items()}


def compare(snapshot: dict, actual_tier: str, models: dict, usd_to_inr: float) -> dict:
    """Cost of this call on every tier. `snapshot` is CostTracker.snapshot()."""
    parts = dict.fromkeys(COMPONENTS + ("other",), 0.0)
    chars = 0
    for event in snapshot.get("events", []):
        component = event.get("component") if event.get("component") in COMPONENTS else "other"
        parts[component] += float(event.get("estimated_inr") or 0)
        if component == "tts":
            chars += int(event.get("units", {}).get("characters", 0) or 0)
    shared = parts["stt"] + parts["llm"] + parts["other"]          # the same on every tier
    minutes = max(float(snapshot.get("duration_seconds") or 0), 1.0) / 60
    tiers = {}
    for key, (provider, model) in models.items():
        if key == actual_tier:
            speaking, basis = parts["tts"], "measured"
        else:
            rate = unit_rates(provider, model, {"characters": chars})
            if not rate or "characters" not in rate[1]:
                tiers[key] = {"inr": None, "basis": f"no rate for {provider} {model}"}
                continue
            currency, prices = rate
            speaking = chars * prices["characters"] * (usd_to_inr if currency == "USD" else 1)
            basis = "same characters at this tier's rate"
        total = shared + speaking
        tiers[key] = {"inr": round(total, 4), "speaking_inr": round(speaking, 4), "per_minute_inr": round(total / minutes, 3),
                      "provider": provider, "model": model, "basis": basis}
    return {"actual_tier": actual_tier, "spoken_characters": chars, "duration_s": round(minutes * 60, 1),
            "parts_inr": {k: round(v, 4) for k, v in parts.items()}, "tiers": tiers,
            "rate_version": RATE_VERSION, "is_estimate": True}
