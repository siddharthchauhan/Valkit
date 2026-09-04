/* Boot, shell and router.
 *
 * Hash routes, not the History API: the console is served by a FileResponse at
 * `/`, so a path router would 404 on refresh and on the auditor's deep link.
 * Query state lives inside the hash, which makes a filtered view shareable.
 *
 * The integrity bar is re-verified on boot, on a route change older than sixty
 * seconds, and after every write — never on a timer, because each verification
 * re-derives the whole chain server-side.
 */

import { api, IntegrityFailure } from './api.js';
import { COPY } from './copy.js';
import { announce, clear, digest, el, errorBlock, tok } from './dom.js';
import { hooks, identity } from './session.js';

import { renderIndex } from './views/index.js';
import { renderVerdict } from './views/verdict.js';
import { renderAcceptance } from './views/acceptance.js';
import { renderChain } from './views/chain.js';
import { renderPackage } from './views/package.js';
import { renderSign } from './views/sign.js';
import { renderDocument } from './views/document.js';
import { renderAudit } from './views/audit.js';
import { renderEvidence } from './views/evidence.js';
import { renderDigest } from './views/digest.js';
import { renderMonitoring } from './views/monitoring.js';
import { renderSpec } from './views/spec.js';
import { renderPrint } from './views/print.js';

// -------------------------------------------------------------- identity

function renderIdentity() {
  const held = identity.get();
  const input = document.getElementById('actor-input');
  const note = document.getElementById('actor-note');
  const clearBtn = document.getElementById('actor-clear');
  if (!input) return;
  // Never write back into the field while it has focus: it would move the
  // caret out from under someone mid-word.
  if (document.activeElement !== input) input.value = held?.value || '';
  note.textContent = held ? COPY.ID_SET(held.set_at) : COPY.ID_UNSET;
  clearBtn.hidden = !held;
}

// ---------------------------------------------------------------- theme

const THEME_KEY = 'valkit.theme';

