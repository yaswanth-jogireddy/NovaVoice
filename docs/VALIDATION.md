# Validation — 31 October 2026

Full local suite: **352 passed, 14 failed** (366 tests).

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests -p no:cacheprovider
```

- All 11 implicit-hangup regression cases passed: farewell playback before closure, model hangup tags, question protection, cancellation on a new request, and silence closure. Model, speech and socket providers are simulated; this is not a live microphone test.
- 13 failures in `tests/test_conversation_recovery.py` cover pending-action cancellation, corrections during verification, spoken references, retries and OTP renewal. These were already documented in [the handover](HANDOVER.md).
- One failure in `tests/test_csm_guardrails.py::test_male_persona_gets_masculine_hindi`: the current runtime prompt contains the full prompt reference and does not substitute the selected persona name as expected.
- The Level 2 evaluation pack in `docs/evals/level2/` is a specification; its result rows remain `NOT_RUN`.
