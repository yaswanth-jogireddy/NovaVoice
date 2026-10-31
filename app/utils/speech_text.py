import re

# Short forms the LLM writes but a TTS voice reads as letter-strings. Spelled out
# here so the spoken line is always full words: "6 PM" was read "pee-em", "Dr."
# as "drr", "AC" as "ay-see".
#
# ---------------------------------------------------------------------------
# TWO HARD-WON RULES. Read both before adding a pattern.
#
# 1. CONTEXT MUST PROVE IT IS AN ABBREVIATION.
#    The first version expanded every match unconditionally and broke the live
#    greeting: "I am Maya" -> "I in the morning Maya", because \bam\b matches the
#    ordinary English verb as happily as the clock abbreviation. The same flaw
#    renamed a caller ("Sun Li" -> "Sunday Li") and mangled prose ("Sat down" ->
#    "Saturday down", "Wed to her" -> "Wednesday to her").
#    So: am/pm only directly after a digit. Day and month names are NOT expanded
#    at all — bare "Sun"/"Sat"/"Wed"/"Aug" are far more often a name or an
#    ordinary word, and TTS reads "Sun." acceptably anyway. A missed expansion is
#    a blemish; a corrupted sentence is a broken demo. "St." is excluded too
#    (Saint vs Street — a wrong guess is worse than the abbreviation).
#
# 2. NO PATTERN MAY CONSUME A TRAILING PERIOD.
#    An optional group like `\.?` is still part of the match when it fires, so it
#    silently ate sentence terminators: "Meet at 6 PM." -> "...6 in the evening"
#    and "6 PM. Next line." ran two sentences together, flattening the prosody.
#    Three attempts (`\.?`, `\b\.?`, `(?:\.(?=\s|$))?`) all failed identically.
#    Match the LETTERS ONLY and assert what follows with a lookahead, which
#    consumes nothing. The abbreviation's own period is then just a period in the
#    text, and _drop_inner_abbreviation_dots removes it only where it is clearly
#    not a sentence end (i.e. followed by a lowercase word or a digit).
# ---------------------------------------------------------------------------

# (pattern, replacement) applied case-SENSITIVELY — case carries meaning here.
_ABBREVIATIONS: list[tuple[str, str]] = [
    # Titles: a title is ALWAYS followed by a name, never by a sentence end, so
    # its period is unambiguously the abbreviation's own — consume it. Leaving it
    # produced "Meet Doctor. Rao." because the following name is capitalised and
    # so looked like a new sentence to _drop_inner_abbreviation_dots.
    (r"\bDr\.", "Doctor"),
    (r"\bMrs\.", "Missus"),
    (r"\bMr\.", "Mister"),
    (r"\bMs\.", "Miss"),
    # Street types CAN end a sentence ("the office is on Rd."), so these keep
    # the period and let _drop_inner_abbreviation_dots decide.
    (r"\bRd(?=\.)", "Road"),
    (r"\bAve(?=\.)", "Avenue"),
    # Word-forms that are never ordinary English words.
    (r"\bapprox(?![\w])", "approximately"),
    (r"\bdept(?![\w])", "department"),
    (r"\bappt(?![\w])", "appointment"),
    (r"\betc(?![\w])", "and so on"),
    # Standalone capitals only — an HVAC demo says this constantly, but "ac"
    # inside ordinary text must never match, so this one is case-sensitive.
    (r"\bAC\b", "air conditioning"),
]

# Clock forms, case-INSENSITIVE ("6 PM", "6 pm", "9 a.m.", "10 A.M.").
# Anchored to a preceding digit, and the letters are all that is consumed.
_CLOCK: list[tuple[str, str]] = [
    (r"(?<=\d)\s*a\.?\s?m(?![\w])", " in the morning"),
    (r"(?<=\d)\s*p\.?\s?m(?![\w])", " in the evening"),
]

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_HINDI_CLOCK = re.compile(r"\b(\d{1,2})(:\d{2})?\s*([ap])\.?\s?m(?![\w])", re.IGNORECASE)


def _hindi_clock(match: "re.Match[str]") -> str:
    """'10 AM' -> 'सुबह 10 बजे', '2 PM' -> 'दोपहर 2 बजे', '7 PM' -> 'शाम 7 बजे', '9 PM' -> 'रात 9 बजे'."""
    hour, minutes, half = int(match.group(1)), match.group(2) or "", match.group(3).lower()
    if half == "a":
        part = "रात" if hour == 12 or hour < 4 else "सुबह"
    else:
        part = "दोपहर" if hour == 12 or hour < 4 else "शाम" if hour < 8 else "रात"
    return f"{part} {hour}{minutes} बजे"


_IDENTIFIER_RE = re.compile(r"\S*(?:@|://)\S*")

_PHONE_RE = re.compile(r"(?<![\w+])\+?(?:\([0-9]{1,4}\)|[0-9])[0-9 \u00a0\u202f\u2010\u2011()-]*[0-9](?!\w)")
_PHONE_CUE = re.compile(
    r"(?:\b(?:phone|telephone|mobile|callback|whatsapp)(?:\s+number)?"
    r"|\b(?:contact|my|your)\s+number|\b(?:call|reach|contact)\s+(?:me|you|us)(?:\s+(?:at|on))?"
    r"|फोन|फ़ोन|मोबाइल)\b", re.I)
_NON_PHONE_CUE = re.compile(
    r"\b(?:order|invoice|account|part|reference|ticket|serial|budget|price|cost|amount|total|units)\b"
    r"|[$€£₹]", re.I)
_DIGIT_WORDS = {
    "en": "zero one two three four five six seven eight nine".split(),
    "hi": "शून्य एक दो तीन चार पाँच छह सात आठ नौ".split(),
}


