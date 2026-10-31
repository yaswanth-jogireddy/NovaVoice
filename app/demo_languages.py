"""Conversation selections are not provider locales."""
from aiohttp import web

# Sarvam speaks Indian languages and English only. A call in one of these languages
# uses ElevenLabs speech for that call alone, when its key is configured.
ELEVENLABS_ONLY = frozenset({"es", "de", "it", "ar"})


def route_speech(settings, language, mode):
    """Send a voice call Sarvam cannot speak to ElevenLabs; every other call keeps its providers."""
    if (mode != 'voice' or language not in ELEVENLABS_ONLY or not settings.elevenlabs_api_key
            or 'sarvam' not in {settings.stt_provider, settings.tts_provider}):
        return settings
    update = {'stt_provider': 'elevenlabs', 'tts_provider': 'elevenlabs'}
    if language == 'ar' and settings.elevenlabs_arabic_voice_id:
        update['elevenlabs_voice_id'] = settings.elevenlabs_arabic_voice_id
    return settings.model_copy(update=update)


def speech_language(language):
    return 'hi' if language == 'hinglish' else language


def speech_settings(language):
    code = speech_language(language)
    values = dict(speech_default_language=code, sarvam_language=code + '-IN')
    if language == 'hinglish':
        values['caller_language_switch'] = False
    return values


def validate_hinglish(settings, language, scenario, context, accent='neutral', voice='default'):
    """Hinglish speaks through the Hindi voice: regional styles and other voices do not apply."""
    if language != 'hinglish':
        return
    if accent != 'neutral' or voice != 'default':
        raise web.HTTPBadRequest(text='Choose the default voice and neutral style for Hinglish.')


def language_options(settings, names):
    return dict(names)
