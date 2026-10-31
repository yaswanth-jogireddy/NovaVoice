// Customer call page. Audio protocol kept from the tuned Voice_Agent_temp browser client:
// 16 kHz PCM frames from the capture worklet, caller_activity / caller_speech_end, agent PCM
// scheduled gaplessly, duck-then-pause on barge-in, and a `played` ack once audio has drained.
import { $, el, mask, api, initChrome, on, config, openMockDialog, staffToken } from '/assets/shared.js';

const LANG_NAMES = { en: 'English', hi: 'Hindi', hinglish: 'Hindi + English (Hinglish)', ar: 'Arabic', bn: 'Bengali',
  ta: 'Tamil', te: 'Telugu', kn: 'Kannada', ml: 'Malayalam', mr: 'Marathi', gu: 'Gujarati', pa: 'Punjabi', od: 'Odia' };
const STATES = {
  listening: ['Listening', 'Go ahead. You can interrupt at any time.'],
  thinking: ['Checking', 'One moment.'],
  speaking: ['Speaking', 'Just talk to interrupt.'],
  summarizing: ['Wrapping up', 'Preparing the call summary.'],
};
let agentName = 'Aria';
const DUCK_LEVEL = 0.25, DUCK_CONFIRM_MS = 300, PTT_TAIL_MS = 300;

let ws = null, mode = 'voice', active = false, finishing = false;
let capture, playback, mic, worklet, micSource, muteNode, duckNode;
let scheduled = new Set(), nextAudio = 0, paused = false, pendingGen, playTimer, resumeTimer, duckTimer, ducked = false;
let speaking = false, resumeMs = 900, agentMsg = null;
let lastTurn = 0, pendingUsed = null;   // the turn record waiting for its reply ("What I used")
let pttMode = false, holding = false, pttTail = 0;

function notice(text) { $('notice').textContent = text || ''; $('notice').hidden = !text; }
function setState(state) { const [title, detail] = STATES[state] || [state, '']; $('state').textContent = title; $('state-detail').textContent = detail; }
function send(data) { if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify(data)); }

function message(role, text) {
  $('empty')?.remove();
  const row = el('div', `msg ${role}`); row.append(el('div', 'who', role === 'user' ? 'You' : agentName), el('div', 'text', mask(text)));
  $('transcript').append(row); $('transcript').scrollTop = $('transcript').scrollHeight;
  return row;
}
function agentText(text) {
  if (!agentMsg) agentMsg = message('assistant', text); else agentMsg.querySelector('.text').textContent = mask(text);
  $('transcript').scrollTop = $('transcript').scrollHeight;
}

// --- what each turn used (Level 1: intent, verification, retrieved information) ------------------
// Values come from the bank systems already masked; dates and SR-/LA- references stay readable here.
const shown = v => (/^\d{4}-\d{2}-\d{2}/.test(v) || /^(SR|LA)-/.test(v) ? v : mask(v));
const label = key => key.replaceAll('_', ' ');
const DUPES = new Set(['emi', 'next_due_on']);   // older field names, repeated inside next_scheduled_instalment
function flat(value) {
  if (value === null || value === undefined) return '';
  if (Array.isArray(value)) return value.map(flat).join('; ');
  if (typeof value === 'object') return Object.entries(value).filter(([k]) => !DUPES.has(k)).map(([k, v]) => `${label(k)} ${flat(v)}`).join(', ');
  return shown(String(value));
}
function toolBlock(t) {
  const box = el('div', 'used-tool');
  const head = el('div', 'used-head');
  head.append(el('b', '', label(t.tool)),
    el('span', t.held ? 'warn' : t.ok ? 'ok' : 'bad', t.held ? `held ${t.held}` : t.ok ? '✓ ' + t.summary : '✗ ' + t.summary));
  box.append(head);
  const rows = Array.isArray(t.data) ? t.data : t.data && typeof t.data === 'object' ? [t.data] : [];
  if (rows.length) {
    const list = el('ul');
    for (const row of rows.slice(0, 6)) {
      const item = el('li');
      if (row && typeof row === 'object') {
        for (const [k, v] of Object.entries(row)) {
          if (DUPES.has(k)) continue;
          item.append(el('span', 'k', label(k)), el('span', 'v', flat(v)));
        }
      } else item.append(el('span', 'v', flat(row)));
      list.append(item);
    }
    box.append(list);
  }
  return box;
}
function usedPanel(turn) {
  const panel = el('details', 'used');
  panel.append(el('summary', '', 'What I used'));
  const meta = el('div', 'used-meta');
  meta.append(el('span', '', `Intent: ${turn.intent}`), el('span', '', `Routed by ${turn.source}`),
    el('span', '', turn.identity === 'ANONYMOUS' ? 'Caller not verified' : turn.identity === 'LOCKED' ? 'Verification locked'
      : `Caller verified${turn.verified_this_turn ? ' this turn' : ''}`));
  panel.append(meta);
  for (const t of turn.tools || []) panel.append(toolBlock(t));
  if ((turn.policy_docs || []).length) panel.append(el('div', 'used-docs', `Policy sources: ${turn.policy_docs.join(', ')}`));
  if (!(turn.tools || []).length && !(turn.policy_docs || []).length) panel.append(el('div', 'used-docs', 'No bank data was needed for this reply.'));
  return panel;
}
function showTurn(turn) {
  if (!turn || turn.turn === lastTurn) return;
  lastTurn = turn.turn; pendingUsed = turn;
  const users = $('transcript').querySelectorAll('.msg.user');
  const last = users[users.length - 1];
  if (last && !last.querySelector('.intent')) last.append(el('div', 'intent', turn.intent));
}
function attachUsed(msg) {
  if (!pendingUsed || !msg) return;
  msg.append(usedPanel(pendingUsed)); pendingUsed = null;
}

