# Strip log: what was taken from Voice_Agent_temp and why (2026-10-30)

Source: `Voice_Agent_temp/voice_module` at the last commit (the uncommitted Maya SEO edits are excluded on purpose).
Method: the import closure of `demo_server.py` (the browser call path) = 35 of 83 modules, plus
`utils/pii_redactor.py` for the guardrails.

| Removed | Why |
|---|---|
| LiveKit transport, SIP outbound, runtime/phone | The browser WebSocket path doesn't need them. SIP comes back as an optional adapter in the stretch phase |
| runtime accounts / admission / google login / cookies / playground / waiting / worker / supervisor / trial | Multi-tenant trial hosting for Maya. Not needed for a single-bank demo |
| memory (Redis), async_workers (Kafka) | Not on the browser path; SQLite covers the demo. Named in the HLD as production scale-out |
| core.agent / core.session / core.state, main.py, managed_server.py | The LiveKit agent runtime |
| Real-estate / solar / sales / interviewer prompts, objections, pronunciations, accent dictionaries | Domain content for other products |
| LiveKit, Redis, Kafka, Google packages in requirements | Unused |
| Env: Google OAuth, LiveKit, Redis, Kafka secrets, AGENT_ACCENT, AGENT_PERSONA | Least privilege: the hackathon host shouldn't hold Drevan's Google/LiveKit secrets |

Kept temporarily until P3/P4 replace them: `prompts/international_demo.py`, `preset_catalog.py`,
`demo_presets.json`, `knowledge/data/*.json`, `demo_web/`.
