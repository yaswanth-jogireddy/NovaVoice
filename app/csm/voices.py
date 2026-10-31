"""Voice modes (cost-effective / standard) and the voice catalogue, from app/voices.json.

The call page only offers what this module lists; the session route applies a choice through
apply(), which rejects anything not in the file. Cloned voices are stored in data/cloned_voices.json.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "app" / "voices.json"
CLONES = ROOT / "data" / "cloned_voices.json"
_lock = threading.Lock()


def config() -> dict:
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def cloned() -> list[dict]:
    try:
        return json.loads(CLONES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def add_clone(name: str, voice_id: str, gender: str = "female") -> dict:
    gender = gender if gender in ("female", "male") else "female"
    entry = {"id": "clone-" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] + f"-{int(time.time()) % 100000}",
             "label": f"{name} (cloned)", "agent_name": name.split()[0][:20] or "Aria", "gender": gender, "tier": "standard",
             "elevenlabs_voice_id": voice_id, "cloned": True}
    with _lock:
        voices = cloned() + [entry]
        CLONES.parent.mkdir(parents=True, exist_ok=True)
        CLONES.write_text(json.dumps(voices, indent=1), encoding="utf-8")
    return entry


def _voice_id(voice: dict, settings) -> str:
    return voice.get("elevenlabs_voice_id") or getattr(settings, voice.get("elevenlabs_setting", ""), "") or ""


def catalogue(settings) -> dict:
    """What the call page may offer, with availability (keys and voice ids actually configured)."""
    cfg = config()
    tiers = {}
    for key, tier in cfg["tiers"].items():
        # Sarvam always listens; the speaking provider needs its own key (sarvam's is the same one).
        ready = bool(settings.sarvam_api_key) and bool(getattr(settings, tier["tts_provider"] + "_api_key", ""))
        tiers[key] = {"label": tier["label"], "note": tier["note"], "languages": tier["languages"], "available": ready}
    voices = []
    for v in cfg["voices"] + cloned():
        available = tiers.get(v["tier"], {}).get("available", False) and (v["tier"] != "standard" or bool(_voice_id(v, settings)))
        voices.append({"id": v["id"], "label": v["label"], "tier": v["tier"], "languages": v.get("languages"),
                       "agent_name": v.get("agent_name", "Aria"), "gender": v.get("gender", "female"),
                       "cloned": bool(v.get("cloned")), "available": available})
    return {"default_tier": cfg["default_tier"], "tiers": tiers, "voices": voices}


def apply(settings, tier: str, voice_id: str, language: str, mode: str, pace: float | None = None):
    """Settings for one call. Raises ValueError (shown to the caller) for anything not offered."""
    cfg = config()
    tier = tier or cfg["default_tier"]
    if tier not in cfg["tiers"]:
        raise ValueError("Unknown voice mode")
    spec = cfg["tiers"][tier]
    if language not in spec["languages"]:
        raise ValueError(f"{spec['label']} mode doesn't offer this language")
    voices = [v for v in cfg["voices"] + cloned() if v["tier"] == tier]
    voice = next((v for v in voices if v["id"] == voice_id), None) if voice_id else voices[0] if voices else None
    if voice is None:
        raise ValueError("Unknown voice for this mode")
    if voice.get("languages") and language not in voice["languages"]:
        raise ValueError(f"{voice['label']} doesn't speak this language")
    update = {"stt_provider": "sarvam", "tts_provider": spec["tts_provider"]}
    if spec["tts_provider"] == "sarvam":
        gender = voice.get("gender", voice.get("sarvam_gender", "female"))
        update["sarvam_tts_voice_gender"] = gender
        if voice.get("sarvam_speaker"):
            update[f"sarvam_tts_voice_{gender}"] = voice["sarvam_speaker"]
    elif spec["tts_provider"] == "smallest":
        if mode == "voice" and not settings.smallest_api_key:
            raise ValueError("Smallest AI mode needs SMALLEST_API_KEY")
        update["smallest_voice_id"] = voice.get("smallest_voice_id") or settings.smallest_voice_id
        # A Pro-pool voice (rhea) only speaks on the Pro model; the others use SMALLEST_MODEL.
        update["smallest_model"] = voice.get("smallest_model") or settings.smallest_model
    else:
        vid = _voice_id(voice, settings)
        if mode == "voice" and not (vid and settings.elevenlabs_api_key):
            raise ValueError("Standard mode needs an ElevenLabs key and voice")
        update["elevenlabs_voice_id"] = vid
    if pace is not None:
        pace = min(max(float(pace), 0.8), 1.3)
        update["sarvam_tts_pace"] = pace
        update["elevenlabs_speed"] = min(max(pace, 0.7), 1.2)
        update["smallest_speed"] = pace
    return settings.model_copy(update=update)


def persona(tier: str, voice_id: str) -> dict:
    """The persona (agent name, gender) of the voice a call uses; the default voice when not chosen."""
    cfg = config()
    tier = tier or cfg["default_tier"]
    voices = [v for v in cfg["voices"] + cloned() if v["tier"] == tier]
    voice = next((v for v in voices if v["id"] == voice_id), None) or (voices[0] if voices else {})
    return {"name": voice.get("agent_name", "Aria"), "gender": voice.get("gender", "female"), "voice": voice.get("id", "")}