// --- playback --------------------------------------------------------------------------------
function setDuck(level) { if (duckNode && playback) duckNode.gain.setTargetAtTime(level, playback.currentTime, 0.03); }
function duck() {
  if (!playback || !scheduled.size || paused || ducked) return;
  ducked = true; setDuck(DUCK_LEVEL);
  clearTimeout(duckTimer); duckTimer = setTimeout(() => { if (ducked && speaking) pause(); }, DUCK_CONFIRM_MS);
}
function unduck() { clearTimeout(duckTimer); if (ducked) { ducked = false; setDuck(1); } }
function pause() {
  if (!playback || !scheduled.size || paused) return;
  paused = true; clearTimeout(playTimer); playback.suspend(); unduck();
  clearTimeout(resumeTimer); resumeTimer = setTimeout(resume, resumeMs);
}
function resume() {
  if (!paused || finishing) return;
  if (speaking) { resumeTimer = setTimeout(resume, 250); return; }
  paused = false; playback?.resume();
  if (pendingGen !== undefined) playbackDone(pendingGen);
}
function clearAudio() {
  clearTimeout(resumeTimer); unduck(); paused = false; pendingGen = undefined; clearTimeout(playTimer);
  for (const source of scheduled) { try { source.stop(); } catch { /* already ended */ } }
  scheduled.clear(); nextAudio = 0; playback?.resume().catch(() => {});
}
function playPCM(raw) {
  if (!playback || finishing) return;
  const view = new DataView(raw), buffer = playback.createBuffer(1, raw.byteLength / 2, playback.sampleRate), samples = buffer.getChannelData(0);
  for (let i = 0; i < samples.length; i++) samples[i] = view.getInt16(i * 2, true) / 32768;
  const source = playback.createBufferSource(); source.buffer = buffer; source.connect(duckNode);
  const at = Math.max(nextAudio, playback.currentTime + 0.025); nextAudio = at + buffer.duration;
  const first = scheduled.size === 0; scheduled.add(source);
  if (first && speaking) pause();
  source.onended = () => { scheduled.delete(source); source.disconnect(); };
  source.start(at);
}
function playbackDone(gen) {
  pendingGen = gen; clearTimeout(playTimer); if (paused) return;
  const delay = playback ? Math.max(0, (nextAudio - playback.currentTime) * 1000) : 0;
  playTimer = setTimeout(() => {
    if (pendingGen !== gen || paused || finishing) return;
    if (scheduled.size) { playbackDone(gen); return; }
    pendingGen = undefined; send({ type: 'played', generation: gen });
  }, Math.max(80, delay + 80));
}