def phone_numbers_for_speech(text: str, language: str = "en") -> str:
    """Speak phone digits individually; leave other numbers and identifiers alone.

    Require a phone cue, international '+' notation, or a number-only utterance.
    This runs only on TTS input, keeping contact data in the transcript intact.
    """
    protected = [m.span() for m in _IDENTIFIER_RE.finditer(text)]
    words = _DIGIT_WORDS.get(language.split("-")[0])
    if words is None:
        return text  # Other languages use the prompt; never force English digits.

    def replace(match):
        if any(start < match.end() and end > match.start() for start, end in protected):
            return match.group()
        number = match.group().rstrip()
        digits = re.sub(r"\D", "", number)
        if not 7 <= len(digits) <= 15:
            return match.group()
        if re.fullmatch(r"(?:\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{4})", number):
            return match.group()
        # Consider only the nearby clause, not a phone mention elsewhere in a recap.
        prefix = re.split(r"[.!?;\n]", text[:match.start()])[-1][-70:]
        phone_cues = list(_PHONE_CUE.finditer(prefix))
        other_cues = list(_NON_PHONE_CUE.finditer(prefix))
        phone_at = phone_cues[-1].start() if phone_cues else -1
        other_at = other_cues[-1].start() if other_cues else -1
        standalone = not text[:match.start()].strip() and not text[match.end():].strip(" .?!")
        if other_at >= phone_at and other_at >= 0:
            return match.group()
        if phone_at < 0 and not number.startswith("+") and not standalone:
            return match.group()
        spoken = " ".join(words[int(digit)] for digit in digits)
        if number.startswith("+"):
            spoken = ("प्लस " if language.split("-")[0] == "hi" else "plus ") + spoken
        return spoken + match.group()[len(number):]

    return _PHONE_RE.sub(replace, text)


def _drop_inner_abbreviation_dots(text: str) -> str:
    """Remove a period left behind mid-sentence by an expansion.

    "Doctor. Rao" -> "Doctor Rao", "6 in the evening. and" -> "... and", while a
    real sentence end ("6 in the evening. Next line.") keeps its period because
    what follows is capitalised.
    """
    # DERIVED from the tables above, never restated. A hand-kept copy drifted
    # immediately: "air conditioning" was added to _ABBREVIATIONS but missing
    # here, so "AC. Next" kept a stray dot. Deriving removes that whole bug class.
    expansions = "|".join(
        re.escape(replacement.strip())
        for _, replacement in sorted(
            _ABBREVIATIONS + _CLOCK, key=lambda pair: -len(pair[1].strip())
        )
    )
    # Drop the dot only when the next word is lowercase or a digit — i.e. the
    # sentence plainly continues. Anything else (capital, quote, end) keeps it.
    return re.sub(rf"\b({expansions})\.(?=\s+[a-z0-9])", r"\1", text)


def expand_short_forms(text: str) -> str:
    """Spell out abbreviations so TTS speaks full words, never letter-strings.

    Patterns run over the WHOLE string, not per whitespace-separated piece. An
    earlier version iterated pieces, which silently broke every multi-word form:
    in "6 PM" the digit and "PM" are different pieces, so the (?<=\\d) lookbehind
    never saw the number and the expansion never fired at all.

    Identifiers are masked first, so an email or URL is left byte-identical —
    "sales@acme.com" must not become "sales@air conditioningme.com".
    """
    holes: list[str] = []

    def _stash(match: "re.Match[str]") -> str:
        holes.append(match.group())
        return f"\x00{len(holes) - 1}\x00"

    masked = _IDENTIFIER_RE.sub(_stash, text)

    # "6:00 PM" -> "6 PM"; a non-zero minute is kept and read normally.
    masked = re.sub(r"\b(\d{1,2}):00\b", r"\1", masked)
    if _DEVANAGARI.search(masked):
        # Hindi/Hinglish reply: "10 AM से 7 PM" -> "सुबह 10 बजे से शाम 7 बजे", never English inside Hindi.
        masked = _HINDI_CLOCK.sub(_hindi_clock, masked)
    for pattern, replacement in _CLOCK:
        masked = re.sub(pattern, replacement, masked, flags=re.IGNORECASE)
    for pattern, replacement in _ABBREVIATIONS:
        masked = re.sub(pattern, replacement, masked)

    masked = _drop_inner_abbreviation_dots(masked)
    masked = re.sub(r"[ \t]{2,}", " ", masked)
    return re.sub(r"\x00(\d+)\x00", lambda m: holes[int(m.group(1))], masked)


def to_spoken_text(text: str) -> str:
    """Convert markdown/rich text to plain speech-safe text for TTS."""
    try:
        from core.output.user_text import coerce_user_text
        cleaned = coerce_user_text(text)
    except Exception:
        cleaned = re.sub(r'\[([^\]]+)\]\((https?://[^\)]+)\)', r'\1', text)
        cleaned = re.sub(r'https?://\S+', 'link omitted', cleaned)
        cleaned = re.sub(r'^\s{0,3}[-*+]\s+', '', cleaned, flags=re.MULTILINE)
        cleaned = re.sub(r'^\s{0,3}\d+[\.)]\s+', '', cleaned, flags=re.MULTILINE)
        cleaned = re.sub(r'[*_~`]+', '', cleaned)
        cleaned = re.sub(r'\s*\n\s*', ' ', cleaned)
        cleaned = re.sub(r'\s{2,}', ' ', cleaned)
        cleaned = cleaned.strip()
    # Runs on BOTH branches: this is the single chokepoint every spoken line
    # passes through (demo_session.py and core/agent.py alike), so expanding
    # here fixes the browser demo and the phone agent in one place.
    return expand_short_forms(cleaned)
