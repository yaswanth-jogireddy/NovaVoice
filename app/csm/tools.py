"""Tool contracts (HLD section 6). The LLM fills these schemas; it never executes anything.

Each ToolSpec carries the fields the tool-contract brief requires: permission level, side
effects, idempotency and the failure codes a call can return. `registry()` filters by feature
flag, so a tool that is off is never even shown to the model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Machine-readable failure codes (never prose).
INVALID_INPUT = "INVALID_INPUT"
NOT_AUTHORIZED = "NOT_AUTHORIZED"
RATE_LIMIT = "RATE_LIMIT"
PROVIDER_FAILURE = "PROVIDER_FAILURE"
RESOURCE_UNAVAILABLE = "RESOURCE_UNAVAILABLE"
HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"

PUBLIC, VERIFIED, STEP_UP = "public", "verified", "step_up"

TICKET_CATEGORIES = ["statement", "card", "dispute", "loan", "kyc", "complaint", "general", "account"]
LOAN_PRODUCTS = ["personal", "vehicle", "home"]
EMI_ISSUES = ["wrong_amount", "double_debit", "payment_not_updated", "wrong_due_date", "other"]
_PRODUCT = {"type": "string", "enum": LOAN_PRODUCTS, "description": "Only if the caller named the loan type"}
HANDOFF_REASONS = ["caller_request", "identity_failed", "risk_flag", "out_of_scope", "negative_sentiment",
                   "complex_issue", "tool_failure"]


class ToolInputError(ValueError):
    code = INVALID_INPUT


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    feature: str                  # capability flag that registers the tool
    permission: str = PUBLIC      # public | verified | step_up
    side_effects: str = "none"
    idempotent: bool = True
    version: int = 1
    failure_codes: tuple = (INVALID_INPUT, PROVIDER_FAILURE)
    hidden: bool = False          # handled by deterministic code, not offered to the model

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


def _obj(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


_S = {"type": "string"}

TOOLS: dict[str, ToolSpec] = {t.name: t for t in [
    ToolSpec("search_policy", "Search Nova Trust Bank policy and FAQ documents (charges, card loss, disputes, "
             "statements, KYC, loans, branch hours, grievance). Use for any general bank question.",
             _obj({"query": {**_S, "description": "The caller's question in a few keywords"}}, ["query"]),
             feature="FAQ_KNOWLEDGE"),
    ToolSpec("create_ticket", "Raise a service request when the caller needs something you cannot do directly.",
             _obj({"category": {"type": "string", "enum": TICKET_CATEGORIES},
                   "summary": {**_S, "description": "One sentence, no account numbers"}}, ["category", "summary"]),
             feature="TICKETS", side_effects="inserts a ticket", idempotent=False,
             failure_codes=(INVALID_INPUT, RATE_LIMIT, PROVIDER_FAILURE)),
    ToolSpec("ticket_status", "Look up the status of an existing service request by its id, e.g. SR-261028-0042.",
             _obj({"ticket_id": {**_S, "description": "Ticket id like SR-YYMMDD-NNNN"}}, ["ticket_id"]),
             # A service request is a personal service record (Level 1 brief, req. 4): verify first.
             feature="TICKETS", permission=VERIFIED, failure_codes=(INVALID_INPUT, NOT_AUTHORIZED, RESOURCE_UNAVAILABLE)),
    ToolSpec("verify_identity", "Verify the caller. Call only when the caller has given BOTH: customer id or "
             "registered mobile, and date of birth.",
             _obj({"customer_ref": {**_S, "description": "Customer id like C1001, or a 10-digit mobile"},
                   "dob": {**_S, "description": "Date of birth as YYYY-MM-DD"},
                   "last4": {**_S, "description": "Not needed; ignored if given"}},
                  ["customer_ref", "dob"]),
             feature="ACCOUNT_TOOLS", failure_codes=(INVALID_INPUT, NOT_AUTHORIZED, RATE_LIMIT)),
    ToolSpec("check_otp", "Check a one-time code.", _obj({"code": _S}, ["code"]),
             feature="ACCOUNT_TOOLS", hidden=True, failure_codes=(INVALID_INPUT, NOT_AUTHORIZED)),
    ToolSpec("customer_profile", "The verified caller's own profile: customer ID (for callers who forgot it), "
             "KYC status, home city, preferred language, masked contact details.", _obj({}, []),
             feature="ACCOUNT_TOOLS", permission=VERIFIED, failure_codes=(NOT_AUTHORIZED, PROVIDER_FAILURE)),
    ToolSpec("list_cards", "The verified caller's cards and whether each is active or blocked.", _obj({}, []),
             feature="ACCOUNT_TOOLS", permission=VERIFIED, failure_codes=(NOT_AUTHORIZED, PROVIDER_FAILURE)),
    ToolSpec("get_balance", "Available balance of the verified caller's accounts.", _obj({}, []),
             feature="ACCOUNT_TOOLS", permission=VERIFIED, failure_codes=(NOT_AUTHORIZED, PROVIDER_FAILURE)),
    ToolSpec("recent_transactions", "The verified caller's most recent transactions.",
             _obj({"n": {"type": "integer", "minimum": 1, "maximum": 10, "description": "How many, default 5"}}, []),
             feature="ACCOUNT_TOOLS", permission=VERIFIED, failure_codes=(NOT_AUTHORIZED, PROVIDER_FAILURE)),
    ToolSpec("block_card", "Permanently block the verified caller's lost or stolen card and order a replacement.",
             _obj({"card_last4": {**_S, "description": "Last 4 digits of the card; empty if the caller has one card"},
                   "reason": {"type": "string", "enum": ["lost", "stolen", "damaged", "suspected_fraud"]}},
                  ["reason"]),
             feature="ACCOUNT_TOOLS", permission=STEP_UP, side_effects="blocks the card, raises a replacement ticket",
             idempotent=True, failure_codes=(INVALID_INPUT, NOT_AUTHORIZED, RESOURCE_UNAVAILABLE)),
    ToolSpec("request_statement", "Order an account statement for the verified caller.",
             _obj({"period": {**_S, "description": "e.g. last 3 months, October 2026"},
                   "channel": {"type": "string", "enum": ["email", "post", "branch"]}}, ["period", "channel"]),
             feature="ACCOUNT_TOOLS", permission=VERIFIED, side_effects="inserts a statement ticket", idempotent=False,
             failure_codes=(INVALID_INPUT, NOT_AUTHORIZED, RATE_LIMIT)),
    ToolSpec("loan_emi_info", "The verified caller's loan accounts (Loan Management System): next instalment / EMI "
             "date and amount, any overdue amount, outstanding balance, repayment schedule.",
             _obj({"product": _PRODUCT}, []),
             feature="ACCOUNT_TOOLS", permission=VERIFIED, failure_codes=(NOT_AUTHORIZED, PROVIDER_FAILURE)),
    ToolSpec("report_emi_issue", "The verified caller says something is WRONG with their EMI (wrong amount, debited twice, "
             "paid but not updated, wrong due date): raise a loan ticket. Not for a plain EMI question.",
             _obj({"issue": {"type": "string", "enum": EMI_ISSUES}, "product": _PRODUCT,
                   "summary": {**_S, "description": "One sentence, no account numbers"}}, ["issue"]),
             feature="TICKETS", permission=VERIFIED, side_effects="inserts a loan ticket; the call then ends",
             idempotent=False, failure_codes=(INVALID_INPUT, NOT_AUTHORIZED, PROVIDER_FAILURE)),
    ToolSpec("loan_application_status", "Status of the verified caller's submitted loan applications (Loan Origination "
             "System): stage, action needed, when the first instalment starts.",
             _obj({"product": _PRODUCT}, []),
             feature="ACCOUNT_TOOLS", permission=VERIFIED, failure_codes=(NOT_AUTHORIZED, PROVIDER_FAILURE)),
    ToolSpec("request_loan_document", "Send the verified caller a loan document (repayment schedule or loan account "
             "statement) through a service request.",
             _obj({"doc": {"type": "string", "enum": ["repayment_schedule", "loan_statement"]}, "product": _PRODUCT,
                   "channel": {"type": "string", "enum": ["email", "post", "branch"]}}, ["doc"]),
             feature="ACCOUNT_TOOLS", permission=VERIFIED, side_effects="inserts a loan document ticket", idempotent=False,
             failure_codes=(INVALID_INPUT, NOT_AUTHORIZED, RESOURCE_UNAVAILABLE)),
    ToolSpec("raise_dispute", "Dispute a debit the verified caller does not recognise. Needs the transaction ref "
             "(from recent_transactions) like T777001.",
             _obj({"txn_ref": {**_S, "description": "Transaction ref like T777001"},
                   "reason": {"type": "string", "enum": ["not_recognised", "duplicate", "amount_wrong", "service_not_received"]}},
                  ["txn_ref", "reason"]),
             feature="ACCOUNT_TOOLS", permission=STEP_UP, side_effects="inserts a dispute and a ticket", idempotent=True,
             failure_codes=(INVALID_INPUT, NOT_AUTHORIZED, RESOURCE_UNAVAILABLE)),
    ToolSpec("handoff_to_human", "Transfer to a human agent: the caller asks for a person, is upset, or needs "
             "something outside your tools.",
             _obj({"reason": {"type": "string", "enum": HANDOFF_REASONS},
                   "summary": {**_S, "description": "One sentence for the human agent"}}, ["reason"]),
             feature="VOICE_CALL", side_effects="queues a handoff for the agent desk", idempotent=True,
             failure_codes=(HUMAN_APPROVAL_REQUIRED,)),
]}


def registry(flags, *, include_hidden: bool = False) -> list[ToolSpec]:
    """Tools whose capability flag is on. Off tools are absent from the model's tool list."""
    return [t for t in TOOLS.values() if flags.enabled(t.feature) and (include_hidden or not t.hidden)]