// --- microphone ------------------------------------------------------------------------------
function speechStart() { speaking = true; duck(); send({ type: 'caller_activity' }); }
function speechEnd() { speaking = false; send({ type: 'caller_speech_end' }); unduck(); clearTimeout(resumeTimer); resumeTimer = setTimeout(resume, resumeMs); }
const SILENCE = new Int16Array(640).buffer;
function onFrame(buffer) {
  const pcm = new Int16Array(buffer); let sum = 0;
  for (let i = 0; i < pcm.length; i += 4) sum += pcm[i] * pcm[i];
  $('meter').style.width = `${Math.min(100, Math.sqrt(sum / (pcm.length / 4)) / 60)}%`;
  if (!ws || ws.readyState !== WebSocket.OPEN || finishing || ws.bufferedAmount > 64000) return;
  // Push to talk: outside a press only silence is sent (keeps recognition open, captures no room audio).
  const live = !pttMode || holding || performance.now() < pttTail;
  ws.send(live ? buffer : SILENCE);
}
async function startMic(interruption) {
  micSource = capture.createMediaStreamSource(mic);
  worklet = new AudioWorkletNode(capture, 'microphone-capture', { processorOptions: interruption });
  muteNode = capture.createGain(); muteNode.gain.value = 0;
  micSource.connect(worklet); worklet.connect(muteNode); muteNode.connect(capture.destination);
  worklet.port.onmessage = e => {
    if (e.data?.type === 'speech_start') { if (!pttMode) speechStart(); return; }
    if (e.data?.type === 'speech_end') { if (!pttMode) speechEnd(); return; }
    if (e.data instanceof ArrayBuffer) onFrame(e.data);
  };
}
function pttDown() { if (!active || mode !== 'voice' || holding) return; holding = true; $('ptt').classList.add('holding'); speechStart(); }
function pttUp() {
  if (!holding) return; holding = false; $('ptt').classList.remove('holding');
  pttTail = performance.now() + PTT_TAIL_MS; setTimeout(speechEnd, PTT_TAIL_MS);
}

// --- call lifecycle --------------------------------------------------------------------------
function chips(state) {
  const list = [];
  const id = state.identity;
  if (id === 'STEPPED_UP') { list.push(['ok', `✓ Verified${state.first_name ? ' · ' + state.first_name : ''} · code confirmed`]); if (!$('phone').hidden) hideSms(2500); }
  else if (id === 'VERIFIED') list.push(['ok', `✓ Verified${state.first_name ? ' · ' + state.first_name : ''}`]);
  else if (id === 'LOCKED') list.push(['bad', 'Verification locked']);
  else list.push(['', 'Not verified']);
  if (state.last_turn) list.push(['info', `Intent: ${state.last_turn.intent}`]);
  for (const ticket of state.tickets || []) list.push(['info', `Ticket ${ticket}`]);
  if ((state.risk_flags || []).length) list.push(['bad', `Risk: ${state.risk_flags.map(f => f.replaceAll('_', ' ')).join(', ')}`]);
  if (state.handed_off) list.push(['warn', 'Handed to a person']);
  $('chips').replaceChildren(...list.map(([kind, text]) => el('span', `chip ${kind}`, text)));
}
let smsTimer = null;
function showSms(code) {
  clearTimeout(smsTimer);
  $('otp').textContent = code; $('phone-time').textContent = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  $('phone').hidden = false; navigator.vibrate?.([60, 40, 60]);
}
// Closes on ×, on Esc, a few seconds after the code is accepted, and when the call ends.
function hideSms(delayMs = 0) {
  clearTimeout(smsTimer);
  smsTimer = setTimeout(() => { $('phone').hidden = true; }, delayMs);
}
function showSummary(fields = {}, usage = {}) {
  const labels = { disposition: 'Call outcome', labelled_by: 'Labelled by', intents: 'Each request', customer_query: 'Caller wanted', customer: 'Customer',
    actions: 'Done', tickets: 'Tickets', follow_up: 'Follow-up', sentiment: 'Caller mood',
    handoff_reason: 'Handoff reason', risk_flags: 'Risk flags' };
  const list = $('summary-list'); list.replaceChildren();
  for (const [key, label] of Object.entries(labels)) {
    if (!(key in fields)) continue;
    const value = fields[key];
    if (Array.isArray(value)) {   // one line per request: "balance enquiry: Solved"
      const dd = el('dd'); dd.append(...value.map(v => el('div', '', String(v).replaceAll('_', ' '))));
      list.append(el('dt', '', label), dd);
      continue;
    }
    list.append(el('dt', '', label), el('dd', '', String(value).replaceAll('_', ' ')));
  }
  if (usage.estimated_inr !== undefined) list.append(el('dt', '', 'AI cost of this call'), el('dd', '', `₹${Number(usage.estimated_inr).toFixed(3)} (speech + model, excl. tax)`));
  // The same call priced on each voice mode: same listening and model, the other mode's speaking rate.
  const t = usage.tiers;
  if (t?.spoken_characters) {
    for (const [key, row] of Object.entries(t.tiers || {})) {
      if (row.inr === null || row.inr === undefined) continue;
      const name = voices?.tiers?.[key]?.label || key;
      list.append(el('dt', '', key === t.actual_tier ? `${name} (this call)` : `Same call on ${name}`),
        el('dd', '', `₹${Number(row.inr).toFixed(3)} · ₹${Number(row.per_minute_inr).toFixed(2)}/min`));
    }
  }
  $('summary').hidden = false;
}
function setActive(value) {
  active = value;
  $('call-label').textContent = value ? 'End call' : 'Start call'; $('call').classList.toggle('live', value);
  $('human').disabled = !value;
  $('composer').hidden = !(value && mode === 'text'); $('suggest').hidden = !(value && mode === 'text');
  for (const b of $('mode').querySelectorAll('button')) b.disabled = value;
  for (const b of $('tier').querySelectorAll('button')) b.disabled = value || !voices?.tiers?.[b.dataset.value]?.available;
  // Language stays changeable during a call: the agent switches from its next reply (set_language below).
  for (const id of ['voice', 'pace', 'clone']) $(id).disabled = value;
}

