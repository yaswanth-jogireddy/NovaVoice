"""Run the local recording studio: python voice_module/demo_server.py"""
import argparse
import asyncio
import contextlib
import json
import re
import hashlib
import hmac
import os
import secrets
import time
import httpx
from pathlib import Path

from aiohttp import web, WSMsgType
from demo_languages import speech_settings, validate_hinglish, language_options, route_speech, ELEVENLABS_ONLY
from config import Settings
from demo_session import DemoSession
from utils.logger import get_logger
from prompts.international_demo import LANGUAGE_NAMES, SCENARIOS, REGIONAL_STYLES
from runtime.conversations import ConversationStore, call_config, POLICY_VERSION
from runtime.history_http import register_history
from preset_catalog import DEMO_PRESETS, CATALOG_VERSION, get_preset
from voice_profiles import voice_profiles

from dotenv import load_dotenv
from features import flags
from csm import persona
from csm.metrics import summarize
from csm import voices as voice_catalogue

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT.parent / ".env"  # secrets live at the repo root, outside app/
# Settings reads .env itself, but the feature flags and ADMIN_TOKEN read os.environ (review R1).
load_dotenv(ENV_FILE, override=False)
logger = get_logger("demo_server")
LANGUAGES = set(LANGUAGE_NAMES)
CSM_LANGUAGES = {"en", "hi", "hinglish", "ar"}
SARVAM_LANGUAGES = LANGUAGES - ELEVENLABS_ONLY  # Sarvam speaks Indian languages and English only


def demo_settings(language="en"):
    cfg = Settings(_env_file=ENV_FILE)
    return cfg.model_copy(update={
        **speech_settings(language),
        "agent_accent": "neutral",
        "groq_max_tokens": 700, "openai_max_tokens": 300,
        "llm_phrase_buffer_max_ms": 450, "groq_temperature": 0.45,
    })


def readiness(cfg, language, mode):
    missing = []
    if not (cfg.groq_api_key or cfg.openai_api_key):
        missing.append("The conversation service is unavailable. Please try again later.")
    if mode == "voice":
        for provider in {cfg.stt_provider, cfg.tts_provider}:
            if not getattr(cfg, provider + "_api_key"):
                missing.append("Voice service unavailable. Please try again later.")
        if language not in voice_languages(cfg):
            missing.append("This language is unavailable with the selected voice service.")
    return missing


@web.middleware
async def local_only(request, handler):
    # Block DNS rebinding/cross-origin calls into a localhost server with paid APIs.
    if request.host not in request.app["hosts"]:
        raise web.HTTPForbidden(text="Local recording studio only")
    origin = request.headers.get("Origin")
    if origin and origin not in {f"http://{h}" for h in request.app["hosts"]}:
        raise web.HTTPForbidden(text="Origin not allowed")
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


WEB = ROOT / "web"
PAGES = {"/": "index.html", "/desk": "desk.html", "/supervisor": "supervisor.html"}
ASSETS = {"shared.css", "shared.js", "call.js", "desk.js", "supervisor.js", "capture.js", "logo.svg"}


async def page(request):
    # Pages are always served; what they may show comes from /api/config and the data routes,
    # which enforce the flags themselves (the UI never decides access).
    return web.FileResponse(WEB / PAGES[request.path])


async def asset(request):
    name = request.match_info["name"]
    if name not in ASSETS:
        raise web.HTTPNotFound()
    return web.FileResponse(WEB / name)


def voice_languages(cfg):
    if not all(getattr(cfg, p + "_api_key") for p in {cfg.stt_provider, cfg.tts_provider}):
        return set()
    if "sarvam" not in {cfg.stt_provider, cfg.tts_provider}:
        return LANGUAGES
    # The other languages are spoken through ElevenLabs per call (demo_languages.route_speech).
    return LANGUAGES if cfg.elevenlabs_api_key else SARVAM_LANGUAGES


