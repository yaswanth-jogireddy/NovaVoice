"""Item 5: local end-of-turn model (Smart Turn v3, BSD-2-Clause, pipecat-ai).

Scores the caller's recent audio: probability that they have finished their
turn. Optional: a missing model or onnxruntime returns None from load(), and
the session keeps its previous timing.

Model: smart-turn-v3.2-cpu.onnx from huggingface.co/pipecat-ai/smart-turn-v3
(sha256 2bb02631...7e4f, 8.7 MB), kept out of git in voice_module/models/.
Input as in pipecat's LocalSmartTurnAnalyzerV3: 16 kHz mono, last 8 s,
zero-padded at the start, Whisper log-mel features (80 x 800).
"""
from pathlib import Path

import numpy as np

from utils.logger import get_logger

logger = get_logger(__name__)

SAMPLE_RATE = 16000
WINDOW_SAMPLES = 8 * SAMPLE_RATE
DEFAULT_MODEL = Path(__file__).resolve().parents[1] / 'models' / 'smart-turn-v3.2-cpu.onnx'


class SmartTurn:
    def __init__(self, session):
        self._session = session

    @classmethod
    def load(cls, path=None, threads=4):
        """The model, or None (and a log line) when it cannot be used here.

        Measured 2026-10-11 on the dev PC: ~120 ms per decision on 1 thread,
        ~51 ms on 4 (features ~13 ms more).
        """
        model = Path(path) if path else DEFAULT_MODEL
        if not model.is_file():
            logger.warning("turn_detector_model_missing")
            return None
        try:
            import onnxruntime as ort
            options = ort.SessionOptions()
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            options.inter_op_num_threads = 1
            options.intra_op_num_threads = max(1, int(threads))
            options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            return cls(ort.InferenceSession(str(model), sess_options=options, providers=['CPUExecutionProvider']))
        except Exception as exc:
            logger.warning("turn_detector_unavailable", code=type(exc).__name__)
            return None

    def predict(self, pcm16):
        """Probability (0..1) that the caller has finished, from 16 kHz PCM16 bytes."""
        from utils.whisper_features import compute_whisper_log_mel_features
        audio = np.frombuffer(pcm16[:len(pcm16) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
        audio = audio[-WINDOW_SAMPLES:]
        if audio.size < WINDOW_SAMPLES:
            audio = np.pad(audio, (WINDOW_SAMPLES - audio.size, 0))
        features = np.expand_dims(compute_whisper_log_mel_features(audio, do_normalize=True), axis=0)
        outputs = self._session.run(None, {'input_features': features})
        return float(np.asarray(outputs[0]).reshape(-1)[0])
