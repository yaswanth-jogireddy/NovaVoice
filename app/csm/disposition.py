"""Call disposition: one typed record per call, written when the call ends.

A call can carry several requests, so the record is a list: each user intent with its own result
(solved / ticket created / transferred / not solved), plus an overall outcome derived from that list.

The labels come from Jev (TypeSafe's System One classifier, https://api.typesafe.ai/v1/systemone), as in
the jev-experimentation disposition runs: one request per call, the transcript as one plain string in
`state`, and one `choice` question per intent whose options include NOT_ASKED. Only the transcript is sent,
no tool calls or tool responses. Jev's answers are validated into the types below. Without JEV_API_KEY, or
if the request fails, the record comes from call state instead (rules).
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel

from csm import guardrails, policy
from utils.logger import get_logger

logger = get_logger(__name__)


class Intent(str, Enum):
    LOAN_APPLICATION = "loan_application"
    EMI_ENQUIRY = "emi_enquiry"
    EMI_ISSUE = "emi_issue"
    LOAN_DOCUMENT = "loan_document"
    BALANCE = "balance"
    TRANSACTIONS = "transactions"
    DISPUTE = "dispute"
    CARD = "card"
    STATEMENT = "statement"
    COMPLAINT = "complaint"
    REQUEST_STATUS = "request_status"
    PROFILE = "profile"
    PRODUCT_QUESTION = "product_question"
    HUMAN = "human"
    OTHER = "other"


# (label, what the caller asks about): the label for the dashboard, the definition for Jev.
INTENTS = {
    Intent.LOAN_APPLICATION: ("Loan application status", "the status of a loan application they submitted: its stage, "
                              "approval, documents needed, disbursal or first instalment"),
    Intent.EMI_ENQUIRY: ("EMI / repayment enquiry", "their loan EMI or instalment details: the next amount or due date, "
                         "an overdue amount, the outstanding balance or the repayment schedule (not a problem with it)"),
    Intent.EMI_ISSUE: ("EMI issue", "a problem with their EMI: a wrong amount, debited twice, paid but not updated, or a "
                       "wrong due date"),
    Intent.LOAN_DOCUMENT: ("Loan document", "being sent a loan document such as the repayment schedule or loan statement"),
    Intent.BALANCE: ("Account balance", "their account balance"),
    Intent.TRANSACTIONS: ("Transactions / payment status", "seeing their recent transactions, or whether a payment or "
                          "transfer they made went through (not an EMI)"),
    Intent.DISPUTE: ("Transaction dispute", "disputing a card or account debit they don't recognise (not an EMI problem)"),
    Intent.CARD: ("Card", "a lost, stolen or blocked card, blocking a card, or a card's status"),
    Intent.STATEMENT: ("Account statement", "an account statement"),
    Intent.COMPLAINT: ("Complaint / service request", "raising a complaint or a service request, such as an address or "
                       "contact change"),
    Intent.REQUEST_STATUS: ("Service request status", "the status of a service request, ticket or complaint they raised "
                            "earlier (not a loan application)"),
    Intent.PROFILE: ("Profile / customer ID", "their profile details or a forgotten customer ID"),
    Intent.PRODUCT_QUESTION: ("Product / policy question", "a general question about bank products, charges, rules or "
                              "processes, not about their own account"),
    Intent.HUMAN: ("Speak to a person", "speaking to a human agent or representative"),
    Intent.OTHER: ("Other", "a banking request that fits none of these: loan applications, EMIs, loan documents, balance, "
                   "transactions, disputes, cards, statements, complaints, request status, profile, product questions "
                   "or speaking to a person"),
}


class Resolution(str, Enum):
    SOLVED = "solved"
    TICKET = "ticket_created"
    TRANSFERRED = "transferred"
    NOT_SOLVED = "not_solved"


class Outcome(str, Enum):
    RESOLVED = "resolved_by_ai"
    REQUEST_RAISED = "request_raised"
    PARTLY_RESOLVED = "partly_resolved"
    EMI_TICKET = "emi_ticket_call_ended"
    TRANSFER_CALLER = "transferred_caller_request"
    TRANSFER_SECURITY = "transferred_security"
    TRANSFER_OTHER = "transferred_other"
    VERIFICATION_FAILED = "verification_failed"
    UNRESOLVED = "unresolved"
    NO_REQUEST = "no_request"


RESOLUTION_LABELS = {Resolution.SOLVED: "Solved", Resolution.TICKET: "Ticket created",
                     Resolution.TRANSFERRED: "Transferred", Resolution.NOT_SOLVED: "Not solved"}
LABELS = {Outcome.RESOLVED: "Resolved by AI", Outcome.REQUEST_RAISED: "Ticket raised",
          Outcome.PARTLY_RESOLVED: "Partly resolved", Outcome.EMI_TICKET: "EMI issue: ticket raised, call ended",
          Outcome.TRANSFER_CALLER: "Transferred: caller asked", Outcome.TRANSFER_SECURITY: "Transferred: security risk",
          Outcome.TRANSFER_OTHER: "Transferred to a colleague", Outcome.VERIFICATION_FAILED: "Verification failed",
          Outcome.UNRESOLVED: "Not resolved", Outcome.NO_REQUEST: "No request"}

# Rules fallback: the orchestrator's tool names (and its two non-tool intents) as dashboard intents.
TOOL_INTENT = {"loan_application_status": Intent.LOAN_APPLICATION, "loan_emi_info": Intent.EMI_ENQUIRY,
               "report_emi_issue": Intent.EMI_ISSUE, "request_loan_document": Intent.LOAN_DOCUMENT,
               "get_balance": Intent.BALANCE, "recent_transactions": Intent.TRANSACTIONS, "raise_dispute": Intent.DISPUTE,
               "block_card": Intent.CARD, "list_cards": Intent.CARD, "request_statement": Intent.STATEMENT,
               "create_ticket": Intent.COMPLAINT, "ticket_status": Intent.REQUEST_STATUS,
               "customer_profile": Intent.PROFILE, "handoff_to_human": Intent.HUMAN, "out_of_scope": Intent.OTHER}
TICKET_TOOLS = {"create_ticket", "report_emi_issue", "request_statement", "request_loan_document", "raise_dispute",
                "block_card"}
_HANDOFF_OUTCOME = {"caller_request": Outcome.TRANSFER_CALLER, "risk_flag": Outcome.TRANSFER_SECURITY,
                    "identity_failed": Outcome.VERIFICATION_FAILED}


class IntentOutcome(BaseModel):
    intent: Intent
    resolution: Resolution
    ticket_id: str = ""
    confidence: float | None = None   # Jev's confidence in this answer; None from rules

    def public(self) -> dict:
        return {**self.model_dump(mode="json"), "intent_label": INTENTS[self.intent][0], "query": INTENTS[self.intent][0],
                "resolution_label": RESOLUTION_LABELS[self.resolution]}


class Disposition(BaseModel):
    outcome: Outcome
    intents: list[IntentOutcome]
    customer_query: str            # everything the caller wanted, in a few words
    resolved: bool
    sentiment: Literal["positive", "neutral", "negative"]
    follow_up: str = ""            # what still has to happen, or empty
    source: Literal["jev", "rules"] = "rules"
    note: str = ""                 # who labelled it, and why it fell back to the call record if it did

    def public(self) -> dict:
        return {**self.model_dump(mode="json"), "label": LABELS[self.outcome],
                "intents": [i.public() for i in self.intents]}


def _record(items: list[IntentOutcome], outcome: Outcome, sentiment: str, source: str, tickets=()) -> Disposition:
    open_items = [INTENTS[i.intent][0] for i in items if i.resolution == Resolution.NOT_SOLVED]
    follow_up = ("A colleague picks up the call." if any(i.resolution == Resolution.TRANSFERRED for i in items)
                 else f"Open request {', '.join(tickets)}." if tickets
                 else "Not done: " + ", ".join(open_items) + "." if open_items else "")
    return Disposition(outcome=outcome, intents=items, sentiment=sentiment, source=source, follow_up=follow_up,
                       customer_query=", ".join(INTENTS[i.intent][0] for i in items) or "No request",
                       resolved=outcome in (Outcome.RESOLVED, Outcome.REQUEST_RAISED, Outcome.EMI_TICKET))


def overall(items: list[IntentOutcome]) -> Outcome:
    """The call's outcome from its intents' results."""
    results = {i.resolution for i in items}
    if Resolution.TRANSFERRED in results:
        return Outcome.TRANSFER_CALLER if any(i.intent == Intent.HUMAN for i in items) else Outcome.TRANSFER_OTHER
    if any(i.intent == Intent.EMI_ISSUE and i.resolution == Resolution.TICKET for i in items):
        return Outcome.EMI_TICKET
    if not results:
        return Outcome.NO_REQUEST
    if results == {Resolution.SOLVED}:
        return Outcome.RESOLVED
    if Resolution.NOT_SOLVED not in results:
        return Outcome.REQUEST_RAISED
    return Outcome.UNRESOLVED if results == {Resolution.NOT_SOLVED} else Outcome.PARTLY_RESOLVED


# -- rules: from call state, when Jev is not configured or fails -------------------------------------
def from_state(state) -> Disposition:
    s = state
    done: dict[str, list[str]] = {}
    for action in s.actions:
        done.setdefault(action["tool"], []).append(action["summary"])
    items: dict[Intent, IntentOutcome] = {}
    for name in dict.fromkeys(s.intents):
        if name == "verify_identity":
            continue
        intent = TOOL_INTENT.get(name, Intent.OTHER)
        ticket = next((t for summary in reversed(done.get(name, [])) for t in _ticket_ids(summary)), "")
        if name in done:
            resolution = Resolution.TICKET if name in TICKET_TOOLS and ticket else Resolution.SOLVED
        elif s.handed_off:
            resolution = Resolution.TRANSFERRED
        else:
            resolution = Resolution.NOT_SOLVED   # held for verification and never run, declined, or failed
        if intent not in items or items[intent].resolution == Resolution.NOT_SOLVED:   # e.g. list_cards then block_card
            items[intent] = IntentOutcome(intent=intent, resolution=resolution, ticket_id=ticket)
    if s.kb_docs and Intent.PRODUCT_QUESTION not in items:
        items[Intent.PRODUCT_QUESTION] = IntentOutcome(intent=Intent.PRODUCT_QUESTION, resolution=Resolution.SOLVED)
    found = list(items.values())
    if s.handed_off:
        outcome = _HANDOFF_OUTCOME.get(s.handoff_reason, Outcome.TRANSFER_OTHER)
    elif s.identity == policy.LOCKED:
        outcome = Outcome.VERIFICATION_FAILED
    else:
        outcome = overall(found)
    return _record(found, outcome, "negative" if s.negative_turns else "neutral", "rules", s.tickets)


def _ticket_ids(text: str) -> list[str]:
    return re.findall(r"SR-\d{6}-\d{4}", text)


# -- Jev ---------------------------------------------------------------------------------------------
# One `choice` per request slot, over every intent x result (as the reference runs pick one label from all of
# them): a request lands in exactly one category, and its result comes in the same answer.
MAX_REQUESTS = 4
NONE = "NONE"
RESULTS = {
    "SOLVED": (Resolution.SOLVED, "the Assistant answered or completed it on the call"),
    "TICKET_CREATED": (Resolution.TICKET, "the Assistant said a ticket or service request was raised for it"),
    "TRANSFERRED": (Resolution.TRANSFERRED, "the call was transferred to a human agent for it"),
    "NOT_SOLVED": (Resolution.NOT_SOLVED, "it was not answered or completed: refused, unclear, identity not verified, "
                                          "or the call ended first"),
}
_SENTIMENT = {"POSITIVE": "positive", "NEUTRAL": "neutral", "NEGATIVE": "negative"}
_ORDINAL = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th"}


def request_criteria() -> dict:
    """{"balance__SOLVED": "The User asked about their account balance, and the Assistant answered ...", ..., "NONE": ...}"""
    out = {f"{intent.value}__{key}": f"The User asked about {definition}, and {text}."
           for intent, (_, definition) in INTENTS.items() for key, (_, text) in RESULTS.items()}
    out[NONE] = "The User made fewer requests than this."
    return out


def questions() -> dict:
    criteria = request_criteria()
    out = {f"request_{n}": {
        "type": "choice",
        "instructions": f"List the User's distinct banking requests in `transcript` in the order they made them. A "
                        "request repeated or corrected later counts once, with its final result. A greeting, thanks, "
                        "goodbye, or the identity details the User gives are not requests. Label what the User asked "
                        "for, not what the Assistant discussed. A reported problem with an EMI is an EMI issue, not a "
                        "transaction or dispute. The transcript may mix English and Hindi. "
                        f"Which label fits the User's {_ORDINAL[n]} request and what happened to it?",
        "criteria": criteria} for n in range(1, MAX_REQUESTS + 1)}
    out["caller_mood"] = {"type": "choice", "instructions": "How did the User sound by the end of `transcript`?",
                          "criteria": {"POSITIVE": "Satisfied or thankful.", "NEUTRAL": "Neither pleased nor upset.",
                                       "NEGATIVE": "Frustrated, angry or upset."}}
    return out


def transcript_text(history: list[dict]) -> str:
    """The plain-string transcript Jev reads ("User: ...\\nAssistant: ..."): the format the runs found cheapest.
    Redacted first: it leaves for an external service, and numbers and identity details don't change the labels."""
    return "\n".join(f"{'User' if m['role'] == 'user' else 'Assistant'}: {guardrails.redact(str(m['content']))}"
                     for m in history
                     if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip())