// The agent's language follows the screen during a call; the server confirms, or names why it can't.
function showLanguage(code, accepted = true) {
  if (![...$('language').options].some(o => o.value === code)) $('language').append(new Option(LANG_NAMES[code] || code, code));
  $('language').value = code;
  notice(accepted ? `Speaking ${LANG_NAMES[code] || code} now.` : `This call can't switch language; it stays in ${LANG_NAMES[code] || code}.`);
  setTimeout(() => notice(''), 3000);
}

// One entry point for every server event, whatever the transport (WebSocket today, LiveKit data
// channel on the livekit branch). Events: ready, state, partial, transcript, agent_text, agent_done,
// pause_audio, speak_over_noise, clear_audio, csm_state, otp_sms, brief, error.
async function onEvent(data) {
  switch (data.type) {
    case 'ready':
      resumeMs = data.interruption?.resume_ms ?? 900;
      if (mode === 'voice') {
        playback = new AudioContext({ sampleRate: data.sample_rate }); await playback.resume();
        duckNode = playback.createGain(); duckNode.connect(playback.destination);
        await startMic(data.interruption);
      }
      break;
    case 'state': setState(data.state); break;
    case 'partial': $('partial').textContent = mask(data.text); break;
    case 'transcript': message(data.role, data.text); $('partial').textContent = ''; agentMsg = null; break;
    case 'agent_text': agentText(data.text); break;
    case 'agent_done':
      // The reply's language tag and the speech language used (the tag is never spoken or saved as text).
      if (agentMsg && data.tts?.length) agentMsg.append(el('span', 'muted lang-badge',
        ` · ${data.tag ? `<lang:${data.tag}-IN>` : 'no tag'} → voice ${data.tts.map(c => c + '-IN').join(', ')}`));
      attachUsed(agentMsg); agentMsg = null; playbackDone(data.generation); break;
    case 'pause_audio': if (speaking || data.explicit_interrupt) pause(); break;
    case 'speak_over_noise': resume(); break;
    case 'clear_audio': clearAudio(); agentMsg?.classList.add('cut'); agentMsg = null; break;
    case 'csm_state': chips(data); showTurn(data.last_turn); break;
    case 'otp_sms': showSms(data.code); break;
    case 'brief': showSummary(data.fields, data.usage); break;
    case 'error': notice(data.message); break;
    case 'language': showLanguage(data.language, data.accepted !== false); break;
  }
}
window.ariaCall = { onEvent, playPCM };