async def status(request):
    cfg = demo_settings()
    token = None
    try: local_owner(request)
    except web.HTTPUnauthorized:
        value = secrets.token_hex(16) + '.' + str(int(time.time()) + 365*86400)
        token = value + '.' + hmac.new(request.app['trace'].key, value.encode(), hashlib.sha256).hexdigest()
    response = web.json_response({
        "voice_languages": sorted(voice_languages(cfg)),
        "voice_profiles": voice_profiles(cfg),
        "languages": language_options(cfg, LANGUAGE_NAMES),
        "text_ready": bool(cfg.groq_api_key or cfg.openai_api_key),
        "voice": "Signature voice" if cfg.tts_provider == "elevenlabs" else "Natural voice",
        "cloning_ready": bool(cfg.elevenlabs_api_key),
        "signature_ready": bool(request.app["runtime"].get("cloned_voice")),
        "context_presets": DEMO_PRESETS,
        "preset_catalog_version": CATALOG_VERSION,
        "history_enabled": True, "retention_policy": "until_deleted", "policy_version": POLICY_VERSION,
        "local_studio": True, "trial_exempt": True, "trial_limited": False,
        "contact_email": cfg.demo_contact_email if re.fullmatch(r"[^\s@<>]{1,64}@[^\s@<>]+\.[^\s@<>]+", cfg.demo_contact_email) else "",
    })
    if token: response.set_cookie('voice_owner', token, httponly=True, samesite='Strict', max_age=365*86400)
    return response


def local_owner(request):
    try:
        owner, expires, signature = request.cookies.get('voice_owner', '').split('.')
        expected = hmac.new(request.app['trace'].key, f'{owner}.{expires}'.encode(), hashlib.sha256).hexdigest()
        if not re.fullmatch('[a-f0-9]{32}', owner) or int(expires) < time.time() or not hmac.compare_digest(signature, expected):
            raise ValueError()
        return owner
    except ValueError:
        raise web.HTTPUnauthorized(text='Refresh the page to identify your saved conversations') from None


async def local_deletable(request, owner):
    if request.app['runtime']['active']:
        raise web.HTTPConflict(text='End the active call before deleting saved data')


async def delete_local_data(request):
    if request.headers.get('X-Playground-Request') != '1': raise web.HTTPForbidden()
    owner = local_owner(request)
    await local_deletable(request, owner)
    try: await request.app['trace'].acall('delete', owner)
    except ValueError as exc: raise web.HTTPConflict(text=str(exc)) from None
    return web.json_response({'deleted': True})


