"""Deterministic policy engine: every proposed tool call is decided here, never by the model.

Identity state machine (HLD section 5):
    ANONYMOUS -> CLAIMED -> VERIFIED -> STEPPED_UP;   3 failed checks -> LOCKED (then handoff only)
Only functions in this module change `CallState.identity`. Pure code over plain state, so every
transition is unit-tested without a model or a database.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum

from csm.tools import PUBLIC, STEP_UP, VERIFIED, NOT_AUTHORIZED, RATE_LIMIT, ToolSpec

ANONYMOUS, CLAIMED, VERIFIED_STATE, STEPPED_UP, LOCKED = "ANONYMOUS", "CLAIMED", "VERIFIED", "STEPPED_UP", "LOCKED"

MAX_ID_FAILURES = 3
MAX_OTP_FAILURES = 3
MAX_OTP_SENDS = 3
OTP_TTL_S = 300
STEP_UP_TTL_S = 600          # a step-up covers risky actions for 10 minutes
MAX_TOOL_CALLS_PER_MIN = 12
MAX_SE_HITS_BEFORE_HANDOFF = 2


class Decision(str, Enum):
    ALLOW = "ALLOW"
    NEED_VERIFICATION = "NEED_VERIFICATION"
    STEP_UP = "STEP_UP"
    DENY = "DENY"
    HANDOFF = "HANDOFF"


@dataclass
class Verdict:
    decision: Decision
    reason: str
    code: str | None = None


@dataclass
class CallState:
    call_id: str
    language: str = "en"
    identity: str = ANONYMOUS
    customer_id: str | None = None
    first_name: str | None = None
    claimed_ref: str | None = None
    id_failures: int = 0
    otp_hash: str | None = None
    otp_salt: str = ""
    otp_expires: float = 0.0
    otp_failures: int = 0
    otp_sends: int = 0
    stepped_up_at: float = 0.0
    tool_times: list = field(default_factory=list)
    pending: list = field(default_factory=list)      # proposals waiting for verification / step-up
    se_hits: int = 0
    risk_flags: list = field(default_factory=list)
    intents: list = field(default_factory=list)
    actions: list = field(default_factory=list)       # successful tool results: {"tool", "summary", ...}
    tickets: list = field(default_factory=list)
    kb_docs: list = field(default_factory=list)
    negative_turns: int = 0
    offer: object = None                              # a Proposal Aria offered and the caller may accept
    turns: int = 0
    handed_off: bool = False
    handoff_reason: str | None = None
    started: float = field(default_factory=time.time)
    id_fields: dict = field(default_factory=dict)     # identity factors heard so far (cleared after each attempt)
    seen_txns: list = field(default_factory=list)     # transactions read out this call (for dispute refs)
    awaiting_identity: bool = False
    asked_field: str | None = None                    # the identity detail Aria asked for last (T15)
    loan_product: str | None = None                   # the loan being talked about ("send me the schedule")
    last_loan_intent: str | None = None               # repeated with a new product on "I meant my vehicle loan"

    @property
    def verified(self) -> bool:
        return self.identity in (VERIFIED_STATE, STEPPED_UP)

    def stepped_up(self, now: float) -> bool:
        return self.identity == STEPPED_UP and now - self.stepped_up_at <= STEP_UP_TTL_S


def decide(spec: ToolSpec, state: CallState, now: float | None = None) -> Verdict:
    """ALLOW / NEED_VERIFICATION / STEP_UP / DENY / HANDOFF for one proposed tool call."""
    now = time.time() if now is None else now
    if spec.name == "handoff_to_human":
        return Verdict(Decision.HANDOFF, "handoff requested")
    if state.identity == LOCKED:
        return Verdict(Decision.HANDOFF, "identity locked after failed checks", NOT_AUTHORIZED)
    # After a fraud or lockout handoff nothing more runs; after an ordinary handoff Aria keeps helping
    # while the colleague picks it up (live calls showed callers stuck at "a colleague will be with you").
    if state.handed_off and state.handoff_reason in ("risk_flag", "identity_failed"):
        return Verdict(Decision.DENY, "call handed to a human for security", NOT_AUTHORIZED)
    recent = [t for t in state.tool_times if now - t < 60]
    if len(recent) >= MAX_TOOL_CALLS_PER_MIN:
        return Verdict(Decision.DENY, "tool rate limit", RATE_LIMIT)
    if spec.name == "verify_identity":
        return Verdict(Decision.ALLOW, "verification attempt")
    if spec.permission == PUBLIC:
        return Verdict(Decision.ALLOW, "public tool")
    if not state.verified:
        return Verdict(Decision.NEED_VERIFICATION, "account data needs identity verification", NOT_AUTHORIZED)
    if spec.permission == VERIFIED:
        return Verdict(Decision.ALLOW, "caller verified")
    if spec.permission == STEP_UP:
        if state.stepped_up(now):
            return Verdict(Decision.ALLOW, "caller stepped up")
        if state.otp_sends >= MAX_OTP_SENDS or state.otp_failures >= MAX_OTP_FAILURES:
            return Verdict(Decision.HANDOFF, "step-up unavailable", NOT_AUTHORIZED)
        return Verdict(Decision.STEP_UP, "risky action needs a one-time code", NOT_AUTHORIZED)
    return Verdict(Decision.DENY, "unknown permission", NOT_AUTHORIZED)


def note_tool_call(state: CallState, now: float | None = None) -> None:
    now = time.time() if now is None else now
    state.tool_times = [t for t in state.tool_times if now - t < 60] + [now]


def claim(state: CallState, customer_ref: str) -> None:
    if state.identity == ANONYMOUS:
        state.identity = CLAIMED
    state.claimed_ref = customer_ref


def record_verification(state: CallState, ok: bool, customer_id: str | None = None,
                        first_name: str | None = None) -> str:
    """Apply a verification result. Returns the new identity state."""
    if state.identity == LOCKED:
        return LOCKED
    if ok:
        state.identity, state.customer_id, state.first_name = VERIFIED_STATE, customer_id, first_name
        state.id_failures = 0
    else:
        state.id_failures += 1
        state.identity = LOCKED if state.id_failures >= MAX_ID_FAILURES else (CLAIMED if state.claimed_ref else ANONYMOUS)
    return state.identity


def issue_otp(state: CallState, now: float | None = None) -> str:
    """Create a 6-digit code. Only a salted hash is kept; the caller of this function sends the code."""
    now = time.time() if now is None else now
    code = f"{secrets.randbelow(10**6):06d}"
    state.otp_salt = secrets.token_hex(8)
    state.otp_hash = _otp_digest(state.otp_salt, code)
    state.otp_expires = now + OTP_TTL_S
    state.otp_sends += 1
    return code


def check_otp(state: CallState, code: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    if not state.otp_hash or now > state.otp_expires or state.otp_failures >= MAX_OTP_FAILURES:
        return False
    if hmac.compare_digest(state.otp_hash, _otp_digest(state.otp_salt, (code or "").strip())):
        state.identity, state.stepped_up_at, state.otp_hash = STEPPED_UP, now, None
        return True
    state.otp_failures += 1
    return False


def otp_matches(state: CallState, digits: str) -> bool:
    """True if `digits` is the live OTP (used by the outbound guard to stop it being spoken)."""
    return bool(state.otp_hash) and hmac.compare_digest(state.otp_hash, _otp_digest(state.otp_salt, digits))


def _otp_digest(salt: str, code: str) -> str:
    return hashlib.sha256((salt + code).encode()).hexdigest()


HUMAN_WORDS = re.compile(r"\b(?:agent|person|human|someone|somebody|representative|executive|officer|colleague|"
                         r"manager|staff|insaan|aadmi)\b", re.I)
AFFIRM = re.compile(r"^\W*(?:yes|yeah|yep|yup|sure|ok(?:ay)?|please|go ahead|do it|connect me|haan|han|ji|theek hai|"
                    r"हाँ|हां|जी|ठीक है)\b", re.I)
DECLINE = re.compile(r"^\W*(?:no|nope|nah|not now|don'?t|nahi|nahin|नहीं)\b", re.I)


def model_handoff_now(reason: str, caller_text: str) -> bool:
    """The model proposed a transfer. Escalation is policy-owned: it transfers at once only when the
    caller's own words ask for a person; anything else becomes an offer the caller must accept.
    Live calls (30 Oct) showed the model transferring on "Customer." and on "open my account"."""
    return reason == "caller_request" and bool(HUMAN_WORDS.search(caller_text or ""))


def answers_offer(text: str) -> bool | None:
    """True = yes, False = no, None = the caller said something else (keep the offer open)."""
    short = len((text or "").split()) <= 6
    if short and AFFIRM.search(text or ""):
        return True
    if short and DECLINE.search(text or ""):
        return False
    return None


def register_se_hit(state: CallState, kind: str) -> bool:
    """Count a social-engineering / injection cue. True when the call must go to a human."""
    state.se_hits += 1
    if kind not in state.risk_flags:
        state.risk_flags.append(kind)
    return state.se_hits >= MAX_SE_HITS_BEFORE_HANDOFF
