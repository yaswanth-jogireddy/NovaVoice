"""Generate the NovaTrust Bank synthetic dataset (deterministic for a given --seed).

    python scripts/seed.py --seed 42            -> data/novatrust.sqlite3 + data/golden_customers.md

All people, numbers and merchants are fictional. The reference "today" is fixed so two runs
produce identical content.
"""
from __future__ import annotations

import argparse
import hashlib
import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from bank.db import connect, init_schema  # noqa: E402

TODAY = date(2026, 10, 30)

FIRST = ["Aarav", "Vivaan", "Aditya", "Ishaan", "Kabir", "Arjun", "Rohan", "Karan", "Nikhil", "Siddharth",
         "Diya", "Aisha", "Meera", "Kavya", "Sneha", "Pooja", "Neha", "Riya", "Tanvi", "Shreya"]
LAST = ["Mehta", "Kapoor", "Nair", "Reddy", "Patel", "Gupta", "Joshi", "Menon", "Kulkarni", "Bose",
        "Chopra", "Desai", "Pillai", "Saxena", "Bhat"]
CITIES = ["Bengaluru", "Mumbai", "Delhi", "Chennai", "Hyderabad", "Pune", "Kolkata", "Ahmedabad", "Jaipur", "Kochi"]
ME_PEOPLE = [("Omar", "Haddad", "Dubai"), ("Layla", "Nasser", "Riyadh"), ("Yusuf", "Rahman", "Doha"),
             ("Noura", "Saleh", "Abu Dhabi")]
MERCHANTS = [("UPI", "SWIGGY ORDER"), ("UPI", "ZEPTO GROCERIES"), ("CARD", "AMAZON RETAIL"), ("CARD", "BIGBASKET"),
             ("UPI", "BESCOM ELECTRICITY"), ("UPI", "JIO RECHARGE"), ("CARD", "IRCTC TICKET"), ("ATM", "ATM CASH WDL"),
             ("UPI", "UBER TRIP"), ("CARD", "DECATHLON"), ("NEFT", "RENT TRANSFER"), ("UPI", "PHARMEASY")]

# Golden demo customers: fixed ids and facts referenced by the demo script and evals.
GOLDEN = [
    dict(id="C1001", full_name="Priya Sharma", dob="1992-03-14", city="Bengaluru", region="IN", lang="en",
         segment="premium", balance=8425050, story="Happy path: balance + last 5 transactions + statement by email"),
    dict(id="C1002", full_name="Rahul Verma", dob="1988-11-02", city="Delhi", region="IN", lang="hinglish",
         segment="retail", balance=2310075, story="Lost debit card ending 4821 -> verify -> step-up -> block card"),
    dict(id="C1003", full_name="Ananya Iyer", dob="1995-07-21", city="Chennai", region="IN", lang="en",
         segment="retail", balance=4620000, story="Unrecognised card charge 'QUICKPAY*ONLINE' Rs 12,499 -> dispute"),
    dict(id="C1004", full_name="Mohammed Arif", dob="1985-01-09", city="Hyderabad", region="IN", lang="hi",
         segment="retail", balance=1795025, story="Home loan: next EMI date and amount, outstanding"),
    dict(id="C1005", full_name="Sunita Rao", dob="1958-05-30", city="Pune", region="IN", lang="en",
         segment="retail", balance=12550000, story="Social-engineering target: caller claims to be her son, asks for OTP"),
    dict(id="C1006", full_name="Fatima Al-Mansouri", dob="1990-09-12", city="Dubai", region="ME", lang="ar",
         segment="premium", balance=3875000, story="Middle East customer (AED), Arabic-English call"),
]


def digits(rng: random.Random, n: int) -> str:
    return "".join(str(rng.randint(0, 9)) for _ in range(n))


