import json
import re

import pytest

from bank.port import BankError

FULL_ACCOUNT = re.compile(r"\b501\d{9}\b|A501\d{9}")


def test_seed_is_deterministic(tmp_path):
    import seed
    assert seed.seed(tmp_path / "a.sqlite3", 42) == seed.seed(tmp_path / "b.sqlite3", 42)
    assert seed.seed(tmp_path / "c.sqlite3", 7) != seed.seed(tmp_path / "d.sqlite3", 42)


def test_find_customer_by_id_and_unknown(bank):
    assert bank.find_customer("c1001") == {"id": "C1001", "first_name": "Priya"}
    assert bank.find_customer("C9999") is None
    assert bank.find_customer("") is None


def test_verify_identity_needs_both_factors(bank):
    assert bank.verify_identity("C1001", "1992-03-14", "7306")
    assert not bank.verify_identity("C1001", "1992-03-14", "0000")
    assert not bank.verify_identity("C1001", "1990-01-01", "7306")
    assert not bank.verify_identity("C9999", "1992-03-14", "7306")


def test_balance_and_transactions_are_masked(bank):
    out = json.dumps([bank.get_balance("C1001"), bank.recent_transactions("C1001", 50), bank.loan_summary("C1004"),
                      bank.contact_hint("C1001")])
    assert not FULL_ACCOUNT.search(out)
    assert "XXXX7306" in out
    assert len(bank.recent_transactions("C1001", 50)) == 10   # capped


def test_block_card_is_idempotent(bank):
    first = bank.block_card("C1002", "4821", "lost")
    again = bank.block_card("C1002", "4821", "lost")
    assert first["status"] == "blocked" and not first["already_blocked"] and first["ticket"].startswith("SR-261031-")
    assert again["already_blocked"]
    with pytest.raises(BankError):
        bank.block_card("C1002", "9999", "lost")


def test_cannot_block_another_customers_card(bank):
    with pytest.raises(BankError):
        bank.block_card("C1001", "4821", "lost")   # 4821 belongs to C1002


def test_dispute_flow(bank):
    t = bank.raise_dispute("C1003", "T777001", "not recognised")
    assert t["status"] == "raised" and t["category"] == "dispute"
    assert bank.raise_dispute("C1003", "T777001", "again")["status"] == "already_raised"
    with pytest.raises(BankError):
        bank.raise_dispute("C1001", "T777001", "not mine")   # other customer's txn


def test_ticket_status_scoped_to_owner(bank):
    t = bank.create_ticket("C1001", "complaint", "Branch queue too long")
    assert bank.ticket_status(t["ticket_id"], "C1001")["status"] == "open"
    assert bank.ticket_status(t["ticket_id"], "C1002") is None
    assert bank.ticket_status("SR-261028-0042", "C1001")["status"] == "in_progress"


def test_statement_channel_validation(bank):
    assert bank.request_statement("C1001", "last 3 months", "email")["category"] == "statement"
    with pytest.raises(BankError):
        bank.request_statement("C1001", "last 3 months", "whatsapp")


def test_loan_summary_golden(bank):
    (loan,) = bank.loan_summary("C1004")
    assert loan["product"] == "home" and loan["next_due_on"] == "2026-11-05" and loan["emi"] == "INR 39,421.00"


def test_mock_customer_defaults_create_and_verify(bank):
    defaults = bank.mock_defaults()
    created = bank.create_customer(defaults)
    assert created["customer_id"] == "C9001"
    cid = created["customer_id"]
    assert bank.verify_identity(cid, defaults["dob"], defaults["account_last4"])
    assert bank.find_customer(defaults["mobile"])["id"] == cid            # verify by mobile works too
    assert len(bank.recent_transactions(cid, 10)) == 7
    assert bank.loan_summary(cid) and bank.get_balance(cid)[0]["available_balance"] == "INR 125,000.00"
    assert any(t["customer_id"] == cid and t["kind"] == "added" for t in bank.test_customers())
    assert bank.mock_defaults()["mobile"] != defaults["mobile"]           # next defaults don't collide


@pytest.mark.parametrize("change", [{"dob": "2015-01-01"}, {"mobile": "12345"}, {"full_name": "<script>"},
                                    {"account_last4": "12"}, {"language": "fr"}, {"balance_inr": -5}])
def test_mock_customer_validation(bank, change):
    with pytest.raises(BankError):
        bank.create_customer({**bank.mock_defaults(), **change})


def test_mock_customer_mobile_must_be_unique(bank):
    defaults = bank.mock_defaults()
    bank.create_customer(defaults)
    with pytest.raises(BankError, match="already registered"):
        bank.create_customer({**bank.mock_defaults(), "mobile": defaults["mobile"]})


def test_golden_customers_can_verify_by_mobile(bank):
    for person in bank.test_customers():
        if person["customer_id"] in ("C1001", "C1002", "C1003", "C1004", "C1005"):
            assert len(person["mobile"]) == 10 and person["mobile"][0] in "6789"
            assert bank.find_customer(person["mobile"])["id"] == person["customer_id"]
