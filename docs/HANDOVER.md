# Handover: CSM voice agent, branch `yaswanth/csm-handover`

Base: `origin/main` at `85fb019` ("Cost-effective vs Standard…", 31 Oct 14:04). This branch goes to prod.

## What you need to do
**The main thing: prod renders the agent's prompt through the CSM.** That is the decided architecture. The CSM
builds what the model gets on every turn from `app/prompts/prompt.md`: the system prompt, plus the per-turn note
with facts, instructions and the tool's answer guide. Prod must use that render, not a separate or hard-coded
prompt. `docs/PROMPT_AND_TOOLS.md` shows the render exactly.

Along with it, for prod you only need:

1. **The prompt: `app/prompts/prompt.md`, as is.** See "The prompt" below.
2. **Implicit language switch.** The agent follows the caller between English and Hindi on its own.
3. **Jev.** Each request in a call and its result are labelled when the call ends. Set `JEV_API_KEY`.
4. **A few smaller pieces that come with the branch** (table at the end).

Nothing else.

Keys for `.env` (ask Yaswanth; never commit them): `JEV_API_KEY`, and `SMALLEST_API_KEY` only if you use the
Smallest AI voice.

## 1. The prompt
- **Source:** `app/prompts/prompt.md`. It is read again at the start of every call, so an edit applies to the next
  call without a restart (`persona.load_prompt()` in `csm/persona.py`). HTML comments are not sent.
  `{name}` and `{bank}` are filled in per call.
- **Render:** `docs/PROMPT_AND_TOOLS.md` shows exactly what the model gets:
  1. the system prompt;
  2. the per-turn note;
  3. every tool and its answer guide;
  4. the fixed lines;
  5. the router prompt.

  Regenerate it after any prompt or tool change: `.venv\Scripts\python.exe scripts\prompt_reference.py`.
- **What the code adds to it:** the voice's gender line; on a fixed-language call, a language lock in place of the
  `## Language` section. Every turn also gets a note with the caller's identity, TURN FACTS, INSTRUCTIONS and the
  current language. The one tool's `## Tool answers` entry is sent only on the turn that tool ran.
- **Keep when editing** (the code reads these):
  - `{name}`, `{bank}`;
  - the `## Language` heading and the `<lang:` tags;
  - `<HANGUP>`;
  - `## Tool answers` with `### tool_name` and Say / Add / Never lines.
- Exact wording that must never vary (greeting, refusals, OTP, transfer, EMI ticket) stays in `persona.py` `LINES`,
  not in the prompt.

## 2. Implicit language switch
- **How it works:**
  1. prompt.md's `## Language` section makes every reply start with `<lang:en-IN>` or `<lang:hi-IN>`.
  2. The code strips the tag (it is never spoken or saved) and uses it as the speech language for that reply.
  3. That language also becomes the call's language, so fixed lines, the "one moment" clips and the next turn's
     note follow.
- **The caller doesn't press anything.** Speaking Hindi (2+ Hindi words) or English (5+ real English words;
  numbers and IDs like C1001 don't count) switches the call. So does asking: "speak in Hindi", "हिंदी में बात करें".
- **Extras:** the on-screen picker also works mid-call. A language picked there holds until the caller picks again
  or asks out loud. A Hinglish call, or `CALLER_LANGUAGE_SWITCH=false`, keeps one language for the whole call.
- **Code** (`app/demo_session.py`):
  - `LANG_TAG`, `TAG_LANGUAGES` (en, hi);
  - `_take_lang_tag` (reply tag → speech and call language);
  - `_follow_script` (caller's script);
  - `switch_language` (spoken request);
  - `set_language_from_ui` (picker);
  - `_language_note` (per-turn reminder);
  - `_static_system` (fixed-language calls).
- **Also:**
  - Hindi fixed lines in `csm/persona.py`;
  - `caller_language_switch` (now `True` by default) in `config.py`;
  - `set_language` in `demo_server.py` and `web/call.js`.
- **Tests:** `tests/test_language_tag.py`.

## 3. Jev
- **What it sends:** when a call ends, only the transcript (no tool data) goes to Jev (TypeSafe System One,
  `jev-1.13.0`) in one request.
- **What it answers:** up to 4 questions, "the caller's 1st/2nd/3rd/4th request". Each answer is one label that
  combines the request type and its result: `balance__SOLVED`, `emi_issue__TICKET_CREATED`, or `NONE`. It also
  gives the caller's mood.
- **What the code keeps:** a typed record with each request and its result (Solved / Ticket created / Transferred /
  Not solved), an overall outcome, the mood and a follow-up.
- **Fallback:** no key, an empty call, or any Jev error → the same record is built from the call state. The record
  says who labelled it, and "· Jev" appears only on requests Jev labelled. Labelling never breaks the end of a call.
- **Code:**
  - `app/csm/disposition.py` (new): request types (`INTENTS`), questions, Jev call, fallback (`from_state`,
    `TOOL_INTENT`).
  - `config.py`: `jev_api_key`, `jev_endpoint`, `jev_model`.
  - Call-end hook: `demo_session.py:1495`, plus `intent_line` for the summary.
  - Shown on `web/supervisor.*` and in `csm/metrics.py`.
- A new tool also needs an entry in `TOOL_INTENT`.
- **Tests:** `tests/test_disposition.py`.
- Note: the module docstring in `disposition.py` still describes an older "one question per intent" shape; the code
  works as described here.

## 4. Smaller pieces on this branch

| Change | Files |
|---|---|
| `report_emi_issue` tool: "my EMI was debited twice / wrong amount / not updated" raises a loan ticket, reads the reference and target date, then ends the call | `csm/tools.py`, `csm/router.py`, `csm/orchestrator.py`, `csm/persona.py` |
| Identity in two factors: customer ID or registered mobile + date of birth (card block and dispute still need an OTP); year-first dates; "C1. Double zero one" read as C1001 | `csm/tools.py`, `csm/router.py`, `bank/port.py` |
| Complaint confirmed before it's registered; contact changes explained, no ticket; card dispute offers a card block; fixed "Anything else?" after an answered enquiry | `csm/orchestrator.py`, `demo_session.py` |
| `<HANGUP>` after a goodbye; "are you still there?" after 7 s of silence, then the closing line; background noise no longer drops a reply | `demo_session.py`, `utils/call_closing.py` |
| Smallest AI voice mode | `pipeline/tts_smallest.py`, `voices.json`, `config.py` |
| Ticket target dates from the policy docs; demo past tickets at start; staff add a ticket on the desk | `bank/mock_adapter.py`, `demo_server.py`, `web/desk.*` |
| "NovaTrust" → "Nova Trust" (code, prompts, `data/knowledge`) | many |
| `CLAUDE.md` and `livekit.env` gitignored | `.gitignore` |

## FYI: known issues (not part of this handover)
- `pytest`: 342 pass, 13 fail. All 13 are in `tests/test_conversation_recovery.py`, and they cover behaviour that
  isn't built yet (cancelling a pending card block, corrections while verifying).
- The 3-strike off-topic hang-up (`OFFTOPIC_LIMIT`) never fires: prompt.md doesn't tell the model to tag
  `<OFFTOPIC>`.
- Old fallback copies of the prompt are still in code and run only if prompt.md is missing:
  `persona._BUILTIN_PROMPT`, `orchestrator.INSTRUCT`, `demo_session.CONTROL_PROMPT`.
