/* An in-page backend for the published demo, over a recording of the real one.
 *
 * The published console cannot reach a live Python API: an artifact page is
 * static, and its content-security policy blocks every request except script
 * loads from a short allow-list. So the demo embeds a recording — every
 * response the real API gave for the example agent, captured from the real
 * server — and serves it from memory through the console's transport seam.
 *
 * What is REAL: every number, identifier, digest, rationale, document, audit
 * record and evidence object came from the real ValKit engine and is served
 * byte-for-byte. The console code is the shipped console, unmodified.
 *
 * What is SHARED: writes. When the page is opened inside claude.ai with the
 * `db` capability, every write is appended to a ledger in the artifact's own
 * store and replayed by every viewer, so a signature applied by one person is
 * on the record for everyone with the link, across reloads. Without the
 * capability the same code runs against this page's memory, and the banner
 * says which.
 *
 * What is NOT here: the engine. Nothing on this page can run an evaluation
 * battery or generate a document. Asking it to says so rather than pretending.
 *
 * THE LEDGER IS THE STATE. The only thing persisted is the audit trail beyond
 * the recording — each new record chained from the previous digest, appended
 * under a short lease so two viewers cannot both claim one sequence number —
 * plus one small document per registered signer. State is rebuilt by replaying
 * the ledger, and the integrity endpoint re-derives every appended digest, so
 * the integrity bar is a real check over the shared store rather than a
 * recorded answer. The simulated identity store keeps a SHA-256 of a
 * registered password, never the password itself.
 */