def schemas(specs: list[ToolSpec]) -> list[dict]:
    return [s.schema() for s in specs]


_CUSTOMER_REF = re.compile(r"^C\d{4}$|^\+?\d{10,13}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate(spec: ToolSpec, args: dict | None) -> dict:
    """Clean model-supplied arguments against the schema. Raises ToolInputError (INVALID_INPUT)."""
    args = dict(args or {})
    props = spec.parameters.get("properties", {})
    unknown = set(args) - set(props)
    if unknown:
        raise ToolInputError(f"unknown fields {sorted(unknown)}")
    clean = {}
    for name, rule in props.items():
        value = args.get(name)
        if value is None or value == "":
            if name in spec.parameters.get("required", []):
                raise ToolInputError(f"missing {name}")
            continue
        if rule.get("type") == "integer":
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ToolInputError(f"{name} must be an integer") from None
            value = max(rule.get("minimum", value), min(rule.get("maximum", value), value))
        else:
            value = str(value).strip()[:200]
            if "enum" in rule and value not in rule["enum"]:
                raise ToolInputError(f"{name} must be one of {rule['enum']}")
        clean[name] = value
    if spec.name == "verify_identity":
        clean["customer_ref"] = re.sub(r"[\s-]", "", clean["customer_ref"]).upper()
        if not _CUSTOMER_REF.match(clean["customer_ref"]) or not _DATE.match(clean["dob"]) \
                or ("last4" in clean and not re.fullmatch(r"\d{4}", clean["last4"][-4:])):
            raise ToolInputError("identity details are not in the expected format")
        clean.pop("last4", None)   # two factors: customer ref + date of birth
    if spec.name == "ticket_status":
        clean["ticket_id"] = re.sub(r"\s", "", clean["ticket_id"]).upper()
        if not re.fullmatch(r"SR-\d{6}-\d{4}", clean["ticket_id"]):
            raise ToolInputError("ticket id must look like SR-261028-0042")
    if spec.name == "block_card" and clean.get("card_last4"):
        digits = re.sub(r"\D", "", clean["card_last4"])
        if len(digits) < 4:
            raise ToolInputError("card_last4 must be 4 digits")
        clean["card_last4"] = digits[-4:]
    if spec.name == "raise_dispute":
        clean["txn_ref"] = clean["txn_ref"].upper().replace(" ", "")
        if not re.fullmatch(r"T\d{3,9}", clean["txn_ref"]):
            raise ToolInputError("txn_ref must look like T777001")
    return clean