async def conversation(request):
    language = request.query.get("language", "en")
    mode = request.query.get("mode", "voice")
    if language not in LANGUAGES or mode not in {"voice", "text"}:
        raise web.HTTPBadRequest(text="Unsupported language or mode")
    # Flags are enforced here, on the server; the UI only mirrors /api/config.
    if mode == "voice" and not flags().enabled("VOICE_CALL"):
        raise web.HTTPForbidden(text="Voice calls are not enabled")
    if language not in CSM_LANGUAGES or (language != "en" and not flags().enabled("MULTILINGUAL")):
        raise web.HTTPBadRequest(text="This language is not available yet")
    scenario = request.query.get("scenario", "sales")
    context = request.query.get("context", "")
    voice = request.query.get("voice", "")
    tier = request.query.get("tier", "")
    accent = request.query.get("accent", "neutral")
    if scenario not in SCENARIOS or len(context) > 4000 or accent not in REGIONAL_STYLES or len(voice) > 64:
        raise web.HTTPBadRequest(text="Invalid conversation settings")
    cfg = demo_settings(language)
    managed = bool(cfg.managed_call_id)
    owner = None if managed else local_owner(request)
    try:
        preset = get_preset(request.query.get('preset', ''))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from None
    if preset:
        context, scenario = preset['context'], preset['scenario']
    else:
        preset = next((p for p in DEMO_PRESETS if p['context'] == context and p['scenario'] == scenario), None)
    cfg = cfg.model_copy(update={"agent_accent": accent if accent in {"haryanvi", "rajasthani", "up"} else "neutral"})
    # Voice mode (cost-effective / standard) and voice: only what app/voices.json offers.
    try:
        pace = float(request.query["pace"]) if request.query.get("pace") else None
        cfg = voice_catalogue.apply(cfg, tier, voice, language, mode, pace)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc) if str(exc) else "Invalid voice settings") from None
    validate_hinglish(cfg, language, scenario, context, accent)
    cfg = route_speech(cfg, language, mode)
    if language == 'ar' and accent == 'neutral': accent = 'ar_sa'
    missing = readiness(cfg, language, mode)
    if missing:
        raise web.HTTPServiceUnavailable(text=" ".join(missing))
    if request.app["runtime"]["active"]:
        raise web.HTTPConflict(text="One recording session at a time. End the other call first.")
    request.app["runtime"]["active"] = True
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024, compress=False)
    session = None
    call_id = cfg.managed_call_id or secrets.token_hex(16)
    cfg = cfg.model_copy(update={'managed_call_id': call_id, 'cost_async_write': True})
    reason = 'browser_disconnected'
    try:
        if not managed:
            await request.app['trace'].acall('begin', call_id, owner,
                call_config(cfg, language, mode, scenario, context, accent, preset['id'] if preset else ''))
        await ws.prepare(request)
        session = DemoSession(cfg, ws, language, mode, scenario, context, accent, trace=request.app['trace'],
                              agent=voice_catalogue.persona(tier, voice))
        async with asyncio.timeout(cfg.browser_session_timeout_s):
            await session.start()
            async for msg in ws:
                if msg.type == WSMsgType.BINARY and session.stt:
                    if len(msg.data) % 2:
                        raise ValueError("PCM sample alignment")
                    from utils.costs import ProviderBudgetExceeded
                    session.caller_audio(msg.data)
                    try:
                        await session.stt.feed_audio(msg.data)
                    except ProviderBudgetExceeded:
                        await session.end_for_provider_allowance()
                        await session.end_task
                        reason = 'provider_allowance_reached'
                        break
                elif msg.type == WSMsgType.TEXT:
                    data = json.loads(msg.data)
                    kind = data.get("type")
                    if kind == "text" and mode == "text":
                        await session.transcript(str(data.get("text", "")), True)
                    elif kind == "set_language":
                        # The call page's language picker, during a call.
                        async with session.lock:
                            await session.set_language_from_ui(str(data.get("language", "")))
                    elif kind == "request_human":
                        # The "Talk to a person" button: recorded as the caller's own request.
                        await session.transcript("I'd like to talk to a person, please.", True)
                    elif kind == "interrupt":
                        async with session.lock:
                            await session.interrupt()
                    elif kind == 'caller_activity':
                        async with session.lock:
                            await session.caller_activity()
                    elif kind == 'caller_speech_end':
                        async with session.lock:
                            await session.caller_speech_end()
                    elif kind == "played":
                        if not session.finished and data.get("generation") == session.generation and (not session.turn or session.turn.done()):
                            await session.playback_received(data.get('generation'))
                            session.playing = False
                            await session.send("state", state="listening")
                            await session.playback_finished()
                    elif kind == 'playback_stats':
                        await session.playback_stats(data)
                    elif kind == "trial_warning" and cfg.playground_conversation:
                        await session.warn_trial()
                    elif kind == "finish":
                        await session.finish()
                        reason = 'caller_finished'
                        break
                elif msg.type == WSMsgType.ERROR:
                    break
    except ConnectionResetError:
        logger.info("demo_browser_disconnected")
    except Exception as exc:
        reason = 'session_error'
        logger.error("demo_session_error", call_id=call_id, code=type(exc).__name__)
        with contextlib.suppress(Exception):
            await request.app['trace'].acall('record', call_id, 'error', {'stage': 'session', 'code': type(exc).__name__})
        if not ws.prepared:
            raise web.HTTPServiceUnavailable(text='Conversation storage or session startup unavailable') from None
        if ws.prepared and not ws.closed:
            with contextlib.suppress(ConnectionResetError):
                await ws.send_json({"type": "error", "message": "Session ended. Check provider access, then start a new call."})
    finally:
        try:
            if session:
                await session.close()
        finally:
            try:
                # Workers finish their records too; the coordinator is a fallback
                # when a worker is killed before it can execute cleanup.
                await request.app['trace'].acall('finish', call_id, session.close_reason if session and isinstance(session.close_reason, str) and session.close_reason != 'browser_disconnected' else reason)
            finally:
                with contextlib.suppress(ConnectionResetError):
                    await ws.close()
                request.app["runtime"]["active"] = False
    return ws


