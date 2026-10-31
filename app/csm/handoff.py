"""Warm handoff: a packet the human agent reads instead of asking the customer to repeat themselves.

The packet is built by code from CallState (no model in the loop, so nothing in it is invented),
saved to the `handoffs` table (this module is its only writer) and pushed to /ws/desk subscribers.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import sqlite3
import threading
from datetime import datetime

from csm.guardrails import redact

INTENT_NAMES = {"get_balance": "balance enquiry", "recent_transactions": "recent transactions",
                "block_card": "lost/stolen card", "request_statement": "statement request",
                "loan_emi_info": "loan EMI enquiry", "report_emi_issue": "EMI issue ticket",
                "raise_dispute": "transaction dispute",
                "ticket_status": "ticket status", "create_ticket": "service request",
                "search_policy": "policy question", "handoff_to_human": "asked for a person",
                "customer_profile": "profile / customer ID", "list_cards": "card status",
                "loan_application_status": "loan application status", "request_loan_document": "loan document request"}
NEXT_STEP = {"identity_failed": "Verify the caller through the branch / video KYC process before any account action.",
             "risk_flag": "Possible social engineering. Treat as a fraud-desk call; do not share codes; consider an account alert.",
             "out_of_scope": "Caller needs a decision or advice the AI may not give (credit, fees, investments).",
             "negative_sentiment": "Caller is upset. Acknowledge, then resolve the open item.",
             "caller_request": "Caller asked for a person. Continue from the open item below.",
             "tool_failure": "A system action failed. Retry it manually.",
             "complex_issue": "Issue outside L1 tools. Continue from the summary."}


def build_packet(state, history: list[dict], reason: str, note: str = "") -> dict:
    intents = [INTENT_NAMES.get(i, i) for i in dict.fromkeys(state.intents)]
    who = (f"{state.first_name} ({state.customer_id}), verified" if state.verified
           else f"Unverified caller (claimed {state.claimed_ref})" if state.claimed_ref else "Unverified caller")
    done = "; ".join(a["summary"] for a in state.actions) or "nothing completed yet"
    summary = f"{who}. Asked about: {', '.join(intents) or 'not stated'}. Done: {done}."
    if state.risk_flags:
        summary += f" Risk cues: {', '.join(f.replace('_', ' ') for f in state.risk_flags)}."
    return {
        "handoff_id": "H-" + secrets.token_hex(4).upper(),
        "call_id": state.call_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "reason": reason,
        "summary": redact(summary + (f" Note: {note}" if note else "")),
        "customer": {"id": state.customer_id, "first_name": state.first_name} if state.verified else None,
        "identity": state.identity,
        "intents": intents,
        "actions": [a["summary"] for a in state.actions],
        "tickets": list(state.tickets),
        "sentiment": "negative" if state.negative_turns else "neutral",
        "risk_flags": list(state.risk_flags),
        "policy_docs": list(dict.fromkeys(state.kb_docs)),
        "language": state.language,
        "last_turns": [{"role": m["role"], "text": redact(m["content"])[:300]} for m in history[-6:]],
        "suggested_next_step": NEXT_STEP.get(reason, NEXT_STEP["complex_issue"]),
    }


class DeskHub:
    """In-process fan-out to Agent Desk websockets. Production: Redis pub/sub (HLD section 8)."""

    def __init__(self) -> None:
        self.clients: set = set()

    async def publish(self, message: dict) -> None:
        for ws in list(self.clients):
            try:
                await ws.send_json(message)
            except Exception:
                self.clients.discard(ws)


class HandoffService:
    def __init__(self, conn: sqlite3.Connection, hub: DeskHub | None = None, lock: threading.Lock | None = None) -> None:
        self._db, self.hub, self._lock = conn, hub or DeskHub(), lock or threading.Lock()
        self._tasks: set = set()

    def create(self, packet: dict) -> dict:
        with self._lock:
            self._db.execute("INSERT INTO handoffs (id, call_id, customer_id, reason, packet_json, status, created_at) "
                             "VALUES (?,?,?,?,?,?,?)",
                             (packet["handoff_id"], packet["call_id"], (packet.get("customer") or {}).get("id"),
                              packet["reason"], json.dumps(packet, ensure_ascii=False), "waiting", packet["created_at"]))
            self._db.commit()
        with contextlib.suppress(RuntimeError):   # no running loop in sync tests: persisted, not pushed
            task = asyncio.get_running_loop().create_task(self.hub.publish({"type": "handoff", "packet": packet}))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return packet

    def list(self, status: str | None = None, limit: int = 50) -> list[dict]:
        sql, args = "SELECT packet_json, status, accepted_by FROM handoffs", []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        rows = self._db.execute(sql + " ORDER BY created_at DESC LIMIT ?", (*args, limit)).fetchall()
        return [{**json.loads(r["packet_json"]), "status": r["status"], "accepted_by": r["accepted_by"]} for r in rows]

    def accept(self, handoff_id: str, agent: str) -> bool:
        with self._lock:
            cur = self._db.execute("UPDATE handoffs SET status='accepted', accepted_by=? WHERE id=? AND status='waiting'",
                                   (agent[:40], handoff_id))
            self._db.commit()
        return cur.rowcount == 1
