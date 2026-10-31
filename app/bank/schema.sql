-- NovaTrust mock core banking + CSM operational tables. Idempotent.
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS customers (
    id            TEXT PRIMARY KEY,           -- e.g. C1001
    full_name     TEXT NOT NULL,
    dob           TEXT NOT NULL,              -- YYYY-MM-DD (verification factor)
    mobile        TEXT NOT NULL,              -- E.164, never spoken in full
    email         TEXT NOT NULL,
    city          TEXT NOT NULL,
    region        TEXT NOT NULL,              -- IN | ME
    preferred_language TEXT NOT NULL,         -- en | hi | hinglish | ar
    segment       TEXT NOT NULL,              -- retail | premium | sme
    kyc_status    TEXT NOT NULL               -- complete | due | expired
);

CREATE TABLE IF NOT EXISTS accounts (
    id            TEXT PRIMARY KEY,           -- A + 12 digits (full number never leaves the adapter)
    customer_id   TEXT NOT NULL REFERENCES customers(id),
    type          TEXT NOT NULL,              -- savings | current
    balance_paise INTEGER NOT NULL,
    currency      TEXT NOT NULL,
    status        TEXT NOT NULL,              -- active | frozen | dormant
    opened_on     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cards (
    id            TEXT PRIMARY KEY,
    customer_id   TEXT NOT NULL REFERENCES customers(id),
    account_id    TEXT NOT NULL REFERENCES accounts(id),
    last4         TEXT NOT NULL,
    network       TEXT NOT NULL,
    kind          TEXT NOT NULL,              -- debit | credit
    status        TEXT NOT NULL,              -- active | blocked
    blocked_reason TEXT,
    blocked_at    TEXT
);

CREATE TABLE IF NOT EXISTS loans (
    id            TEXT PRIMARY KEY,           -- L + 6 digits
    customer_id   TEXT NOT NULL REFERENCES customers(id),
    product       TEXT NOT NULL,              -- home | personal | vehicle ("auto" in older databases)
    principal_paise INTEGER NOT NULL,
    outstanding_paise INTEGER NOT NULL,
    rate_bps      INTEGER NOT NULL,
    emi_paise     INTEGER NOT NULL,
    tenure_months INTEGER NOT NULL,
    next_due_on   TEXT NOT NULL,              -- next SCHEDULED instalment
    status        TEXT NOT NULL,             -- current | overdue
    overdue_paise INTEGER NOT NULL DEFAULT 0, -- missed instalments, kept apart from the next scheduled one
    overdue_since TEXT                        -- due date of the oldest unpaid instalment
);

-- Simulated Loan Origination System: applications before a loan account exists.
CREATE TABLE IF NOT EXISTS loan_applications (
    id            TEXT PRIMARY KEY,           -- LA-YYMM-NNNN
    customer_id   TEXT NOT NULL REFERENCES customers(id),
    product       TEXT NOT NULL,              -- personal | vehicle | home
    amount_paise  INTEGER NOT NULL,
    tenure_months INTEGER NOT NULL,
    submitted_on  TEXT NOT NULL,
    stage         TEXT NOT NULL,              -- submitted | document_verification | credit_assessment | approved | disbursed | rejected
    stage_updated_on TEXT NOT NULL,
    pending_action TEXT,                      -- what the customer still has to provide; NULL = nothing recorded
    first_emi_on  TEXT                        -- set only once disbursed
);

CREATE TABLE IF NOT EXISTS transactions (
    id            TEXT PRIMARY KEY,           -- T + digits
    account_id    TEXT NOT NULL REFERENCES accounts(id),
    posted_on     TEXT NOT NULL,
    description   TEXT NOT NULL,
    channel       TEXT NOT NULL,              -- UPI | CARD | ATM | NEFT | IMPS | EMI | SALARY | CHARGE
    amount_paise  INTEGER NOT NULL,           -- negative = debit
    flag          TEXT,                       -- NULL | suspicious
    status        TEXT NOT NULL DEFAULT 'posted' -- posted | pending | failed (as recorded; the cause is not stored)
);
CREATE INDEX IF NOT EXISTS ix_txn_account_date ON transactions(account_id, posted_on DESC);

CREATE TABLE IF NOT EXISTS tickets (
    id            TEXT PRIMARY KEY,           -- SR-YYMMDD-NNNN
    customer_id   TEXT REFERENCES customers(id),
    category      TEXT NOT NULL,
    summary       TEXT NOT NULL,
    status        TEXT NOT NULL,              -- open | in_progress | resolved
    sla_due_at    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL               -- ai_agent | human:<id> | seed
);

CREATE TABLE IF NOT EXISTS disputes (
    id            TEXT PRIMARY KEY,
    txn_id        TEXT NOT NULL REFERENCES transactions(id),
    customer_id   TEXT NOT NULL REFERENCES customers(id),
    reason        TEXT NOT NULL,
    ticket_id     TEXT NOT NULL REFERENCES tickets(id),
    created_at    TEXT NOT NULL,
    UNIQUE (txn_id)
);

CREATE TABLE IF NOT EXISTS handoffs (
    id            TEXT PRIMARY KEY,
    call_id       TEXT NOT NULL,
    customer_id   TEXT,
    reason        TEXT NOT NULL,
    packet_json   TEXT NOT NULL,              -- summary, intent, sentiment, verified, fields (PII-masked)
    status        TEXT NOT NULL,              -- waiting | accepted | closed
    created_at    TEXT NOT NULL,
    accepted_by   TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    at            TEXT NOT NULL,
    call_id       TEXT,
    kind          TEXT NOT NULL,              -- tool_proposed | policy_decision | tool_result | guardrail_hit | handoff | resolved ...
    detail_json   TEXT NOT NULL               -- PII-masked
);
CREATE INDEX IF NOT EXISTS ix_audit_call ON audit_events(call_id, seq);
-- The audit trail is append-only at the database level, not just by convention.
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
