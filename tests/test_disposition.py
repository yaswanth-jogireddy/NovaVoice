"""Call disposition (csm/disposition.py): each request with its own result, labelled by Jev from the transcript,
or from call state when Jev is not configured or fails; the call's overall outcome derived from those."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from conftest import FIXED_NOW
from csm import disposition
from csm.disposition import INTENTS, Intent, Outcome, Resolution
from csm.orchestrator import Services
from test_csm_journeys import Call

PRIYA = "C1001, 14 March 1992, last four 7306"
JEV = SimpleNamespace(jev_api_key="k", jev_endpoint="https://jev.test/v1/systemone", jev_model="jev-1.13.0")
NO_JEV = SimpleNamespace(jev_api_key="", jev_endpoint="", jev_model="")
HISTORY = [{"role": "assistant", "content": "Hello, how can I help?"},
           {"role": "user", "content": "My balance please, account 1234567890. Also my EMI was debited twice."},
           {"role": "assistant", "content": "Your balance is 39,421 rupees. I've raised a ticket for your EMI issue."}]


@pytest.fixture()
def svc(seeded_db):
    s = Services(seeded_db, clock=lambda: FIXED_NOW)
    yield s
    s.close()


def jev_answers(*requests, mood="NEUTRAL"):
    """A Jev reply: one "intent__RESULT" label per request slot in order, NONE after, e.g. ("balance", "SOLVED")."""
    answers = {f"request_{n}": {"type": "choice", "confidence": 0.9,
                                "choice": f"{requests[n - 1][0]}__{requests[n - 1][1]}" if n <= len(requests) else "NONE"}
               for n in range(1, disposition.MAX_REQUESTS + 1)}
    answers["caller_mood"] = {"type": "choice", "choice": mood, "confidence": 0.8}
    return {"model": "jev-1.13.0", "answers": answers}


def fake_jev(monkeypatch, reply):
    sent = {}

    async def post(endpoint, key, body, timeout_s):
        sent.update(endpoint=endpoint, key=key, body=body)
        if isinstance(reply, Exception):
            raise reply
        return reply
    monkeypatch.setattr(disposition, "_post", post)
    return sent


def results(d):
    return [(i.intent, i.resolution) for i in d.intents]


def test_jev_labels_each_request_from_the_transcript_only(svc, monkeypatch):
    c = Call(svc, milestone=1)
    sent = fake_jev(monkeypatch, jev_answers(("balance", "SOLVED"), ("emi_issue", "TICKET_CREATED"), mood="POSITIVE"))
    d = asyncio.run(disposition.classify(HISTORY, c.o.state, JEV))
    assert d.source == "jev" and d.sentiment == "positive"
    assert results(d) == [(Intent.BALANCE, Resolution.SOLVED), (Intent.EMI_ISSUE, Resolution.TICKET)]
    assert d.outcome == Outcome.EMI_TICKET and d.resolved
    body = sent["body"]
    assert sent["endpoint"] == JEV.jev_endpoint and body["model"] == "jev-1.13.0"
    assert set(body["state"]) == {"transcript"}                      # no tool calls or tool responses
    assert body["state"]["transcript"].startswith("Assistant: Hello") and "\nUser: My balance" in body["state"]["transcript"]
    assert "1234567890" not in body["state"]["transcript"]            # redacted before it leaves
    q = body["questions"]["request_1"]
    assert q["type"] == "choice" and len(q["criteria"]) == len(INTENTS) * 4 + 1
    assert "balance__SOLVED" in q["criteria"] and "other__TRANSFERRED" in q["criteria"] and "NONE" in q["criteria"]
    assert set(body["questions"]) == {"request_1", "request_2", "request_3", "request_4", "caller_mood"}


@pytest.mark.parametrize("given,outcome", [
    ([("balance", "SOLVED"), ("statement", "SOLVED")], Outcome.RESOLVED),
    ([("balance", "SOLVED"), ("complaint", "TICKET_CREATED")], Outcome.REQUEST_RAISED),
    ([("balance", "SOLVED"), ("other", "NOT_SOLVED")], Outcome.PARTLY_RESOLVED),
    ([("loan_application", "NOT_SOLVED")], Outcome.UNRESOLVED),
    ([("balance", "TRANSFERRED"), ("human", "TRANSFERRED")], Outcome.TRANSFER_CALLER),
    ([], Outcome.NO_REQUEST)])
def test_overall_outcome_follows_the_requests(given, outcome):
    assert disposition.from_answers(jev_answers(*given)["answers"]).outcome == outcome


def test_a_repeated_request_keeps_its_later_result():
    d = disposition.from_answers(jev_answers(("balance", "NOT_SOLVED"), ("emi_enquiry", "SOLVED"), ("balance", "SOLVED"))["answers"])
    assert results(d) == [(Intent.EMI_ENQUIRY, Resolution.SOLVED), (Intent.BALANCE, Resolution.SOLVED)]


@pytest.mark.parametrize("reply", [jev_answers(("balance", "MADE_UP")), jev_answers(("made_up", "SOLVED")), {"answers": {}}, RuntimeError("HTTP 500"),
                                   TimeoutError()])
def test_a_bad_or_failed_jev_reply_falls_back_to_call_state(svc, monkeypatch, reply):
    c = Call(svc, milestone=1)
    c.say(PRIYA)
    c.say("What's my balance?")
    fake_jev(monkeypatch, reply)
    d = asyncio.run(disposition.classify(HISTORY, c.o.state, JEV))
    assert d.source == "rules" and results(d) == [(Intent.BALANCE, Resolution.SOLVED)]


def test_without_a_key_the_record_comes_from_call_state(svc):
    c = Call(svc, milestone=1)
    c.say(PRIYA)
    c.say("What's my balance?")
    c.say("Send me my repayment schedule.")
    d = asyncio.run(disposition.classify(HISTORY, c.o.state, NO_JEV))
    assert d.source == "rules"
    assert results(d) == [(Intent.BALANCE, Resolution.SOLVED), (Intent.LOAN_DOCUMENT, Resolution.TICKET)]
    assert d.intents[1].ticket_id in c.o.state.tickets and d.outcome == Outcome.REQUEST_RAISED


def test_call_state_fallback_outcomes(svc):
    c = Call(svc, milestone=1)
    assert disposition.from_state(c.o.state).outcome == Outcome.NO_REQUEST
    c.say("What's my balance?")                                       # held for verification, never run
    assert results(disposition.from_state(c.o.state)) == [(Intent.BALANCE, Resolution.NOT_SOLVED)]
    c2 = Call(svc, milestone=2)
    c2.say("I want to talk to a person.")
    d = disposition.from_state(c2.o.state)
    assert results(d) == [(Intent.HUMAN, Resolution.TRANSFERRED)] and d.outcome == Outcome.TRANSFER_CALLER


def test_call_ended_event_carries_each_request(svc):
    c = Call(svc, milestone=1)
    c.say(PRIYA)
    c.say("What's my balance?")
    c.o.end()
    ended = [json.loads(r["detail_json"]) for r in svc.conn.execute("SELECT detail_json FROM audit_events WHERE kind='call_ended'")]
    assert ended[-1]["disposition"] == "resolved_by_ai" and ended[-1]["disposition_label"] == "Resolved by AI"
    assert [(i["intent"], i["resolution_label"]) for i in ended[-1]["intents"]] == [("balance", "Solved")]


def test_a_none_slot_in_the_middle_does_not_drop_later_requests():
    answers = jev_answers(("balance", "SOLVED"), ("statement", "TICKET_CREATED"))["answers"]
    answers["request_3"], answers["request_2"] = answers["request_2"], {"type": "choice", "choice": "NONE", "confidence": 0.6}
    d = disposition.from_answers(answers)                                   # 1 = balance, 2 = NONE, 3 = statement
    assert results(d) == [(Intent.BALANCE, Resolution.SOLVED), (Intent.STATEMENT, Resolution.TICKET)]
    assert d.outcome == Outcome.REQUEST_RAISED
