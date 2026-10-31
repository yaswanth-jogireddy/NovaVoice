"""Every scripted scenario in evals/scenarios must pass offline (rules router, no network)."""
import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
import run as evals  # noqa: E402

SCENARIOS = evals.load()


def test_suite_has_golden_edge_and_adversarial():
    suites = {s["suite"] for s in SCENARIOS}
    assert suites == {"golden", "edge", "adversarial", "live_calls", "level1"} and len(SCENARIOS) >= 40


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_scenario_offline(scenario):
    result = asyncio.run(evals.run_scenario(scenario, live=False))
    assert result["passed"], result["failures"]
    assert not result["leaks"]


def test_checker_can_fail():
    """A test that cannot fail is not a test: a wrong expectation must be reported."""
    wrong = {**SCENARIOS[0], "expect": {"outcome": "handoff", "tools_ok": ["block_card"]}}
    result = asyncio.run(evals.run_scenario(wrong, live=False))
    assert not result["passed"] and len(result["failures"]) == 2


def test_audit_trail_is_append_only(seeded_db):
    from csm.orchestrator import Services
    svc = Services(seeded_db)
    svc.audit.record("c1", "turn", text="hello")
    for sql in ("UPDATE audit_events SET kind='x'", "DELETE FROM audit_events"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            svc.conn.execute(sql)
    svc.close()
