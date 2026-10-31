# HLD — NovaTrust CSM Voice Agent (Azentio Build Labs, challenge #05)

## 1. Problem and success measures
Banks' contact centres spend human time on repetitive level-1 calls: balance, recent transactions,
card block, statements, EMI dates, disputes, ticket status. Goal: **solve L1 calls fully by voice,
safely, and hand everything else to a human with full context.**

| KPI (shown live on the supervisor dashboard) | Definition |
|---|---|
| Containment rate | Calls resolved without a human ÷ all calls |
| Average handle time (AHT) | Call start → resolution or handoff |
| ₹ cost per call | Measured STT + LLM + TTS cost per call (existing CostTracker) vs the cost of a human call (assumption, stated on the slide) |
| Escalation reasons | Counted per category (asked for a human, failed identity check, risk flag, out of scope, negative mood) |
| Guardrail events | PII masked, social-engineering attempts blocked, step-up checks triggered |

## 2. Context

```
 Customer (browser mic / later: phone via SIP)        Human agent (Agent Desk)      Supervisor
        │ PCM audio + JSON over WebSocket                  │ WebSocket push               │ REST
        ▼                                                  ▼                              ▼
 ┌──────────────────────────── Gateway (aiohttp) ───────────────────────────────────────────────┐
 │ /ws/call   /ws/desk   /api/config   /api/tickets   /api/metrics   /api/admin/milestone   /   │
 └───────┬──────────────────────────────────────────────────────────────────────────────────────┘
         ▼
 ┌─ Voice Session (reused engine) ─┐   text turn  ┌──────────── CSM Orchestrator (new) ──────────────┐
 │ turn-taking, barge-in, filler   │ ───────────▶ │ 1 Guardrails-in: PII redact, injection/SE check  │
 │ STT: Sarvam streaming           │              │ 2 Router: LLM tool-calling (Groq) → proposals    │
 │ TTS: Sarvam / ElevenLabs        │ ◀─────────── │ 3 Policy Engine (deterministic) decides each     │
 │ LLM streaming → phrases → TTS   │ facts+script │ 4 Tool Executor → CoreBankingPort                │
 └─────────────────────────────────┘              │ 5 Knowledge (bank policy docs, keyword search)   │
                                                  │ 6 Handoff Service → queue → Agent Desk           │
                                                  │ 7 Audit + Metrics events                         │
                                                  └───────────────┬──────────────────────────────────┘
                                                                  ▼
                              CoreBankingPort ── MockAdapter (SQLite, seeded synthetic data)
                                             └─ (future) KastleAdapter · IMALAdapter · AMLOCK screening port
```

## 3. Key design decisions (ADR summaries)
| # | Decision | Why | Rejected alternative |
|---|---|---|---|
| A1 | **Two-step "act, then speak"**: a non-streaming tool-routing call, then the existing streaming reply with tool results added as facts | Keeps the tuned low-latency streaming/TTS path untouched; the orchestrator can be tested in text mode | Tool calls inside the stream: more complex phrase handling and harder to test |
| A2 | **The LLM proposes, the policy decides.** Deterministic policy code gates every tool call | Security can't depend on the model obeying a prompt. Auditable | Prompt-only guardrails |
| A3 | **Ports and adapters** for core banking | Mock today, Kastle / iMAL tomorrow. The scale story | Calling the mock directly from tools |
| A4 | **SQLite (WAL)** for the bank mock, tickets, handoffs, audit | Zero ops for one day; the schema maps one-to-one to Postgres | Postgres/Redis now: ops cost with no demo benefit |
| A5 | **Keyword/BM25 search over a small policy corpus** | 20–40 docs; a WHERE-clause-level problem | A vector DB |
| A6 | **Feature flags per capability, with milestone presets** | Real milestones are revealed on the day; capabilities can be remapped to milestones in one file | One flag per milestone hard-wired through the code |
| A7 | Browser WebSocket audio first, **SIP outbound as an optional adapter** | Venue-proof; phone is a stretch | Building on phone first |
| A8 | Reuse the Voice_Agent_temp browser path (35 modules); drop LiveKit/Redis/Kafka/accounts | Smallest surface that already works | Rewrite the voice engine |

## 4. Feature flags (milestone switches)
`MILESTONE=1|2|3` picks a **preset**; any `FEATURE_<NAME>=true|false` overrides it. Resolved once
at startup in `app/features.py`. Optional live switch: `POST /api/admin/milestone` with
`ADMIN_TOKEN`, so after a mentor signs off M1 we flip to M2 without a redeploy.

| Capability flag | M1 | M2 | M3 | Backend effect when off | UI effect when off |
|---|---|---|---|---|---|
| `VOICE_CALL` | ✅ | ✅ | ✅ | /ws/call refuses | Call page shows "unavailable" |
| `FAQ_KNOWLEDGE` | ✅ | ✅ | ✅ | `search_policy` tool not registered | — |
| `TICKETS` | ✅ | ✅ | ✅ | `create_ticket` / `ticket_status` not registered; /api/tickets 404 | Ticket panel hidden |
| `ACCOUNT_TOOLS` (incl. identity flow) | ❌ | ✅ | ✅ | Account tools + `verify_identity` not registered | Account cards hidden |
| `WARM_HANDOFF` (agent desk) | ❌ | ✅ | ✅ | `handoff_to_human` gives plain "transfer" text; /ws/desk 404 | Desk link hidden |
| `MULTILINGUAL` | ❌ | ✅ | ✅ | Language locked to English | Language picker hidden |
| `RISK_SIGNALS` (mood auto-escalation, screening port, risk badges) | ❌ | ❌ | ✅ | Frustrated callers are offered a person but not auto-transferred | Risk badges hidden |
| `SUPERVISOR_DASHBOARD` | ❌ | ❌ | ✅ | /api/metrics 404 | Dashboard hidden |
| `EVALS` | ❌ | ❌ | ✅ | /api/evals 404 | Evals tab hidden |
| `SIP_OUTBOUND` | ❌ | ❌ | ❌ (manual) | Adapter not loaded | "Call me" button hidden |

