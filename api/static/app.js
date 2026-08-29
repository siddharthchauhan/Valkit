/* The console.
 *
 * No framework and no build step, deliberately: this is a thin view over the
 * API, and a validation tool whose console needs a toolchain to audit is a
 * validation tool with an extra supplier to assess.
 *
 * Two rules are enforced here as well as on the server, because a leak is a
 * leak wherever it happens:
 *   - the password field is read at submit time and never stored, and
 *   - nothing carrying a credential is ever put in a URL.
 */

'use strict';

const $ = (id) => document.getElementById(id);

const state = {
  specRef: null,
  validationId: null,
  docId: null,
};

function actor() {
  const value = $('actor').value.trim();
  if (!value) throw new Error('Set who you are acting as. It is recorded in the audit trail.');
  return value;
}

async function api(path, options = {}) {
  const headers = { 'Accept': 'application/json' };
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    headers['X-ValKit-Actor'] = actor();
  } else if (options.withActor) {
    headers['X-ValKit-Actor'] = actor();
  }

  const response = await fetch(path, {
    method: options.method || (options.body !== undefined ? 'POST' : 'GET'),
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });

  const type = response.headers.get('content-type') || '';
  const payload = type.includes('application/json') ? await response.json() : await response.text();
  if (!response.ok) {
    const message = (payload && payload.error) || response.statusText;
    const detail = payload && payload.detail;
    throw new Error(typeof detail === 'string' ? `${message}\n${detail}` : message);
  }
  return payload;
}

function show(id) { $(id).classList.remove('hidden'); }

function fail(target, error) {
  $(target).innerHTML = '';
  const box = document.createElement('div');
  box.className = 'error';
  box.textContent = error.message || String(error);
  $(target).appendChild(box);
}

function clear(target) { $(target).innerHTML = ''; }

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function pill(ok, passText, failText) {
  return el('span', `pill ${ok ? 'pass' : 'fail'}`, ok ? passText : failText);
}

function table(headings, rows) {
  const t = el('table');
  const thead = el('thead');
  const tr = el('tr');
  headings.forEach((h) => tr.appendChild(el('th', null, h)));
  thead.appendChild(tr);
  t.appendChild(thead);
  const tbody = el('tbody');
  rows.forEach((cells) => {
    const row = el('tr');
    cells.forEach((cell) => {
      const td = el('td');
      if (cell && cell.nodeType) td.appendChild(cell);
      else if (cell && typeof cell === 'object') {
        td.className = cell.className || '';
        if (cell.node) td.appendChild(cell.node);
        else td.textContent = cell.text;
      } else td.textContent = cell;
      row.appendChild(td);
    });
    tbody.appendChild(row);
  });
  t.appendChild(tbody);
  return t;
}

// -- 1. specification ------------------------------------------------------

$('load-example').addEventListener('click', async () => {
  try {
    const response = await fetch('/api/v1/example-spec');
    $('spec').value = await response.text();
  } catch (error) {
    fail('spec-result', error);
  }
});

$('ingest').addEventListener('click', async () => {
  clear('spec-result');
  try {
    const result = await api('/api/v1/specs', { body: { yaml: $('spec').value, strict: true } });
    state.specRef = result.ref;

    const target = $('spec-result');
    const escalated = result.risk_class !== result.derived_risk_class;
    target.appendChild(
      table(
        ['Agent', 'Version', 'GAMP', 'Risk class', 'Requirements', 'Risks', 'Tests'],
        [[
          result.agent_id,
          result.version,
          `Category ${result.gamp_category}`,
          escalated
            ? `${result.derived_risk_class} → ${result.risk_class} (escalated)`
            : result.risk_class,
          { className: 'num', text: result.requirements },
          { className: 'num', text: result.risks },
          { className: 'num', text: result.tests },
        ]]
      )
    );
    target.appendChild(
      el('p', 'note', `Specification digest ${result.spec_sha256.slice(0, 16)}…`)
    );
    if (result.warnings.length) {
      const list = el('ul', 'reasons conditions');
      result.warnings.forEach((w) => list.appendChild(el('li', null, w)));
      target.appendChild(el('h3', null, 'Warnings'));
      target.appendChild(list);
    }

    const select = $('spec-ref');
    if (![...select.options].some((o) => o.value === result.ref)) {
      select.appendChild(new Option(result.ref, result.ref));
    }
    select.value = result.ref;
    show('run-section');
  } catch (error) {
    fail('spec-result', error);
  }
});

