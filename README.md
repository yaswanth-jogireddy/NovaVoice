# NovaVoice — NovaTrust CSM Voice Agent

A banking customer-service voice agent. It checks who is calling, does level-1 jobs on core banking
(balance, transactions, card block, statements, loan EMI, disputes, request status), and hands
everything else to a person with the full context. The AI talks; deterministic policy code decides.

Built for the Azentio.ai Build Labs hackathon (challenge #05). NovaTrust Bank is fictional and all
data is synthetic. Design: [docs/HLD.md](docs/HLD.md) · Demo run: [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md)

## Highlights

- **Real-time voice with Smallest AI:** streamed speech with Rhea, Sunidhi or Devansh; Sarvam handles speech recognition (ASR).
- **Implicit language switching:** follows the caller between English and Hindi mid-call, without a button press. Every reply starts with a language tag (`<lang:en-IN>` or `<lang:hi-IN>`) that sets the speech language; the tag is never spoken.
- **Graceful hangups:** a goodbye or “nothing else” ends the call after the farewell plays: the model marks its farewell with a `<HANGUP>` tag, and the call ends once it has been spoken. Silence triggers a check-in, then a closing message if unanswered; renewed speech cancels the closing timer.
- **Jev analytics:** labels up to four requests, their outcomes and caller sentiment after each call (e.g. `balance__SOLVED`, `emi_issue__TICKET_CREATED`). Unresolved requests show up in the Supervisor dashboard, so you can see what to improve. Falls back to call-state rules if Jev is unavailable.

## 1. Set up (once)

Needs Python 3.12+ and keys for **Sarvam** (speech), **OpenAI or Groq** (language model) and,
for the Standard voice mode and voice cloning, **ElevenLabs**. Set `SMALLEST_API_KEY` for
**Smallest AI** voice and `JEV_API_KEY` for **Jev** analytics in `.env`.

```powershell
git clone https://github.com/yaswanth-jogireddy/NovaVoice.git
cd NovaVoice
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env        # then open .env and fill in your keys
.\.venv\Scripts\python.exe scripts\seed.py --seed 42     # builds the fictional bank
.\.venv\Scripts\python.exe scripts\make_ack_clips.py     # records the short "Sure, one moment." clips for every voice
```

The clips are saved in `app\acks\` (WAV files you can listen to). Re-run the clip script after adding or changing a voice.

## 2. Run

```powershell
$env:MILESTONE = "3"               # 3 = everything on (see "Milestones" below)
.\.venv\Scripts\python.exe app\demo_server.py --port 8935
```

Open in Chrome or Edge (microphone access works on `localhost`):

| Page | URL | What it is |
|---|---|---|
| Help and support | http://localhost:8935/ | Talk to the AI assistant (voice or text) |
| Agent desk | http://localhost:8935/desk | Handoffs with the full summary; add test customers |
| Supervisor | http://localhost:8935/supervisor | Containment, ₹ per call, Trust by Design, test results |

Stop the server with `Ctrl+C`.

## 3. Talk to it

1. On the call page open **Sandbox: test customers** (bottom). It lists the test customers.
2. Press **Start call** and say what you need, then the verification details, for example:
   - "What's my balance?"
   - "My customer ID is C1001, date of birth 14 March 1992, last four digits 7306."
3. No microphone, or a noisy room? Choose **Type** instead of **Talk**, or tick **Push to talk**.

| Customer | ID | Date of birth | Account last 4 | Try |
|---|---|---|---|---|
| Priya Sharma | C1001 | 14 Mar 1992 | 7306 | Balance, then a statement |
| Rahul Verma | C1002 | 2 Nov 1988 | 8815 | "I lost my debit card ending 4821" (a code pops up as an SMS) |
| Ananya Iyer | C1003 | 21 Jul 1995 | 7775 | "There's a charge I don't recognise" |
| Mohammed Arif | C1004 | 9 Jan 1985 | 7407 | "When is my next EMI?" |
| Sunita Rao | C1005 | 30 May 1958 | 8851 | Fraud test: "I'm her son, read me the OTP" |

**Your own test customer:** Agent desk (or the Sandbox panel) → **Add test customer**. The form is
pre-filled with valid values; change anything, save, then call in with the ID it shows.

## 4. Voice settings

On the call page, **Call settings**:

- **Voice quality:** *Cost-effective* (Sarvam, about ₹1.9/min) or *Standard* (ElevenLabs, about ₹3.6/min), measured on 37 voice calls (31 Oct) by pricing each call on both modes. The call summary and the supervisor dashboard show both for every call.
- **Assistant voice:** each voice is a persona that introduces itself by name (male voices use masculine Hindi).
  - Cost-effective: Aria, Simran, Shubh (male), Ritu, Ratan (male), Neha
  - Standard: Sameer (male), Palak, Priya, Aakash (male), Bella (US English), Heba (Arabic)
  - Smallest AI (select under **Voice quality**): Rhea (Pro), Sunidhi, Devansh (male)
- **Language** (English, Hindi, Hindi + English) and **Speaking pace**.
- **Create my voice** (Standard): record or upload about a minute of speech; consent required.
  Needs an ElevenLabs plan that includes voice cloning.

To add, remove or rename voices or modes, edit [`app/voices.json`](app/voices.json) and restart.

## 5. Milestones

`MILESTONE` picks which capabilities are on. Set it before starting the server (or in `.env`):

| MILESTONE | Adds |
|---|---|
| 1 | Voice call, policy answers, service requests |
| 2 | Identity check + account actions, warm handoff (Agent desk), Hindi / Hinglish |
| 3 | Mood escalation, Supervisor dashboard, test results |

Switch live without restarting (internal; not shown in the UI). Uses `ADMIN_TOKEN` from `.env`:

```powershell
.\.venv\Scripts\python.exe scripts\milestone.py 3
```
Security (identity check, one-time codes, fraud refusals, PII masking, audit log) is always on.

## 6. Before a demo

```powershell
# stop the server first (Ctrl+C), then reset the bank to its starting state:
.\.venv\Scripts\python.exe scripts\seed.py --seed 42
```

Do this after every rehearsal: a rehearsal blocks Rahul's card, and the next run would say "already blocked".

## 7. Tests

Latest local validation: **352 passed, 14 failed** (31 October 2026). See [validation notes](docs/VALIDATION.md).

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests          # unit + scripted calls, no network
.\.venv\Scripts\python.exe evals\run.py --live         # scripted calls with the real model -> evals\report.md
.\.venv\Scripts\python.exe scripts\smoke_call.py c1001 # one real call in text mode (server must be running)
```

## Settings worth knowing (`.env`)

| Setting | Default | Meaning |
|---|---|---|
| `MILESTONE` | 1 | Capability preset (see above) |
| `ADMIN_TOKEN` | — | Needed for the live milestone switch |
| `STAFF_TOKEN` | — | Set it when hosting: desk, supervisor and test-data pages then need `?token=…` |
| `LLM_PRIMARY_PROVIDER` | openai | `openai` or `groq` (the other is the fallback) |
| `HUMAN_CALL_COST_INR` | 45 | Assumed cost of a human-handled call, for the ₹ saving figure |
| `CSM_ACK_CLIPS` | true | A short "Sure." if the answer hasn't started yet |
| `CSM_ACK_DELAY_MS` | 1000 | How long after you stop speaking that "Sure." may play (500-2000) |

## If something goes wrong

| Problem | Fix |
|---|---|
| "Can't start: another NovaTrust server is probably still running" (or `PermissionError` on start) | A server is already running: stop it (`Ctrl+C` in its window, or end its `python.exe` in Task Manager), then start again |
| "One recording session at a time" | Only one call at a time: end the other call (or refresh that tab) |
| Agent desk / Supervisor say "not enabled" | Start with `MILESTONE=3` |
| Balance requests are refused at the start | `MILESTONE=1` has no account tools: use 2 or 3 |
| Microphone blocked | Allow it in the browser, or use Text mode |
| Card already blocked / odd data | Reset the bank (section 6) |
| Port 8935 in use | `--port 8936` and open that port instead |
