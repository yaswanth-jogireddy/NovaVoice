"""Smallest AI speech (pipeline/tts_smallest.py) against a fake SSE endpoint, and its voice mode (csm/voices.py)."""
import asyncio
import base64
import json

import httpx
import pytest

from config import Settings
from csm import voices
from pipeline.tts import create_tts_processor
from pipeline.tts_smallest import SmallestTTSProcessor

PCM = bytes(range(200)) * 3   # 600 bytes of 16-bit audio


def sse(*frames):
    return "".join(f"event: audio\ndata: {json.dumps(f)}\n\n" for f in frames).encode()


def processor(handler, **settings):
    p = SmallestTTSProcessor(Settings(_env_file=None, smallest_api_key="k", **settings))
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return p


def collect(p, phrase, language="en"):
    async def run():
        return b"".join([c async for c in p.synthesize(phrase, language)])
    return asyncio.run(run())


def test_streams_base64_pcm_frames_and_sends_the_documented_request():
    sent = {}

    def handler(request):
        sent.update(url=str(request.url), body=json.loads(request.content))
        half = len(PCM) // 2 + 1   # an odd split: one sample spans two frames
        return httpx.Response(200, content=sse({"audio": base64.b64encode(PCM[:half]).decode(), "done": False, "status": "206"},
                                               {"audio": base64.b64encode(PCM[half:]).decode(), "done": False, "status": "206"},
                                               {"status": "200", "done": True}))
    p = processor(handler)
    assert collect(p, "Your balance is ready.", "hi") == PCM
    assert sent["url"] == "https://api.smallest.ai/waves/v1/tts/live"
    assert sent["body"] == {"text": "Your balance is ready.", "voice_id": "sunidhi", "model": "lightning_v3.1",
                            "sample_rate": 24000, "speed": 1.0, "language": "hi", "output_format": "pcm"}


def test_other_indian_languages_use_a_voice_that_speaks_them():
    p = processor(lambda r: httpx.Response(200, content=sse({"done": True})))
    assert p.voice_for("ta") == "rajeshwari" and p.voice_for("en") == "sunidhi" and p.voice_for("hi") == "sunidhi"
    sent = {}

    def handler(request):
        sent.update(json.loads(request.content))
        return httpx.Response(200, content=sse({"audio": base64.b64encode(PCM).decode(), "done": True}))
    collect(processor(handler), "ନମସ୍କାର", "od")
    assert sent["language"] == "or"                    # Smallest's code for Odia


def test_an_http_error_is_raised_with_its_detail():
    p = processor(lambda r: httpx.Response(401, content=b'{"error":"invalid key"}'))
    with pytest.raises(RuntimeError, match="HTTP 401"):
        collect(p, "Hello.")


def test_factory_and_sample_rate():
    s = Settings(_env_file=None, tts_provider="smallest", smallest_api_key="k", smallest_sample_rate=16000)
    assert isinstance(create_tts_processor(s, "smallest"), SmallestTTSProcessor)
    assert s.tts_output_sample_rate == 16000


def test_voice_mode_applies_the_chosen_smallest_voice():
    s = Settings(_env_file=None, sarvam_api_key="k", smallest_api_key="k")
    applied = voices.apply(s, "smallest", "sm-devansh", "hi", "voice")
    assert applied.tts_provider == "smallest" and applied.smallest_voice_id == "devansh"
    assert voices.catalogue(s)["tiers"]["smallest"]["available"]
    with pytest.raises(ValueError, match="SMALLEST_API_KEY"):
        voices.apply(Settings(_env_file=None, sarvam_api_key="k", smallest_api_key=""), "smallest", "sm-sunidhi", "en", "voice")


def test_rhea_uses_the_pro_model_and_others_keep_the_default():
    s = Settings(_env_file=None, sarvam_api_key="k", smallest_api_key="k")
    rhea = voices.apply(s, "smallest", "sm-rhea", "hi", "voice")
    assert rhea.smallest_voice_id == "rhea" and rhea.smallest_model == "lightning_v3.1_pro"
    assert voices.persona("smallest", "sm-rhea") == {"name": "Rhea", "gender": "female", "voice": "sm-rhea"}
    assert voices.apply(s, "smallest", "sm-sunidhi", "en", "voice").smallest_model == "lightning_v3.1"
