"""SQLite-backed MockAdapter implementing CoreBankingPort on the seeded Nova Trust data."""
from __future__ import annotations

import hmac
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta
from typing import Optional

from bank.port import BankError

CATEGORIES = {"statement", "card", "dispute", "loan", "kyc", "complaint", "general", "account"}
PRODUCTS = ("personal", "vehicle", "home")
# What each LOS stage means, in the words the agent may use. Only approved/disbursed say "approved".
STAGES = {"submitted": "received and waiting to be picked up for review",
          "document_verification": "under document verification",
          "credit_assessment": "under credit assessment",
          "approved": "approved and waiting for disbursal",
          "disbursed": "approved and disbursed",
          "rejected": "not approved"}
LOAN_DOCS = {"repayment_schedule": "repayment schedule", "loan_statement": "loan account statement"}
TXN_STATUS = {"posted": "completed", "pending": "pending (not yet completed)", "failed": "failed"}
CHANNELS = {"email", "branch", "post"}
# Target dates follow the policy documents (data/knowledge), so a reference's target date matches what the
# agent quotes: complaints 7 working days (KB-GRV-01), disputes 10 (KB-DISPUTE-01), a replacement card 5-7
# (KB-CARD-01), a statement by email 24 hours and by post or branch 5-7 working days (KB-STMT-01).
SLA = {"complaint": ("working_days", 7), "dispute": ("working_days", 10), "card": ("working_days", 7),
       "statement": ("hours", 24)}
DEFAULT_SLA = ("hours", 48)


def due_after(start: datetime, category: str, sla: tuple | None = None) -> datetime:
    """When a request in `category` raised at `start` is due. Working days skip Saturday and Sunday and end
    at 6 pm."""
    kind, n = sla or SLA.get(category, DEFAULT_SLA)
    if kind == "hours":
        return start + timedelta(hours=n)
    day = start
    while n:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day.replace(hour=18, minute=0, second=0, microsecond=0)
TICKET_STATUSES = ("open", "in_progress", "resolved")
# Past service requests on the demo customers, so a first call can already ask "what's the status of my
# request?": open, in progress (one past its target date), and resolved. Added once at server start.
DEMO_TICKETS = [
    ("SR-261015-0101", "C1001", "complaint", "Staff at the Koramangala branch were unhelpful", "resolved",
     "2026-10-18T18:00:00", "2026-10-15T11:20:00", "2026-10-17T16:05:00"),
    ("SR-261029-0102", "C1001", "account", "Registered mobile number update", "open",
     "2026-11-01T18:00:00", "2026-10-29T15:40:00", "2026-10-29T15:40:00"),
    ("SR-261020-0103", "C1002", "card", "Replacement debit card dispatch query", "resolved",
     "2026-10-21T18:00:00", "2026-10-20T09:10:00", "2026-10-21T12:30:00"),
    ("SR-261024-0104", "C1003", "dispute", "Duplicate UPI debit at a grocery store", "in_progress",
     "2026-10-27T18:00:00", "2026-10-24T19:45:00", "2026-10-26T10:00:00"),
    ("SR-261022-0105", "C1004", "loan", "Home loan EMI debited twice in October", "resolved",
     "2026-10-24T18:00:00", "2026-10-22T08:30:00", "2026-10-23T17:15:00"),
    ("SR-261027-0106", "C1004", "loan", "Home loan repayment schedule by email", "open",
     "2026-10-29T18:00:00", "2026-10-27T14:00:00", "2026-10-27T14:00:00"),
    ("SR-261010-0107", "C1005", "general", "Cheque book request, 25 leaves", "resolved",
     "2026-10-12T18:00:00", "2026-10-10T10:05:00", "2026-10-11T15:20:00"),
]


def mask(number: str) -> str:
    return "XXXX" + str(number)[-4:]


