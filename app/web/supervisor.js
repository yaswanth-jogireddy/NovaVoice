// Supervisor dashboard: KPIs from the audit trail (/api/metrics), eval results (/api/evals).
import { $, el, api, initChrome, on, fmtTime, pct, inr, humanize } from '/assets/shared.js';

const PILLARS = {
  'Identity & SSO': 'Callers verified by code, plus one-time-code step-ups',
  'Role-Based Access': 'Requests held until identity or a code was confirmed',
  'Auditability': 'Append-only audit events, every decision recorded',
  'Data Protection': 'Numbers masked, unbacked claims and guesses blocked',
  'Human Oversight': 'Warm handoffs to a person, with full context',
  'Secure Deployment': 'Injection and social-engineering attempts refused',
};
const notice = text => { $('notice').textContent = text || ''; $('notice').hidden = !text; };

function bars(target, counts, warn = false) {
  const entries = Object.entries(counts || {}).filter(([k]) => k && k !== 'null').sort((a, b) => b[1] - a[1]);
  if (!entries.length) { target.replaceChildren(el('div', 'muted', 'None yet')); return; }
  const max = Math.max(...entries.map(([, n]) => n));
  target.replaceChildren(...entries.map(([key, n]) => {
    const row = el('div', 'bar' + (warn ? ' warn' : '')); const track = el('div', 'track'); const fill = el('i');
    fill.style.width = `${(n / max) * 100}%`; track.append(fill);
    row.append(el('span', '', humanize(key)), track, el('span', 'n', String(n))); return row;
  }));
}

// Each voice call is priced on both modes from its own usage (same spoken characters, the other provider's
// rate), so the two columns compare the same calls. Text calls have no speech cost and are left out.
function tiers(v) {
  const entries = Object.entries(v?.tiers || {});
  if (!v?.voice_calls || !entries.length) {
    $('tiers').replaceChildren(el('div', 'muted', 'No voice calls yet. Make one in Talk mode from the Help and support screen.'));
    $('tier-gap').textContent = ''; $('tier-foot').textContent = ''; return;
  }
  $('tiers').replaceChildren(...entries.map(([key, t]) => {
    const box = el('div', 'tier' + (t.calls_on_this_tier ? ' used' : ''));
    const dl = el('dl');
    for (const [k, val] of [['Per minute', inr(t.per_minute_inr)], [`At ${v.monthly_calls.toLocaleString('en-IN')} calls/month`, inr(t.monthly_inr)],
      ['Calls made on it', String(t.calls_on_this_tier)]]) dl.append(el('dt', '', k), el('dd', '', val));
    box.append(el('h3', '', t.label), el('div', 'big', `${inr(t.avg_per_call_inr)} per call`), dl);
    return box;
  }));
  $('tier-gap').textContent = v.standard_vs_cost_pct !== undefined
    ? `Standard costs ${v.standard_vs_cost_pct}% more per call: ${inr(v.monthly_difference_inr)} a month at ${v.monthly_calls.toLocaleString('en-IN')} calls. Same listening and AI model; only the speaking voice differs.`
    : '';
  $('tier-foot').textContent = `Based on ${v.voice_calls} voice call${v.voice_calls === 1 ? '' : 's'}, each priced on both modes from its own usage. `
    + 'Estimates from published rates, excluding tax; monthly volume is an assumption.';
}

