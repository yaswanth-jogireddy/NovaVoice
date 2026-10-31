import re
from typing import Optional

from config import Settings
from utils.logger import get_logger

logger = get_logger("pii_redactor")


# Pre-compiled regex patterns for PII detection.
# Architecture requirement: must run under 2ms on typical transcript length.
# Regex-only approach (no ML model) keeps this in the hot-path latency budget.

_BUILTIN_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Credit card numbers: 13-19 digits, optionally separated by spaces or dashes
    (
        "[REDACTED_CC]",
        re.compile(
            r"\b(?:\d[ -]*?){13,19}\b"
        ),
    ),
    # US Social Security Numbers: XXX-XX-XXXX
    (
        "[REDACTED_SSN]",
        re.compile(
            r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b"
        ),
    ),
    # Aadhaar numbers (India): 12 digits, optionally separated by spaces
    (
        "[REDACTED_AADHAAR]",
        re.compile(
            r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"
        ),
    ),
    # Email addresses
    (
        "[REDACTED_EMAIL]",
        re.compile(
            r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
        ),
    ),
    # Phone numbers: international and domestic formats
    # Matches +91-XXXXX-XXXXX, (XXX) XXX-XXXX, +1-XXX-XXX-XXXX, 10-digit sequences
    (
        "[REDACTED_PHONE]",
        re.compile(
            r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{2,4}\)?[-.\s]?\d{3,5}[-.\s]?\d{4,5}\b"
        ),
    ),
    # PAN Card (India): ABCDE1234F
    (
        "[REDACTED_PAN]",
        re.compile(
            r"\b[A-Z]{5}\d{4}[A-Z]\b"
        ),
    ),
]

# Order matters: check longer/more specific patterns first to avoid partial matches.
# SSN before phone (SSN is 9 digits, phone is 10+), Aadhaar before CC (both digit sequences).
# The ordering above is: CC (13-19 digits) > SSN (9 digits) > Aadhaar (12 digits) > Email > Phone > PAN


class PIIRedactor:
    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._patterns: list[tuple[str, re.Pattern]] = list(_BUILTIN_PATTERNS)

        # Load additional user-configured patterns from settings
        if settings is not None and settings.pii_additional_patterns:
            for raw_pattern in settings.pii_additional_patterns.split(","):
                raw_pattern = raw_pattern.strip()
                if raw_pattern:
                    try:
                        compiled = re.compile(raw_pattern)
                        self._patterns.append(("[REDACTED_CUSTOM]", compiled))
                    except re.error as e:
                        logger.warning(
                            "pii_invalid_custom_pattern",
                            pattern=raw_pattern,
                            error=str(e),
                        )

    def redact(self, text: str) -> str:
        if not text:
            return text

        result = text
        for replacement, pattern in self._patterns:
            result = pattern.sub(replacement, result)

        return result

    def contains_pii(self, text: str) -> bool:
        if not text:
            return False

        for _, pattern in self._patterns:
            if pattern.search(text):
                return True
        return False