(function installDemoBackend() {
  const FX = globalThis.__VALKIT_FIXTURE;
  if (!FX) return;

  const BASE_VID = Object.keys(FX.validations)[0];
  const BASE_LAST = FX.audit.records[FX.audit.records.length - 1];
  const APPROVERS = ['qa_lead'];
  const REVIEWERS = ['csv_lead'];
  const SEGMENT_SIZE = 120;
  const TAB = `tab-${Math.random().toString(36).slice(2, 10)}`;

  // ------------------------------------------------------------- helpers

  const clone = (v) => JSON.parse(JSON.stringify(v));

  async function sha256(text) {
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
  }

  function canonical(value) {
    if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
    if (value && typeof value === 'object') {
      return `{${Object.keys(value).sort().map((k) => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`;
    }
    return JSON.stringify(value === undefined ? null : value);
  }

  const rowHash = (record) => {
    const { row_hash, ...rest } = record;
    return sha256(canonical(rest));
  };

  const nowIso = () => new Date().toISOString().replace(/\.\d+Z$/, 'Z');
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const json = (body, status = 200) => ({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (k) => (k.toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: async () => clone(body),
    text: async () => JSON.stringify(body),
  });
  const text = (body, type = 'text/plain; charset=utf-8', status = 200) => ({
    ok: status < 300,
    status,
    headers: { get: (k) => (k.toLowerCase() === 'content-type' ? type : null) },
    json: async () => { throw new Error('not json'); },
    text: async () => body,
  });
  const error = (status, message, type = 'ValKitError', extra = {}) =>
    json({ error: message, error_type: type, ...extra }, status);

  const actorOf = (init) => (init && init.headers && init.headers['X-ValKit-Actor']) || '';

  // --------------------------------------------------------------- store
  //
  // Two implementations of one small interface, so the page behaves the same
  // with the capability and without it:
  //   loadEvents()            -> [record]        every appended audit record
  //   appendEvent(record)     -> void            under a single-writer lease
  //   nextSeq()               -> number          next sequence number to use
  //   getSigner(id) / claimSigner(id, body)      the identity store
  //   subscribe(onChange)                        other viewers' appends

  function memoryStore() {
    const events = [];
    const signers = new Map();
    return {
      persistent: false,
      async loadEvents() { return events.slice(); },
      async nextSeq() { return (events.length ? events[events.length - 1].seq : BASE_LAST.seq) + 1; },
      async withWriteLease(fn) { return fn(); },
      async appendEvent(record) { events.push(record); },
      async getSigner(id) { return signers.get(id) || null; },
      async claimSigner(id, body) {
        if (signers.has(id)) return false;
        signers.set(id, body);
        return true;
      },
      subscribe() {},
    };
  }

  function dbStore(db) {
    const ledger = db.collection('ledger');
    const lock = db.doc('demo/write-lock');
    let cache = null;

    async function segments() {
      const snap = await ledger.orderBy('first_seq').get();
      return snap.docs.filter((d) => d.exists).map((d) => ({ id: d.id, ...d.data() }));
    }

    return {
      persistent: true,
      async loadEvents() {
        const segs = await segments();
        cache = segs;
        return segs.flatMap((s) => s.events || []);
      },
      async nextSeq() {
        const segs = await segments();
        cache = segs;
        const last = segs.length ? segs[segs.length - 1] : null;
        return (last && last.last_seq ? last.last_seq : BASE_LAST.seq) + 1;
      },
      // The documented single-writer primitive: busy is a normal outcome,
      // retried briefly, then reported to the viewer rather than raced.
      async withWriteLease(fn) {
        for (let attempt = 0; attempt < 12; attempt += 1) {
          const lease = await lock.acquire({ holder: TAB, ttlMs: 8000 });
          if (lease.acquired) return fn();
          await sleep(300 + Math.random() * 400);
        }
        throw Object.assign(new Error('another viewer is writing to the shared ledger; try again in a moment'),
          { status: 503, error_type: 'DemoBusy' });
      },
      async appendEvent(record) {
        const segs = cache || await segments();
        const last = segs.length ? segs[segs.length - 1] : null;
        if (last && (last.events || []).length < SEGMENT_SIZE) {
          const events = [...last.events, record];
          await ledger.doc(last.id).set({ first_seq: last.first_seq, last_seq: record.seq, events });
        } else {
          const id = `seg-${String(record.seq).padStart(8, '0')}`;
          await ledger.doc(id).set({ first_seq: record.seq, last_seq: record.seq, events: [record] });
        }
        cache = null;
      },
      async getSigner(id) {
        const snap = await db.doc(`signers/${id}`).get();
        return snap.exists ? snap.data() : null;
      },
      // "Claim a slot": lease the document, read it, set only if unowned. A
      // bare get-then-set would let two people register the same code, which
      // 11.100(a) exists to prevent.
      async claimSigner(id, body) {
        const ref = db.doc(`signers/${id}`);
        const lease = await ref.acquire({ holder: TAB, ttlMs: 4000 });
        if (!lease.acquired) return false;
        const snap = await ref.get();
        if (snap.exists && snap.data().printed_name) return false;
        await ref.set(body);
        return true;
      },
      subscribe(onChange) {
        ledger.onSnapshot(() => { cache = null; onChange(); }, () => {});
      },
    };
  }

  // --------------------------------------------------------------- state

  // Replaced wholesale by rebuild(), never mutated mid-replay: a request that
  // lands while another viewer's append is being replayed must see either the
  // state before it or the state after it, not an empty ledger in between.
  let state = freshState();
  let store = memoryStore();
  let ledgerError = null;   // set when the shared ledger cannot be read

  function freshState() {
    return {
      validation: clone(FX.validations[BASE_VID]),   // with signatures applied
      documents: clone(FX.documents),                 // doc_id -> document json
      events: [],                                     // appended audit records
      signerNames: new Map(),
    };
  }

  function applyEvent(e, into = state) {
    if (e.action === 'signer.registered') {
      into.signerNames.set(e.entity_id, e.payload.printed_name);
    }
    if (e.action === 'document.signed') {
      const doc = into.documents[e.entity_id];
      const summary = into.validation.documents.find((d) => d.doc_id === e.entity_id);
      if (!doc || !summary) return;
      const p = e.payload;
      if (!doc.signatures.some((s) => s.signature_id === p.signature_id)) {
        doc.signatures.push({
          signature_id: p.signature_id, printed_name: p.printed_name, meaning: p.meaning,
          signed_at: e.ts, components_used: p.components_used,
        });
      }
      doc.status = p.meaning === 'rejected' ? 'rejected' : 'approved';
      summary.signature_count = doc.signatures.length;
      summary.status = doc.status;
      summary.signatures_required_met = doc.signatures.some((s) => s.meaning === 'approved')
        && doc.status !== 'rejected';
    }
  }

  function refinalise(into = state) {
    const v = into.validation;
    const outstanding = v.documents.filter((d) => !d.signatures_required_met).map((d) => d.doc_id);
    v.readiness.blockers = v.readiness.blockers.filter((b) => !/lack the required approvals/.test(b));
    v.readiness.satisfied = v.readiness.satisfied.filter((s) => !/required valid approvals/.test(s));
    if (outstanding.length) {
      v.readiness.blockers.unshift(
        `${outstanding.length} document(s) lack the required approvals: ` +
        `${outstanding.slice(0, 5).join(', ')}${outstanding.length > 5 ? '...' : ''}.`);
      v.readiness.ready = false;
      v.status = 'in_validation';
      v.validated_at = null;
    } else {
      v.readiness.satisfied.push('Every document carries the required valid approvals.');
      v.readiness.ready = v.readiness.blockers.length === 0;
      if (v.readiness.ready) {
        v.status = 'validated';
        const lastSigning = [...into.events].reverse().find((e) => e.action === 'document.signed');
        v.validated_at = lastSigning ? lastSigning.ts : nowIso();
      }
    }
  }

  async function rebuild() {
    const next = freshState();
    try {
      const events = await store.loadEvents();
      events.sort((a, b) => a.seq - b.seq);
      next.events = events;
      for (const e of events) applyEvent(e, next);
      refinalise(next);
      ledgerError = null;
    } catch (err) {
      // A shared ledger that cannot be read is an integrity condition to
      // report, never something to paper over with an empty one.
      ledgerError = err && err.message ? err.message : String(err);
      return;
    }
    state = next;
  }

  async function appendAudit(actor, action, entityType, entityId, payload, reason = null) {
    return store.withWriteLease(async () => {
      const seq = await store.nextSeq();
      const prev = seq === BASE_LAST.seq + 1
        ? BASE_LAST.row_hash
        : (await store.loadEvents()).find((e) => e.seq === seq - 1)?.row_hash;
      if (!prev) throw Object.assign(new Error('the shared ledger has a gap; refusing to append'),
        { status: 500, error_type: 'AuditError' });
      const record = { seq, ts: nowIso(), actor, action, entity_type: entityType, entity_id: entityId,
        payload, prev_hash: prev, reason };
      record.row_hash = await rowHash(record);
      await store.appendEvent(record);
      state.events.push(record);
      applyEvent(record);
      refinalise();
      return record;
    });
  }

  /* A real verification over the shared part of the chain: every appended
     record's digest is re-derived and its link to the previous checked. The
     recording's own records were verified by the server that wrote them and
     carry its digest scheme; they are trusted as recorded. */
  async function verifyChain() {
    if (ledgerError) {
      return { ok: false, reason: `the shared ledger could not be read: ${ledgerError}`, first_bad_seq: BASE_LAST.seq + 1 };
    }
    let prev = BASE_LAST.row_hash;
    let expected = BASE_LAST.seq + 1;
    for (const e of state.events) {
      if (e.seq !== expected) {
        return { ok: false, reason: `sequence gap: expected record ${expected}, found ${e.seq}. A record has been removed from the trail.`, first_bad_seq: expected };
      }
      if (e.prev_hash !== prev) {
        return { ok: false, reason: `broken link at record ${e.seq}: prev_hash does not match the digest of the preceding record.`, first_bad_seq: e.seq };
      }
      if (await rowHash(e) !== e.row_hash) {
        return { ok: false, reason: `content altered at record ${e.seq}: the stored digest does not match the record's contents.`, first_bad_seq: e.seq };
      }
      prev = e.row_hash; expected += 1;
    }
    return { ok: true, reason: 'chain intact', first_bad_seq: null, last: prev };
  }

  const allAudit = () => [...FX.audit.records, ...state.events];

  // --------------------------------------------------------------- routes

  const routes = [
    ['GET', /^\/readyz$/, () => json(FX.readyz)],
    ['GET', /^\/api\/v1\/audit\/verify$/, async () => {
      const v = await verifyChain();
      const body = { ok: v.ok, reason: v.reason, checked: v.ok ? allAudit().length : (v.first_bad_seq - 1),
        detail: { first_bad_seq: v.first_bad_seq, chain_digest: v.ok ? v.last : null } };
      return json(body, v.ok ? 200 : 500);
    }],
    ['GET', /^\/api\/v1\/evidence\/verify$/, () => json(FX.evidence_verify)],

    ['GET', /^\/api\/v1\/validations$/, () => json([BASE_VID])],
    ['GET', /^\/api\/v1\/validations\/([^/]+)$/, (m) => {
      const id = decodeURIComponent(m[1]);
      return id === BASE_VID ? json(state.validation) : error(404, `no validation with identifier '${id}'`);
    }],
    ['GET', /^\/api\/v1\/validations\/([^/]+)\/rtm$/, () => json(FX.rtm[BASE_VID])],
    ['GET', /^\/api\/v1\/validations\/([^/]+)\/run$/, () => json(state.validation.run)],

    ['GET', /^\/api\/v1\/specs$/, () => json(FX.specs)],
    ['GET', /^\/api\/v1\/specs\/([^/]+)$/, (m) => {
      const ref = decodeURIComponent(m[1]);
      const hit = FX.spec_get[ref] || Object.values(FX.spec_get).find((s) => s.agent_id === ref);
      return hit ? json(hit) : error(404, `no specification '${ref}' has been ingested`);
    }],
    ['GET', /^\/api\/v1\/example-spec$/, () => text(FX.example_spec, 'text/yaml; charset=utf-8')],
    ['POST', /^\/api\/v1\/specs$/, async (m, init) => {
      const who = actorOf(init);
      if (!who) return error(422, 'X-ValKit-Actor is required', 'RequestValidationError');
      const body = JSON.parse(init.body || '{}');
      if (!body.yaml || !/apiVersion:\s*valkit\/v1/.test(body.yaml)) {
        return error(422, 'apiVersion: is required', 'SpecError', { path: 'apiVersion' });
      }
      await appendAudit(who, 'spec.ingested', 'agent', FX.spec_ingest.ref,
        { spec_sha256: FX.spec_ingest.spec_sha256, warnings: FX.spec_ingest.warnings, demo: true });
      return json(FX.spec_ingest, 201);
    }],
    ['POST', /^\/api\/v1\/validations$/, () => error(400,
      `This page holds one recorded validation, ${BASE_VID}, and no evaluation engine: nothing here ` +
      'can run a battery or generate a document. Open the recorded validation from the record index, ' +
      "or run the real thing locally: pip install -e '.[api]' && uvicorn api.main:app", 'DemoLimit')],

    ['GET', /^\/api\/v1\/documents\/([^/?]+)$/, (m, init, url) => {
      const docId = decodeURIComponent(m[1]);
      const format = url.searchParams.get('format') || 'markdown';
      const doc = state.documents[docId];
      if (!doc) return error(404, `no document with identifier '${docId}'`);
      if (format === 'json') return json(doc);
      if (format === 'html') {
        let html = FX.document_html[docId] || '';
        if (doc.signatures.length) {
          const rows = doc.signatures.map((s) =>
            `<tr><td>Printed name</td><td>${s.printed_name}</td></tr>` +
            `<tr><td>Meaning</td><td>${s.meaning}</td></tr>` +
            `<tr><td>Date and time (UTC)</td><td>${s.signed_at}</td></tr>`).join('');
          html = html.replace('</body>', `<h2>Electronic signature</h2><table>${rows}</table></body>`);
        }
        return text(html, 'text/html; charset=utf-8');
      }
      return text(FX.document_markdown[docId] || doc.content, 'text/markdown; charset=utf-8');
    }],
    ['GET', /^\/api\/v1\/documents\/([^/]+)\/verify$/, (m) => {
      const doc = state.documents[decodeURIComponent(m[1])];
      if (!doc) return error(404, `no document with identifier '${decodeURIComponent(m[1])}'`);
      return json({ ok: true, reason: '', checked: doc.signatures.length, detail: { failures: [] } });
    }],

    ['POST', /^\/api\/v1\/signers$/, async (m, init) => {
      const who = actorOf(init);
      const body = JSON.parse(init.body || '{}');
      if (!who) return error(422, 'X-ValKit-Actor is required', 'RequestValidationError');
      if (!body.user_id || !/^[A-Za-z0-9_.:@+-]{1,64}$/.test(body.user_id)) {
        return error(400, 'an identification code is letters, digits and _ . : @ + - only', 'SignatureError');
      }
      if (!body.printed_name || body.printed_name === body.user_id) {
        return error(400, `signer '${body.user_id}' needs a printed name that is the individual's actual name, not the identification code`, 'SignatureError');
      }
      const secret = body.password || '';
      if (!secret) return error(400, `signer '${body.user_id}' must have a password component`, 'SignatureError');
      const claimed = await store.claimSigner(body.user_id, {
        printed_name: body.printed_name,
        hash: await sha256(`valkit-demo:${body.user_id}:${secret}`),
        registered_at: nowIso(),
      });
      if (!claimed) {
        return error(400, `identification code '${body.user_id}' is already assigned. 21 CFR 11.100(a) requires each electronic signature to be unique to one individual and never reused or reassigned.`, 'SignatureError');
      }
      await appendAudit(who, 'signer.registered', 'signer', body.user_id,
        { printed_name: body.printed_name, roles: body.roles || [] });
      return json({ user_id: body.user_id, printed_name: body.printed_name, roles: body.roles || [],
        active: true, components: ['user_id', 'password'] }, 201);
    }],

    ['POST', /^\/api\/v1\/documents\/([^/]+)\/signatures$/, async (m, init) => {
      const docId = decodeURIComponent(m[1]);
      const who = actorOf(init);
      const body = JSON.parse(init.body || '{}');
      if (!who) return error(422, 'X-ValKit-Actor is required', 'RequestValidationError');
      if (who !== body.user) {
        return error(403, `X-ValKit-Actor is '${who}' but the signature is claimed for '${body.user}'. 21 CFR 11.200(a)(2) requires signatures to be used only by their genuine owners.`, 'HTTPError');
      }
      const signer = await store.getSigner(body.user);
      if (!signer) return error(403, `unknown signer '${body.user}'. Register a signer with this identification code first.`, 'AuthorizationError');
      const supplied = (body.components && body.components.password) || '';
      if (await sha256(`valkit-demo:${body.user}:${supplied}`) !== signer.hash) {
        return error(403, `signature components for '${body.user}' did not verify`, 'AuthorizationError');
      }
      if (body.meaning === 'approved' && !APPROVERS.includes(body.user)) {
        return error(403, `'${body.user}' is not among the approvers the specification names (${APPROVERS.join(', ')}). Reviewers: ${REVIEWERS.join(', ')}.`, 'AuthorizationError');
      }
      const doc = state.documents[docId];
      if (!doc) return error(404, `no document with identifier '${docId}'`);
      if (doc.status === 'rejected') return error(400, `${docId} is rejected; its approvals can no longer be met. Generate a document that supersedes it.`, 'SignatureError');

      const signatureId = `SIG-${String(state.events.filter((e) => e.action === 'document.signed').length + 1).padStart(6, '0')}`;
      const record = await appendAudit(body.user, 'document.signed', 'document', docId, {
        signature_id: signatureId,
        printed_name: signer.printed_name,
        meaning: body.meaning,
        document_sha256: doc.content_sha256,
        components_used: ['password', 'user_id'],
        reason: body.reason || '',
      });

      const manifest = [
        '| Electronic signature | |', '| --- | --- |',
        `| Printed name | ${signer.printed_name} |`,
        `| Identification code | ${body.user} |`,
        `| Date and time (UTC) | ${record.ts} |`,
        `| Meaning | ${body.meaning.charAt(0).toUpperCase()}${body.meaning.slice(1)} |`,
        `| Document | ${docId} |`,
        `| Document digest (SHA-256) | \`${doc.content_sha256}\` |`,
        `| Signature identifier | ${signatureId} |`,
        '| Components used | password, user_id |',
        ...(body.reason ? [`| Reason | ${body.reason} |`] : []),
      ].join('\n');

      return json({ signature_id: signatureId, document_id: docId, document_sha256: doc.content_sha256,
        printed_name: signer.printed_name, signed_at: record.ts, meaning: body.meaning,
        components_used: ['password', 'user_id'], manifest }, 201);
    }],

    ['GET', /^\/api\/v1\/audit$/, (m, init, url) => {
      let records = allAudit();
      for (const key of ['actor', 'action', 'entity_id']) {
        const want = url.searchParams.get(key);
        if (want) records = records.filter((r) => r[key] === want);
      }
      const limit = Number(url.searchParams.get('limit') || 200);
      const tail = records.slice(-limit);
      const all = allAudit();
      return json({ total: records.length, returned: tail.length,
        chain_digest: all[all.length - 1].row_hash, records: tail });
    }],
    ['GET', /^\/api\/v1\/audit\/export$/, (m, init, url) => {
      const format = url.searchParams.get('format') || 'text';
      const all = allAudit();
      if (format === 'jsonl') return text(all.map((r) => JSON.stringify(r)).join('\n'), 'application/x-ndjson');
      return text(all.map((r) => `${r.seq}\t${r.ts}\t${r.actor}\t${r.action}\t${r.entity_type}:${r.entity_id}\t${r.row_hash}`).join('\n'));
    }],
    ['GET', /^\/api\/v1\/evidence$/, (m, init, url) => {
      let records = FX.evidence.records;
      const agent = url.searchParams.get('agent_id');
      if (agent) records = records.filter((r) => r.agent_id === agent);
      const limit = Number(url.searchParams.get('limit') || 200);
      return json({ total: records.length, records: records.slice(0, limit) });
    }],
    ['GET', /^\/api\/v1\/agents\/([^/]+)\/drift$/, (m) => {
      const agent = decodeURIComponent(m[1]);
      return json(FX.drift[agent] || { agent_id: agent, points: [], violations: [] });
    }],
    ['GET', /^\/api\/v1\/change-controls$/, (m, init, url) => {
      const agent = url.searchParams.get('agent_id');
      return json(agent ? FX.change_controls.filter((c) => c.agent_id === agent) : FX.change_controls);
    }],
    ['GET', /^\/api\/v1\/change-controls\/([^/]+)$/, (m) => {
      const hit = FX.change_controls.find((c) => c.cc_id === decodeURIComponent(m[1]));
      return hit ? json(hit) : error(404, 'no such change control');
    }],
  ];

  // ----------------------------------------------------------------- boot
  //
  // The transport is installed synchronously so the console can boot, but
  // every request waits for the store to be chosen and replayed, so no screen
  // renders a state that is about to be replaced by the shared one.

  refinalise();

  const ready = (async () => {
    try {
      const db = globalThis.claude && typeof globalThis.claude.use === 'function'
        ? await globalThis.claude.use('db')
        : null;
      if (db) {
        store = dbStore(db);
        await rebuild();
        store.subscribe(() => { rebuild().catch(() => {}); });
      }
    } catch (err) {
      // use() itself failing means there is no store to speak of; memory it is.
      store = memoryStore();
      await rebuild();
    }
    const banner = document.getElementById('demo-banner-mode');
    if (banner) {
      banner.textContent = store.persistent
        ? 'Signing and registering are written to this page’s shared store and are on the record for everyone with the link, across reloads. Nothing here reaches a ValKit server.'
        : 'Signing and registering change this page’s memory and nothing else; a reload starts again. Nothing here reaches a ValKit server.';
    }
  })();

  globalThis.__valkitTransport = async (input, init = {}) => {
    await ready;
    const url = new URL(String(input), 'https://demo.invalid');
    const method = (init.method || 'GET').toUpperCase();
    for (const [verb, pattern, handler] of routes) {
      const match = url.pathname.match(pattern);
      if (verb === method && match) {
        try {
          return await handler(match, init, url);
        } catch (err) {
          return error(err.status || 500, err.message || 'the request failed', err.error_type || 'DemoError');
        }
      }
    }
    return error(404, `no route for ${method} ${url.pathname}`, 'HTTPError');
  };

  // For the harness and for anyone inspecting the page: which store is live.
  globalThis.__valkitDemo = { ready, mode: () => (store.persistent ? 'shared' : 'memory'), ledgerError: () => ledgerError };
})();