def seed(db_path: Path, seed_value: int) -> str:
    rng = random.Random(seed_value)
    if db_path.exists():
        db_path.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    conn = connect(db_path)
    init_schema(conn)

    customers = list(GOLDEN)
    for i in range(44):
        if i < len(ME_PEOPLE):
            fn, ln, city = ME_PEOPLE[i]
            region, lang = "ME", rng.choice(["ar", "en"])
        else:
            fn, ln, city = rng.choice(FIRST), rng.choice(LAST), rng.choice(CITIES)
            region, lang = "IN", rng.choice(["en", "hi", "hinglish"])
        customers.append(dict(id=f"C{1007 + i}", full_name=f"{fn} {ln}",
                              dob=str(date(1955, 1, 1) + timedelta(days=rng.randint(0, 17000))),
                              city=city, region=region, lang=lang, segment=rng.choice(["retail", "retail", "premium", "sme"]),
                              balance=rng.randint(50_000, 25_000_000), story=""))

    txn_seq = 900000
    for c in customers:
        currency = "AED" if c["region"] == "ME" else "INR"
        mobile = ("+9715" if c["region"] == "ME" else "+919") + digits(rng, 8)
        if c["region"] == "IN":   # Indian mobiles have 10 digits; the extra one uses no randomness,
            mobile += str(int(c["id"][1:]) % 10)   # so every other seeded value stays the same
        email = c["full_name"].lower().replace(" ", ".").replace("-", "") + "@example.com"
        kyc = "due" if c["id"] == "C1004" else rng.choice(["complete"] * 8 + ["due", "expired"])
        conn.execute("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (c["id"], c["full_name"], c["dob"], mobile, email, c["city"], c["region"], c["lang"], c["segment"], kyc))
        acc_id = "A" + ("50100" + digits(rng, 7))
        if c["id"] == "C1001":
            acc_id = "A501004417306"   # golden: last 4 = 7306
        elif c["id"] == "C1002":
            acc_id = "A501002298815"   # golden: last 4 = 8815
        conn.execute("INSERT INTO accounts VALUES (?,?,?,?,?,?,?)",
                     (acc_id, c["id"], "savings", c["balance"], currency, "active",
                      str(TODAY - timedelta(days=rng.randint(400, 4000)))))
        c["account_last4"] = acc_id[-4:]
        last4 = "4821" if c["id"] == "C1002" else digits(rng, 4)
        conn.execute("INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?)",
                     ("K" + c["id"][1:], c["id"], acc_id, last4, rng.choice(["RuPay", "Visa", "Mastercard"]),
                      "debit", "active", None, None))
        c["card_last4"] = last4

        # 90 days of activity: salary, merchants, charges
        for day in range(90, 0, -1):
            d = TODAY - timedelta(days=day)
            if d.day == 1:
                txn_seq += 1
                conn.execute("INSERT INTO transactions (id, account_id, posted_on, description, channel, amount_paise, flag) VALUES (?,?,?,?,?,?,?)",
                             (f"T{txn_seq}", acc_id, str(d), "SALARY CREDIT NOVATRUST PAYROLL", "SALARY",
                              rng.randint(4_500_000, 18_000_000), None))
            for _ in range(rng.choice([0, 0, 1, 1, 2])):
                ch, desc = rng.choice(MERCHANTS)
                txn_seq += 1
                conn.execute("INSERT INTO transactions (id, account_id, posted_on, description, channel, amount_paise, flag) VALUES (?,?,?,?,?,?,?)",
                             (f"T{txn_seq}", acc_id, str(d), desc, ch, -rng.randint(9_900, 650_000), None))
        if c["id"] == "C1003":   # planted unrecognised charge for the dispute journey
            conn.execute("INSERT INTO transactions (id, account_id, posted_on, description, channel, amount_paise, flag) VALUES (?,?,?,?,?,?,?)",
                         ("T777001", acc_id, str(TODAY - timedelta(days=2)), "QUICKPAY*ONLINE STORE", "CARD", -1_249_900, "suspicious"))
        if c["id"] == "C1004":
            conn.execute("INSERT INTO loans (id, customer_id, product, principal_paise, outstanding_paise, rate_bps, emi_paise, tenure_months, next_due_on, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         ("L204417", c["id"], "home", 450_000_000, 362_418_000, 875, 3_942_100, 240,
                          str(date(2026, 11, 5)), "current"))
        elif rng.random() < 0.3:
            principal = rng.randint(20_000_000, 150_000_000)
            conn.execute("INSERT INTO loans (id, customer_id, product, principal_paise, outstanding_paise, rate_bps, emi_paise, tenure_months, next_due_on, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (f"L{digits(rng, 6)}", c["id"], rng.choice(["personal", "vehicle"]), principal,
                          int(principal * rng.uniform(0.3, 0.9)), rng.choice([1050, 1125, 1399]),
                          int(principal / rng.choice([36, 48, 60])), rng.choice([36, 48, 60]),
                          str(date(2026, 11, rng.randint(1, 28))), rng.choice(["current"] * 9 + ["overdue"])))

    golden_lending(conn)
    conn.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?,?,?)",
                 ("SR-261028-0042", "C1001", "statement", "Account statement for Q2 by email", "in_progress",
                  "2026-10-31T18:00:00", "2026-10-28T10:15:00", "2026-10-29T09:00:00", "seed"))
    conn.commit()

    dump = "\n".join(conn.iterdump())
    digest = hashlib.sha256(dump.encode()).hexdigest()[:16]
    conn.close()

    lines = ["# Golden demo customers (generated, local only)", "",
             "| Id | Name | DOB | Acct last 4 | Card last 4 | Lang | Story |", "|---|---|---|---|---|---|---|"]
    for c in GOLDEN:
        lines.append(f"| {c['id']} | {c['full_name']} | {c['dob']} | {c['account_last4']} | {c['card_last4']} | {c['lang']} | {c['story']} |")
    lines += ["", f"Dataset digest: `{digest}` (seed {seed_value}, reference date {TODAY})"]
    (db_path.parent / "golden_customers.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return digest


def golden_lending(conn) -> None:
    """Level 1 brief data (31 Oct): loan applications (LOS), a vehicle loan, an overdue instalment and
    transactions that are pending or failed. Fixed values, no rng, so every other seeded value stays the same."""
    apps = [  # id, customer, product, amount (paise), tenure, submitted, stage, stage updated, pending action, first EMI
        ("LA-2610-1001", "C1001", "personal", 50_000_000, 36, "2026-10-24", "document_verification", "2026-10-27", None, None),
        ("LA-2610-1002", "C1002", "vehicle", 65_000_000, 60, "2026-10-12", "approved", "2026-10-29", None, None),
        ("LA-2610-1003", "C1003", "home", 450_000_000, 240, "2026-10-15", "document_verification", "2026-10-22",
         "latest 3 months' salary slips", None),
        ("LA-2610-1005", "C1005", "personal", 20_000_000, 24, "2026-10-20", "credit_assessment", "2026-10-28", None, None),
    ]
    conn.executemany("INSERT INTO loan_applications VALUES (?,?,?,?,?,?,?,?,?,?)", apps)
    # Priya (C1001): a vehicle loan account, so "I meant my vehicle loan, not my personal loan" has an answer.
    conn.execute("INSERT INTO loans (id, customer_id, product, principal_paise, outstanding_paise, rate_bps, emi_paise, "
                 "tenure_months, next_due_on, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("L301001", "C1001", "vehicle", 80_000_000, 51_234_000, 925, 1_685_000, 60, "2026-11-07", "current"))
    # Ananya (C1003): last month's instalment unpaid, so overdue and scheduled amounts must be told apart.
    conn.execute("UPDATE loans SET status='overdue', overdue_paise=emi_paise, overdue_since='2026-10-06' WHERE customer_id='C1003'")
    # Priya: one failed UPI payment and one pending NEFT, recorded without a cause.
    acc = conn.execute("SELECT id FROM accounts WHERE customer_id='C1001'").fetchone()[0]
    conn.executemany("INSERT INTO transactions (id, account_id, posted_on, description, channel, amount_paise, flag, status) "
                     "VALUES (?,?,?,?,?,?,?,?)",
                     [("T777101", acc, str(TODAY - timedelta(days=1)), "UPI PAYMENT TO RAJESH ELECTRICALS", "UPI", -250_000, None, "failed"),
                      ("T777102", acc, str(TODAY), "NEFT TO ANIL KUMAR (RENT)", "NEFT", -1_800_000, None, "pending")])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--db", type=Path, default=ROOT / "data" / "novatrust.sqlite3")
    a = p.parse_args()
    a.db.parent.mkdir(parents=True, exist_ok=True)
    print(f"seeded {a.db} digest={seed(a.db, a.seed)}")


if __name__ == "__main__":
    main()