def from_answers(answers: dict) -> Disposition:
    """Jev's answers as a typed record. Raises (KeyError, ValueError) on an answer outside the question's options.
    The same intent in two slots keeps its later result (asked, verified, then answered). A NONE slot is skipped,
    not the end: each slot is answered on its own, so request_3 can hold a request after a NONE in request_2."""
    items: dict[Intent, IntentOutcome] = {}
    for n in range(1, MAX_REQUESTS + 1):
        answer = answers[f"request_{n}"]
        if answer["choice"] == NONE:
            continue
        intent, _, result = answer["choice"].partition("__")
        item = IntentOutcome(intent=Intent(intent), resolution=RESULTS[result][0], confidence=answer.get("confidence"))
        items.pop(item.intent, None)
        items[item.intent] = item
    mood = _SENTIMENT.get((answers.get("caller_mood") or {}).get("choice"), "neutral")
    found = list(items.values())
    return _record(found, overall(found), mood, "jev")


async def _post(endpoint: str, key: str, body: dict, timeout_s: float) -> dict:
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as session:
        async with session.post(endpoint, json=body, headers={"Authorization": f"Bearer {key}",
                                                              "Accept": "application/json"}) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            return await response.json()


async def classify(history: list[dict], state, settings, timeout_s: float = 15.0) -> Disposition:
    """The call's disposition from Jev, or from call state when Jev isn't configured or fails. `note` says which,
    and why it fell back, so the summary never leaves the source in doubt."""
    transcript = transcript_text(history)
    if not settings.jev_api_key or not transcript:
        why = "JEV_API_KEY is not set" if not settings.jev_api_key else "the call has no transcript"
        return from_state(state).model_copy(update={"note": f"Call record ({why})"})
    body = {"model": settings.jev_model, "state": {"transcript": transcript}, "questions": questions()}
    try:
        reply = await _post(settings.jev_endpoint, settings.jev_api_key, body, timeout_s)
        return from_answers(reply["answers"]).model_copy(update={"note": f"Jev {settings.jev_model} (TypeSafe)"})
    except Exception as exc:   # never let labelling break the end of a call
        logger.warning("disposition_fallback", code=type(exc).__name__, detail=str(exc)[:200])
        return from_state(state).model_copy(update={"note": f"Call record (Jev failed: {type(exc).__name__} {str(exc)[:80]})".strip()})