async def clone_voice(request):
    """Clone a voice with ElevenLabs (consent required). Stored in data/cloned_voices.json and offered
    in Standard mode. Staff-only when hosted: it spends the account's ElevenLabs quota."""
    require_staff(request)
    cfg = demo_settings()
    if not cfg.elevenlabs_api_key:
        return web.json_response({"message": "Voice cloning is not enabled for this workspace."}, status=503)
    runtime = request.app["runtime"]
    if runtime["active"] or runtime.get("cloning"):
        return web.json_response({"message": "Finish the current conversation or voice creation first."}, status=409)
    runtime["cloning"] = True
    try:
        reader = await request.multipart()
        values, audio, filename = {}, bytearray(), ""
        total = 0
        async for part in reader:
            chunks = bytearray()
            while chunk := await part.read_chunk():
                total += len(chunk)
                if total > 20 * 1024 * 1024:
                    return web.json_response({"message": "Choose a recording under 20 MB."}, status=413)
                chunks.extend(chunk)
            if part.name == "sample":
                audio, filename = chunks, part.filename or ""
            elif part.name in {"name", "consent", "gender"}:
                values[part.name] = chunks.decode("utf-8")
        if values.get("consent") != "true" or not audio or Path(filename).suffix.lower() not in {".wav", ".mp3", ".m4a", ".flac"}:
            return web.json_response({"message": "Provide a supported recording and confirm voice permission."}, status=400)
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post("https://api.elevenlabs.io/v1/voices/add",
                headers={"xi-api-key": cfg.elevenlabs_api_key},
                data={"name": values.get("name", "Signature voice")[:60]},
                files={"files": ("sample" + Path(filename).suffix.lower(), bytes(audio))})
            response.raise_for_status()
            data = response.json()
        verification = bool(data.get("requires_verification"))
        entry = None
        if not verification:
            entry = voice_catalogue.add_clone(values.get("name", "My voice")[:40] or "My voice", data["voice_id"], values.get("gender", "female"))
        return web.json_response({"requires_verification": verification, "voice": entry})
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        hint = {401: "the ElevenLabs key was rejected", 402: "the ElevenLabs plan doesn't include voice cloning",
                403: "the ElevenLabs plan doesn't include voice cloning", 422: "ElevenLabs couldn't use this recording"}.get(code, "ElevenLabs refused the request")
        return web.json_response({"message": f"Voice creation failed: {hint} (HTTP {code})."}, status=502)
    except (ValueError, UnicodeError, httpx.HTTPError, KeyError):
        return web.json_response({"message": "Voice creation could not finish. Check account access and the recording, then retry."}, status=502)
    finally:
        runtime["cloning"] = False


# --- CSM: config, milestone switch, agent desk, supervisor ------------------------------------------
def require_staff(request):
    """Desk and supervisor data. With STAFF_TOKEN set (hosted), a matching X-Staff-Token header or
    ?token= is required; without it the server is localhost-only (local_only middleware)."""
    expected = os.environ.get("STAFF_TOKEN", "")
    given = request.headers.get("X-Staff-Token") or request.query.get("token", "")
    if expected and not hmac.compare_digest(given, expected):
        raise web.HTTPUnauthorized(text="Staff token required")


