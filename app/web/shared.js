// Shared chrome for the three screens: top bar, theme, staff token, milestone switcher.
// Visibility comes from GET /api/config; the server enforces every flag on its own routes.
export const $ = id => document.getElementById(id);

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

// Screens never show a full account, card or phone number, even in the caller's own words.
// Ticket ids (SR-261030-0001) are references the caller needs, so they stay readable.
export const mask = text => String(text ?? '').replace(/SR-\d{6}-\d{4}|\d(?:[ -]?\d){6,}/g,
  m => (m.startsWith('SR-') ? m : '••••' + m.replace(/\D/g, '').slice(-4)));

const store = {
  get(key) { try { return sessionStorage.getItem(key) || localStorage.getItem(key) || ''; } catch { return ''; } },
  set(key, value, persist = false) { try { (persist ? localStorage : sessionStorage).setItem(key, value); } catch { /* storage blocked: still works for this page */ } },
};

// A staff link like /desk?token=... is remembered for the tab, then dropped from the address bar.
const urlToken = new URLSearchParams(location.search).get('token');
if (urlToken) { store.set('nt.staff', urlToken); history.replaceState(null, '', location.pathname); }
export const staffToken = () => store.get('nt.staff');

export async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (staffToken()) headers['X-Staff-Token'] = staffToken();
  const response = await fetch(path, { cache: 'no-store', ...options, headers });
  if (!response.ok) { const error = new Error(await response.text() || response.statusText); error.status = response.status; throw error; }
  return response.json();
}

export function wsUrl(path) {
  const url = new URL(path, location.href);
  url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  if (staffToken()) url.searchParams.set('token', staffToken());
  return url.toString();
}

export let config = null;
export async function loadConfig() { config = await api('/api/config'); return config; }
export const on = name => Boolean(config?.features?.[name]);

const PAGES = [['/', 'Help and support', null], ['/desk', 'Agent desk', 'WARM_HANDOFF'], ['/supervisor', 'Supervisor', 'SUPERVISOR_DASHBOARD']];

export async function initChrome() {
  applyTheme(store.get('nt.theme'));
  await loadConfig();
  const bar = $('topbar');
  bar.className = 'topbar';
  const brand = el('a', 'brand'); brand.href = '/';
  const logo = el('img'); logo.src = '/assets/logo.svg'; logo.alt = '';
  const name = el('span', '', 'Nova Trust Bank'); name.append(el('small', '', 'AI customer service'));
  brand.append(logo, name);
  const nav = el('nav', 'nav'); nav.setAttribute('aria-label', 'Screens');
  for (const [href, label, flag] of PAGES) {
    if (flag && !on(flag)) continue;
    const link = el('a', '', label); link.href = href;
    if (location.pathname === href) link.setAttribute('aria-current', 'page');
    nav.append(link);
  }
  // The milestone switch is internal (scripts/milestone.py); it is not part of the product UI.
  const theme = el('button', 'icon-btn'); theme.title = 'Light or dark'; theme.setAttribute('aria-label', 'Toggle light or dark theme');
  theme.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';
  theme.onclick = () => { const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'; applyTheme(next); store.set('nt.theme', next, true); };
  bar.replaceChildren(brand, nav, el('span', 'spacer'), theme);
  return config;
}

function applyTheme(theme) {
  if (theme === 'dark' || theme === 'light') document.documentElement.dataset.theme = theme;
}

export const fmtTime = iso => { try { return new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); } catch { return iso; } };
export const pct = v => v === null || v === undefined ? '—' : `${Math.round(v * 100)}%`;
export const inr = v => v === null || v === undefined ? '—' : `₹${Number(v).toLocaleString('en-IN', { maximumFractionDigits: v < 1 ? 3 : 2 })}`;
export const humanize = s => String(s ?? '').replaceAll('_', ' ');

// "Add test customer": synthetic data with editable defaults, created on the server (staff only).
const MOCK_FIELDS = [
  ['full_name', 'Full name', 'text'], ['dob', 'Date of birth', 'date'], ['mobile', 'Registered mobile (10 digits)', 'text'],
  ['city', 'City', 'text'], ['language', 'Language', 'select'], ['balance_inr', 'Balance (₹)', 'number'],
  ['account_last4', 'Account last 4', 'text'], ['card_last4', 'Debit card last 4', 'text'],
  ['with_loan', 'Add a home loan (EMI questions)', 'checkbox'], ['with_suspicious_charge', 'Add an unrecognised ₹12,499 charge (dispute)', 'checkbox'],
  ['with_past_tickets', 'Add past service requests (one resolved, one in progress)', 'checkbox'],
];
export async function openMockDialog(onCreated) {
  let defaults;
  try { defaults = await api('/api/mock-customers/defaults'); }
  catch (error) { alert(error.status === 401 ? 'Staff access needed (open the page with ?token=…).' : 'Test customers need account tools (milestone 2+).'); return; }
  const dialog = el('dialog'); dialog.style.width = 'min(460px, 94vw)';
  const form = el('form'); form.method = 'dialog'; form.style.display = 'grid'; form.style.gap = '10px';
  form.append(el('h3', '', 'Add a test customer'), el('p', 'muted', 'Synthetic data for testing the agent. Defaults work as they are; change anything you like.'));
  const inputs = {};
  for (const [key, label, type] of MOCK_FIELDS) {
    const row = el('label'); row.style.display = 'grid'; row.style.gap = '4px'; row.style.fontSize = '14px';
    let input;
    if (type === 'select') { input = el('select'); for (const [v, t] of [['en', 'English'], ['hi', 'Hindi'], ['hinglish', 'Hinglish']]) input.add(new Option(t, v)); }
    else { input = el('input'); input.type = type; }
    if (type === 'checkbox') { input.checked = Boolean(defaults[key]); row.style.display = 'flex'; row.style.alignItems = 'center'; row.style.gap = '8px'; row.append(input, el('span', '', label)); }
    else { input.value = defaults[key] ?? ''; row.append(el('span', 'muted', label), input); }
    inputs[key] = input; form.append(row);
  }
  const status = el('p', 'muted', ''); const actions = el('div', 'row');
  const save = el('button', 'btn primary', 'Add customer'); save.type = 'submit';
  const cancel = el('button', 'btn', 'Cancel'); cancel.type = 'button'; cancel.onclick = () => dialog.close();
  actions.append(save, cancel); form.append(status, actions);
  form.onsubmit = async event => {
    event.preventDefault(); save.disabled = true; status.textContent = 'Adding…';
    const body = Object.fromEntries(Object.entries(inputs).map(([k, i]) => [k, i.type === 'checkbox' ? i.checked : i.value]));
    try {
      const created = await api('/api/mock-customers', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      status.textContent = `Added ${created.full_name} as ${created.customer_id}. Say: “My customer ID is ${created.customer_id}, date of birth ${created.dob}.”`
        + (created.past_tickets?.length ? ` Past requests: ${created.past_tickets.join(', ')}.` : '');
      save.hidden = true; cancel.textContent = 'Done'; onCreated?.(created);
    } catch (error) { status.textContent = error.message || 'Could not add the customer.'; save.disabled = false; }
  };
  dialog.append(form); document.body.append(dialog); dialog.addEventListener('close', () => dialog.remove()); dialog.showModal();
}
