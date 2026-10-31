"""
Accent loader.

Each accent is a Python file in this directory that defines Sarvam bulbul:v3
tuning parameters plus an LLM-facing dialect vocabulary hint:
    ACCENT_ID: str                — matches the file's stem
    SARVAM_SPEAKER: str | None    — overrides the gender-based speaker default
    SARVAM_PACE: float | None     — overrides sarvam_tts_pace
    SARVAM_TEMPERATURE: float | None — overrides sarvam_tts_temperature
    SARVAM_DICT_ID: str | None    — pronunciation dictionary id (see tools/build_pronunciation_dict.py)
    DIALECT_PROMPT_FRAGMENT: str  — appended to the persona's SYSTEM_PROMPT

This is a separate axis from prompts/ (persona = *what* the agent says and its
job; accent = *how* it sounds and which dialect words it reaches for) — the two
compose: a persona's SYSTEM_PROMPT gets DIALECT_PROMPT_FRAGMENT appended, and
its TTS call gets the accent's speaker/pace/temperature/dict_id layered on top
of the existing gender-based defaults.

Usage in .env:
    AGENT_ACCENT=haryanvi     # loads accents/haryanvi.py
    AGENT_ACCENT=rajasthani   # loads accents/rajasthani.py
    AGENT_ACCENT=up           # loads accents/up.py
    AGENT_ACCENT=neutral      # (default) no accent layer — today's behavior
"""

import importlib
from pathlib import Path
from typing import Any


def load_accent(name: str) -> dict[str, Any]:
    """Import an accent module by name and return its exported values.

    Returns a dict with:
        accent_id (str)
        speaker (str | None)
        pace (float | None)
        temperature (float | None)
        dict_id (str | None)
        prompt_fragment (str)
    """
    try:
        module = importlib.import_module(f"accents.{name}")
    except ModuleNotFoundError:
        raise ValueError(
            f"Accent '{name}' not found. "
            f"Create accents/{name}.py or check AGENT_ACCENT in your .env."
        )

    return {
        "accent_id": getattr(module, "ACCENT_ID", name),
        "speaker": getattr(module, "SARVAM_SPEAKER", None) or None,
        "pace": getattr(module, "SARVAM_PACE", None),
        "temperature": getattr(module, "SARVAM_TEMPERATURE", None),
        "dict_id": getattr(module, "SARVAM_DICT_ID", None) or None,
        "prompt_fragment": getattr(module, "DIALECT_PROMPT_FRAGMENT", "") or "",
    }


def list_accents() -> list[str]:
    """Return names of all available accent files."""
    here = Path(__file__).parent
    return [f.stem for f in here.glob("*.py") if f.stem != "__init__"]
