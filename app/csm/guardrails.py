"""Guardrails around every turn. Rules first: fast (<1 ms), explainable, and testable.

inbound(text)            -> cues in what the caller said (injection, social engineering, wants a human,
                            out of scope, negative mood). Always on: these are security basics.
redact(text)             -> PII-masked copy for logs, audit and the stored transcript.
outbound(sentence, ...)  -> what the agent may actually say: no full numbers, never an OTP, no claim
                            of an action that no tool performed, no credit decisions, no unsupported figures.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from csm.spoken import spoken_numbers
from utils.pii_redactor import PIIRedactor

# --- inbound -------------------------------------------------------------------------------------
_I = re.IGNORECASE
INJECTION = re.compile(
    r"\b(?:ignore|disregard|forget|override)\b.{0,30}\b(?:instructions?|rules?|prompts?|guidelines|polic(?:y|ies))\b"
    r"|\bsystem prompt\b|\bdeveloper mode\b|\bjailbreak\b|\bsudo\b|\badmin mode\b"
    r"|\byou are now\b|\bact as (?:an? )?(?:admin|administrator|developer|manager|supervisor|bank officer)\b"
    r"|\b(?:reveal|print|show)\b.{0,15}\b(?:your|the) (?:prompt|instructions)\b"
    r"|\b(?:show|list|give|read|tell)\b.{0,20}\b(?:all|every|other) (?:accounts?|customers?|users?)\b"
    r"|\banother customer'?s\b|\bsomeone else'?s account\b", _I)
SECRET_REQUEST = re.compile(
    r"\b(?:read|tell|give|share|say|forward|what(?:'s| is))\b.{0,25}\b(?:otp|one[- ]time (?:code|password)|pin|cvv|password|verification code)\b"
    r"|\b(?:otp|pin|cvv)\b.{0,20}\b(?:bata|batao|bataiye|bolo|boliye|de do|dedo|share karo)\b"
    r"|(?:ओटीपी|otp|पिन).{0,20}(?:बताओ|बताइए|बता दो|बोलो|दे दो)", _I)
THIRD_PARTY = re.compile(
    r"\b(?:on behalf of|calling for)\b"
    r"|\bfor my (?:father|mother|mom|mum|dad|son|daughter|wife|husband|brother|sister|grandmother|grandfather|boss|friend|uncle|aunt)\b"
    r"|\bi(?:'m| am) (?:his|her|their) (?:son|daughter|wife|husband|brother|sister|nephew|niece|relative|friend)\b"
    r"|\bmy (?:father|mother|mom|mum|dad|son|daughter|wife|husband|brother|sister|grandmother|grandfather)'?s? account\b"
    r"|\b(?:papa|mummy|maa|mammi|bhai|didi) (?:ka|ki|ke) (?:account|khata|card)\b"
    r"|(?:मेरी माँ|मेरे पिता|मेरे पापा|मम्मी|पापा) (?:का|की|के) (?:खाता|अकाउंट|कार्ड)", _I)
CALLER_SECRET = re.compile(r"\b(?:my|mera|meri)\s+(?:pin|cvv|password|net ?banking password)\b.{0,12}\d", _I)
WANTS_HUMAN = re.compile(
    r"\b(?:human|real person|live (?:agent|person)|representative|customer care (?:executive|officer)|"
    r"speak to (?:someone|somebody|a person|an agent|an officer|your manager)|talk to (?:someone|somebody|a person|an agent|an officer|your manager)|"
    r"(?:connect|transfer) me to (?:an? )?(?:agent|person|officer|executive|human|colleague|specialist))\b"
    r"|\b(?:insaan|kisi (?:insaan|aadmi|agent) se)\b|इंसान|किसी (?:व्यक्ति|एजेंट) से", _I)
OUT_OF_SCOPE = re.compile(
    r"\b(?:approve|sanction)\b.{0,20}\b(?:loan|credit|limit)\b|\b(?:loan|credit) approv"
    r"|\bam i eligible\b|\beligib(?:le|ility) for (?:a |the )?(?:loan|credit)"
    r"|\b(?:increase|raise|enhance)\b.{0,15}\b(?:credit )?limit\b"
    r"|\bshould i (?:invest|buy|put|take)\b|\b(?:investment|financial|tax) advice\b"
    r"|\bwhich (?:mutual fund|stock|shares?|fund) (?:should|to)\b"
    r"|\bwaive\b.{0,20}\b(?:charge|fee|penalty|interest)s?\b"
    r"|\b(?:loan|limit) (?:pass|approve) (?:kar|karo|kardo|kar do)", _I)
# "Is my loan approved?" / "status of my loan approval" asks what is on record, not for a decision.
STATUS_ENQUIRY = re.compile(r"\b(?:status|stage|update)\b|\b(?:is|has|was) (?:my|the)\b[^.?]{0,30}\b(?:approved|sanctioned)\b", _I)
NEGATIVE = re.compile(
    r"\b(?:angry|furious|frustrat\w*|fed up|worst|useless|ridiculous|pathetic|terrible|horrible|disgusting|"
    r"stupid|nonsense|unacceptable|third time|again and again|wasting my time|bakwas|bekaar|pareshan)\b"
    r"|गुस्सा|बकवास|बेकार|परेशान|!!", _I)


@dataclass
class Inbound:
    redacted: str
    hits: list = field(default_factory=list)      # ["injection", "secret_request", "third_party", "caller_secret"]
    wants_human: bool = False
    out_of_scope: bool = False
    negative: bool = False

    @property
    def high_risk(self) -> bool:
        return bool({"injection", "secret_request", "third_party"} & set(self.hits))


def inbound(text: str) -> Inbound:
    text = text or ""
    hits = [kind for kind, rx in (("injection", INJECTION), ("secret_request", SECRET_REQUEST),
                                   ("third_party", THIRD_PARTY), ("caller_secret", CALLER_SECRET)) if rx.search(text)]
    return Inbound(redacted=redact(text), hits=hits, wants_human=bool(WANTS_HUMAN.search(text)),
                   out_of_scope=bool(OUT_OF_SCOPE.search(text)) and not STATUS_ENQUIRY.search(text), negative=bool(NEGATIVE.search(text)))


# --- redaction (logs, audit, stored transcripts) --------------------------------------------------
_redactor = PIIRedactor()
_KEEP = re.compile(r"\bSR-\d{6}-\d{4}\b|\bLA-\d{4}-\d{4}\b|\bT\d{6,9}\b|\bC\d{4}\b")
_MONTHS = (r"(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*|जनवरी|फ़रवरी|फरवरी|मार्च|अप्रैल|मई|जून|"
           r"जुलाई|अगस्त|सितंबर|सितम्बर|अक्टूबर|नवंबर|नवम्बर|दिसंबर|दिसम्बर)")
_YEAR_RX = r"'?\d{2}(?:\d{2})?\b"   # 1992 or 92, as the identity parser accepts both
_DATES = re.compile(
    r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b"
    rf"|\b\d{{1,2}}(?:st|nd|rd|th)?\s*(?:of\s+)?{_MONTHS}\.?,?\s*{_YEAR_RX}"
    rf"|\b{_MONTHS}\.?,?\s*(?:the\s+)?\d{{1,2}}(?:st|nd|rd|th)?,?\s*{_YEAR_RX}", _I)
_LONG_DIGITS = re.compile(r"(?<!\d)(?<!\d[,.])\d(?:[\s-]?\d){3,}(?!\d|[,.]\d)")


def redact(text: str) -> str:
    """Mask PII for storage: cards, phones, emails, PAN, Aadhaar, dates, and any 4+ digit run
    (account / card last 4 and OTPs included). Ticket, transaction and customer ids are kept.
    Details spoken as words ("seven three zero six", "fourteenth March nineteen ninety two") are
    masked too: if the digit form hides more, the digit form is what gets stored."""
    if not text:
        return text
    masked = _mask(text)
    spoken = spoken_numbers(text)
    if spoken != text:
        alt = _mask(spoken)
        if len(_MASK_TOKEN.findall(alt)) > len(_MASK_TOKEN.findall(masked)):
            return alt
    return masked


_MASK_TOKEN = re.compile(r"\[[A-Z_]+\]")


def _mask(text: str) -> str:
    kept: list[str] = []

    def keep(m):
        kept.append(m.group(0))
        return f"\x00{len(kept) - 1}\x00"
    out = _KEEP.sub(keep, text)
    out = _DATES.sub("[DATE]", out)
    out = _redactor.redact(out)
    out = _LONG_DIGITS.sub("[NUM]", out)
    return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], out)


SENSITIVE_ARGS = {"dob", "last4", "code", "customer_ref"}


def redact_args(args: dict) -> dict:
    """Tool arguments safe for the audit log: identity factors and codes never stored."""
    out = {}
    for key, value in (args or {}).items():
        if key == "customer_ref":   # a customer id is not secret; a mobile number is
            out[key] = value if re.fullmatch(r"C\d{4}", str(value)) else "[REDACTED]"
        elif key in SENSITIVE_ARGS:
            out[key] = "[REDACTED]"
        else:
            out[key] = redact(value) if isinstance(value, str) else value
    return out


# --- outbound ------------------------------------------------------------------------------------
_NEG = re.compile(r"\b(?:not|n't|cannot|can't|won't|haven't|hasn't|unable|yet to|once|after|before|if)\b", _I)
CLAIMS = [
    ("block_card", re.compile(r"\b(?:i(?:'ve| have)? blocked|has been blocked|is (?:now )?blocked|blocked (?:it|your card|the card))\b", _I)),
    ("raise_dispute", re.compile(r"\bdispute (?:is|has been|was) (?:raised|registered|logged|filed)|\b(?:raised|registered|logged|filed) (?:a|the|your) dispute\b", _I)),
    ("request_statement", re.compile(r"\bstatement\b.{0,40}\b(?:sent|emailed|ordered|requested|on its way|will (?:arrive|reach))\b|\b(?:sent|emailed|ordered|requested) (?:you )?(?:your|the|a) statement\b", _I)),
    ("ticket", re.compile(r"\b(?:raised|created|logged|registered|opened) (?:a|an|the|your) (?:ticket|service request|complaint)\b|\b(?:ticket|request) (?:number|id) is\b", _I)),
    ("handoff", re.compile(r"\b(?:transferr\w*|connect\w*) you (?:to|with) (?:a |an |our |my )?(?:human|colleague|agent|team|person|specialist|officer)\b", _I)),
]
OFFER = re.compile(r"\b(?:would you like|shall i|should i|do you want|want me to)\b", _I)
CREDIT_CLAIM = re.compile(
    r"\byou(?:'re| are) (?:eligible|approved|pre-approved)\b|\b(?:loan|limit|credit)\b.{0,20}\b(?:is|has been|was) (?:approved|sanctioned|increased)\b"
    r"|\bi (?:recommend|suggest|advise)\b.{0,20}\b(?:invest|buy|mutual fund|stock|shares)\b", _I)
# Saying an application is approved is allowed only when the Loan Origination System says so.
_APPROVAL_ON_RECORD = re.compile(r"\b(?:is|has been|was) (?:approved|sanctioned)\b", _I)
_SIX = re.compile(r"(?<!\d)\d{6}(?!\d)")
_FULL_NUMBER = re.compile(r"(?<![\d,.])\d(?:[ -]?\d){6,}(?![\d,.])")
_FIGURE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> set[str]:
    """Every number form a speaker might use for figures in `text` (whole, integer part, comma groups)."""
    out: set[str] = set()
    for raw in _FIGURE.findall(text or ""):
        raw = raw.strip(",.")
        flat = raw.replace(",", "")
        out.update({flat, flat.split(".")[0], *raw.replace(".", ",").split(",")})
        for part in re.split(r"\D+", raw):
            if part:
                out.add(part)
    return {n.lstrip("0") or "0" for n in out if n}


@dataclass
class Outbound:
    text: str
    hits: list = field(default_factory=list)


def outbound(sentence: str, *, done: set, otp_check, allowed_numbers: set, lines: dict) -> Outbound:
    """Check one sentence the agent is about to say.

    done            names of actions that really happened in this call ("block_card", "ticket", "handoff" ...)
    otp_check(d)    True if a 6-digit string is the live OTP
    allowed_numbers numbers present in tool facts or the caller's own words
    lines           persona lines for replacements (language-specific)
    """
    hits = []
    if any(otp_check(m) for m in _SIX.findall(sentence)):
        return Outbound(lines["no_otp"], ["otp_spoken"])
    text = _FULL_NUMBER.sub(lambda m: "ending " + re.sub(r"\D", "", m.group(0))[-4:], sentence)
    if text != sentence:
        hits.append("full_number_masked")
    if CREDIT_CLAIM.search(_APPROVAL_ON_RECORD.sub(" ", text) if "approved_on_record" in done else text):
        return Outbound(lines["no_credit_decision"], hits + ["credit_claim"])
    # An offer or a question ("Would you like me to connect you…?") claims nothing.
    offer = text.rstrip().endswith("?") or OFFER.search(text)
    for action, rx in ([] if offer else CLAIMS):
        if rx.search(text) and not _NEG.search(text) and action not in done:
            return Outbound(lines["unbacked_claim"], hits + [f"unbacked_claim:{action}"])
    # full numbers were masked above; every other figure must trace back to facts or the caller
    figures = {n.replace(",", "").split(".")[0].lstrip("0") for n in _FIGURE.findall(_FULL_NUMBER.sub(" ", sentence))}
    unsupported = [f for f in figures if len(f) >= 3 and f not in allowed_numbers]
    if unsupported:
        return Outbound(lines["unsupported_figure"], hits + ["unsupported_figure"])
    return Outbound(text, hits)
