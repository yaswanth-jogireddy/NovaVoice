"""Server-owned speech profiles. No silent accent/provider fallback."""
from aiohttp import web


def voice_profiles(settings):
    # Listening stays on Sarvam; only the speaking voice changes.
    speaker_key = {'openai': settings.openai_api_key, 'elevenlabs': settings.elevenlabs_api_key}.get(settings.us_voice_provider)
    configured = bool(speaker_key and settings.sarvam_api_key)
    return [dict(id='default', label='Current voice', languages=None, available=True),
            dict(id='en_us', label='English (US)', languages=['en'], available=configured,
                 unavailable_reason='' if configured else 'US English voice is not configured yet.')]


def select_voice(settings, voice, language, mode):
    if voice != 'en_us':
        return settings
    if language != 'en':
        raise web.HTTPBadRequest(text='Choose English for the US English voice')
    if mode == 'voice' and not voice_profiles(settings)[1]['available']:
        raise web.HTTPServiceUnavailable(text='US English voice is not configured yet')
    return settings.model_copy(update={
        # 'off' only reaches here in text mode, where no voice speaks: keep the provider.
        'studio_voice_profile':'en_us', 'stt_provider':'sarvam',
        'tts_provider':settings.tts_provider if settings.us_voice_provider == 'off' else settings.us_voice_provider,
        'elevenlabs_voice_id':settings.elevenlabs_us_voice_id or settings.elevenlabs_voice_id,
        'sarvam_language':'en-IN', 'speech_default_language':'en',
        'agent_accent':'neutral',
    })
