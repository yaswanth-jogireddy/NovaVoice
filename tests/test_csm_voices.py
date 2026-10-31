"""Voice modes and catalogue (app/voices.json): only offered choices are accepted."""
import pytest

from config import Settings
from csm import voices


@pytest.fixture()
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(voices, "CLONES", tmp_path / "cloned_voices.json")
    return Settings(_env_file=None, sarvam_api_key="s", elevenlabs_api_key="e", groq_api_key="g",
                    elevenlabs_voice_id="EL_DEFAULT", elevenlabs_us_voice_id="EL_US", elevenlabs_arabic_voice_id="")


def test_cost_mode_uses_sarvam_v4_persona(settings):
    cfg = voices.apply(settings, "cost", "shubh", "en", "voice")
    assert cfg.tts_provider == "sarvam" and cfg.sarvam_tts_voice_gender == "male" and cfg.stt_provider == "sarvam"
    assert cfg.sarvam_tts_voice_male == "shubh_enhi_banking"
    default = voices.apply(settings, "", "", "hinglish", "voice")
    assert default.sarvam_tts_voice_gender == "female" and default.sarvam_tts_voice_female == "ishita_enhi_customer"
    assert voices.persona("standard", "sameer") == {"name": "Sameer", "gender": "male", "voice": "sameer"}
    assert voices.persona("", "") == {"name": "Aria", "gender": "female", "voice": "aria"}


def test_every_sarvam_voice_is_a_v4_id():
    # bulbul:v4 needs persona ids like "ishita_enhi_customer"; a bare v3 name ("shubh") is rejected.
    for v in voices.config()["voices"]:
        if v["tier"] == "cost":
            assert v["sarvam_speaker"].count("_") >= 2


def test_standard_mode_uses_elevenlabs_voice(settings):
    cfg = voices.apply(settings, "standard", "el-us", "en", "voice", pace=1.15)
    assert cfg.tts_provider == "elevenlabs" and cfg.elevenlabs_voice_id == "EL_US" and cfg.stt_provider == "sarvam"
    assert cfg.sarvam_tts_pace == 1.15 and cfg.elevenlabs_speed == 1.15


@pytest.mark.parametrize("tier,voice,language", [("premium", "", "en"), ("cost", "el-us", "en"),
                                                 ("cost", "neha", "hi"), ("cost", "aria", "ar"),
                                                 ("standard", "el-arabic", "ar"), ("standard", "nope", "en")])
def test_rejects_what_is_not_offered(settings, tier, voice, language):
    with pytest.raises(ValueError):
        voices.apply(settings, tier, voice, language, "voice")


def test_catalogue_marks_unconfigured_voices(settings):
    cat = voices.catalogue(settings)
    by_id = {v["id"]: v for v in cat["voices"]}
    assert cat["tiers"]["standard"]["available"] and by_id["el-us"]["available"]
    assert not by_id["el-arabic"]["available"]                     # no Arabic voice id configured
    no_el = voices.catalogue(settings.model_copy(update={"elevenlabs_api_key": ""}))
    assert not no_el["tiers"]["standard"]["available"]


def test_cloned_voice_is_persisted_and_offered(settings):
    entry = voices.add_clone("Abhi", "EL_CLONE_1", "male")
    assert entry["id"].startswith("clone-abhi") and entry["tier"] == "standard"
    assert voices.persona("standard", entry["id"]) == {"name": "Abhi", "gender": "male", "voice": entry["id"]}
    assert any(v["id"] == entry["id"] and v["cloned"] for v in voices.catalogue(settings)["voices"])
    assert voices.apply(settings, "standard", entry["id"], "hi", "voice").elevenlabs_voice_id == "EL_CLONE_1"
