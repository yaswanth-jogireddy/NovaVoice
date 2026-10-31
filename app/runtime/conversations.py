"""Private, incremental browser call records. No audio or provider credentials.

All methods are synchronous transactions; use acall from an async server.
One coordinator per database, many local worker processes. Never use NFS.
"""
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

POLICY_VERSION = "2026-10-01-consent-retention"
PROMPT_VERSION = "novatrust-aria-csm-2026-10-30"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class ConversationStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, owner TEXT NOT NULL, created REAL NOT NULL,
                    updated REAL NOT NULL, status TEXT NOT NULL, config TEXT NOT NULL,
                    brief TEXT, usage TEXT NOT NULL DEFAULT '{}', end_reason TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS conversations_owner_created ON conversations(owner,created DESC,id DESC);
                CREATE INDEX IF NOT EXISTS conversations_created ON conversations(created DESC,id DESC);
                CREATE TABLE IF NOT EXISTS turns (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    turn_key TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL, delivery TEXT NOT NULL,
                    UNIQUE(call_id,turn_key));
                CREATE INDEX IF NOT EXISTS turns_call ON turns(call_id,seq);
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    call_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                    at REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_call ON events(call_id,seq);
                CREATE TABLE IF NOT EXISTS deletions (call_id TEXT PRIMARY KEY, deleted REAL NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_version','1')")
            version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
            if version != '1':
                raise RuntimeError("Unsupported conversation schema")
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('cookie_key',?)", (secrets.token_hex(32),))
            self.key = bytes.fromhex(db.execute("SELECT value FROM metadata WHERE key='cookie_key'").fetchone()[0])
            if not db.execute("SELECT 1 FROM metadata WHERE key='prompt_compaction_v1'").fetchone():
                db.execute("UPDATE events SET payload=json_remove(payload,'$.system_messages') WHERE kind='prompt' AND json_valid(payload)")
                db.execute("INSERT INTO metadata VALUES ('prompt_compaction_v1','done')")
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def authority(self):
        """Only the server owns recovery; workers share transactions, not this lock."""
        handle = open(str(self.path) + '.lock', 'a+b')
        try:
            handle.seek(0); handle.write(b'0'); handle.flush(); handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            handle.close()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA secure_delete=ON")
            with db:
                yield db
        finally:
            db.close()

    async def acall(self, method, *args, **kwargs):
        task = asyncio.create_task(asyncio.to_thread(getattr(self, method), *args, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def begin(self, call_id, owner, config):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM deletions WHERE call_id=?', (call_id,)).fetchone():
                raise ValueError('This conversation was deleted')
            db.execute("INSERT INTO conversations(id,owner,created,updated,status,config) VALUES (?,?,?,?,?,?)",
                       (call_id, owner, now, now, "active", encoded(config)))

    def record(self, call_id, kind, data):
        """Allowlisted events only. Never recreate a missing/deleted call."""
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM conversations WHERE id=?", (call_id,)).fetchone()
            if not row or row['status'] != 'active':
                return
            if kind in {"transcript", "agent_text"}:
                role = "assistant" if kind == "agent_text" else "user"
                key = str(data["turn_key"])
                delivery = "generated_not_confirmed_heard" if role == "assistant" else "recognized"
                db.execute("""INSERT INTO turns(call_id,turn_key,role,content,created,updated,delivery)
                    VALUES (?,?,?,?,?,?,?) ON CONFLICT(call_id,turn_key) DO UPDATE SET
                    content=excluded.content,updated=excluded.updated""",
                    (call_id, key, role, str(data.get("text", ""))[:20000], now, now, delivery))
            elif kind == "delivery":
                db.execute("UPDATE turns SET delivery=?,updated=? WHERE call_id=? AND turn_key=?",
                           (data["state"], now, call_id, data["turn_key"]))
            elif kind == "cost":
                db.execute("UPDATE conversations SET usage=? WHERE id=?", (encoded(data['usage']), call_id))
            elif kind == "brief":
                db.execute("UPDATE conversations SET brief=?,usage=? WHERE id=?",
                           (encoded(data['fields']), encoded(data.get('usage', {})), call_id))
            if kind in {"error", "latency", "interrupted", "delivery", "prompt", "language", "brief", "ready", "closing",
                        "backchannel", "turn_completion", "tts_stall", "playback_stats", "turn_detector", "csm_turn", "ack_clip",
                        "reply_language", "hangup", "silence"}:
                # Caller data lives only in this private store, never console logs.
                event = {"saved": True} if kind == "brief" else data
                if kind == 'prompt':
                    # Defence in depth for older workers: strip full prompt copies.
                    event = {k:v for k,v in data.items() if k != 'system_messages'}
                db.execute("INSERT INTO events(call_id,at,kind,payload) VALUES (?,?,?,?)",
                           (call_id, now, kind, encoded(event)))
            db.execute("UPDATE conversations SET updated=? WHERE id=?", (now, call_id))

    def finish(self, call_id, reason):
        with self.connect() as db:
            db.execute("""UPDATE conversations SET status=CASE WHEN brief IS NULL THEN 'incomplete'
                ELSE CASE WHEN json_extract(brief,'$.status')='Summary unavailable'
                THEN 'summary_unavailable' ELSE 'complete' END END, end_reason=?,updated=?
                WHERE id=? AND status='active'""", (reason, time.time(), call_id))

    def recover(self):
        # Called only by the owning server at startup, never by a worker.
        with self.connect() as db:
            db.execute("UPDATE conversations SET status='incomplete',end_reason='server_restart',updated=? WHERE status='active'", (time.time(),))

    def probe(self):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('last_health_write',?)", (str(time.time()),))
        return True

    def owner_of(self, call_id):
        with self.connect() as db:
            row = db.execute('SELECT owner FROM conversations WHERE id=?', (call_id,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _decode(row):
        result = dict(row)
        for key in ('config', 'brief', 'usage'):
            if key in result and result[key] is not None:
                result[key] = json.loads(result[key])
        result.pop('owner', None)
        return result

    def listing(self, owner=None, query="", before=None, limit=25):
        clauses, args = [], []
        if owner is not None:
            clauses.append('c.owner=?'); args.append(owner)
        if before:
            created, call_id = before
            clauses.append('(c.created<? OR (c.created=? AND c.id<?))'); args.extend([created, created, call_id])
        if query:
            if len(query) > 200: raise ValueError('Search is too long')
            clauses.append("(instr(lower(coalesce(c.brief,'')),lower(?))>0 OR EXISTS (SELECT 1 FROM turns t WHERE t.call_id=c.id AND instr(lower(t.content),lower(?))>0))")
            args.extend([query] * 2)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.connect() as db:
            rows = db.execute('SELECT c.id,c.created,c.updated,c.status,c.config,c.brief,c.end_reason FROM conversations c' + where + ' ORDER BY c.created DESC,c.id DESC LIMIT ?', args + [min(max(limit, 1), 100) + 1]).fetchall()
        more = len(rows) > limit
        records = [self._decode(r) for r in rows[:limit]]
        # Do not send full business prompts in a list of results.
        for r in records:
            cfg = r.pop('config')
            r.update(preset=cfg.get('preset', ''), scenario=cfg.get('scenario'), language=cfg.get('language'))
        return {'conversations': records, 'next': [records[-1]['created'], records[-1]['id']] if more else None}

    def detail(self, call_id, owner=None):
        with self.connect() as db:
            db.execute('BEGIN')
            row = db.execute('SELECT * FROM conversations WHERE id=?' + (' AND owner=?' if owner is not None else ''), [call_id] + ([owner] if owner is not None else [])).fetchone()
            if not row: return None
            result = self._decode(row)
            result['transcript'] = [dict(r) for r in db.execute('SELECT seq,role,content,created,updated,delivery FROM turns WHERE call_id=? ORDER BY seq', (call_id,))]
            result['events'] = [dict(seq=r['seq'], at=r['at'], kind=r['kind'], data=json.loads(r['payload'])) for r in db.execute('SELECT * FROM events WHERE call_id=? ORDER BY seq', (call_id,))]
            return result

    def delete(self, owner, call_id=None):
        where = 'owner=?' + (' AND id=?' if call_id else '')
        args = [owner] + ([call_id] if call_id else [])
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM conversations WHERE ' + where + " AND status='active'", args).fetchone():
                raise ValueError('End the active call before deleting saved data')
            db.execute('INSERT OR IGNORE INTO deletions(call_id,deleted) SELECT id,? FROM conversations WHERE ' + where, [time.time()] + args)
            count = db.execute('DELETE FROM conversations WHERE ' + where, args).rowcount
        # Clear deleted pages from the active WAL when readers allow it. Backups
        # need the operator deletion procedure described in the runbook.
        with self.connect() as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        return count

    def backup(self, destination):
        destination = Path(destination).resolve()
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Do not overwrite existing records or a backup by accident.
        with destination.open('xb'):
            pass
        if os.name != 'nt': destination.chmod(0o600)
        with self.connect() as source:
            target = sqlite3.connect(destination)
            try: source.backup(target)
            finally: target.close()

    def apply_deletions(self, source):
        """Apply the current deletion ledger to an OFFLINE restored database."""
        source = Path(source).resolve()
        if source == self.path: raise ValueError('Use the current database as source and an offline restored copy as target')
        reader = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
        try: deletions = reader.execute('SELECT call_id,deleted FROM deletions').fetchall()
        finally: reader.close()
        with self.connect() as db:
            db.executemany('INSERT OR IGNORE INTO deletions VALUES (?,?)', deletions)
            db.execute('DELETE FROM conversations WHERE id IN (SELECT call_id FROM deletions)')
        with self.connect() as db: db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        return len(deletions)

    def briefs(self, owner=None):
        """Compatibility view for earlier /api/briefs clients."""
        with self.connect() as db:
            rows = db.execute('SELECT id,created,status,config,brief,usage FROM conversations'
                + (' WHERE owner=?' if owner is not None else '')
                + ' ORDER BY created DESC,id DESC LIMIT 100', [owner] if owner is not None else []).fetchall()
            evidence = {}
            if owner is not None and rows:
                marks = ','.join('?' for r in rows)
                for turn in db.execute(f"SELECT call_id,content FROM turns WHERE role='user' AND call_id IN ({marks}) ORDER BY seq", [r['id'] for r in rows]):
                    evidence.setdefault(turn['call_id'], []).append(turn['content'])
        return [dict(id=r['id'], created=r['created'], status=r['status'],
            fields=json.loads(r['brief']) if r['brief'] else {'status':'Call ended before summary'},
            scenario=json.loads(r['config']).get('scenario'), language=json.loads(r['config']).get('language'),
            usage=json.loads(r['usage']), **({'caller_evidence':evidence.get(r['id'], [])} if owner is not None else {})) for r in rows]

    def prune(self, days=7):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cutoff = time.time() - days*86400
            db.execute("INSERT OR IGNORE INTO deletions SELECT id,? FROM conversations WHERE created<? AND status!='active'", (time.time(), cutoff))
            return db.execute("DELETE FROM conversations WHERE created<? AND status!='active'", (cutoff,)).rowcount


def call_config(settings, language, mode, scenario, context, accent, preset=""):
    from csm.persona import SYSTEM_PROMPT
    from preset_catalog import CATALOG_VERSION, get_preset
    from demo_languages import validate_hinglish, speech_settings
    validate_hinglish(settings, language, scenario, context, accent)
    if language == 'hinglish':
        settings = settings.model_copy(update={**speech_settings(language), 'caller_language_switch': False})
    profile = get_preset(preset)
    prompt = SYSTEM_PROMPT   # the CSM persona; per-turn facts are recorded with each turn
    names = ('speech_default_language', 'caller_language_switch', 'agent_flow_principles', 'turn_completion',
        'knowledge_enabled', 'knowledge_fact_guard', 'preset_voices', 'turn_hold_ms', 'release_wait_ms', 'stt_flush_silence_ms', 'tts_send_ahead', 'tts_fewer_cuts', 'tts_group_chars', 'tts_fast_recovery', 'turn_detector', 'audio_sample_rate', 'stt_provider', 'tts_provider', 'sarvam_mode', 'sarvam_language',
        'sarvam_model', 'sarvam_tts_model', 'sarvam_tts_pace', 'sarvam_tts_temperature',
        'sarvam_tts_sample_rate', 'sarvam_tts_voice_gender', 'sarvam_tts_voice_male',
        'sarvam_tts_voice_female', 'elevenlabs_model_id', 'elevenlabs_voice_id',
        'elevenlabs_tts_sample_rate', 'elevenlabs_stability', 'elevenlabs_similarity_boost',
        'elevenlabs_speed', 'openai_tts_voice', 'openai_tts_model', 'groq_model',
        'groq_temperature', 'groq_max_tokens', 'openai_fallback_model', 'openai_max_tokens',
        'llm_primary_provider', 'openai_model', 'openai_reasoning_effort', 'groq_reasoning_effort',
        'stt_endpoint_s', 'studio_barge_in_min_speech_ms', 'studio_barge_in_min_rms', 'studio_barge_in_noise_ratio',
        'studio_barge_in_release_ms', 'studio_false_interrupt_resume_ms', 'filler_nudge_s',
        'studio_final_reply_wait_s', 'studio_playback_timeout_s', 'playground_conversation',
        'demo_expand_contractions', 'semantic_cache_enabled', 'llm_backoff_max_retries',
        'browser_max_turns', 'browser_context_bytes', 'browser_session_budget_inr', 'cost_usd_to_inr',
        'llm_phrase_buffer_max_ms', 'llm_backoff_base_s', 'llm_backoff_max_s')
    resolved = {name: getattr(settings, name) for name in names}
    resolved.update(effective_openai_model=settings.effective_openai_model,
                    openai_service_tier=settings.openai_service_tier if settings.openai_base_url.rstrip('/') == 'https://api.openai.com/v1' else None,
                    initial_llm_provider=settings.available_llm_provider)
    fingerprint = hashlib.sha256(encoded(dict(settings=resolved, prompt=prompt,
        language=language, mode=mode, scenario=scenario, accent=accent,
        preset=preset, prompt_version=PROMPT_VERSION)).encode()).hexdigest()
    return dict(schema_version=1, policy_version=POLICY_VERSION, prompt_version=PROMPT_VERSION,
                resolved_settings=resolved, config_sha256=fingerprint,
                language=language, mode=mode, scenario=scenario, context=context,
                accent=accent, preset=preset, system_prompt=prompt,
                voice_profile=settings.studio_voice_profile,
                openai_speech={'model':settings.openai_tts_model, 'voice':settings.openai_tts_voice} if settings.tts_provider == 'openai' else None,
                preset_catalog_version=CATALOG_VERSION if profile else None,
                preset_title=profile['title'] if profile else None,
                closing={'reply_wait_seconds': settings.studio_final_reply_wait_s, 'countdown_seconds': 4,
                         'playback_timeout_seconds': settings.studio_playback_timeout_s},
                prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                providers={k: getattr(settings, k) for k in ('stt_provider', 'tts_provider', 'groq_model', 'openai_fallback_model', 'sarvam_model', 'sarvam_tts_model', 'elevenlabs_model_id', 'elevenlabs_voice_id', 'sarvam_tts_voice_gender', 'sarvam_tts_voice_male', 'sarvam_tts_voice_female')})
