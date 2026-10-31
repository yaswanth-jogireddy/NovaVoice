"""Pre-record the acknowledgement clips ("Sure, one moment.") for every voice in app/voices.json.

    python scripts/make_ack_clips.py               all voices (cloned ones too)
    python scripts/make_ack_clips.py --voice sameer

Each clip is synthesised with exactly the settings a call with that voice uses, as a short natural
phrase (a single word in isolation tends to come out in a different tone). Saved as WAV files in
app/acks/ (listen to them there; delete one and re-run to re-record it) plus app/acks/manifest.json.
Calls load them from disk; a voice without clips falls back to synthesising them per call.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
OUT = ROOT / "app" / "acks"

# Order matters: the session plays the last one ("Thanks…") after the caller gave details.
PHRASES = {"en": ["Sure, one moment.", "Okay, let me check.", "Thanks, one moment."],
           "hi": ["जी, एक पल।", "ठीक है, देखती हूँ।", "धन्यवाद, एक पल।"]}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "clip"


async def record(voice: dict, language: str, base) -> dict | None:
    from csm import persona, voices
    from pipeline.tts import create_tts_processor
    try:
        settings = voices.apply(base, voice["tier"], voice["id"], language, "voice")
    except ValueError:
        return None   # this voice doesn't speak this language
    phrases = PHRASES[language]
    if voice.get("gender") == "male" and language == "hi":
        phrases = [persona.masculine(p) for p in phrases]
    tts = create_tts_processor(settings, settings.tts_provider)
    files = []
    try:
        await tts.initialize()
        for i, phrase in enumerate(phrases):
            tts.reset()
            lang = "" if settings.tts_provider == "elevenlabs" else language
            pcm = b"".join([c async for c in tts.synthesize(phrase, language=lang)])
            if not pcm:
                continue
            name = f"{voice['id']}-{language}-{i + 1}.wav"
            with wave.open(str(OUT / name), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(settings.tts_output_sample_rate)
                w.writeframes(pcm)
            files.append({"phrase": phrase, "file": name, "seconds": round(len(pcm) / 2 / settings.tts_output_sample_rate, 2)})
    finally:
        await tts.close()
    return {"rate": settings.tts_output_sample_rate, "provider": settings.tts_provider, "clips": files} if files else None


async def main(only: str | None) -> None:
    from dotenv import load_dotenv
    from config import Settings
    from csm import voices
    load_dotenv(ROOT / ".env")
    base = Settings(_env_file=ROOT / ".env")
    OUT.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    for voice in voices.config()["voices"] + voices.cloned():
        if only and voice["id"] != only:
            continue
        for language in ("en", "hi"):
            entry = None
            for attempt in (1, 2):   # providers time out now and then: one retry, then move on
                try:
                    entry = await record(voice, language, base)
                    break
                except Exception as exc:
                    print(f"{voice['id']:14} {language}  attempt {attempt} failed: {type(exc).__name__}")
            if entry:
                manifest[f"{voice['id']}|{language}"] = entry
                print(f"{voice['id']:14} {language}  {len(entry['clips'])} clips  ({entry['provider']}, {entry['rate']} Hz)")
                # saved after every voice, so a later failure never loses what was recorded
                manifest_path.write_text(json.dumps(manifest, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Saved in {OUT}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--voice", help="only this voice id")
    asyncio.run(main(parser.parse_args().voice))
