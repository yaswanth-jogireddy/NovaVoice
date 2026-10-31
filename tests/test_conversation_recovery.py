"""Conversation regressions: changes of mind must affect the bank, not just the reply."""
import pytest

from bank.port import BankError
from conftest import FIXED_NOW
from csm.orchestrator import Services
from test_csm_journeys import Call

PRIYA = "C1001, 14 March 1992, last four 7306"
RAHUL = "C1002, 2 November 1988, account ending 8815"


@pytest.fixture()
def svc(seeded_db):
    service = Services(seeded_db, clock=lambda: FIXED_NOW)
    yield service
    service.close()


@pytest.mark.parametrize("cancel", ["Cancel that", "Never mind", "Don't block my card", "Do not block it"])
def test_cancelled_card_action_cannot_run_with_its_old_code(svc, cancel):
    c = Call(svc)
    c.say("Block my stolen card ending 4821")
    c.say(RAHUL)
    code = c.otp
    c.say(cancel)
    assert not c.o.state.pending and not c.o.state.otp_hash
    c.say(code)
    assert svc.conn.execute("SELECT status FROM cards WHERE last4='4821'").fetchone()[0] == "active"


def test_cancel_before_verification_does_not_resume_the_action(svc):
    c = Call(svc)
    c.say("Please send my account statement")
    c.say("Cancel that")
    c.say(PRIYA)
    assert not c.o.state.tickets
    assert not c.o.state.awaiting_identity


def test_corrected_loan_while_verifying_replaces_the_queued_product(svc):
    c = Call(svc)
    c.say("What's my personal loan EMI?")
    c.say("No, I meant my vehicle loan, not my personal loan")
    p = c.say(PRIYA)
    facts = "\n".join(p.facts)
    assert '"product": "vehicle"' in facts and "INR 16,850.00" in facts
    assert "no personal loan account" not in facts


def test_changed_statement_period_while_verifying_uses_the_latest_request(svc):
    c = Call(svc)
    c.say("Email my statement for the last 3 months")
    c.say("Actually email my statement for the last 6 months")
    c.say(PRIYA)
    row = svc.conn.execute("SELECT summary FROM tickets WHERE id=?", (c.o.state.tickets[-1],)).fetchone()
    assert "last 6 months" in row[0] and len(c.o.state.tickets) == 1


def test_new_request_in_the_same_turn_as_identity_is_not_dropped(svc):
    c = Call(svc)
    c.say("What's my balance?")
    p = c.say(PRIYA + ". Also email my statement for the last 3 months.")
    assert "get_balance result" in "\n".join(p.facts)
    assert "request_statement result" in "\n".join(p.facts)


def test_status_question_does_not_register_another_complaint(svc):
    c = Call(svc)
    before = svc.conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
    c.say("What is my complaint status?")
    assert svc.conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == before
    c.say("SR-261028-0042")
    p = c.say(PRIYA)
    assert "ticket_status result" in "\n".join(p.facts) and "in_progress" in "\n".join(p.facts)


def test_spoken_ticket_reference_can_complete_a_status_question(svc):
    c = Call(svc)
    c.say(PRIYA)
    c.say("Check my request status")
    p = c.say("S R two six one zero two eight zero zero four two")
    assert "ticket_status result" in "\n".join(p.facts)


def test_statement_request_status_does_not_order_another_statement(svc):
    c = Call(svc)
    c.say(PRIYA)
    p = c.say("What is the status of my statement request SR-261028-0042?")
    assert "ticket_status result" in "\n".join(p.facts)
    assert not c.o.state.tickets


def test_transient_failure_can_be_retried_without_a_false_already_done(svc, monkeypatch):
    c = Call(svc)
    c.say(PRIYA)
    real = svc.bank.request_statement
    attempts = []

    def flaky(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise BankError("Temporary service outage")
        return real(*args)

    monkeypatch.setattr(svc.bank, "request_statement", flaky)
    assert "FAILED" in "\n".join(c.say("Email my statement").facts)
    p = c.say("Email my statement")
    assert len(attempts) == 2 and c.o.state.tickets
    assert "already done" not in "\n".join(p.facts)
    c.say("Email my statement")
    assert len(attempts) == 2 and len(c.o.state.tickets) == 1


def test_multiple_requests_survive_the_otp_pause(svc):
    c = Call(svc)
    c.say(RAHUL)
    c.say("Block my stolen card ending 4821 and email my account statement")
    assert c.otp
    p = c.say(c.otp)
    assert "block_card result" in "\n".join(p.facts)
    assert "request_statement result" in "\n".join(p.facts)


def test_requesting_a_fresh_code_invalidates_the_previous_code(svc):
    c = Call(svc)
    c.say("Block my stolen card ending 4821")
    c.say(RAHUL)
    old = c.otp
    p = c.say("Please resend the code")
    assert p.otp_code and c.o.state.otp_sends == 2
    # Hashing and freshness are asserted independently of the one-in-a-million code collision.
    assert c.o.state.pending and p.otp_code != old
    assert "block_card result" not in "\n".join(c.say(old).facts)
    assert "block_card result" in "\n".join(c.say(p.otp_code).facts)
