"""Keep provider selection shared by the phone agent and recording studio."""
def create_stt_processor(settings):
    if settings.stt_provider == "elevenlabs":
        from pipeline.stt_elevenlabs import ElevenLabsSTTProcessor
        return ElevenLabsSTTProcessor(settings)
    from pipeline.stt import STTProcessor
    return STTProcessor(settings)
