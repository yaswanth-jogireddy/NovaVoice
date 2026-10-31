// Agent Desk: live handoff queue (/ws/desk) and the warm-handoff packet for the selected caller.
import { $, el, mask, api, wsUrl, initChrome, on, fmtTime, humanize, openMockDialog } from '/assets/shared.js';

const REASON = { caller_request: ['info', 'Asked for a person'], identity_failed: ['bad', 'Verification failed'],
  risk_flag: ['bad', 'Risk: possible fraud'], out_of_scope: ['warn', 'Out of scope'], negative_sentiment: ['warn', 'Upset caller'],
  tool_failure: ['warn', 'System action failed'], complex_issue: ['info', 'Complex issue'] };
const handoffs = new Map();
let selected = null;

const notice = text => { $('notice').textContent = text || ''; $('notice').hidden = !text; };
const reasonChip = reason => { const [kind, label] = REASON[reason] || ['', humanize(reason)]; return el('span', `chip ${kind}`, label); };
const who = h => h.customer ? `${h.customer.first_name} · ${h.customer.id}` : 'Unverified caller';

function renderQueue(fresh) {
  const waitingFirst = h => (h.status === 'waiting' ? 0 : 1);
  const items = [...handoffs.values()].sort((a, b) => waitingFirst(a) - waitingFirst(b) || b.created_at.localeCompare(a.created_at));
  if (!items.length) return;
  $('queue').replaceChildren(...items.map(h => {
    const item = el('button', 'item' + (h.handoff_id === fresh ? ' new' : '')); item.type = 'button';
    item.setAttribute('aria-current', String(h.handoff_id === selected));
    const top = el('div', 'top'); top.append(el('span', '', who(h)), el('span', 'sub', fmtTime(h.created_at)));
    const status = h.status === 'accepted' ? el('span', 'chip ok', `Accepted · ${h.accepted_by || 'agent'}`) : reasonChip(h.reason);
    item.append(top, el('div', 'sub', (h.intents || []).join(', ') || 'No request stated'), status);
    item.onclick = () => { selected = h.handoff_id; renderQueue(); renderDetail(); };
    return item;
  }));
}

function list(title, values, empty = 'None') {
  const box = el('div'); box.append(el('h3', '', title));
  const ul = el('ul'); for (const v of values?.length ? values : [empty]) ul.append(el('li', '', mask(humanize(v))));
  box.append(ul); return box;
}

function renderDetail() {
  const h = handoffs.get(selected); const box = $('detail');
  if (!h) return;
  const head = el('div', 'head'); const title = el('div');
  title.append(el('h1', '', who(h)), reasonChip(h.reason));
  const identity = el('span', `chip ${h.identity === 'VERIFIED' || h.identity === 'STEPPED_UP' ? 'ok' : h.identity === 'LOCKED' ? 'bad' : ''}`, humanize(h.identity).toLowerCase());
  const mood = el('span', `chip ${h.sentiment === 'negative' ? 'warn' : ''}`, `Mood: ${h.sentiment}`);
  const meta = el('div', 'row'); meta.append(identity, mood, el('span', 'chip', `Language: ${h.language}`));
  title.append(meta);
  const accept = el('button', 'btn primary', h.status === 'accepted' ? `Accepted by ${h.accepted_by}` : 'Accept call');
  accept.disabled = h.status !== 'waiting';
  accept.onclick = async () => {
    accept.disabled = true;
    try { await api(`/api/handoffs/${h.handoff_id}/accept?agent=Desk%20agent`, { method: 'POST' }); h.status = 'accepted'; h.accepted_by = 'Desk agent'; renderQueue(); renderDetail(); }
    catch (error) { notice(error.status === 409 ? 'Another agent already accepted this caller.' : 'Accept failed.'); accept.disabled = false; }
  };
  head.append(title, accept);
  const summary = el('div', 'summary', mask(h.summary));
  const next = el('div', 'next'); next.append(el('b', '', 'Suggested next step: '), document.createTextNode(h.suggested_next_step));
  const grid = el('div', 'grid');
  grid.append(list('Requests', h.intents), list('Already done by Aria', h.actions), list('Tickets', h.tickets),
    list('Risk cues', h.risk_flags), list('Policies cited', h.policy_docs));
  const turns = el('div', 'turns');
  for (const t of h.last_turns || []) { const row = el('div', t.role); row.append(el('b', '', t.role === 'user' ? 'Caller' : 'Aria'), document.createTextNode(mask(t.text))); turns.append(row); }
  const transcript = el('div'); transcript.append(el('h2', '', 'Last turns (PII masked)'), turns);
  box.replaceChildren(head, summary, next, grid, transcript);
}

function live(state, text) { $('live').className = 'live' + (state ? ' on' : ''); $('live').querySelector('span').textContent = text; }

function connect() {
  const socket = new WebSocket(wsUrl('/ws/desk'));
  socket.onopen = () => live(true, 'Live');
  socket.onmessage = event => {
    const data = JSON.parse(event.data);
    if (data.type === 'queue') { for (const h of data.handoffs) handoffs.set(h.handoff_id, { ...h, status: h.status || 'waiting' }); renderQueue(); }
    else if (data.type === 'handoff') {
      handoffs.set(data.packet.handoff_id, { ...data.packet, status: 'waiting' });
      if (!selected) selected = data.packet.handoff_id;
      renderQueue(data.packet.handoff_id); renderDetail(); loadTickets();
    } else if (data.type === 'handoff_accepted') {
      const h = handoffs.get(data.handoff_id); if (h) { h.status = 'accepted'; h.accepted_by = data.agent; renderQueue(); renderDetail(); }
    } else if (data.type === 'config') location.reload();
  };
  socket.onclose = () => { live(false, 'Reconnecting…'); setTimeout(connect, 2000); };
}

