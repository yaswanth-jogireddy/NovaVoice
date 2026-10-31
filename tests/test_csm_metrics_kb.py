from csm.knowledge import KnowledgeBase
from csm.metrics import summarize


def ended(outcome, turns=2, cost=0.5, aht=60, reason=None):
    return {"kind": "call_ended", "outcome": outcome, "caller_turns": turns, "cost_inr": cost, "aht_s": aht, "reason": reason}


def test_containment_cost_and_saving():
    events = [ended("contained"), ended("contained"), ended("contained"), ended("handoff", reason="caller_request"),
              ended("contained", turns=0),                       # abandoned before speaking: not counted
              {"kind": "handoff", "reason": "caller_request"},
              {"kind": "guardrail_hit", "guard": "injection"}, {"kind": "guardrail_hit", "guard": "injection"},
              {"kind": "tool_result", "tool": "get_balance", "ok": True},
              {"kind": "tool_result", "tool": "block_card", "ok": False}]
    m = summarize(events, human_cost_inr=45.0)
    assert m["calls"] == 4 and m["contained"] == 3 and m["handed_off"] == 1
    assert m["containment_rate"] == 0.75
    assert m["ai_cost_per_call_inr"] == 0.5
    assert m["saving_inr"] == round(3 * (45.0 - 0.5), 2)
    assert m["escalation_reasons"] == {"caller_request": 1}
    assert m["guardrail_events"] == {"injection": 2}
    assert m["tool_success_rate"] == 0.5 and m["tools_used"] == {"get_balance": 1}
    assert m["human_cost_is_assumption"] is True


def test_metrics_empty():
    m = summarize([], human_cost_inr=45.0)
    assert m["calls"] == 0 and m["containment_rate"] is None and m["saving_inr"] == 0


def test_kb_finds_the_right_policy():
    kb = KnowledgeBase()
    assert len(kb.docs) == 31
    assert kb.search("lost my debit card")[0]["doc_id"] == "KB-CARD-01"
    assert kb.search("minimum balance charges")[0]["doc_id"] == "KB-CHG-01"
    assert kb.search("how long does a dispute take")[0]["doc_id"].startswith("KB-DISPUTE")
    assert kb.search("zebra quantum banana") == []
