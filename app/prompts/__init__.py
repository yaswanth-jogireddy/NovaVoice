"""
Prompt / persona loader.

Each persona is a Python file in this directory that defines:
    SYSTEM_PROMPT: str   — the system prompt sent to the LLM
    ROOM_NAME: str       — the LiveKit room the agent should join (optional)

Usage in .env:
    AGENT_PERSONA=sales_agent     # loads prompts/sales_agent.py
    AGENT_PERSONA=support_agent   # loads prompts/support_agent.py
    AGENT_PERSONA=default         # loads prompts/default.py  (fallback)
"""

import importlib
import re
from pathlib import Path
from typing import Any

# Matches a {g:male_form|female_form} marker in persona text (GREETING, etc.).
# Lets a persona write one string that stays grammatically correct no matter
# which voice speaks it, e.g. "bol {g:raha|rahi} hoon".
_GENDER_MARKER_RE = re.compile(r"\{g:([^|{}]*)\|([^|{}]*)\}")


def apply_gender_template(text: str, gender: str) -> str:
    """Resolve {g:male_form|female_form} markers against the configured TTS voice gender.

    "bol {g:raha|rahi} hoon" -> "bol raha hoon" for a male voice,
    "bol rahi hoon" for a female voice. Text with no markers passes through
    unchanged, so this is always safe to call.
    """
    is_male = (gender or "").strip().lower() == "male"

    def _sub(m: "re.Match[str]") -> str:
        male_form, female_form = m.group(1), m.group(2)
        return male_form if is_male else female_form

    return _GENDER_MARKER_RE.sub(_sub, text)


def gender_prompt_fragment(gender: str) -> str:
    """System-prompt fragment instructing the LLM to use grammatically correct
    Hindi gender for whichever voice is actually configured (SARVAM_TTS_VOICE_GENDER).

    Composes onto every persona's SYSTEM_PROMPT the same way the accent layer
    composes on top (see main.py) — one fragment, not duplicated per persona,
    so it never drifts out of sync with an env change.
    """
    is_male = (gender or "").strip().lower() == "male"
    if is_male:
        return (
            "VOICE GENDER — you are speaking through a MALE voice:\n"
            "Use masculine Hindi grammar for every self-reference — "
            '"raha hoon" not "rahi hoon", "bola"/"kaha" not "boli"/"kahi", '
            '"karta hoon" not "karti hoon". A feminine verb ending on a male '
            "voice is one of the fastest ways to break the illusion of a real "
            "person on this call — check every self-referential verb before "
            "it lands."
        )
    return (
        "VOICE GENDER — you are speaking through a FEMALE voice:\n"
        "Use feminine Hindi grammar for every self-reference — "
        '"rahi hoon" not "raha hoon", "boli"/"kahi" not "bola"/"kaha", '
        '"karti hoon" not "karta hoon". A masculine verb ending on a female '
        "voice is one of the fastest ways to break the illusion of a real "
        "person on this call — check every self-referential verb before it "
        "lands."
    )


def load_persona(name: str) -> dict[str, Any]:
    """Import a persona module by name and return its exported values.

    Returns a dict with at least:
        system_prompt (str)
        room_name (str)
    """
    try:
        module = importlib.import_module(f"prompts.{name}")
    except ModuleNotFoundError:
        raise ValueError(
            f"Persona '{name}' not found. "
            f"Create prompts/{name}.py or check AGENT_PERSONA in your .env."
        )

    system_prompt = getattr(module, "SYSTEM_PROMPT", None)
    if not system_prompt:
        raise ValueError(f"prompts/{name}.py must define a non-empty SYSTEM_PROMPT string.")

    return {
        "system_prompt": system_prompt,
        "room_name": getattr(module, "ROOM_NAME", "voice-agent-room"),
        # Spoken once, when a human actually joins. Empty string = stay silent.
        "greeting": getattr(module, "GREETING", ""),
        # Optional {accent_name: greeting} overrides. The greeting is HARDCODED
        # text that never passes through the LLM, so an accent's dialect prompt
        # fragment cannot reach it — without this, AGENT_ACCENT=haryanvi opened
        # every call in textbook Hindi and only drifted into dialect from the
        # second turn onward. A persona that cares supplies GREETINGS; one that
        # doesn't keeps GREETING for every accent, exactly as before.
        "greetings": getattr(module, "GREETINGS", {}) or {},
        # Language for agent-initiated speech that carries no script hint
        # ("en", "hi", …). Falls back to SPEECH_DEFAULT_LANGUAGE when unset.
        "language": getattr(module, "LANGUAGE", ""),
    }


def list_personas() -> list[str]:
    """Return names of all available persona files."""
    here = Path(__file__).parent
    return [f.stem for f in here.glob("*.py") if f.stem != "__init__"]
