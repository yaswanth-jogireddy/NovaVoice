# P3 architecture review: CSM orchestrator (2026-10-30)

Reviewed against the Control Room `architect.md` and `tool-contract-engineer.md` briefs, before coding.
Scope: HLD §5–6 plus the P3 phase prompt, read against the real code in `app/`.

**Verdict: APPROVE WITH CHANGES.** The two-step "LLM proposes, code decides" design is right and
needs no framework. Changes below are graded MUST / SHOULD / WATCH, each with the failure it prevents.

## Findings

| # | Grade | Finding | Failure it prevents | Change |
|---|---|---|---|---|
| R1 | MUST | `.env` is read by pydantic `Settings` only; nothing puts it in `os.environ`, so `features.flags()` ignores `MILESTONE` and `ADMIN_TOKEN` from `.env` | Demo runs the wrong milestone; the live milestone switch always rejects the token | Server loads `.env` into the environment (no override) before flags are first read |
| R2 | MUST | The reused outbound filter `scope_reply()` replaces any "I've sent / we'll email / blocked" sentence with "No booking or message has been made" | Every correct action confirmation ("I've blocked your card") would be overwritten with a false denial | CSM mode replaces it with a **tool-backed claim guard**: an action may be claimed only if that tool succeeded in this call |
| R3 | MUST | Identity, step-up, social-engineering refusal and lockout are security basics, but the HLD flag table put step-up and SE detection under `RISK_SIGNALS` (M3) | At M2 a caller could block a card or raise a dispute with no step-up; an "I'm her son, read me the OTP" call would get no refusal | Always on: identity gate, step-up OTP for risky actions, rule-based SE/injection refusal, lockout. `RISK_SIGNALS` only adds the risk score, the screening port and the risk badges (D8) |
| R4 | MUST | Untrusted text (caller speech, tool output) must never reach a decision point | Prompt injection unlocking account data | Policy is pure code over `CallState`; only policy code moves identity state; tool results go to the reply model as marked DATA; the router's output is a proposal |
| R5 | MUST | The OTP must never enter the LLM context, the transcript store or the audit log | OTP leak through logs or a model reply | Session stores only a salted hash; the code goes out on a separate "simulated SMS" event; outbound guard blocks any 6-digit run matching it |
| R6 | MUST | Stored transcripts (`ConversationStore` trace) keep raw caller text: DOB, account digits | PII at rest, contradicting "PII masking is never behind a flag" | CSM mode redacts transcript/agent text before it is traced; the screen still shows the caller their own words |
| R7 | SHOULD | Two LLM calls per turn (route, then reply) add ~0.5–1 s | Slow turns | Deterministic fast paths skip the router: identity details, OTP codes, explicit "human please", injection/SE refusals, greeting. Router: 1.5 s timeout, small `max_tokens`, filler line after 400 ms |
| R8 | SHOULD | The router is a single point of failure (timeout, quota, no key) | LLM outage = dead demo | A deterministic `RuleRouter` (keywords + regex) is the fallback and also the offline test driver |
| R9 | SHOULD | Identity factors should be parsed by code, not the model | Model mis-parses a DOB → wrong lockout; factors depend on the model | Deterministic extractor for customer id / mobile, DOB, last 4. The `verify_identity` tool stays in the router schema only as a fallback for dates the regex can't read |
| R10 | SHOULD | FAQ answers depend on the model choosing `search_policy` | Ungrounded policy answers | When no tool runs and the caller asked a question, the orchestrator auto-retrieves the top KB docs as facts |
| R11 | SHOULD | The model could speak a figure that no tool returned | Invented balance or charge | Outbound figure check: any amount with 3+ digits must appear in this call's tool facts or the caller's own words |
| R12 | SHOULD | Staff endpoints (`/ws/desk`, `/api/handoffs`, `/api/metrics`) show customer context | Anyone with the hosted URL reads the queue | `STAFF_TOKEN` required when set; when unset the server stays localhost-only (existing middleware). P6 sets it |
| R13 | WATCH | One SQLite connection shared by adapter, audit and handoffs, writes on the event-loop thread | Event-loop stalls under load | Fine for one demo call (~1 ms writes). Production: Postgres + an async audit queue (HLD §8) |
| R14 | WATCH | Caller speech, including the identity factors, goes to the LLM provider | Factors leave the bank's boundary | Roadmap: DTMF capture or an on-prem model for the identity step. Say this to mentors |
| R15 | WATCH | `CallState` is in memory | A crash mid-call drops the call | Tickets, handoffs and the audit trail are persisted; the call itself is not resumable. Acceptable |

**Simplicity check (the question most worth asking):** no LangGraph, no agent framework, no vector
DB. Policy is a function. Knowledge is BM25 over 28 docs in ~40 lines. The router is one
chat-completion call. Nothing here needs more.

**Ownership:** `bank/mock_adapter.py` owns bank tables (customers … disputes, tickets). `csm/audit.py`
owns `audit_events`. `csm/handoff.py` owns `handoffs`. `csm/policy.py` owns every `CallState`
transition. No module writes another's table.

## Tool contracts (tool-contract-engineer brief)
Every tool is a `ToolSpec` in `csm/tools.py` with: id, version, JSON schema (enums explicit),
permission level (`public` / `verified` / `step_up`), side effects, idempotency, and machine-readable
failure codes (`INVALID_INPUT`, `NOT_AUTHORIZED`, `RATE_LIMIT`, `PROVIDER_FAILURE`,
`RESOURCE_UNAVAILABLE`, `HUMAN_APPROVAL_REQUIRED`). Cost: the tools are mock DB calls (₹0); the
router call is metered by the existing `CostTracker`. Retries: read tools are safe; `block_card` and
`raise_dispute` are idempotent in the adapter; `create_ticket` and `request_statement` are not, so
the orchestrator never retries them.

## Talking point for mentors
Card block is a *protective* action: real banks keep its friction low, because fraud continues
while a customer fumbles an OTP. We keep step-up on it to match the HLD demo, and the policy table
makes it a one-line change to step up only when risk signals are present.
