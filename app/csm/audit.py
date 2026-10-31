"""Append-only audit trail (audit_events table). This module is the only writer of that table.

Kinds: call_started, turn, guardrail_hit, tool_proposed, policy_decision, tool_result, handoff,
call_ended. Every string value is PII-redacted on the way in, whatever the caller passed.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime

from csm.guardrails import redact


def _scrub(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _scrub(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v) for v in value]
    return value


class Audit:
    def __init__(self, conn: sqlite3.Connection, lock: threading.Lock | None = None, clock=datetime.now) -> None:
        self._db, self._lock, self._clock = conn, lock or threading.Lock(), clock

    def record(self, call_id: str | None, kind: str, **detail) -> None:
        payload = json.dumps(_scrub(detail), ensure_ascii=False, default=str)
        with self._lock:
            self._db.execute("INSERT INTO audit_events (at, call_id, kind, detail_json) VALUES (?,?,?,?)",
                             (self._clock().isoformat(timespec="milliseconds"), call_id, kind, payload))
            self._db.commit()

    def events(self, call_id: str | None = None, kinds: tuple[str, ...] = (), limit: int = 5000) -> list[dict]:
        sql, args = "SELECT seq, at, call_id, kind, detail_json FROM audit_events WHERE 1=1", []
        if call_id:
            sql += " AND call_id=?"
            args.append(call_id)
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            args.extend(kinds)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(limit)
        rows = self._db.execute(sql, args).fetchall()
        return [{"seq": r["seq"], "at": r["at"], "call_id": r["call_id"], "kind": r["kind"],
                 **json.loads(r["detail_json"])} for r in reversed(rows)]
