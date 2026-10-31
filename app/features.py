"""Capability flags with milestone presets (HLD section 4).

MILESTONE=1|2|3 picks a preset; FEATURE_<NAME>=true|false overrides one capability.
The server is the source of truth: tools and routes check `enabled()`; the UI only reads
`snapshot()` to decide what to draw. Security basics are deliberately NOT flags.
"""
from __future__ import annotations

import hmac
import os
import threading
from typing import Mapping

FEATURES = (
    "VOICE_CALL", "FAQ_KNOWLEDGE", "TICKETS", "ACCOUNT_TOOLS",    # milestone 1
    "WARM_HANDOFF", "MULTILINGUAL",                               # milestone 2
    "RISK_SIGNALS", "SUPERVISOR_DASHBOARD", "EVALS",              # milestone 3
    "SIP_OUTBOUND",                                               # manual only
)

# Mapped to the real briefs (31 Oct). Level 1 of #05 requires verification and retrieval from the
# simulated banking/lending systems, so ACCOUNT_TOOLS is milestone 1.
PRESETS: dict[int, frozenset[str]] = {
    1: frozenset({"VOICE_CALL", "FAQ_KNOWLEDGE", "TICKETS", "ACCOUNT_TOOLS"}),
    2: frozenset({"VOICE_CALL", "FAQ_KNOWLEDGE", "TICKETS", "ACCOUNT_TOOLS", "WARM_HANDOFF", "MULTILINGUAL"}),
    3: frozenset({"VOICE_CALL", "FAQ_KNOWLEDGE", "TICKETS", "ACCOUNT_TOOLS", "WARM_HANDOFF", "MULTILINGUAL",
                  "RISK_SIGNALS", "SUPERVISOR_DASHBOARD", "EVALS"}),
}

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def resolve(env: Mapping[str, str]) -> tuple[int, frozenset[str]]:
    """Pure resolution of milestone preset + overrides (unit-tested)."""
    try:
        milestone = int(env.get("MILESTONE", "1"))
    except ValueError:
        milestone = 1
    milestone = min(max(milestone, 1), max(PRESETS))
    active = set(PRESETS[milestone])
    for name in FEATURES:
        raw = env.get(f"FEATURE_{name}")
        if raw is None:
            continue
        value = raw.strip().lower()
        if value in _TRUE:
            active.add(name)
        elif value in _FALSE:
            active.discard(name)
    return milestone, frozenset(active)


class FeatureFlags:
    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = dict(os.environ if env is None else env)
        self._lock = threading.Lock()
        self.milestone, self._active = resolve(self._env)

    def enabled(self, name: str) -> bool:
        if name not in FEATURES:
            raise KeyError(f"Unknown feature {name!r}")
        return name in self._active

    def snapshot(self) -> dict:
        return {"milestone": self.milestone, "features": {n: n in self._active for n in FEATURES}}

    def set_milestone(self, milestone: int, token: str) -> dict:
        """Live switch for the demo (after a mentor signs off). Env overrides still win."""
        expected = self._env.get("ADMIN_TOKEN", "")
        if not expected or expected == "change-me-long-random" or not hmac.compare_digest(token or "", expected):
            raise PermissionError("Invalid admin token")
        with self._lock:
            self.milestone, self._active = resolve({**self._env, "MILESTONE": str(milestone)})
        return self.snapshot()


_flags: FeatureFlags | None = None


def flags() -> FeatureFlags:
    """Process-wide flags, loaded once (after .env has been read into the environment)."""
    global _flags
    if _flags is None:
        _flags = FeatureFlags()
    return _flags