// -- 2 to 5. run, acceptance, readiness, package ---------------------------

$('run').addEventListener('click', async () => {
  clear('run-result');
  $('run').disabled = true;
  $('run').textContent = 'Running…';
  try {
    const result = await api('/api/v1/validations', { body: { spec_ref: $('spec-ref').value } });
    state.validationId = result.validation_id;
    render(result);
  } catch (error) {
    fail('run-result', error);
  } finally {
    $('run').disabled = false;
    $('run').textContent = 'Run validation';
  }
});

function render(validation) {
  renderRun(validation);
  renderReadiness(validation);
  renderDocuments(validation);
  show('acceptance-section');
  show('readiness-section');
  show('documents-section');
  show('sign-section');
}

function renderRun(validation) {
  clear('run-result');
  clear('metrics');
  const run = validation.run;
  if (!run) return;

  $('run-result').appendChild(
    el('p', 'muted mono', `${run.run_id} · ${run.model} · dataset ${run.dataset_sha256.slice(0, 16)}…`)
  );

  $('metrics').appendChild(
    table(
      ['Metric', 'Passed', 'n', 'Observed', 'Lower bound', 'Target', 'Method', 'Result'],
      run.metrics.map((m) => [
        m.critical ? `${m.name} (critical)` : m.name,
        { className: 'num', text: `${m.k}` },
        { className: 'num', text: `${m.n}` },
        { className: 'num', text: m.point_estimate.toFixed(4) },
        { className: 'num', text: m.lower_bound.toFixed(4) },
        { className: 'num', text: m.target.toFixed(2) },
        m.method,
        { node: pill(m.passed, 'met', 'not met') },
      ])
    )
  );

  if (run.calibration) {
    const c = run.calibration;
    const line = el('p', 'note');
    line.appendChild(
      document.createTextNode(
        `Judge calibration: Cohen's κ ${c.cohen_kappa.toFixed(3)} against a required ` +
        `minimum of ${c.min_required.toFixed(2)} over ${c.n} labelled cases — `
      )
    );
    line.appendChild(pill(c.passed, 'met', 'not met'));
    $('metrics').appendChild(line);
  }
}

function renderReadiness(validation) {
  const target = $('readiness');
  target.innerHTML = '';
  const r = validation.readiness;

  const banner = el('div', `status-banner ${r.ready ? 'ready' : 'blocked'}`);
  banner.appendChild(el('strong', null, r.ready ? 'VALIDATED' : validation.status.toUpperCase()));
  banner.appendChild(
    el('div', 'muted', r.ready
      ? 'Every condition for validated status holds.'
      : `${r.blockers.length} condition(s) outstanding.`)
  );
  target.appendChild(banner);

  const section = (title, items, className) => {
    if (!items.length) return;
    target.appendChild(el('h3', null, title));
    const list = el('ul', `reasons ${className}`);
    items.forEach((item) => list.appendChild(el('li', null, item)));
    target.appendChild(list);
  };

  section('Blocking', r.blockers, 'blockers');
  section('Outstanding obligations', r.conditions, 'conditions');
  section('Satisfied', r.satisfied, '');
}

function renderDocuments(validation) {
  const target = $('documents');
  target.innerHTML = '';
  if (!validation.documents.length) {
    target.appendChild(el('p', 'muted', 'No documents were generated.'));
    return;
  }

  target.appendChild(
    table(
      ['Document', 'Type', 'Digest', 'Signatures', 'Approvals', ''],
      validation.documents.map((d) => {
        const open = el('button', 'secondary', 'Open');
        open.addEventListener('click', () => openDocument(d.doc_id));
        return [
          d.title,
          d.doc_type,
          { className: 'mono', text: `${d.content_sha256.slice(0, 12)}…` },
          { className: 'num', text: `${d.signature_count}` },
          { node: pill(d.signatures_required_met, 'complete', 'outstanding') },
          { node: open },
        ];
      })
    )
  );

  const skipped = Object.entries(validation.skipped_documents || {});
  if (skipped.length) {
    target.appendChild(el('h3', null, 'Not generated'));
    const list = el('ul', 'reasons conditions');
    skipped.forEach(([type, reason]) => list.appendChild(el('li', null, `${type}: ${reason}`)));
    target.appendChild(list);
  }
}