async function start() {
  notice(''); finishing = false; agentMsg = null; lastTurn = 0; pendingUsed = null; $('summary').hidden = true; $('phone').hidden = true;
  $('transcript').replaceChildren(); chips({});
  setActive(true); setState('thinking'); $('state').textContent = 'Connecting…';
  try {
    await fetch('/api/status', { cache: 'no-store' });   // sets the call-owner cookie
    if (mode === 'voice') {
      if (!navigator.mediaDevices?.getUserMedia) throw Error('Open this page over HTTPS (or localhost) in a browser with microphone access.');
      mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
      capture = new AudioContext({ sampleRate: 16000 }); await capture.resume();
      if (capture.sampleRate !== 16000) throw Error('This browser cannot capture at 16 kHz. Use current Chrome or Edge.');
      await capture.audioWorklet.addModule('/assets/capture.js');
    }
    const params = new URLSearchParams({ language: $('language').value || 'en', mode, scenario: 'support', accent: 'neutral' });
    if (voices) { params.set('tier', tier); if ($('voice').value) params.set('voice', $('voice').value); }
    if ($('pace').value) params.set('pace', $('pace').value);
    const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/session?${params}`);
    socket.binaryType = 'arraybuffer'; ws = socket;
    socket.onmessage = async event => {
      if (socket !== ws) return;
      if (event.data instanceof ArrayBuffer) { playPCM(event.data); return; }
      await onEvent(JSON.parse(event.data));
    };
    socket.onerror = () => { if (socket === ws) notice('Could not connect. Check the server and try again.'); };
    socket.onclose = async () => { if (socket !== ws) return; await cleanup(); setState('listening'); $('state').textContent = 'Call ended'; $('state-detail').textContent = 'Start a new call any time.'; };
  } catch (error) {
    await cleanup();
    notice(error.name === 'NotAllowedError' ? 'Microphone permission was declined. Allow it in the browser, or switch to Text mode.' : error.message);
    $('state').textContent = 'Available now';
  }
}
async function cleanup() {
  hideSms(); clearAudio(); setActive(false); ws = null; holding = false; speaking = false; $('meter').style.width = '0';
  if (worklet) { worklet.port.onmessage = null; worklet.disconnect(); worklet = null; }
  micSource?.disconnect(); muteNode?.disconnect(); mic?.getTracks().forEach(t => t.stop()); mic = null;
  for (const ctx of [capture, playback]) await ctx?.close().catch(() => {});
  capture = playback = null; duckNode = null;
}
function finish() { finishing = true; clearAudio(); setState('summarizing'); send({ type: 'finish' }); $('call').disabled = true; setTimeout(() => { $('call').disabled = false; }, 1500); }

// --- wiring ----------------------------------------------------------------------------------
await initChrome().catch(() => notice('Configuration unavailable. Is the server running?'));
if (config) {
  if (!on('VOICE_CALL')) { mode = 'text'; $('mode').querySelector('[data-value="voice"]').disabled = true; }
  const langs = config.languages || ['en'];
  $('language').replaceChildren(...langs.map(code => new Option(LANG_NAMES[code] || code, code)));
  $('language').value = 'en'; $('lang-row').hidden = !on('MULTILINGUAL');
  $('human').hidden = false;
}
for (const b of $('mode').querySelectorAll('button')) b.onclick = () => {
  mode = b.dataset.value;
  for (const x of $('mode').querySelectorAll('button')) x.setAttribute('aria-pressed', String(x === b));
  $('state-detail').textContent = mode === 'voice' ? 'Uses your microphone. Headset recommended.' : 'Type instead of speaking. Same agent, same rules.';
};
$('call').onclick = () => (active ? finish() : start());
$('human').onclick = () => send({ type: 'request_human' });
$('ptt-toggle').onchange = e => { pttMode = e.target.checked; $('ptt').hidden = !pttMode; if (!pttMode && holding) pttUp(); };
$('ptt').onpointerdown = e => { e.preventDefault(); pttDown(); };
$('ptt').onpointerup = $('ptt').onpointerleave = $('ptt').onpointercancel = pttUp;
addEventListener('keydown', e => { if (pttMode && e.code === 'Space' && !e.repeat && document.activeElement?.tagName !== 'INPUT') { e.preventDefault(); pttDown(); } });
addEventListener('keyup', e => { if (pttMode && e.code === 'Space') { e.preventDefault(); pttUp(); } });
$('composer').onsubmit = e => { e.preventDefault(); const text = $('text').value.trim(); if (text) { send({ type: 'text', text }); $('text').value = ''; } };
for (const b of $('suggest').querySelectorAll('button')) b.onclick = () => { send({ type: 'text', text: b.textContent }); };
$('phone-close').onclick = () => hideSms();
addEventListener('keydown', e => { if (e.key === 'Escape' && !$('phone').hidden) hideSms(); });
addEventListener('beforeunload', () => { mic?.getTracks().forEach(t => t.stop()); ws?.close(); });

// --- demo guide: who to be, and what to say ----------------------------------------------------
const STORIES = { C1001: 'Personal loan application, vehicle loan EMI, failed UPI', C1002: 'Approved vehicle loan application; lost card 4821',
  C1003: 'Overdue personal loan EMI; unknown QUICKPAY charge', C1004: 'Home-loan EMI (Hindi)',
  C1005: 'Loan under credit assessment; fake “son” call', C1006: 'Balance in AED' };
async function loadGuide() {
  if (!on('ACCOUNT_TOOLS')) return;
  try {
    const people = await api('/api/test-customers');
    $('guide').hidden = false;
    $('guide-rows').replaceChildren(...people.map(p => {
      const tr = el('tr');
      const story = STORIES[p.customer_id] || (p.kind === 'added' ? 'Added by staff' : '');
      for (const v of [p.full_name, p.customer_id, p.dob, p.account_last4, p.card_last4, p.mobile, story]) tr.append(el('td', '', v));
      return tr;
    }));
  } catch { /* guide is optional */ }
}
$('add-customer').onclick = () => openMockDialog(() => { loadGuide(); $('guide').open = true; });
if (config) loadGuide();

// --- voice mode, voice, language, pace (app/voices.json; the server validates every choice) -----
let voices = null, tier = 'cost';
function renderVoiceControls(keepVoice) {
  const t = voices.tiers[tier];
  $('tier-note').textContent = t.note + (t.available ? '' : ' Not configured on this server.');
  for (const b of $('tier').querySelectorAll('button')) {
    b.setAttribute('aria-pressed', String(b.dataset.value === tier));
    b.disabled = active || !voices.tiers[b.dataset.value]?.available;
  }
  const allowed = (on('MULTILINGUAL') ? (config.languages || ['en']) : ['en']).filter(code => t.languages.includes(code));
  const current = $('language').value || 'en';
  $('language').replaceChildren(...allowed.map(code => new Option(LANG_NAMES[code] || code, code)));
  $('language').value = allowed.includes(current) ? current : 'en';
  $('lang-row').hidden = allowed.length < 2;
  const list = voices.voices.filter(v => v.tier === tier && (!v.languages || v.languages.includes($('language').value)));
  $('voice').replaceChildren(...list.map(v => { const o = new Option(v.label + (v.available ? '' : ' (not configured)'), v.id); o.disabled = !v.available; return o; }));
  const pick = list.find(v => v.id === keepVoice && v.available) || list.find(v => v.available);
  if (pick) $('voice').value = pick.id;
  showAgent();
  $('clone').hidden = tier !== 'standard';
}
async function loadVoices(keepVoice) {
  try {
    voices = await api('/api/voices');
    if (!voices.tiers[tier]?.available) tier = voices.default_tier;
    $('tier-row').hidden = $('voice-row').hidden = $('pace-row').hidden = false;
    renderVoiceControls(keepVoice);
  } catch { /* voice settings unavailable: the server defaults apply */ }
}
for (const b of $('tier').querySelectorAll('button')) b.onclick = () => { tier = b.dataset.value; renderVoiceControls(); };
$('language').onchange = () => {
  if (active) { send({ type: 'set_language', language: $('language').value }); return; }   // live switch
  if (voices) renderVoiceControls($('voice').value);
};

// --- "Create my voice": record in the browser (sent as WAV) or upload; consent required --------
function wavFrom(buffer) {
  const data = buffer.getChannelData(0), bytes = new DataView(new ArrayBuffer(44 + data.length * 2));
  const text = (o, t) => { for (let i = 0; i < t.length; i++) bytes.setUint8(o + i, t.charCodeAt(i)); };
  text(0, 'RIFF'); bytes.setUint32(4, 36 + data.length * 2, true); text(8, 'WAVEfmt '); bytes.setUint32(16, 16, true);
  bytes.setUint16(20, 1, true); bytes.setUint16(22, 1, true); bytes.setUint32(24, buffer.sampleRate, true);
  bytes.setUint32(28, buffer.sampleRate * 2, true); bytes.setUint16(32, 2, true); bytes.setUint16(34, 16, true);
  text(36, 'data'); bytes.setUint32(40, data.length * 2, true);
  for (let i = 0; i < data.length; i++) bytes.setInt16(44 + i * 2, Math.max(-1, Math.min(1, data[i])) * 0x7fff, true);
  return new Blob([bytes], { type: 'audio/wav' });
}
function openCloneDialog() {
  const dialog = el('dialog'); dialog.style.width = 'min(440px, 94vw)';
  const form = el('form'); form.style.display = 'grid'; form.style.gap = '10px';
  const name = el('input'); name.type = 'text'; name.value = 'My voice'; name.maxLength = 40;
  const file = el('input'); file.type = 'file'; file.accept = '.wav,.mp3,.m4a,.flac';
  const record = el('button', 'btn', '● Record 30-60 s'); record.type = 'button';
  const consent = el('input'); consent.type = 'checkbox';
  const consentRow = el('label', 'row'); consentRow.append(consent, el('span', '', 'This is my own voice (or I have the speaker’s permission) and I agree to it being cloned with ElevenLabs.'));
  const status = el('p', 'muted', 'Read anything aloud in a quiet room. Clear speech for about a minute works best.');
  const save = el('button', 'btn primary', 'Create voice'); save.type = 'submit';
  const close = el('button', 'btn', 'Cancel'); close.type = 'button'; close.onclick = () => dialog.close();
  const actions = el('div', 'row'); actions.append(save, close);
  let recorded = null, recorder = null, timer = null;
  record.onclick = async () => {
    if (recorder) { recorder.stop(); return; }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true }); const chunks = [];
      recorder = new MediaRecorder(stream); recorder.ondataavailable = e => chunks.push(e.data);
      recorder.onstop = async () => {
        clearInterval(timer); stream.getTracks().forEach(t => t.stop()); recorder = null; record.textContent = '● Record again';
        const ctx = new AudioContext(); const audio = await ctx.decodeAudioData(await new Blob(chunks).arrayBuffer()); await ctx.close();
        recorded = wavFrom(audio); status.textContent = `Recorded ${Math.round(audio.duration)} s. Ready to create.`;
      };
      recorder.start(); const started = Date.now();
      timer = setInterval(() => { const sec = Math.round((Date.now() - started) / 1000); record.textContent = `■ Stop (${sec} s)`; if (sec >= 90) recorder?.stop(); }, 250);
    } catch { status.textContent = 'Microphone unavailable. Upload a recording instead.'; }
  };
  form.onsubmit = async event => {
    event.preventDefault();
    const sample = file.files[0] || (recorded && new File([recorded], 'recording.wav', { type: 'audio/wav' }));
    if (!sample) { status.textContent = 'Record or upload a sample first.'; return; }
    if (!consent.checked) { status.textContent = 'Please confirm consent first.'; return; }
    save.disabled = true; status.textContent = 'Creating your voice… (can take up to a minute)';
    const body = new FormData(); body.append('name', name.value); body.append('consent', 'true'); body.append('sample', sample);
    try {
      const response = await fetch('/api/voice-clone', { method: 'POST', body, headers: staffToken() ? { 'X-Staff-Token': staffToken() } : {} });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw Error(data.message || 'Voice creation failed.');
      if (data.requires_verification) { status.textContent = 'ElevenLabs asks you to verify this voice in your account first.'; save.disabled = false; return; }
      tier = 'standard'; await loadVoices(data.voice?.id);
      status.textContent = `“${data.voice.label}” is ready and selected in Standard mode.`; save.hidden = true; close.textContent = 'Done';
    } catch (error) { status.textContent = error.message; save.disabled = false; }
  };
  form.append(el('h3', '', 'Create my voice'), el('label', 'muted', 'Name'), name, record, el('span', 'muted', 'or upload a recording'), file, consentRow, status, actions);
  dialog.append(form); document.body.append(dialog); dialog.addEventListener('close', () => dialog.remove()); dialog.showModal();
}
$('clone').onclick = openCloneDialog;
if (config) loadVoices();

// --- the assistant's persona follows the chosen voice; settings live in a dialog ----------------
function showAgent() {
  const voice = voices?.voices.find(v => v.id === $('voice').value);
  agentName = voice?.agent_name || 'Aria';
  $('agent-name').textContent = agentName; $('agent-avatar').textContent = agentName[0];
}
$('voice').onchange = showAgent;
$('open-settings').onclick = () => $('settings-dialog').showModal();
$('close-settings').onclick = () => $('settings-dialog').close();
