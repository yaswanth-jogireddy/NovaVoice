"""Supervisor KPIs computed from the audit trail (HLD section 1). Pure maths over event dicts.

Containment = calls that ended without a human / calls where the caller said something.
The human cost per call is an ASSUMPTION (env HUMAN_CALL_COST_INR), shown as such on the dashboard.
"""
from __future__ import annotations

import os
from collections import Counter

DEFAULT_HUMAN_CALL_COST_INR = 45.0   # assumption: loaded L1 agent cost / calls handled; stated on the slide
DEFAULT_MONTHLY_CALLS = 100_000      # assumption for the monthly projection; env MONTHLY_CALLS


def human_call_cost() -> float:
    try:
        return float(os.environ.get("HUMAN_CALL_COST_INR", DEFAULT_HUMAN_CALL_COST_INR))
    except ValueError:
        return DEFAULT_HUMAN_CALL_COST_INR


def monthly_calls() -> int:
    try:
        return max(int(os.environ.get("MONTHLY_CALLS", DEFAULT_MONTHLY_CALLS)), 1)
    except ValueError:
        return DEFAULT_MONTHLY_CALLS


def voice_tiers(engaged: list[dict], labels: dict | None = None) -> dict:
    """Cost-effective vs Standard over the same voice calls: each call is priced on every tier from its own
    usage (csm/tiercost.py), so the comparison is like for like. Text calls have no speech cost; left out."""
    calls = [e for e in engaged if e.get("voice") and e.get("cost_by_tier")]
    volume = monthly_calls()
    minutes = sum(float(e.get("minutes") or 0) for e in calls)
    keys = list(dict.fromkeys(k for e in calls for k in e["cost_by_tier"]))
    tiers = {}
    for key in keys:
        priced = [e["cost_by_tier"].get(key) for e in calls]
        if not priced or any(p is None for p in priced):
            continue
        avg = sum(priced) / len(priced)
        tiers[key] = {"label": (labels or {}).get(key, key), "calls_on_this_tier": sum(e.get("tier") == key for e in calls),
                      "avg_per_call_inr": round(avg, 3), "per_minute_inr": round(sum(priced) / minutes, 3) if minutes else None,
                      "monthly_inr": round(avg * volume, 0)}
    out = {"voice_calls": len(calls), "monthly_calls": volume, "monthly_calls_is_assumption": True, "tiers": tiers}
    if "cost" in tiers and "standard" in tiers and tiers["cost"]["avg_per_call_inr"]:
        out["standard_vs_cost_pct"] = round((tiers["standard"]["avg_per_call_inr"] / tiers["cost"]["avg_per_call_inr"] - 1) * 100)
        out["monthly_difference_inr"] = round(tiers["standard"]["monthly_inr"] - tiers["cost"]["monthly_inr"], 0)
    return out


def summarize(events: list[dict], human_cost_inr: float | None = None, tier_labels: dict | None = None) -> dict:
    human_cost_inr = human_call_cost() if human_cost_inr is None else human_cost_inr
    ended = [e for e in events if e["kind"] == "call_ended"]
    engaged = [e for e in ended if e.get("caller_turns", 0) > 0]
    contained = [e for e in engaged if e.get("outcome") == "contained"]
    handed = [e for e in engaged if e.get("outcome") == "handoff"]
    n = len(engaged)
    ai_cost = [float(e.get("cost_inr") or 0) for e in engaged]
    aht = [float(e.get("aht_s") or 0) for e in engaged]
    results = [e for e in events if e["kind"] == "tool_result"]
    avg_ai = sum(ai_cost) / n if n else 0.0
    return {
        "calls": n,
        "contained": len(contained),
        "handed_off": len(handed),
        "containment_rate": round(len(contained) / n, 3) if n else None,
        "aht_s": round(sum(aht) / n, 1) if n else None,
        "ai_cost_per_call_inr": round(avg_ai, 2),
        "human_cost_per_call_inr": human_cost_inr,
        "human_cost_is_assumption": True,
        "saving_inr": round(len(contained) * (human_cost_inr - avg_ai), 2),
        "dispositions": dict(Counter(e["disposition_label"] for e in engaged if e.get("disposition_label"))),
        # One call can carry several requests: each counts with its own result.
        "intent_results": dict(Counter(i["resolution_label"] for e in engaged for i in e.get("intents") or [])),
        "intents_by_type": dict(Counter(i["intent_label"] for e in engaged for i in e.get("intents") or [])),
        "escalation_reasons": dict(Counter(e.get("reason", "unknown") for e in events if e["kind"] == "handoff")),
        "guardrail_events": dict(Counter(e.get("guard", "unknown") for e in events if e["kind"] == "guardrail_hit")),
        "policy_decisions": dict(Counter(e.get("decision") for e in events if e["kind"] == "policy_decision")),
        "tool_calls": len(results),
        "tool_success_rate": round(sum(bool(e.get("ok")) for e in results) / len(results), 3) if results else None,
        "tools_used": dict(Counter(e.get("tool") for e in results if e.get("ok"))),
        "trust": trust_pillars(events),
        "voice_tiers": voice_tiers(engaged, tier_labels),
        "recent_calls": [{k: e.get(k) for k in ("at", "call_id", "outcome", "reason", "aht_s", "cost_inr", "identity", "tickets",
                                                "disposition", "disposition_label", "customer_query", "disposition_source",
                                                "intents", "disposition_note")}
                         for e in reversed(engaged[-15:])],
    }


def trust_pillars(events: list[dict]) -> dict:
    """Live evidence per Azentio "Trust by Design" pillar (their own six headings)."""
    results = [e for e in events if e["kind"] == "tool_result"]
    decisions = Counter(e.get("decision") for e in events if e["kind"] == "policy_decision")
    guards = Counter(e.get("guard") for e in events if e["kind"] == "guardrail_hit")
    return {
        "Identity & SSO": sum(1 for e in results if e.get("tool") in ("verify_identity", "check_otp") and e.get("ok")),
        "Role-Based Access": sum(decisions[d] for d in ("NEED_VERIFICATION", "STEP_UP", "DENY", "OFFER_HANDOFF")),
        "Auditability": len(events),
        "Data Protection": sum(guards[g] for g in ("full_number_masked", "otp_spoken", "unbacked_claim", "unsupported_figure",
                                                    "credit_claim", "caller_secret")),
        "Human Oversight": sum(1 for e in events if e["kind"] == "handoff"),
        "Secure Deployment": sum(guards[g] for g in ("injection", "secret_request", "third_party")),
    }
