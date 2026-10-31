"""Cost-effective vs Standard: one call priced on both voice modes from its own usage ledger."""
from types import SimpleNamespace

import pytest

from conftest import FIXED_NOW
from csm import metrics, tiercost
from csm.orchestrator import Orchestrator, Services
from features import FeatureFlags

SARVAM = SimpleNamespace(tts_provider="sarvam", sarvam_tts_model="bulbul:v4-flash", elevenlabs_model_id="eleven_flash_v2_5")
ELEVEN = SimpleNamespace(**{**vars(SARVAM), "tts_provider": "elevenlabs"})


def snapshot(chars=1000, provider="sarvam", model="bulbul:v4-flash", tts_inr=2.0, seconds=120):
    return {"duration_seconds": seconds, "events": [
        {"component": "stt", "provider": "sarvam", "model": "saaras:v4", "units": {"audio_seconds": 60}, "estimated_inr": 0.5},
        {"component": "llm", "provider": "openai", "model": "gpt-6-luna", "units": {"input_tokens": 1000}, "estimated_inr": 0.1},
        {"component": "tts", "provider": provider, "model": model, "units": {"characters": chars}, "estimated_inr": tts_inr},
    ]}


def test_tier_of_and_models_follow_voices_json():
    assert tiercost.tier_of(SARVAM) == "cost" and tiercost.tier_of(ELEVEN) == "standard"
    assert tiercost.tts_models(SARVAM) == {"cost": ("sarvam", "bulbul:v4-flash"), "standard": ("elevenlabs", "eleven_flash_v2_5"),
                                           "smallest": ("smallest", "")}


def test_same_call_priced_on_both_modes():
    out = tiercost.compare(snapshot(), "cost", tiercost.tts_models(SARVAM), usd_to_inr=90)
    cost, std = out["tiers"]["cost"], out["tiers"]["standard"]
    assert cost["basis"] == "measured" and cost["inr"] == pytest.approx(2.6)            # 0.5 + 0.1 + measured 2.0
    # 1,000 characters at ElevenLabs Flash $0.05 per 1,000 x 90 = Rs 4.50, plus the same listening and model cost
    assert std["speaking_inr"] == pytest.approx(4.5) and std["inr"] == pytest.approx(5.1)
    assert cost["per_minute_inr"] == pytest.approx(1.3) and out["spoken_characters"] == 1000


def test_standard_call_keeps_its_measured_cost_and_prices_cost_effective():
    out = tiercost.compare(snapshot(provider="elevenlabs", model="eleven_flash_v2_5", tts_inr=4.5), "standard",
                           tiercost.tts_models(ELEVEN), usd_to_inr=90)
    assert out["tiers"]["standard"]["inr"] == pytest.approx(5.1)
    assert out["tiers"]["cost"]["speaking_inr"] == pytest.approx(2.0)                 # 1,000 chars x Rs 2 per 1,000


def test_text_call_costs_the_same_on_both_and_unknown_model_is_not_guessed():
    out = tiercost.compare(snapshot(chars=0, tts_inr=0), "cost", tiercost.tts_models(SARVAM), usd_to_inr=90)
    assert out["tiers"]["cost"]["inr"] == out["tiers"]["standard"]["inr"] == pytest.approx(0.6)
    out = tiercost.compare(snapshot(), "cost", {"cost": ("sarvam", "bulbul:v4-flash"), "standard": ("elevenlabs", "nope")}, 90)
    assert out["tiers"]["standard"]["inr"] is None


def test_supervisor_aggregates_voice_calls_only():
    calls = [{"voice": True, "tier": "cost", "minutes": 2, "cost_by_tier": {"cost": 2.6, "standard": 5.1}},
             {"voice": True, "tier": "standard", "minutes": 1, "cost_by_tier": {"cost": 1.4, "standard": 2.9}},
             {"voice": False, "tier": "cost", "minutes": 1, "cost_by_tier": {"cost": 0.1, "standard": 0.1}}]
    v = metrics.voice_tiers(calls, {"cost": "Cost-effective", "standard": "Standard"})
    assert v["voice_calls"] == 2 and v["monthly_calls"] == metrics.DEFAULT_MONTHLY_CALLS
    assert v["tiers"]["cost"] == {"label": "Cost-effective", "calls_on_this_tier": 1, "avg_per_call_inr": 2.0,
                                  "per_minute_inr": 1.333, "monthly_inr": 200000.0}
    assert v["tiers"]["standard"]["avg_per_call_inr"] == 4.0 and v["standard_vs_cost_pct"] == 100
    assert v["monthly_difference_inr"] == 200000.0
    assert metrics.voice_tiers([])["tiers"] == {}


def test_call_end_records_the_mode_and_both_prices(seeded_db):
    svc = Services(seeded_db, clock=lambda: FIXED_NOW)
    o = Orchestrator(svc, FeatureFlags({"MILESTONE": "3"}), "call-t", "en")
    tiers = tiercost.compare(snapshot(), "cost", tiercost.tts_models(SARVAM), usd_to_inr=90)
    o.end(2.6, tiers=tiers, voice=True)
    (ended,) = [e for e in svc.audit.events("call-t") if e["kind"] == "call_ended"]
    assert ended["tier"] == "cost" and ended["voice"] is True and ended["minutes"] == 2.0
    # Smallest AI has no rate in utils.costs.RATES yet, so it is shown unpriced rather than guessed.
    assert ended["cost_by_tier"] == {"cost": 2.6, "standard": 5.1, "smallest": None}
    assert metrics.summarize(svc.audit.events("call-t"))["voice_tiers"]["voice_calls"] == 0   # no caller turns yet
    svc.close()