def require_feature(name):
    if not flags().enabled(name):
        raise web.HTTPNotFound()


def csm_services():
    from csm.orchestrator import services
    return services()


async def api_voices(request):
    """Voice modes and voices the call page may offer (app/voices.json + cloned voices)."""
    return web.json_response(voice_catalogue.catalogue(demo_settings()))


async def api_config(request):
    return web.json_response({**flags().snapshot(), "bank": persona.BANK, "agent": persona.NAME,
                              "languages": sorted(CSM_LANGUAGES) if flags().enabled("MULTILINGUAL") else ["en"]})


async def admin_milestone(request):
    try:
        body = await request.json()
        milestone = int(body.get("milestone"))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Send {\"milestone\": 1|2|3}") from None
    try:
        snapshot = flags().set_milestone(milestone, request.headers.get("X-Admin-Token", ""))
    except PermissionError:
        raise web.HTTPForbidden(text="Invalid admin token") from None
    logger.info("milestone_switched", milestone=snapshot["milestone"])
    await csm_services().hub.publish({"type": "config", **snapshot})
    return web.json_response(snapshot)


async def handoffs_list(request):
    require_feature("WARM_HANDOFF")
    require_staff(request)
    status = request.query.get("status")
    return web.json_response(csm_services().handoffs.list(status if status in {"waiting", "accepted", "closed"} else None))


async def handoff_accept(request):
    require_feature("WARM_HANDOFF")
    require_staff(request)
    agent = re.sub(r"[^\w .-]", "", request.query.get("agent", "agent"))[:40] or "agent"
    if not csm_services().handoffs.accept(request.match_info["handoff_id"], agent):
        raise web.HTTPConflict(text="Already accepted or unknown")
    await csm_services().hub.publish({"type": "handoff_accepted", "handoff_id": request.match_info["handoff_id"], "agent": agent})
    return web.json_response({"accepted": True})


async def desk_socket(request):
    """Agent Desk live feed: queued handoffs now, then every new handoff as it happens."""
    require_feature("WARM_HANDOFF")
    require_staff(request)
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4096)
    await ws.prepare(request)
    hub = csm_services().hub
    hub.clients.add(ws)
    try:
        await ws.send_json({"type": "queue", "handoffs": csm_services().handoffs.list("waiting")})
        async for _ in ws:   # the desk only listens; actions go through REST
            pass
    finally:
        hub.clients.discard(ws)
    return ws


async def api_metrics(request):
    require_feature("SUPERVISOR_DASHBOARD")
    require_staff(request)
    events = csm_services().audit.events(limit=20000)
    labels = {k: t["label"] for k, t in voice_catalogue.config()["tiers"].items()}
    return web.json_response(summarize(events, tier_labels=labels))


async def call_audit(request):
    require_feature("SUPERVISOR_DASHBOARD")
    require_staff(request)
    call_id = request.match_info["call_id"]
    if not re.fullmatch(r"[a-f0-9]{32}", call_id):
        raise web.HTTPBadRequest()
    return web.json_response(csm_services().audit.events(call_id))


async def api_evals(request):
    """Latest eval report (python evals/run.py [--live]): summary + pass/fail per scenario."""
    require_feature("EVALS")
    require_staff(request)
    path = ROOT.parent / "evals" / "report.json"
    if not path.exists():
        raise web.HTTPNotFound(text="Run python evals/run.py first")
    report = json.loads(path.read_text(encoding="utf-8"))
    keep = ("id", "title", "suite", "adversarial", "passed", "failures", "outcome", "tools_ok", "guards", "cost_inr")
    return web.json_response({"summary": report["summary"],
                              "results": [{k: r.get(k) for k in keep} for r in report["results"]]})


async def test_customers(request):
    """Synthetic sandbox identities for the call page's demo guide (no real data exists here)."""
    require_feature("ACCOUNT_TOOLS")
    return web.json_response(csm_services().bank.test_customers())