async function refresh() {
  try {
    const m = await api('/api/metrics');
    $('k-contain').textContent = pct(m.containment_rate);
    $('k-contain-n').textContent = `${m.contained} of ${m.calls} calls · ${m.handed_off} handed to a person`;
    $('k-aht').textContent = m.aht_s === null ? '—' : m.aht_s >= 60 ? `${Math.floor(m.aht_s / 60)}m ${Math.round(m.aht_s % 60)}s` : `${Math.round(m.aht_s)}s`;
    $('k-cost').textContent = inr(m.ai_cost_per_call_inr);
    $('k-cost-n').textContent = `vs ${inr(m.human_cost_per_call_inr)} for a human-handled call (assumption)`;
    $('k-save').textContent = inr(m.saving_inr);
    tiers(m.voice_tiers);
    $('trust').replaceChildren(...Object.entries(PILLARS).map(([name, detail]) => {
      const card = el('div', 'pillar'); card.append(el('div', 'n', String(m.trust?.[name] ?? 0)), el('div', 't', name), el('div', 'd', detail)); return card;
    }));
    bars($('intent-results'), m.intent_results);
    bars($('intent-types'), m.intents_by_type);
    bars($('reasons'), m.escalation_reasons, true);
    bars($('guards'), m.guardrail_events);
    $('calls').replaceChildren(...(m.recent_calls || []).map(c => {
      const tr = el('tr');
      // The call's disposition (csm/disposition.py); older calls recorded only contained / handoff.
      const label = c.disposition_label || (c.outcome === 'contained' ? 'Solved by AI' : `Handoff · ${humanize(c.reason)}`);
      const good = c.disposition ? ['resolved_by_ai', 'request_raised', 'emi_ticket_call_ended'].includes(c.disposition) : c.outcome === 'contained';
      const outcome = el('span', `chip ${good ? 'ok' : 'warn'}`, label);
      outcome.title = `Labelled by: ${c.disposition_note || (c.disposition_source === 'jev' ? 'Jev' : 'call record')}`;
      if (c.disposition_source === 'jev') outcome.append(el('span', 'muted', ' · Jev'));
      // Each request with its own result: one call can be solved, ticketed and transferred at once.
      const requests = el('div');
      requests.append(...(c.intents || []).map(i => el('div', '',
        `${i.intent_label} → ${i.resolution_label}${i.ticket_id ? ` · ${i.ticket_id}` : ''}`)));
      if (!c.intents?.length) requests.textContent = c.customer_query || '—';
      const cells = [fmtTime(c.at), outcome, requests, humanize(c.identity).toLowerCase(), `${Math.round(c.aht_s || 0)}s`, inr(c.cost_inr), (c.tickets || []).join(', ') || '—'];
      for (const v of cells) { const td = el('td'); td.append(v instanceof Node ? v : document.createTextNode(v)); tr.append(td); }
      return tr;
    }));
    if (!m.recent_calls?.length) $('calls').replaceChildren(Object.assign(el('tr'), { innerHTML: '<td colspan="7" class="muted">No calls yet. Make one from the Customer call screen.</td>' }));
    $('updated').textContent = `Updated ${new Date().toLocaleTimeString()}`;
    notice('');
  } catch (error) { notice(error.status === 401 ? 'Staff access needed: open this page with the staff link (?token=…).' : 'Metrics unavailable.'); }
}

async function loadEvals() {
  if (!on('EVALS')) return;
  try {
    const { summary: s } = await api('/api/evals');
    $('evals-card').hidden = false;
    const chip = (text, ok) => el('span', `chip ${ok ? 'ok' : 'warn'}`, text);
    $('evals').replaceChildren(
      chip(`${s.passed}/${s.scenarios} scripted calls pass (${s.mode})`, s.passed === s.scenarios),
      chip(`Guardrail pass rate ${pct(s.guardrail_pass_rate)}`, s.guardrail_pass_rate === 1),
      chip(`Tool accuracy ${pct(s.tool_accuracy)}`, s.tool_accuracy === 1),
      chip(`${s.leaks} leaks`, s.leaks === 0),
      chip(`Model cost ${inr(s.cost_inr_per_call)}/call`, true));
  } catch { /* no report yet */ }
}

await initChrome().catch(() => notice('Configuration unavailable. Is the server running?'));
if (!on('SUPERVISOR_DASHBOARD')) {
  $('board').hidden = true;
  notice('The supervisor dashboard is not enabled at this milestone.');
} else {
  refresh(); loadEvals(); setInterval(refresh, 5000);
}