function applyTheme(choice) {
  const root = document.documentElement;
  if (choice === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', choice);
  try { localStorage.setItem(THEME_KEY, choice); } catch { /* not essential */ }
  document.querySelectorAll('.theme button').forEach((b) => {
    b.setAttribute('aria-pressed', String(b.dataset.theme === choice));
  });
}

// ------------------------------------------------------- integrity bar

let integrityState = { ok: null, at: null, chain: null, evidence: null };
export const getIntegrity = () => integrityState;

async function verifyIntegrity() {
  const bar = document.getElementById('integrity-bar');
  const [chain, evidence] = await Promise.allSettled([
    api.verifyAudit(),
    api.verifyEvidence(),
  ]);

  const chainValue = chain.status === 'fulfilled' ? chain.value : null;
  const evidenceValue = evidence.status === 'fulfilled' ? evidence.value : null;
  integrityState = {
    chain: chainValue,
    evidence: evidenceValue,
    ok: Boolean(chainValue?.ok && evidenceValue?.ok),
    at: new Date().toISOString().replace(/\.\d+Z$/, 'Z'),
  };

  clear(bar);
  bar.append(el('h2', { id: 'integrity-h', class: 'vh', text: 'Integrity' }));

  const cells = el('div', { class: 'integrity-cells' });
  cells.append(
    cell(chainValue, COPY.INT_CHAIN, 'Audit chain', chainValue?.detail?.chain_digest),
    cell(evidenceValue, COPY.INT_VAULT, 'Evidence vault', null),
  );
  bar.append(cells);

  const asat = el('p', { class: 'asat' }, [
    `Verified at ${integrityState.at}.`,
    el('button', { type: 'button', text: 'Verify again', onclick: () => verifyIntegrity() }),
  ]);
  const detail = el('details', { class: 'integrity-detail' }, [
    el('summary', { text: 'Integrity details' }),
    asat,
    el('p', { class: 'note', text: COPY.INT_LIMIT }),
  ]);
  bar.append(detail);
  return integrityState;
}

function cell(result, line, name, chainDigest) {
  let state = 'unknown';
  let token = 'INT';
  let text = COPY.INT_FAILED_REQUEST;

  if (result && result.ok) {
    state = 'ok';
    token = 'OK';
    text = line(result.checked);
  } else if (result) {
    state = 'integrity';
    token = 'INT';
    text = `${name} — ${result.reason || 'verification failed'}`;
  }

  const node = el('div', { class: 'cell', dataset: { state } }, [tok(token)]);
  const body = el('div', {}, [el('p', { class: 'cell-line', text })]);
  if (chainDigest) {
    const digestLine = el('p', { class: 'cell-line' }, ['Chain digest ']);
    digestLine.append(digest(chainDigest));
    body.append(digestLine);
  }
  node.append(body);
  return node;
}

// --------------------------------------------------------------- router

const ROUTES = [
  [/^\/?$/, () => renderIndex()],
  [/^\/spec$/, (_, query) => renderSpec({ loadExample: query.example === '1' })],
  [/^\/audit$/, (_, query) => renderAudit(query)],
  [/^\/evidence$/, (_, query) => renderEvidence(query)],
  [/^\/digest\/([0-9a-fA-F]+)$/, (m) => renderDigest(m[1].toLowerCase())],
  [/^\/agent\/([^/]+)\/monitoring$/, (m) => renderMonitoring(decodeURIComponent(m[1]))],
  [/^\/doc\/([^/]+)$/, (m, query) => renderDocument(decodeURIComponent(m[1]), query.v || null)],
  [/^\/v\/([^/]+)\/acceptance$/, (m) => renderAcceptance(decodeURIComponent(m[1]))],
  [/^\/v\/([^/]+)\/chain$/, (m) => renderChain(decodeURIComponent(m[1]))],
  [/^\/v\/([^/]+)\/chain\/([^/]+)$/, (m) => renderChain(decodeURIComponent(m[1]), decodeURIComponent(m[2]))],
  [/^\/v\/([^/]+)\/package$/, (m) => renderPackage(decodeURIComponent(m[1]))],
  [/^\/v\/([^/]+)\/sign$/, (m) => renderSign(decodeURIComponent(m[1]))],
  [/^\/v\/([^/]+)\/print$/, (m) => renderPrint(decodeURIComponent(m[1]))],
  [/^\/v\/([^/]+)$/, (m) => renderVerdict(decodeURIComponent(m[1]))],
];

function parseHash() {
  const raw = location.hash.replace(/^#/, '') || '/';
  const [path, search] = raw.split('?');
  return { path: path || '/', query: Object.fromEntries(new URLSearchParams(search || '')) };
}

export function navigate(hash) {
  location.hash = hash;
}

const SUBNAV = [
  ['', 'Overview'],
  ['/acceptance', 'Results'],
  ['/chain', 'Traceability'],
  ['/package', 'Documents'],
  ['/sign', 'Approvals'],
  ['/print', 'Print'],
];

function renderSubnav(path, query = {}) {
  const nav = document.getElementById('subnav');
  const match = path.match(/^\/v\/([^/]+)/);
  clear(nav);

  // A document screen belongs to a validation too, and the reader who opened it
  // from the package needs the way back. The route carries which one.
  const id = match ? match[1] : (path.startsWith('/doc/') && query.v
    ? encodeURIComponent(query.v)
    : null);
  if (!id) { nav.hidden = true; return; }

  nav.hidden = false;
  const list = el('ul');
  list.append(el('li', {}, [el('span', { class: 'val-id', text: decodeURIComponent(id) })]));
  const signable = Boolean(identity.value()) && integrityState.ok !== false;

  for (const [suffix, name] of SUBNAV) {
    const href = `#/v/${id}${suffix}`;
    const current = path === `/v/${id}${suffix}`;
    const disabled = suffix === '/sign' && !signable;
    list.append(el('li', {}, [el('a', {
      href,
      text: name,
      'aria-current': current ? 'page' : null,
      'aria-disabled': disabled ? 'true' : null,
      title: disabled
        ? (identity.value() ? 'Refused while integrity has failed.' : COPY.SIGN_NO_IDENTITY)
        : null,
    })]));
  }
  nav.append(list);
}

function markMainNav(path) {
  const target = path === '/' ? '#/' : `#${path.split('?')[0]}`;
  document.querySelectorAll('.mainnav a').forEach((a) => {
    if (a.getAttribute('href') === target) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
}

let lastVerified = 0;

async function route() {
  const { path, query } = parseHash();
  const main = document.getElementById('main');

  markMainNav(path);
  renderSubnav(path, query);

  const age = Date.now() - lastVerified;
  if (!lastVerified || age > 60_000) {
    lastVerified = Date.now();
    verifyIntegrity().then(() => {
      const now = parseHash();
      renderSubnav(now.path, now.query);
    });
  }

  clear(main);
  main.append(el('p', { class: 'pending', role: 'status', text: COPY.PENDING('the record') }));

  const matched = ROUTES.find(([pattern]) => pattern.test(path));
  try {
    let content;
    if (!matched) {
      content = el('div', {}, [
        el('p', { class: 'console-note', text: COPY.ROUTE_UNKNOWN }),
        await renderIndex(),
      ]);
    } else {
      content = await matched[1](path.match(matched[0]), query);
    }
    clear(main);
    main.append(content);
  } catch (err) {
    clear(main);
    main.append(err instanceof IntegrityFailure ? interdiction(err) : errorBlock(err));
  }

  main.focus({ preventScroll: true });
  window.scrollTo(0, 0);
}

export function interdiction(err) {
  const box = el('section', { class: 'interdiction', role: 'alert' }, [
    el('h1', { text: COPY.INTERDICT_H }),
    el('p', { class: 'server', text: err.error || String(err.message || '') }),
    el('p', { text: COPY.INTERDICT_1 }),
    el('p', { text: COPY.INTERDICT_2 }),
    el('p', { text: COPY.INTERDICT_3 }),
  ]);
  box.append(el('p', {}, [
    el('a', { href: api.auditExportUrl('text'), text: 'Export the audit trail as text' }),
    ' · ',
    el('a', { href: api.auditExportUrl('jsonl'), text: 'Export as JSONL' }),
  ]));
  return box;
}

/** Re-verify after a write, and let the caller re-render. */
async function afterWrite() {
  lastVerified = Date.now();
  await verifyIntegrity();
  const now = parseHash();
  renderSubnav(now.path, now.query);
}
hooks.afterWrite = afterWrite;
hooks.identityChanged = renderIdentity;

// ----------------------------------------------------------------- boot

function boot() {
  document.getElementById('disc-1').textContent = COPY.DISC_1;
  const rest = document.getElementById('disc-rest');
  for (const line of COPY.DISC_REST) rest.append(el('li', { text: line }));

  let theme = 'system';
  try { theme = localStorage.getItem(THEME_KEY) || 'system'; } catch { /* default */ }
  applyTheme(theme);
  document.querySelectorAll('.theme button').forEach((b) => {
    b.addEventListener('click', () => applyTheme(b.dataset.theme));
  });

  renderIdentity();
  const input = document.getElementById('actor-input');
  // `input` as well as `change`, so the value is held from the first keystroke
  // rather than only when focus leaves. Someone who types an identity and goes
  // straight to a screen that needs one should not find it unset.
  for (const event of ['input', 'change', 'blur']) {
    input.addEventListener(event, () => identity.set(input.value.trim()));
  }
  document.getElementById('actor-clear').addEventListener('click', () => {
    identity.set('');
    announce('Identity cleared.');
  });

  api.readyz().then((health) => {
    document.getElementById('mast-version').textContent = `ValKit ${health.version}`;
    if (health.status !== 'ok') {
      const line = document.getElementById('mast-avail');
      line.hidden = false;
      line.textContent = COPY.INT_UNAVAILABLE(
        health.detail?.audit_chain?.reason || '',
        health.detail?.evidence_vault?.reason || '',
      );
    }
  }).catch(() => { /* the integrity bar reports what matters */ });

  window.addEventListener('hashchange', route);
  route();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
else boot();
