import os
import math
from pathlib import Path

from pydantic import Field, field_validator
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict

# Set ENV_FILE=.env.solar before running to use the solar agent config.
# Default falls back to .env (main Jarvis).
_ENV_FILE = os.environ.get("ENV_FILE", ".env")


def validate_browser_settings(settings):
    """Shipped browser capture and ingress accounting are 16 kHz signed PCM."""
    if settings.audio_sample_rate != 16000:
        raise ValueError('Browser voice requires AUDIO_SAMPLE_RATE=16000')
    if settings.tts_output_sample_rate not in {16000, 24000}:
        raise ValueError('Browser speech output must be 16000 or 24000 Hz')
    for name in ('groq_temperature', 'sarvam_tts_pace', 'sarvam_tts_temperature',
                 'studio_playback_timeout_s', 'studio_final_reply_wait_s'):
        if not math.isfinite(getattr(settings, name)):
            raise ValueError('Invalid finite browser speech setting: ' + name)
    if not .5 <= settings.sarvam_tts_pace <= 2 or not .01 <= settings.sarvam_tts_temperature <= 2:
        raise ValueError('Invalid Sarvam speech pace or temperature')
    if not 1 <= settings.groq_max_tokens <= 4096 or not 1 <= settings.openai_max_tokens <= 4096:
        raise ValueError('Browser LLM output token limit must be between 1 and 4096')


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LiveKit Transport ---
    livekit_url: str = "ws://localhost:7880"
    livekit_api_key: str = ""
    livekit_api_secret: str = ""

    # --- Audio Pipeline ---
    # PCM sample rate shared by STT input, VAD, and LiveKit frames.
    audio_sample_rate: int = 16000

    # International speech; retain Sarvam defaults for existing installations.
    stt_provider: Literal["sarvam", "elevenlabs"] = "sarvam"
    tts_provider: Literal["sarvam", "elevenlabs", "openai", "smallest"] = "sarvam"
    # Smallest AI (Waves, Lightning v3.1) speaking, streamed over SSE (pipeline/tts_smallest.py).
    smallest_api_key: str = ""
    smallest_tts_url: str = "https://api.smallest.ai/waves/v1/tts/live"
    smallest_model: str = "lightning_v3.1"
    smallest_voice_id: str = "sunidhi"          # Hindi + Indian English, female; set per voice in voices.json
    smallest_sample_rate: Literal[8000, 16000, 24000, 44100] = 24000
    smallest_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    # Other Indian languages need a voice that speaks them (docs.smallest.ai voices page, v3.1 pool).
    smallest_language_voices: dict[str, str] = {"ta": "rajeshwari", "te": "padmaja", "kn": "deepashri",
                                                "ml": "shibi", "mr": "rupali", "gu": "niharika"}
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    elevenlabs_model_id: str = "eleven_flash_v2_5"
    elevenlabs_tts_sample_rate: Literal[16000, 24000] = 24000
    elevenlabs_stability: float = Field(default=0.5, ge=0, le=1)
    elevenlabs_similarity_boost: float = Field(default=0.8, ge=0, le=1)
    elevenlabs_speed: float = Field(default=1.0, ge=0.7, le=1.2)
    openai_tts_voice: Literal['marin', 'cedar', 'coral', 'nova', 'shimmer', 'alloy', 'ash', 'ballad', 'echo', 'fable', 'onyx', 'sage', 'verse'] = 'marin'
    openai_tts_model: Literal['gpt-4o-mini-tts'] = 'gpt-4o-mini-tts'
    # Who speaks the English (US) voice. 'off' hides it: US demos then use the
    # default Sarvam English voice. ElevenLabs needs ELEVENLABS_API_KEY.
    us_voice_provider: Literal['openai', 'elevenlabs', 'off'] = 'openai'
    elevenlabs_us_voice_id: str = ""      # empty = ELEVENLABS_VOICE_ID
    elevenlabs_arabic_voice_id: str = ""  # Arabic calls; empty = ELEVENLABS_VOICE_ID
    studio_voice_profile: Literal["default", "en_us"] = "default"
    stt_endpoint_s: float = Field(default=0.7, ge=0.3, le=2)
    semantic_cache_enabled: bool = False  # Cache is not voice/locale/tenant scoped.

    # Browser studio: local energy gate, independent of the LiveKit Silero gate.
    studio_barge_in_min_speech_ms: int = Field(default=240, ge=80, le=600)
    studio_barge_in_min_rms: float = Field(default=0.018, ge=0.005, le=0.1)
    studio_barge_in_noise_ratio: float = Field(default=3.5, ge=1.5, le=8)
    studio_barge_in_release_ms: int = Field(default=200, ge=80, le=800)
    studio_false_interrupt_resume_ms: int = Field(default=900, ge=300, le=3000)
    # Turn completion: after a caller's thinking sound ("hmm") Maya stays quiet;
    # if the caller is still silent this many seconds later, one short nudge.
    # Silence before the one nudge / missed-speech repeat (Sarvam's agents use 10 s).
    filler_nudge_s: float = Field(default=8.0, ge=2, le=15)
    # Caller silent this long after the agent spoke: "are you still there?" once, then the closing line.
    silence_check_s: float = Field(default=7.0, ge=3, le=30)
    demo_expand_contractions: bool = False

    @property
    def tts_output_sample_rate(self) -> int:
        if self.tts_provider == 'openai': return 24000
        if self.tts_provider == 'smallest': return self.smallest_sample_rate
        return (self.elevenlabs_tts_sample_rate if self.tts_provider == "elevenlabs"
                else self.sarvam_tts_sample_rate)

    # --- Sarvam AI STT (Saaras v3, codemix) ---
    sarvam_api_key: str = ""
    sarvam_api_url: str = "https://api.sarvam.ai/v1"
    sarvam_model: str = "saaras:v3"
    sarvam_language: str = "hi-IN"
    sarvam_mode: str = "codemix"  # handles Hindi, English, and Hinglish in one stream

    # --- Sarvam AI TTS (Bulbul v3) ---
    # IMPORTANT: v2 and v3 have DIFFERENT speaker rosters. Speakers are NOT
    # cross-compatible — using a v2 speaker on v3 returns HTTP 400.
    # v3 male voices (per Sarvam server-confirmed list):
    #   aditya, ashutosh, rahul, rohan, amit, dev, ratan, varun, manan, sumit,
    #   kabir, aayan, shubh, advait, anand, tarun, sunny, mani, gokul, vijay,
    #   mohit, rehan, soham
    # v3 female voices:
    #   ritu, priya, neha, pooja, simran, kavya, ishita, shreya, roopa, tanya,
    #   shruti, suhani, kavitha, rupali, niharika
    sarvam_tts_voice_gender: str = "female"      # "male" → shubh | "female" → ishita
    sarvam_tts_voice_male: str = "shubh"         # v3 male, warm professional tone
    sarvam_tts_voice_female: str = "ishita"      # v3 female; one voice for every language
    sarvam_tts_model: str = "bulbul:v3"          # v3 = LLM-based prosody, 35+ voices, native Hindi feel
    sarvam_tts_pace: float = 1.0                 # v3 range: 0.5–2.0; 1.0 = natural speed
    sarvam_tts_temperature: float = 0.7          # v3-only: 0.01–2.0; 0.7 = warm sales agent
    sarvam_tts_sample_rate: int = 24000          # 24 kHz wideband OUTPUT — clearer than 16 kHz
                                                 # over WebRTC/Opus. Decoupled from the 16 kHz
                                                 # INPUT rate (audio_sample_rate) that VAD/STT need.
                                                 # Set back to 16000 to revert to narrowband.

    # --- LLM routing (shared by conversation, JSON and summaries) ---
    # Groq first: measured 2026-10-11 on this setup, first phrase p50 ~670 ms (gpt-oss-120b,
    # reasoning low) vs ~1.25 s for OpenAI gpt-6-luna. "openai" switches the order.
    llm_primary_provider: Literal['openai', 'groq'] = 'groq'

    # --- Groq LLM (Primary by default) ---
    groq_api_key: str = ""
    # Call disposition (csm/disposition.py): Jev (TypeSafe System One) labels each request in the call and
    # its result from the transcript when the call ends. Without a key, the labels come from call state.
    jev_api_key: str = ""
    jev_endpoint: str = "https://api.typesafe.ai/v1/systemone"
    jev_model: str = "jev-1.13.0"
    # llama-3.3-70b-versatile was retired from Groq's catalog (confirmed via
    # groq.Groq(...).models.list() on 2026-08-19 — a live call returns HTTP 404
    # "model_not_found", not a rate limit). The code degrades "gracefully" to
    # the OpenAI fallback, but only after paying real exponential-backoff
    # sleeps first (~1s, then ~2s per fresh session) — silent latency, not a
    # crash, which is why this went unnoticed. openai/gpt-oss-120b verified
    # live against this exact persona+accent prompt: 0.5-0.8s time-to-first-
    # token streaming, no <think> reasoning-trace leakage into the reply (a
    # real risk — qwen3.6-27b on this same account DOES leak its scratchpad
    # into message.content, which _extract_phrases would stream straight to
    # TTS on a live call).
    groq_model: str = "openai/gpt-oss-120b"
    groq_max_tokens: int = 1024
    groq_temperature: float = 0.7
    # gpt-oss thinks before its first spoken word; Groq accepts low/medium/high
    # (no "none"). Low keeps that pause short for voice. Only gpt-oss models.
    groq_reasoning_effort: Literal['low', 'medium', 'high'] = 'low'

    # --- OpenAI LLM (Fallback by default) & Embeddings ---
    openai_api_key: str = ""
    openai_model: str = ""  # Explicit selection overrides the legacy setting below.
    openai_fallback_model: str = "gpt-4o-mini"
    openai_reasoning_effort: Literal['none', 'low', 'medium', 'high', 'xhigh', 'max'] = 'none'
    # Fast mode (OpenAI accepts "priority" or "fast"), 2x token price. Measured
    # 2026-10-09 on Maya's prompt: first word p50 920 -> 708 ms, worst 1547 -> 873 ms.
    # "default" restores Standard. Sent only to the official OpenAI endpoint.
    openai_service_tier: Literal['default', 'priority', 'fast'] = 'default'
    openai_max_tokens: int = 1024
    openai_base_url: str = "https://api.openai.com/v1"
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_timeout_s: float = 3.0

    @property
    def effective_openai_model(self) -> str:
        return self.openai_model or self.openai_fallback_model

    @property
    def primary_llm_model(self) -> str:
        return self.effective_openai_model if self.available_llm_provider == 'openai' else self.groq_model

    @property
    def available_llm_provider(self) -> str:
        if self.llm_primary_provider == 'openai' and self.openai_api_key:
            return 'openai'
        if self.groq_api_key:
            return 'groq'
        return 'openai'

    # Max time the semantic-cache lookup may sit on the critical path. The embed
    # continues in the background past this and is still used to STORE the reply.
    semantic_cache_lookup_budget_ms: int = 150

    # --- Redis ---
    redis_url: str = "redis://localhost:6379/0"
    redis_password: str = ""
    redis_max_connections: int = 20
    redis_semantic_cache_ttl_faq: int = 86400
    redis_semantic_cache_threshold: float = 0.92

    # --- Kafka ---
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_transcription_topic: str = "transcription_queue"
    kafka_safety_topic: str = "safety_audit"
    kafka_analytics_topic: str = "analytics"
    kafka_client_id: str = "voice-agent"
    kafka_acks: int = 1

    # --- VAD ---
    vad_silence_threshold_ms: int = 1700
    vad_speech_threshold: float = 0.5
    vad_sample_rate: int = 16000
    vad_frame_duration_ms: int = 30
    vad_model_path: str = "silero_vad.onnx"

    # --- SIP Outbound (Phase 1 — Solar voice agent) ---
    # Outbound transport mode:
    #   "twilio" → place a real PSTN call via the LiveKit SIP trunk (costs money)
    #   "room"   → skip dialing; just open the LiveKit room so you can talk via the
    #              browser caller page (http://localhost:5000/caller) for free testing
    outbound_mode: str = "twilio"
    # SIP trunk ID from LiveKit (run sip_outbound.configure_trunk() once to get this)
    livekit_sip_trunk_id: str = ""
    # Twilio REST API credentials (call-log lookups, account checks)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""         # E.164, e.g. "+12184026902"
    # SIP termination — host is the trunk's Termination URI; username/password
    # come from the Twilio Credential List, NOT the account SID/auth token.
    twilio_sip_host: str = "pstn.twilio.com"
    twilio_sip_username: str = ""
    twilio_sip_password: str = ""

    # --- Agent Persona ---
    # Name of the prompt file to load from the prompts/ directory (without .py).
    # Example: AGENT_PERSONA=sales_agent  →  loads prompts/sales_agent.py
    agent_persona: str = "default"
    # Name of the accent file to load from the accents/ directory (without .py).
    # "neutral" (default) is a no-op — byte-for-byte today's behavior, zero
    # extra work on the TTS hot path. Any other value layers dialect vocabulary
    # onto the persona's prompt plus speaker/pace/temperature/dict_id onto TTS.
    # Example: AGENT_ACCENT=haryanvi  →  loads accents/haryanvi.py
    agent_accent: str = "neutral"
    # Language used for agent-initiated speech (greeting, silence nudge, resume)
    # when the text carries no script hint. Personas may override via LANGUAGE.
    speech_default_language: str = "hi"

    # --- Pipeline Tuning ---
    llm_phrase_buffer_max_ms: int = 80
    llm_processing_timeout_s: int = 3
    llm_backoff_base_s: float = 1.0
    llm_backoff_max_s: float = 30.0
    llm_backoff_max_retries: int = 3
    barge_in_cancel_timeout_ms: int = 300

    # Dead-air masking (from the 2026-07-10 Twilio recording audit: 15/42 real
    # calls had 3+ silent gaps of 2s+, worst 17.6s — silence reads as a dead
    # line). If a solar/vertical turn is still PROCESSING after this many
    # seconds, speak one short persona filler ("haan ji, ek second...") so the
    # prospect hears life while the LLM finishes. 0 disables. Keep above ~3s:
    # most warm turns reply in ~2.5s and fillers on every turn sound robotic.
    latency_filler_after_s: float = 3.0

    # Barge-in sensitivity: how long (ms) the prospect must speak CONTINUOUSLY
    # while Priya is talking before we treat it as a real interruption. Short
    # noises (cough, "hmm", "haan", background TV) under this threshold are
    # ignored so Priya doesn't stop mid-sentence for nothing. Real questions
    # ("ek minute, sawaal hai") exceed it and DO interrupt. Higher = Priya is
    # harder to interrupt (more "I'll finish my point" feel); lower = snappier.
    # Lowered 600->400ms on 2026-08-22: 600ms + the ~100ms VAD detect latency +
    # the 500ms silence-flush (livekit_handler.flush_audio) meant up to ~1.2s
    # between the prospect starting to talk and Priya actually going quiet on
    # the wire, which read as "ignoring the barge-in" in demo recordings.
    # 400ms still clears normal "hmm"/"haan" acknowledgements (typically
    # <300ms) while cutting the worst-case delay by a third.
    barge_in_min_speech_ms: int = 400

    # False-interruption recovery. Sustained line noise (TV, traffic, a cough
    # train) can pass the barge-in gate above, cutting the agent mid-sentence —
    # but a FALSE interruption never produces a transcript. If no final arrives
    # within this many seconds of the interrupt, the agent re-speaks the line it
    # was cut off from instead of sitting silent until the 14s nudge. At most
    # one resume per interrupted line (a second cut falls through to the silence
    # watchdog). 0 disables. Keep >= ~2s: a real interrupter's STT final lands
    # well inside 2s of speech-end.
    false_interrupt_resume_s: float = 2.0

    # Grace period before tearing down the agent when the last participant
    # disconnects. LiveKit fires disconnect events on transient network blips
    # even when the client reconnects within milliseconds — without this wait,
    # a reconnecting prospect lands in a dead session.
    participant_disconnect_grace_s: float = 3.0

    # Prospect-silence recovery (Task 8). If the prospect goes quiet in LISTENING
    # (thinking / distracted / phone down) the call would otherwise sit dead forever.
    # After this many seconds of silence we speak ONE re-engage line ("aap sun rahe
    # hain?"); if still silent after a SECOND interval we close warmly and hang up.
    # 0 disables the feature. Kept conservative so we never talk over a slow thinker.
    # 14s (was 8s): real prospects pause 10-13s to think mid-pitch, and the nudge was
    # firing right after every agent turn (call 20260531T154237 nudged 4x in 108s). The
    # watchdog also now counts silence only from when the AGENT stops talking, not the
    # prospect's last word — so this is genuine dead-air time, not think time.
    prospect_silence_reengage_s: float = 14.0

    # --- Session ---
    session_ttl_buffer_s: int = 60
    session_max_duration_s: int = 1800

    # --- Safety ---
    safety_violation_threshold: float = 0.85
    safety_groq_model: str = "llama-guard-3-8b"
    safety_blocklist_patterns: str = ""

    # --- Logging ---
    cost_tracking_enabled: bool = True
    cost_synthetic: bool = False  # Coordinator/worker sets this for offline fixtures.
    cost_async_write: bool = False
    managed_call_id: str = ""
    conversation_db: str = str(Path(__file__).resolve().parent / "data" / "conversations.sqlite3")
    studio_final_reply_wait_s: float = Field(default=8.0, ge=2, le=30)
    studio_playback_timeout_s: float = Field(default=20.0, ge=2, le=45)
    browser_session_timeout_s: int = Field(default=600, ge=30, le=3600)
    playground_conversation: bool = False
    # On: the agent follows the caller between languages mid-call. Every model reply starts
    # with a <lang:xx-IN> tag that sets the TTS language (demo_session.py), and a caller
    # sentence in another script switches fixed lines (transfer notice) too.
    # Off: the call language is the one selected in the frontend for the whole call.
    caller_language_switch: bool = True
    # Three gentle flow principles in the static prompt (use what was said, no
    # unprompted repeats but answer a repeated question fully, answer then one
    # forward question / act on an agreement). Off = previous prompt.
    agent_flow_principles: bool = False
    # Voice turn-taking: hold an unspoken reply while the caller is still talking,
    # treat "hmm"/"okay" as listening feedback, nudge once after a thinking pause,
    # answer over background noise, and use the provider's speech VAD to skip a
    # false "missed that". Off = the earlier reply-on-every-final behaviour.
    turn_completion: bool = True
    # Fictional knowledge base per demo preset (knowledge/data/*.json): catalogue in the
    # cached prompt, per-turn matches/lookups/quotes, and the rupee-amount fact guard.
    knowledge_enabled: bool = True
    knowledge_fact_guard: bool = True
    # Each preset's researched Sarvam bulbul:v4 voice (Sarvam's ranked use-case voices).
    preset_voices: bool = True
    # Semantic grace period only for recognizable incomplete voice turns.
    # Zero disables the grace period, while the speech/activity safety gate stays.
    # 0 (default, lowest latency): no extra wait after an unfinished-sounding
    # fragment. 650 restores the evaluated semantic hold.
    turn_hold_ms: int = Field(default=0, ge=0, le=1500)
    # After a final transcript, browser "sound" with no provider-VAD speech for
    # this long is background noise (fan, traffic), so Maya answers instead of
    # waiting the full playback timeout in silence. Voices still wait.
    release_wait_ms: int = Field(default=1500, ge=300, le=20000)
    # F3: after the caller goes quiet for this long (ms, browser release included),
    # ask Sarvam to finalise the transcript instead of waiting for its own
    # end-of-speech. Measured 2026-10-10: final about 200 ms after the flush;
    # without it, about 970 ms after speech in a real call. 0 disables.
    stt_flush_silence_ms: int = Field(default=350, ge=0, le=3000)
    # Local end-of-turn model for voice calls (needs TURN_COMPLETION).
    # "smart_turn" scores the caller's last audio when the browser reports the
    # end of speech: >= turn_complete_threshold -> finalise now; <= turn_incomplete_
    # threshold -> keep listening up to turn_incomplete_wait_ms; otherwise, or if
    # the model is missing, errors or is slower than turn_detector_budget_ms,
    # exactly the previous timing. Thresholds are cautious: published v3.2
    # results say Hindi 8.6% / Marathi 15% "finished" calls on unfinished speech
    # at 0.5. Empty model path = voice_module/models/smart-turn-v3.2-cpu.onnx.
    turn_detector: Literal["off", "smart_turn"] = "off"
    turn_detector_model: str = ""
    turn_detector_threads: int = Field(default=4, ge=1, le=16)
    turn_detector_budget_ms: int = Field(default=200, ge=20, le=1000)
    turn_complete_threshold: float = Field(default=0.7, ge=0.5, le=1.0)
    turn_incomplete_threshold: float = Field(default=0.3, ge=0.0, le=0.5)
    turn_incomplete_wait_ms: int = Field(default=1500, ge=0, le=4000)
    # F4: send the next sentence to Sarvam TTS while the current one is received.
    tts_send_ahead: bool = True
    # Item 1 (2026-10-11): recover a silent Sarvam socket fast. On: give up on first
    # audio after tts_recovery_first_audio_ms and between chunks after
    # tts_recovery_chunk_gap_ms, close the dead socket in the background, speak the
    # phrase (or its unspoken rest) on a fresh socket, REST only if that fails too,
    # and reopen the socket in the background after a barge-in. Off = previous
    # behaviour (2 s / 6 s waits, blocking close, REST, rest of phrase dropped).
    # Item 3 (2026-10-11): fewer seams. Each Sarvam request is a separate generation
    # (intonation restarts, silence at the join). On: after the first phrase, no
    # time-based mid-sentence cut below 400 chars, and sentences already waiting
    # are joined into one request up to tts_group_chars (same language, no waiting).
    tts_fewer_cuts: bool = False
    tts_group_chars: int = Field(default=200, ge=80, le=480)
    tts_fast_recovery: bool = False
    tts_recovery_first_audio_ms: int = Field(default=1200, ge=300, le=5000)
    tts_recovery_chunk_gap_ms: int = Field(default=2000, ge=500, le=6000)
    browser_max_turns: int = Field(default=80, ge=1, le=80)
    browser_context_bytes: int = Field(default=128000, ge=4000, le=256000)
    browser_session_budget_inr: float = Field(default=0, ge=0, allow_inf_nan=False)
    # Public contact shown by the local studio's "Let's talk" button; empty hides it.
    demo_contact_email: str = ""
    cost_log_dir: str = str(Path(__file__).resolve().parent / "usage")
    # Explicit planning assumption, not a fetched exchange rate.
    cost_usd_to_inr: float = Field(default=90.0, gt=0, allow_inf_nan=False)

    log_level: str = "INFO"
    log_format: str = "json"

    # --- PII ---
    pii_additional_patterns: str = ""

    @field_validator("vad_silence_threshold_ms")
    @classmethod
    def validate_vad_silence(cls, v: int) -> int:
        if not 200 <= v <= 3000:
            raise ValueError(
                f"vad_silence_threshold_ms must be 200-3000, got {v}."
            )
        return v

    @field_validator("vad_speech_threshold")
    @classmethod
    def validate_vad_speech_threshold(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"vad_speech_threshold must be 0.0-1.0, got {v}")
        return v

    @field_validator("safety_violation_threshold")
    @classmethod
    def validate_safety_threshold(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"safety_violation_threshold must be 0.0-1.0, got {v}")
        return v

    @field_validator("redis_semantic_cache_threshold")
    @classmethod
    def validate_cache_threshold(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"redis_semantic_cache_threshold must be 0.0-1.0, got {v}")
        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {allowed}, got {v}")
        return upper

    @field_validator("log_format")
    @classmethod
    def validate_log_format(cls, v: str) -> str:
        allowed = {"json", "console"}
        lower = v.lower()
        if lower not in allowed:
            raise ValueError(f"log_format must be one of {allowed}, got {v}")
        return lower
