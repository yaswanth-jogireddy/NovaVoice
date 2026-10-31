"""Test data for service requests: the demo customers' past requests, staff-added requests, and past requests on
new test customers, all answerable by ticket_status on a call."""
from datetime import timedelta

import pytest

from bank.mock_adapter import DEMO_TICKETS
from bank.port import BankError
from conftest import FIXED_NOW
from csm.orchestrator import Services
from test_csm_journeys import Call


@pytest.fixture()
def svc(seeded_db):
    s = Services(seeded_db, clock=lambda: FIXED_NOW)
    yield s
    s.close()


def test_demo_tickets_are_added_once_and_answer_status_questions(svc):
    assert svc.bank.ensure_demo_tickets() == len(DEMO_TICKETS)
    assert svc.bank.ensure_demo_tickets() == 0                       # idempotent: safe on every server start
    assert svc.bank.ticket_status("SR-261024-0104", "C1003")["status"] == "in_progress"
    assert svc.bank.ticket_status("SR-261024-0104", "C1001") is None   # another customer's request stays hidden
    c = Call(svc, milestone=1)
    c.say("C1001, 14 March 1992, last four 7306")
    plan = c.say("What's the status of SR-261029-0102?")
    assert "ticket_status result" in "\n".join(plan.facts) and '"status": "open"' in "\n".join(plan.facts)


def test_staff_can_add_a_past_request_on_any_customer(svc):
    t = svc.bank.add_ticket({"customer_id": "c1004", "category": "loan", "summary": "EMI  not updated after payment",
                             "status": "resolved", "days_ago": 5})
    assert t["customer_id"] == "C1004" and t["status"] == "resolved" and t["summary"] == "EMI not updated after payment"
    assert t["ticket_id"].startswith("SR-" + (FIXED_NOW - timedelta(days=5)).strftime("%y%m%d"))   # dated 5 days back
    again = svc.bank.add_ticket({"customer_id": "C1004", "category": "loan", "summary": "Second one", "days_ago": 5})
    assert again["ticket_id"] != t["ticket_id"] and again["status"] == "open"
    listed = {r["id"]: r for r in svc.bank.recent_tickets(50)}
    assert listed[t["ticket_id"]]["created_by"] == "staff_desk"      # shown on the desk as added by staff


@pytest.mark.parametrize("bad,message", [({"customer_id": "C0000"}, "No customer"), ({"category": "pizza"}, "Category"),
                                         ({"status": "done"}, "Status"), ({"summary": ""}, "summary"),
                                         ({"days_ago": "soon"}, "whole number")])
def test_staff_ticket_is_validated(svc, bad, message):
    with pytest.raises(BankError, match=message):
        svc.bank.add_ticket({"customer_id": "C1001", "category": "account", "summary": "Address change", **bad})


def test_new_test_customer_gets_past_requests(svc):
    created = svc.bank.create_customer(svc.bank.mock_defaults())
    assert len(created["past_tickets"]) == 2
    statuses = {svc.bank.ticket_status(t, created["customer_id"])["status"] for t in created["past_tickets"]}
    assert statuses == {"resolved", "in_progress"}
    without = svc.bank.create_customer({**svc.bank.mock_defaults(), "with_past_tickets": False})
    assert without["past_tickets"] == []