async def mock_defaults(request):
    require_feature("ACCOUNT_TOOLS")
    require_staff(request)
    return web.json_response(csm_services().bank.mock_defaults())


async def mock_create(request):
    """Staff add a test customer (editable defaults) to try the agent against."""
    require_feature("ACCOUNT_TOOLS")
    require_staff(request)
    from bank.port import BankError
    try:
        body = await request.json()
        created = csm_services().bank.create_customer(body if isinstance(body, dict) else {})
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Send the customer as JSON") from None
    except BankError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from None
    csm_services().audit.record(None, "mock_customer_created", customer=created["customer_id"])
    return web.json_response(created, status=201)


async def api_tickets(request):
    require_feature("TICKETS")
    require_staff(request)
    return web.json_response(csm_services().bank.recent_tickets(50))


async def ticket_create(request):
    """Staff add a service request for any customer (e.g. a past one, to test status questions)."""
    require_feature("TICKETS")
    require_staff(request)
    from bank.port import BankError
    try:
        body = await request.json()
        created = csm_services().bank.add_ticket(body if isinstance(body, dict) else {})
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="Send the request as JSON") from None
    except BankError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from None
    csm_services().audit.record(None, "ticket_added_by_staff", ticket=created["ticket_id"], customer=created["customer_id"])
    return web.json_response(created)


def create_app(port=8935, trace_path=None):
    app = web.Application(middlewares=[local_only], client_max_size=64 * 1024,
                          handler_args={"max_line_size": 65536})
    app["hosts"] = {f"localhost:{port}", f"127.0.0.1:{port}"}
    app["runtime"] = {"active": False}
    async def storage(app):
        path = trace_path or demo_settings().conversation_db
        app['trace'] = await asyncio.to_thread(ConversationStore, path)
        if port == 0:
            yield
        else:
            # Past service requests on the demo customers, so status questions work on a first call.
            added = await asyncio.to_thread(lambda: csm_services().bank.ensure_demo_tickets())
            if added:
                logger.info("demo_tickets_added", count=added)
            with app['trace'].authority():
                await app['trace'].acall('recover')
                yield
    app.cleanup_ctx.append(storage)
    for path in PAGES:
        app.router.add_get(path, page)
    app.router.add_get("/assets/{name}", asset)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/session", conversation)
    app.router.add_post("/api/voice-clone", clone_voice)
    register_history(app, lambda r: r.app['trace'], local_owner, local_deletable)
    app.router.add_delete('/api/my-data', delete_local_data)
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/api/voices", api_voices)
    app.router.add_post("/api/admin/milestone", admin_milestone)
    app.router.add_get("/api/handoffs", handoffs_list)
    app.router.add_post("/api/handoffs/{handoff_id}/accept", handoff_accept)
    app.router.add_get("/ws/desk", desk_socket)
    app.router.add_get("/api/metrics", api_metrics)
    app.router.add_get("/api/calls/{call_id}/audit", call_audit)
    app.router.add_get("/api/tickets", api_tickets)
    app.router.add_post("/api/tickets", ticket_create)
    app.router.add_get("/api/evals", api_evals)
    app.router.add_get("/api/test-customers", test_customers)
    app.router.add_get("/api/mock-customers/defaults", mock_defaults)
    app.router.add_post("/api/mock-customers", mock_create)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8935)
    args = parser.parse_args()
    print(f"Nova Trust CSM: http://localhost:{args.port}")
    try:
        web.run_app(create_app(args.port), host="127.0.0.1", port=args.port, access_log=None)
    except (PermissionError, OSError) as exc:
        # The conversation store allows one server at a time (a file lock), and the port can be taken.
        raise SystemExit(f"Can't start: another Nova Trust server is probably still running ({type(exc).__name__}). "
                         f"Stop it with Ctrl+C in its window (or end its python.exe in Task Manager), "
                         f"or start this one with --port {args.port + 1}.") from None