def rupees(paise: int, currency: str = "INR") -> str:
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(int(paise)), 100)
    return f"{sign}{currency} {whole:,}.{frac:02d}"


def _product(raw: str) -> str:
    return "vehicle" if raw == "auto" else raw


def _add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, min(d.day, 28))


class MockAdapter:
    def __init__(self, conn: sqlite3.Connection, clock=datetime.now) -> None:
        self._db = conn
        self._clock = clock
        self._lock = threading.Lock()

    # -- identity -------------------------------------------------------------------------
    def find_customer(self, customer_ref: str) -> Optional[dict]:
        ref = (customer_ref or "").strip().upper()
        row = None
        if re.fullmatch(r"C\d{4}", ref):
            row = self._db.execute("SELECT id, full_name FROM customers WHERE id=?", (ref,)).fetchone()
        else:
            tail = re.sub(r"\D", "", ref)[-10:]
            if len(tail) == 10:
                row = self._db.execute("SELECT id, full_name FROM customers WHERE substr(mobile, -10)=?", (tail,)).fetchone()
        return {"id": row["id"], "first_name": row["full_name"].split()[0]} if row else None

    def verify_identity(self, customer_id: str, dob: str, account_last4: Optional[str] = None) -> bool:
        row = self._db.execute("SELECT dob FROM customers WHERE id=?", (customer_id,)).fetchone()
        if not row:
            return False
        accts = [r["id"][-4:] for r in self._db.execute("SELECT id FROM accounts WHERE customer_id=?", (customer_id,))]
        dob_ok = hmac.compare_digest(row["dob"], (dob or "").strip())
        # The call verifies with customer ref + date of birth; the account's last 4 is checked only when given.
        last4_ok = account_last4 is None or any(hmac.compare_digest(a, account_last4.strip()[-4:]) for a in accts)
        return dob_ok and last4_ok

    def contact_hint(self, customer_id: str) -> dict:
        row = self._db.execute("SELECT mobile FROM customers WHERE id=?", (customer_id,)).fetchone()
        return {"mobile": "******" + row["mobile"][-4:]} if row else {}

    # -- accounts -------------------------------------------------------------------------
    def get_balance(self, customer_id: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT id, type, balance_paise, currency, status FROM accounts WHERE customer_id=?", (customer_id,))
        return [{"account": mask(r["id"]), "type": r["type"], "status": r["status"],
                 "available_balance": rupees(r["balance_paise"], r["currency"])} for r in rows]

    def recent_transactions(self, customer_id: str, n: int = 5) -> list[dict]:
        n = max(1, min(int(n or 5), 10))
        rows = self._db.execute(
            """SELECT t.id, t.posted_on, t.description, t.channel, t.amount_paise, t.status, a.currency
               FROM transactions t JOIN accounts a ON a.id = t.account_id
               WHERE a.customer_id=? ORDER BY t.posted_on DESC, t.id DESC LIMIT ?""", (customer_id, n))
        return [{"ref": r["id"], "date": r["posted_on"], "description": r["description"], "channel": r["channel"],
                 "amount": rupees(r["amount_paise"], r["currency"]),
                 "direction": "debit" if r["amount_paise"] < 0 else "credit",
                 "status": TXN_STATUS.get(r["status"], r["status"])} for r in rows]

    def customer_profile(self, customer_id: str) -> dict:
        row = self._db.execute("SELECT id, full_name, city, preferred_language, kyc_status, mobile, email FROM customers WHERE id=?",
                               (customer_id,)).fetchone()
        if not row:
            raise BankError("I couldn't find your profile.")
        user, _, domain = row["email"].partition("@")
        return {"customer_id": row["id"], "name": row["full_name"], "home_city": row["city"],
                "preferred_language": row["preferred_language"], "kyc_status": row["kyc_status"],
                "registered_mobile": "ending " + row["mobile"][-4:], "registered_email": user[:1] + "***@" + domain}

    def list_cards(self, customer_id: str) -> list[dict]:
        rows = self._db.execute("SELECT last4, network, kind, status FROM cards WHERE customer_id=?", (customer_id,))
        return [{"card": "ending " + r["last4"], "network": r["network"], "kind": r["kind"], "status": r["status"]} for r in rows]

    def block_card(self, customer_id: str, card_last4: str, reason: str) -> dict:
        last4 = re.sub(r"\D", "", card_last4 or "")[-4:]
        with self._lock:
            row = self._db.execute("SELECT id, status FROM cards WHERE customer_id=? AND last4=?",
                                   (customer_id, last4)).fetchone()
            if not row:
                raise BankError("I couldn't find a card ending with those digits on your profile.")
            if row["status"] == "blocked":   # idempotent: a repeat request is a no-op
                return {"card": "ending " + last4, "status": "blocked", "already_blocked": True}
            now = self._clock().isoformat(timespec="seconds")
            self._db.execute("UPDATE cards SET status='blocked', blocked_reason=?, blocked_at=? WHERE id=?",
                             ((reason or "lost")[:80], now, row["id"]))
            ticket = self._ticket(customer_id, "card", f"Card ending {last4} blocked ({(reason or 'lost')[:40]}); replacement to be issued")
            self._db.commit()
        return {"card": "ending " + last4, "status": "blocked", "already_blocked": False,
                "replacement": "dispatched to registered address in 5-7 working days", "ticket": ticket["ticket_id"]}

    def loan_summary(self, customer_id: str, product: Optional[str] = None) -> list[dict]:
        """Loan accounts (simulated LMS). The next scheduled instalment and any overdue amount are separate
        fields and never added together; the schedule is the next three instalments as recorded."""
        out = []
        for r in self._db.execute("SELECT * FROM loans WHERE customer_id=? ORDER BY id", (customer_id,)):
            kind = _product(r["product"])
            if product and kind != product:
                continue
            emi, outstanding, due = r["emi_paise"], r["outstanding_paise"], date.fromisoformat(r["next_due_on"])
            left = -(-outstanding // emi) if emi else 0
            out.append({"loan": mask(r["id"]), "product": kind,
                        "next_scheduled_instalment": {"amount": rupees(emi), "due_on": r["next_due_on"]},
                        "overdue": ({"amount": rupees(r["overdue_paise"]), "unpaid_since": r["overdue_since"]}
                                    if r["overdue_paise"] else "nothing overdue"),
                        "outstanding_principal": rupees(outstanding), "instalments_left": left,
                        "rate": f"{r['rate_bps'] / 100:.2f}% p.a.", "status": r["status"],
                        "upcoming_schedule": [{"due_on": str(_add_months(due, i)), "amount": rupees(emi)}
                                              for i in range(min(3, left))],
                        "emi": rupees(emi), "next_due_on": r["next_due_on"]})
        return out

    def loan_applications(self, customer_id: str, product: Optional[str] = None) -> list[dict]:
        """Loan applications (simulated LOS), newest first, with the stage in plain words."""
        out = []
        for r in self._db.execute("SELECT * FROM loan_applications WHERE customer_id=? ORDER BY submitted_on DESC",
                                  (customer_id,)):
            if product and r["product"] != product:
                continue
            disbursed = r["stage"] == "disbursed" and r["first_emi_on"]
            out.append({"application_ref": r["id"], "product": r["product"], "amount_applied": rupees(r["amount_paise"]),
                        "tenure_months": r["tenure_months"], "submitted_on": r["submitted_on"],
                        "stage": STAGES.get(r["stage"], r["stage"]), "stage_updated_on": r["stage_updated_on"],
                        "is_approved": r["stage"] in ("approved", "disbursed"),
                        "action_needed_from_customer": r["pending_action"] or "none recorded at this stage",
                        "first_instalment": r["first_emi_on"] if disbursed
                        else "not scheduled yet; it is set when the loan is disbursed"})
        return out

    def request_loan_document(self, customer_id: str, doc: str, product: Optional[str], channel: str) -> dict:
        channel = (channel or "email").lower()
        if doc not in LOAN_DOCS:
            raise BankError("I can send a repayment schedule or a loan account statement.")
        if channel not in CHANNELS:
            raise BankError("Loan documents can be sent by email, post, or collected at a branch.")
        loans = self.loan_summary(customer_id, product)
        if not loans:
            raise BankError(f"I couldn't find a {product + ' ' if product else ''}loan account on your profile.")
        if len(loans) > 1:
            raise BankError("You have more than one loan. Which one should I send it for: "
                            + " or ".join(f"the {x['product']} loan" for x in loans) + "?")
        loan = loans[0]
        with self._lock:
            t = self._ticket(customer_id, "loan",
                             f"{LOAN_DOCS[doc].capitalize()} for {loan['product']} loan {loan['loan']} via {channel}")
            self._db.commit()
        return {**t, "document": LOAN_DOCS[doc], "loan": f"{loan['product']} loan {loan['loan']}", "channel": channel,
                "eta": "within 24 hours to the registered email" if channel == "email" else "5-7 working days"}

    # -- service requests -----------------------------------------------------------------
    def request_statement(self, customer_id: str, period: str, channel: str) -> dict:
        channel = (channel or "email").lower()
        if channel not in CHANNELS:
            raise BankError("Statements can be sent by email, post, or collected at a branch.")
        with self._lock:
            t = self._ticket(customer_id, "statement", f"Statement for {(period or 'last 3 months')[:40]} via {channel}",
                             sla=None if channel == "email" else ("working_days", 7))
            self._db.commit()
        return {**t, "eta": "within 24 hours to the registered email" if channel == "email" else "5-7 working days"}

    def raise_dispute(self, customer_id: str, txn_ref: str, reason: str) -> dict:
        with self._lock:
            row = self._db.execute(
                """SELECT t.id, t.description, t.amount_paise, a.currency FROM transactions t
                   JOIN accounts a ON a.id=t.account_id WHERE a.customer_id=? AND t.id=?""",
                (customer_id, (txn_ref or "").strip().upper())).fetchone()
            if not row:
                raise BankError("I couldn't find that transaction on your account.")
            if row["amount_paise"] >= 0:
                raise BankError("Only debit transactions can be disputed.")
            existing = self._db.execute("SELECT ticket_id FROM disputes WHERE txn_id=?", (row["id"],)).fetchone()
            if existing:
                return {"ticket_id": existing["ticket_id"], "status": "already_raised"}
            t = self._ticket(customer_id, "dispute",
                             f"Dispute {row['description']} {rupees(row['amount_paise'], row['currency'])}: {(reason or '')[:60]}")
            self._db.execute("INSERT INTO disputes VALUES (?,?,?,?,?,?)",
                             ("D" + t["ticket_id"][3:], row["id"], customer_id, (reason or "not recognised")[:120],
                              t["ticket_id"], self._clock().isoformat(timespec="seconds")))
            self._db.commit()
        return {**t, "status": "raised", "provisional_credit": "decision within 10 working days (policy KB-DISPUTE-01)"}

    def create_ticket(self, customer_id: Optional[str], category: str, summary: str) -> dict:
        with self._lock:
            t = self._ticket(customer_id, category, summary)
            self._db.commit()
        return t

    def ticket_status(self, ticket_id: str, customer_id: Optional[str]) -> Optional[dict]:
        row = self._db.execute("SELECT * FROM tickets WHERE id=?", ((ticket_id or "").strip().upper(),)).fetchone()
        if not row or (row["customer_id"] and row["customer_id"] != customer_id):
            return None   # never confirm another customer's ticket exists
        return {"ticket_id": row["id"], "category": row["category"], "status": row["status"],
                "sla_due_at": row["sla_due_at"], "updated_at": row["updated_at"]}

    # -- test customers (synthetic, added from the staff screens) --------------------------------
    def mock_defaults(self) -> dict:
        """Suggested values for a new test customer: editable in the form, valid as they are."""
        n = self._next_mock_number()
        names = ["Kiran Rao", "Meera Nair", "Arjun Mehta", "Sana Qureshi", "Vikram Joshi", "Leela Iyer"]
        tail = f"{n:04d}"
        return {"full_name": names[(n - 1) % len(names)], "dob": "1990-06-15", "mobile": "98765" + "0" + tail,
                "city": "Bengaluru", "language": "en", "balance_inr": 125000, "account_last4": tail,
                "card_last4": f"{(n * 37) % 10000:04d}", "with_loan": True, "with_suspicious_charge": True,
                "with_past_tickets": True}

    def create_customer(self, p: dict) -> dict:
        """Insert one synthetic customer with an account, a debit card, 30 days of transactions and,
        optionally, a home loan and an unrecognised charge. Returns what a tester needs to call in."""
        p = self._validate_mock(p)
        with self._lock:
            n = self._next_mock_number()
            cid, acc = f"C{9000 + n}", "A5019" + f"{n:04d}" + p["account_last4"]
            today = self._clock().date()
            self._db.execute("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (cid, p["full_name"], p["dob"], "+91" + p["mobile"], f"test.{cid.lower()}@example.com",
                              p["city"], "IN", p["language"], "retail", "complete"))
            self._db.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?)",
                             (acc, cid, "savings", int(p["balance_inr"] * 100), "INR", "active", str(today - timedelta(days=900))))
            self._db.execute("INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?)",
                             ("K" + cid[1:], cid, acc, p["card_last4"], "RuPay", "debit", "active", None, None))
            spends = [("UPI", "SWIGGY ORDER", -45000), ("CARD", "AMAZON RETAIL", -219900), ("UPI", "BESCOM ELECTRICITY", -186000),
                      ("SALARY", "SALARY CREDIT NOVATRUST PAYROLL", 8500000), ("UPI", "UBER TRIP", -32000), ("ATM", "ATM CASH WDL", -500000)]
            for i, (channel, desc, paise) in enumerate(spends):
                self._db.execute("INSERT INTO transactions (id, account_id, posted_on, description, channel, amount_paise, flag) VALUES (?,?,?,?,?,?,?)",
                                 (f"T8{n:03d}{i:02d}", acc, str(today - timedelta(days=3 + i * 5)), desc, channel, paise, None))
            if p["with_suspicious_charge"]:
                self._db.execute("INSERT INTO transactions (id, account_id, posted_on, description, channel, amount_paise, flag) VALUES (?,?,?,?,?,?,?)",
                                 (f"T8{n:03d}99", acc, str(today - timedelta(days=1)), "QUICKPAY*ONLINE STORE", "CARD", -1249900, "suspicious"))
            if p["with_loan"]:
                self._db.execute("INSERT INTO loans (id, customer_id, product, principal_paise, outstanding_paise, rate_bps, emi_paise, "
                                 "tenure_months, next_due_on, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 (f"L9{n:05d}", cid, "home", 450000000, 362418000, 875, 3942100, 240,
                                  str(today + timedelta(days=12)), "current"))
            past = []
            if p["with_past_tickets"]:
                # One resolved complaint and one request still in progress, to ask about on the first call.
                now = self._clock()
                for days_ago, category, summary, status in (
                        (12, "complaint", "Card declined at a shop although the balance was available", "resolved"),
                        (1, "statement", "Stamped account statement by post", "in_progress")):
                    created = now - timedelta(days=days_ago)
                    past.append(self._insert_ticket(cid, category, summary, status, created, due_after(created, category),
                                                    min(now, created + timedelta(days=1)), "seed"))
            self._db.commit()
        return {"customer_id": cid, **{k: p[k] for k in ("full_name", "dob", "mobile", "account_last4", "card_last4", "language")},
                "past_tickets": past}

    def ensure_demo_tickets(self) -> int:
        """Add the demo customers' past service requests (DEMO_TICKETS) if they are missing. Returns how many."""
        with self._lock:
            known = {r[0] for r in self._db.execute("SELECT id FROM customers WHERE id BETWEEN 'C1001' AND 'C1006'")}
            before = self._db.total_changes
            for tid, cid, category, summary, status, due, created, updated in DEMO_TICKETS:
                if cid in known:
                    self._db.execute("INSERT OR IGNORE INTO tickets VALUES (?,?,?,?,?,?,?,?,?)",
                                     (tid, cid, category, summary, status, due, created, updated, "seed"))
            self._db.commit()
            return self._db.total_changes - before

    def add_ticket(self, p: dict) -> dict:
        """A service request added by staff on the agent desk: a past or open request to test status questions."""
        cid = str(p.get("customer_id", "")).strip().upper()
        category, status = str(p.get("category", "")), str(p.get("status", "open"))
        summary = re.sub(r"\s+", " ", str(p.get("summary", ""))).strip()[:200]
        try:
            days_ago = max(0, min(365, int(p.get("days_ago", 0))))
        except (TypeError, ValueError):
            raise BankError("Days ago must be a whole number.") from None
        if category not in CATEGORIES:
            raise BankError(f"Category must be one of: {', '.join(sorted(CATEGORIES))}.")
        if status not in TICKET_STATUSES:
            raise BankError("Status must be open, in progress or resolved.")
        if len(summary) < 3:
            raise BankError("Write a short summary of the request.")
        with self._lock:
            if not self._db.execute("SELECT 1 FROM customers WHERE id=?", (cid,)).fetchone():
                raise BankError(f"No customer {cid or '(blank)'}.")
            now = self._clock()
            created = now - timedelta(days=days_ago)
            updated = created if status == "open" else min(now, created + timedelta(days=1))
            tid = self._insert_ticket(cid, category, summary, status, created, due_after(created, category), updated,
                                      "staff_desk")
            self._db.commit()
        return self.ticket_status(tid, cid) | {"customer_id": cid, "summary": summary}

    def test_customers(self) -> list[dict]:
        """Golden demo customers (C1001-C1006) plus every test customer added from the staff screens."""
        rows = self._db.execute(
            """SELECT c.id, c.full_name, c.dob, c.mobile, c.preferred_language, MIN(a.id) AS acc,
                      (SELECT last4 FROM cards k WHERE k.customer_id=c.id LIMIT 1) AS card
               FROM customers c JOIN accounts a ON a.customer_id=c.id
               WHERE c.id BETWEEN 'C1001' AND 'C1006' OR c.id >= 'C9001' GROUP BY c.id ORDER BY c.id""")
        return [{"customer_id": r["id"], "full_name": r["full_name"], "dob": r["dob"], "mobile": r["mobile"][-10:],
                 "account_last4": r["acc"][-4:], "card_last4": r["card"], "language": r["preferred_language"],
                 "kind": "golden" if r["id"] < "C9001" else "added"} for r in rows]

    def _next_mock_number(self) -> int:
        row = self._db.execute("SELECT MAX(id) FROM customers WHERE id >= 'C9001'").fetchone()[0]
        return int(row[1:]) - 9000 + 1 if row else 1

    def _validate_mock(self, p: dict) -> dict:
        try:
            out = {"full_name": re.sub(r"\s+", " ", str(p.get("full_name", ""))).strip(),
                   "dob": str(p.get("dob", "")).strip(), "mobile": re.sub(r"\D", "", str(p.get("mobile", "")))[-10:],
                   "city": str(p.get("city", "Bengaluru")).strip()[:40] or "Bengaluru",
                   "language": p.get("language", "en"), "balance_inr": float(p.get("balance_inr", 0)),
                   "account_last4": re.sub(r"\D", "", str(p.get("account_last4", ""))),
                   "card_last4": re.sub(r"\D", "", str(p.get("card_last4", ""))),
                   "with_loan": bool(p.get("with_loan")), "with_suspicious_charge": bool(p.get("with_suspicious_charge")),
                   "with_past_tickets": bool(p.get("with_past_tickets"))}
            born = datetime.strptime(out["dob"], "%Y-%m-%d").date()
        except (TypeError, ValueError):
            raise BankError("Check the date of birth (YYYY-MM-DD) and the balance.") from None
        if not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{1,59}", out["full_name"]):
            raise BankError("Name: letters and spaces, up to 60 characters.")
        if not 1920 <= born.year <= self._clock().year - 18:
            raise BankError("Date of birth must make the customer at least 18.")
        if not re.fullmatch(r"[6-9]\d{9}", out["mobile"]):
            raise BankError("Mobile: 10 digits starting with 6-9.")
        if self._db.execute("SELECT 1 FROM customers WHERE substr(mobile, -10)=?", (out["mobile"],)).fetchone():
            raise BankError("That mobile number is already registered to another customer.")
        if len(out["account_last4"]) != 4 or len(out["card_last4"]) != 4:
            raise BankError("Account and card endings must be 4 digits.")
        if out["language"] not in {"en", "hi", "hinglish"}:
            raise BankError("Language must be en, hi or hinglish.")
        if not 0 <= out["balance_inr"] <= 10_000_000:
            raise BankError("Balance must be between 0 and 1,00,00,000.")
        return out

    def recent_tickets(self, limit: int = 50) -> list[dict]:
        """Staff view (agent desk / supervisor): newest tickets, customer id only, no contact details."""
        rows = self._db.execute("SELECT id, customer_id, category, summary, status, sla_due_at, created_at, created_by "
                                "FROM tickets ORDER BY created_at DESC, id DESC LIMIT ?", (max(1, min(limit, 200)),))
        return [dict(r) for r in rows]

    # -- internals --------------------------------------------------------------------------
    def _insert_ticket(self, customer_id, category, summary, status, created, due, updated, created_by) -> str:
        """Insert one ticket dated `created` (the caller holds the lock and commits). Returns its id."""
        prefix = "SR-" + created.strftime("%y%m%d") + "-"
        count = self._db.execute("SELECT COUNT(*) FROM tickets WHERE id LIKE ?", (prefix + "%",)).fetchone()[0]
        tid = f"{prefix}{count + 1:04d}"
        while self._db.execute("SELECT 1 FROM tickets WHERE id=?", (tid,)).fetchone():
            count += 1
            tid = f"{prefix}{count + 1:04d}"
        self._db.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?)",
                         (tid, customer_id, category, summary[:200], status, due.isoformat(timespec="seconds"),
                          created.isoformat(timespec="seconds"), updated.isoformat(timespec="seconds"), created_by))
        return tid

    def _ticket(self, customer_id: Optional[str], category: str, summary: str, sla: tuple | None = None) -> dict:
        category = category if category in CATEGORIES else "general"
        now = self._clock()
        prefix = "SR-" + now.strftime("%y%m%d") + "-"
        count = self._db.execute("SELECT COUNT(*) FROM tickets WHERE id LIKE ?", (prefix + "%",)).fetchone()[0]
        tid = f"{prefix}{count + 1:04d}"
        sla = due_after(now, category, sla)
        stamp = now.isoformat(timespec="seconds")
        self._db.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?)",
                         (tid, customer_id, category, (summary or "")[:200], "open",
                          sla.isoformat(timespec="seconds"), stamp, stamp, "ai_agent"))
        return {"ticket_id": tid, "category": category, "sla_due_at": sla.isoformat(timespec="minutes")}
