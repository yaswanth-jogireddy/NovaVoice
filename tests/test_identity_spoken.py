"""T24 + T15: identity details however they are spoken, asked one at a time, and masked in storage."""
import asyncio

import pytest

from conftest import FIXED_NOW
from csm import guardrails
from csm.orchestrator import Orchestrator, Services
from csm.router import extract_identity, parse_dob
from csm.spoken import spoken_numbers
from features import FeatureFlags

# Every form measured on 31 Oct (14 read, 11 failed) plus the ones seen on real-mic calls.
DOB_FORMS = [
    "14 March 1992", "14th of March, 1992", "March 14, 1992", "14/03/1992", "1992-03-14", "14.03.1992",
    "14thMarch, 1992", "14 मार्च 1992",
    # failed before T24
    "14-3-92", "fourteenth March nineteen ninety two", "one four zero three one nine nine two", "14 03 1992",
    "14031992", "the 14th of March, 92", "March the fourteenth 1992", "चौदह मार्च उन्नीस सौ बानवे",
    "chaudah march unnis sau baanve", "fourteen zero three nineteen ninety two", "14 March '92",
    "March fourteenth, nineteen ninety-two", "14 3 92",
]


@pytest.mark.parametrize("text", DOB_FORMS)
def test_dob_spoken_forms(text):
    assert extract_identity(f"my date of birth is {text}").get("dob") == "1992-03-14", text


@pytest.mark.parametrize("text", [
    "C1001", "c 1001", "C-1001", "customer ID see one zero zero one", "customer ID is sea 1001", "CID 1001",
    "my ID is 1001", "c one zero zero one", "customer number 1001", "see one zero zero one",
])
def test_customer_id_spoken_forms(text):
    found = extract_identity(text)
    assert found.get("customer_ref") == "C1001", text
    assert "last4" not in found, f"{text!r}: the customer ID must never land in the last-4 slot"


@pytest.mark.parametrize("text,last4", [
    ("last four digits seven three zero six", "7306"), ("account ends with seventy three oh six", "7306"),
    ("ending in 7 3 0 6", "7306"), ("aakhri chaar 7306", "7306"), ("last four seven three zero six", "7306"),
    ("account ends with eight eight one five.", "8815"),
])
def test_last4_spoken_forms(text, last4):
    assert extract_identity(text).get("last4") == last4, text


def test_all_three_in_words():
    assert extract_identity("customer ID see one zero zero one, born fourteenth March nineteen ninety two, "
                            "last four seven three zero six") == {"customer_ref": "C1001", "dob": "1992-03-14", "last4": "7306"}


def test_expected_field_decides_a_bare_number():
    assert extract_identity("1001", expect="customer_ref") == {"customer_ref": "C1001"}
    assert extract_identity("one zero zero one", expect="last4") == {"last4": "1001"}
    assert extract_identity("1001") == {"last4": "1001"}                  # unchanged without a hint
    # asked for the date of birth, heard only "1403": ask again rather than burn an attempt
    assert extract_identity("1403", expect="dob") == {}
    assert extract_identity("14 March 1992 7306", expect="dob") == {"dob": "1992-03-14", "last4": "7306"}
    # a cue beats the hint
    assert extract_identity("my last four is 7306", expect="customer_ref") == {"last4": "7306"}


def test_no_false_identity():
    assert extract_identity("I want to block my card") == {}
    assert extract_identity("what's my balance for the last two months") == {}
    assert "last4" not in extract_identity("my card ending 4821 is lost")
    assert "customer_ref" not in extract_identity("my account number ends with 7306")
    assert parse_dob("14 March")[0] is None
    assert parse_dob("March 14 7306")[0] is None


def test_spoken_numbers_leaves_other_words_alone():
    assert spoken_numbers("what do you do") == "what do you do"
    assert spoken_numbers("wait one second please") == "wait 1 second please"
    assert spoken_numbers("second November") == "2nd November"
    assert spoken_numbers("Oh, my ID is C1001") == "Oh, my ID is C1001"
    assert spoken_numbers("two thousand and one") == "2001"
    assert spoken_numbers("do hazaar ek") == "2001"


@pytest.mark.parametrize("text,gone", [
    ("my date of birth is fourteenth March nineteen ninety two", ["fourteenth", "nineteen", "1992", "14th"]),
    ("last four seven three zero six", ["seven", "7306"]),
    ("born 14 March 92", ["14 March", "92"]),
    ("March the 14th 1992", ["14th", "1992"]),
    ("चौदह मार्च उन्नीस सौ बानवे", ["चौदह", "1992"]),
])
def test_spoken_details_are_masked(text, gone):
    out = guardrails.redact(text)
    assert all(g not in out for g in gone), out


def test_masking_keeps_ordinary_wording():
    assert guardrails.redact("I lost two cards") == "I lost two cards"
    assert guardrails.redact("Ticket SR-202610-0001 for C1001") == "Ticket SR-202610-0001 for C1001"


# --- T15: one detail at a time, through the orchestrator ------------------------------------------
@pytest.fixture()
def svc(seeded_db):
    s = Services(seeded_db, clock=lambda: FIXED_NOW)
    yield s
    s.close()


def _say(o, history, text):
    history.append({"role": "user", "content": text})
    plan = asyncio.run(o.handle_turn(text, history))
    history.append({"role": "assistant", "content": plan.script or "(model reply)"})
    return plan


def test_verification_asks_one_detail_at_a_time(svc):
    o, h = Orchestrator(svc, FeatureFlags({"MILESTONE": "3"}), "call-1"), []
    p = _say(o, h, "What's my balance?")
    asks = " ".join(p.instructions)
    assert "verify a few details" in asks and "customer ID or registered mobile number" in asks
    assert "date of birth" not in asks and o.state.asked_field == "customer_ref"
    p = _say(o, h, "see one zero zero one")
    assert o.state.id_fields == {"customer_ref": "C1001"} and o.state.asked_field == "dob"
    assert "ask only for their date of birth" in " ".join(p.instructions)
    p = _say(o, h, "fourteenth March nineteen ninety two")   # two details: the date of birth completes it
    assert o.state.verified and "INR 84,250.50" in "\n".join(p.facts)
    stored = " ".join(e.get("text", "") for e in svc.audit.events("call-1") if e["kind"] == "turn")
    assert "nineteen ninety two" not in stored


def test_all_details_at_once_still_verifies_in_one_turn(svc):
    o, h = Orchestrator(svc, FeatureFlags({"MILESTONE": "3"}), "call-1"), []
    _say(o, h, "What's my balance?")
    p = _say(o, h, "Customer ID C1001, born 14 March 1992, last four 7306")
    assert o.state.verified and "INR 84,250.50" in "\n".join(p.facts)


def test_failed_attempt_restarts_from_the_customer_id(svc):
    o, h = Orchestrator(svc, FeatureFlags({"MILESTONE": "3"}), "call-1"), []
    _say(o, h, "What's my balance?")
    p = _say(o, h, "C1001, 1 January 1990, last four 7306")
    assert not o.state.verified and o.state.asked_field == "customer_ref"
    assert "one at a time" in " ".join(p.instructions)
    _say(o, h, "1001")   # a bare number after "customer ID?" is the customer ID
    assert o.state.id_fields == {"customer_ref": "C1001"}