const RAISED_BY = { ai_agent: 'Aria (AI)', staff_desk: 'Desk (staff)', seed: 'Demo data' };
const TICKET_CATEGORIES = [['complaint', 'Complaint'], ['account', 'Account change'], ['loan', 'Loan / EMI'], ['card', 'Card'],
  ['statement', 'Statement'], ['dispute', 'Dispute'], ['kyc', 'KYC'], ['general', 'General']];

// Staff add a service request (past or open) on any customer, to test "what's the status of my request?".
async function openTicketDialog(onCreated) {
  let customers = [];
  try { customers = await api('/api/test-customers'); } catch { /* a typed customer ID still works */ }
  const dialog = el('dialog'); dialog.style.width = 'min(460px, 94vw)';
  const form = el('form'); form.method = 'dialog'; form.style.display = 'grid'; form.style.gap = '10px';
  form.append(el('h3', '', 'Add a service request'),
    el('p', 'muted', 'A past or open request on a customer. On a call, verify as that customer and ask for its status by number.'));
  const field = (label, input) => {
    const row = el('label'); row.style.display = 'grid'; row.style.gap = '4px'; row.style.fontSize = '14px';
    row.append(el('span', 'muted', label), input); form.append(row); return input;
  };
  const customer = field('Customer', el('select'));
  for (const c of customers) customer.add(new Option(`${c.customer_id} · ${c.full_name}`, c.customer_id));
  if (!customers.length) customer.add(new Option('C1001', 'C1001'));
  const category = field('Category', el('select'));
  for (const [v, t] of TICKET_CATEGORIES) category.add(new Option(t, v));
  const summary = field('Summary', Object.assign(el('input'), { type: 'text', value: 'Address change request', maxLength: 200 }));
  const status = field('Status', el('select'));
  for (const [v, t] of [['open', 'Open'], ['in_progress', 'In progress'], ['resolved', 'Resolved']]) status.add(new Option(t, v));
  const daysAgo = field('Raised how many days ago', Object.assign(el('input'), { type: 'number', min: 0, max: 365, value: 3 }));
  const note = el('p', 'muted', ''); const actions = el('div', 'row');
  const save = el('button', 'btn primary', 'Add request'); save.type = 'submit';
  const cancel = el('button', 'btn', 'Cancel'); cancel.type = 'button'; cancel.onclick = () => dialog.close();
  actions.append(save, cancel); form.append(note, actions);
  form.onsubmit = async event => {
    event.preventDefault(); save.disabled = true; note.textContent = 'Adding…';
    const body = { customer_id: customer.value, category: category.value, summary: summary.value, status: status.value, days_ago: Number(daysAgo.value) };
    try {
      const t = await api('/api/tickets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      note.textContent = `Added ${t.ticket_id} (${humanize(t.status)}) for ${t.customer_id}. On a call: verify as ${t.customer_id}, then ask “What's the status of request ${t.ticket_id}?”`;
      save.hidden = true; cancel.textContent = 'Done'; onCreated?.(t);
    } catch (error) { note.textContent = error.message || 'Could not add the request.'; save.disabled = false; }
  };
  dialog.append(form); document.body.append(dialog); dialog.addEventListener('close', () => dialog.remove()); dialog.showModal();
}

async function loadTickets() {
  if (!on('TICKETS')) return;
  try {
    const rows = await api('/api/tickets');
    $('tickets-card').hidden = false;
    $('tickets').replaceChildren(...rows.slice(0, 12).map(t => {
      const tr = el('tr');
      for (const v of [t.id, t.customer_id || '—', t.category, mask(t.summary), humanize(t.status), RAISED_BY[t.created_by] || t.created_by]) tr.append(el('td', '', v));
      return tr;
    }));
  } catch { /* tickets are optional on this screen */ }
}

await initChrome().catch(() => notice('Configuration unavailable. Is the server running?'));
if (!on('WARM_HANDOFF')) {
  $('layout').hidden = true;
  notice('The Agent Desk is not enabled at this milestone. Handoffs are a plain transfer for now.');
} else {
  try {
    for (const h of await api('/api/handoffs')) handoffs.set(h.handoff_id, h);
    selected = [...handoffs.values()].find(h => h.status === 'waiting')?.handoff_id ?? null;
    renderQueue(); renderDetail();
  } catch (error) { notice(error.status === 401 ? 'Staff access needed: open this page with the staff link (?token=…).' : 'Could not load the queue.'); }
  connect();
}
// Service requests are test data at every milestone, not only with the handoff desk.
loadTickets();

if (on('ACCOUNT_TOOLS')) { $('mock-card').hidden = false; $('add-customer').onclick = () => openMockDialog(loadTickets); }
if (on('TICKETS')) { $('mock-card').hidden = false; $('add-ticket').hidden = false; $('add-ticket').onclick = () => openTicketDialog(loadTickets); }