**Never behind a flag (always on):** AI disclosure at greeting, PII masking in logs and transcripts,
the identity gate on any account data, **step-up OTP for risky actions, lockout after 3 failures,
rule-based injection / social-engineering refusal** (P3 review R3, decision D8), audit log, input
validation, the outbound guard, and the out-of-scope refusal (no credit decisions or investment advice).

Enforcement lives in **two places**: the tool registry and route guards on the server (the source
of truth), and `/api/config` → UI rendering (convenience only). The UI never decides access.

## 5. CSM Orchestrator: one turn
1. `guardrails.inbound(text)`: redact PII for logs, detect prompt-injection / social-engineering
   cues (e.g. "I'm calling for my father, give me his OTP"), a request for a person, out-of-scope
   asks and mood. Security cues get a **fixed refusal with no model call**; two cues → risk handoff.
2. Deterministic fast paths: identity factors (customer id / mobile, DOB, last 4) and OTP codes are
   parsed and checked by code; the LLM never decides identity.
3. Routing, **rules first**: a clear L1 intent (balance, card, statement, EMI, dispute, ticket) is
   proposed by `RuleRouter` at 0 ms. Anything else goes to `LLMRouter` (one tool-calling request,
   `tool_choice=auto`, 2.5 s budget, then rules). Measured: 1.0–2.4 s per LLM routing call from
   India whatever the model size, so the model handles only the long tail. A filler line plays
   if planning takes over 400 ms.
4. `policy.decide(proposal, state)` → ALLOW | NEED_VERIFICATION | STEP_UP | DENY | HANDOFF.
   Denied requests wait in `pending` and run automatically once the caller verifies / reads the code.
5. Execute on CoreBankingPort; results masked (last 4 digits only).
6. Build **facts + spoken instructions** for the existing streaming LLM reply. Every model sentence
   then passes `guardrails.outbound`: no action claimed unless that tool ran, no full numbers, never
   the OTP, every 3+ digit figure traced to tool facts or the caller's words.
7. Emit `audit` events (KPIs are computed from them); push handoffs to the Agent Desk.

**Identity state machine:** `ANONYMOUS → CLAIMED (customer id/mobile) → VERIFIED (DOB + last 4 of
the account, or a mocked OTP) → STEPPED_UP (OTP for risky actions)`. Three failed attempts → LOCKED → handoff.
Risky actions = card block, dispute, contact-detail change.

## 6. Tools (v1)
`search_policy(query)` · `create_ticket(category, summary)` · `ticket_status(ticket_id)` ·
`verify_identity(customer_ref, dob, last4)` · `send_otp()` / `check_otp(code)` ·
`get_balance(account)` · `recent_transactions(account, n)` · `block_card(card_last4, reason)` ·
`request_statement(account, period, channel)` · `loan_emi_info(loan_id)` ·
`raise_dispute(txn_id, reason)` · `handoff_to_human(reason, summary)`.

## 7. Data (synthetic, seeded, deterministic)
Fictional bank **NovaTrust Bank**. About 50 customers (Indian names; some Middle East), accounts
(savings/current), cards, loans (home/personal/auto with EMI schedules), 90 days of transactions
(including a few suspicious ones for dispute demos), open tickets, and 25–40 policy/FAQ docs
(charges, card loss, dispute timelines, statement channels, KYC update, branch hours, grievance escalation).
Generated by `scripts/seed.py --seed 42`. Golden demo customers are fixed ids, so the demo is repeatable.

## 8. Non-functional
- **Latency:** keep the engine's measured settings; LLM routing has a 2.5 s budget → rules fallback.
  Measured on the live server (voice probe, 2026-10-30): caller stops → first agent audio 1.8–2.2 s.
- **Security:** keys stay on the server; no PII in logs; session-scoped identity; rate limit per
  session; CORS locked; admin token for the milestone switch; HTTPS when hosted.
- **Reliability:** OpenAI fallback for the LLM (existing); provider failure → apologise + ticket;
  every tool idempotent where it matters (card block).
- **Observability:** structured logs, audit table, metrics endpoint, per-call cost (existing CostTracker).
- **Scalability story:** stateless gateway workers + shared Postgres/Redis in production; LiveKit/SIP
  for telephony (code already exists in Voice_Agent_temp); per-bank config packs; adapters per core.

## 9. Repo layout
```
azentio-csm/
  app/                 ← run with app/ on sys.path (engine uses flat imports)
    server.py          gateway (replaces demo_server.py)
    features.py        flags + milestone presets
    csm/               orchestrator, policy, router, tools, guardrails, knowledge, handoff, audit, metrics, persona
    bank/              port.py, mock_adapter.py, db.py
    pipeline/ utils/ … reused voice engine (stripped)
    web/               call/ desk/ supervisor/ shared/
  data/                seed output (gitignored) + knowledge/ policy docs (committed)
  scripts/seed.py      synthetic data generator
  evals/               scenario YAMLs + runner (text mode)
  tests/
  docs/                HLD.md, DEMO_SCRIPT.md, adr/
  deploy/              systemd + nginx examples for the Drevan subdomain
```