async function openDocument(docId) {
  state.docId = docId;
  const target = $('document-view');
  target.innerHTML = '';
  try {
    const response = await fetch(`/api/v1/documents/${encodeURIComponent(docId)}?format=markdown`);
    if (!response.ok) throw new Error(`could not fetch ${docId}`);
    const content = await response.text();
    target.appendChild(el('h3', null, docId));
    target.appendChild(el('pre', 'doc', content));
  } catch (error) {
    fail('document-view', error);
  }
}

// -- 6. signature ----------------------------------------------------------

$('register').addEventListener('click', async () => {
  clear('sign-result');
  try {
    const password = $('password').value;
    if (!password) throw new Error('A password component is required.');
    const result = await api('/api/v1/signers', {
      body: {
        user_id: $('signer').value,
        printed_name: $('signer').value,
        password,
        roles: ['qa'],
      },
    });
    $('sign-result').appendChild(
      el('p', 'muted', `Registered ${result.printed_name} (${result.user_id}).`)
    );
  } catch (error) {
    fail('sign-result', error);
  }
});

$('sign').addEventListener('click', async () => {
  clear('sign-result');
  try {
    if (!state.docId) throw new Error('Open a document first, then sign it.');
    // Read the credential at submit time. It is not held in the page state and
    // it is not put in the URL.
    const password = $('password').value;
    if (!password) throw new Error('A password component is required.');

    const result = await api(`/api/v1/documents/${encodeURIComponent(state.docId)}/signatures`, {
      body: {
        user: $('signer').value,
        meaning: $('meaning').value,
        components: { user_id: $('signer').value, password },
      },
    });
    $('password').value = '';

    const target = $('sign-result');
    target.appendChild(el('h3', null, 'Signature manifest'));
    target.appendChild(el('pre', 'doc', result.manifest));

    const refreshed = await api(`/api/v1/validations/${state.validationId}`);
    render(refreshed);
  } catch (error) {
    fail('sign-result', error);
  }
});

// -- integrity -------------------------------------------------------------

$('verify').addEventListener('click', async () => {
  const target = $('integrity-result');
  target.innerHTML = '';
  try {
    const [chain, evidence] = await Promise.all([
      fetch('/api/v1/audit/verify').then((r) => r.json()),
      fetch('/api/v1/evidence/verify').then((r) => r.json()),
    ]);
    target.appendChild(
      table(
        ['Control', 'Checked', 'Result', 'Detail'],
        [
          [
            'Audit chain (11.10(e))',
            { className: 'num', text: `${chain.checked}` },
            { node: pill(chain.ok, 'intact', 'broken') },
            chain.reason || 'Re-derived from the genesis record.',
          ],
          [
            'Evidence vault (11.10(c))',
            { className: 'num', text: `${evidence.checked}` },
            { node: pill(evidence.ok, 'verified', 'failed') },
            evidence.reason || 'Every object re-hashed from its bytes.',
          ],
        ]
      )
    );
  } catch (error) {
    fail('integrity-result', error);
  }
});

$('show-audit').addEventListener('click', async () => {
  const target = $('integrity-result');
  target.innerHTML = '';
  try {
    const result = await api('/api/v1/audit?limit=50');
    target.appendChild(
      el('p', 'muted mono', `${result.total} events · chain digest ${result.chain_digest.slice(0, 16)}…`)
    );
    target.appendChild(
      table(
        ['#', 'Time', 'Actor', 'Action', 'Entity'],
        result.records.map((r) => [
          { className: 'num', text: `${r.seq}` },
          { className: 'mono', text: r.ts },
          r.actor,
          r.action,
          { className: 'mono', text: `${r.entity_type}:${r.entity_id}` },
        ])
      )
    );
  } catch (error) {
    fail('integrity-result', error);
  }
});
