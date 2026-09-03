/* An in-page recording of the ValKit backend, for the published demo.
 *
 * The published console cannot reach a live Python API: an artifact page is
 * static, and its content-security policy blocks every request except script
 * loads from a short allow-list. So the demo embeds a recording — every
 * response the real API gave for the example agent, captured from the real
 * server — and serves it from memory through the console's transport seam.
 *
 * What is REAL here: every number, identifier, digest, rationale, document,
 * audit record and evidence object came from the real ValKit engine and is
 * served byte-for-byte. The console code is the shipped console, unmodified.
 *
 * What is SIMULATED here: writes. Registering a signer, signing, ingesting and
 * running all mutate this page's memory and nothing else. New audit records
 * are appended with a self-consistent hash chain computed in the browser; they
 * are not the server's records. Nothing done in the demo is recorded anywhere,
 * and the page says so on its face.
 *
 * The credential rule still holds in spirit: the simulated identity store
 * keeps a SHA-256 of the registered password so that a wrong password is
 * refused, as the real one is, and never keeps the password itself.
 */

(function installDemoBackend() {
  const FX = globalThis.__VALKIT_FIXTURE;
  if (!FX) return;

  const state = {
    validations: JSON.parse(JSON.stringify(FX.validations)),
    documents: JSON.parse(JSON.stringify(FX.documents)),
    audit: JSON.parse(JSON.stringify(FX.audit.records)),
    changeControls: JSON.parse(JSON.stringify(FX.change_controls)),
    signers: new Map(),
    sigCounter: 0,
    valCounter: Object.keys(FX.validations).length,
    ccCounter: FX.change_controls.length,
    clockSeconds: 0,
  };

  // ---------------------------------------------------------------- helpers

  const now = () => {
    state.clockSeconds += 1;
    const base = new Date('2026-03-02T10:00:00Z').getTime() + state.clockSeconds * 1000;
    return new Date(base).toISOString().replace(/\.\d+Z$/, 'Z');
  };

  async function sha256(text) {
    const bytes = new TextEncoder().encode(text);
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
  }

  function canonical(value) {
    if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
    if (value && typeof value === 'object') {
      return `{${Object.keys(value).sort().map((k) => `${JSON.stringify(k)}:${canonical(value[k])}`).join(',')}}`;
    }
    return JSON.stringify(value);
  }

  async function appendAudit(actor, action, entityType, entityId, payload, reason = null) {
    const last = state.audit[state.audit.length - 1];
    const seq = last.seq + 1;
    const prev = last.row_hash;
    const record = { seq, ts: now(), actor, action, entity_type: entityType, entity_id: entityId,
      payload, prev_hash: prev, reason };
    record.row_hash = await sha256(canonical({ ...record, row_hash: undefined }) + prev);
    state.audit.push(record);
    return record;
  }

  const json = (body, status = 200) => ({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (k) => (k.toLowerCase() === 'content-type' ? 'application/json' : null) },
    json: async () => JSON.parse(JSON.stringify(body)),
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

  function findDocument(docId) {
    for (const [vid, docs] of Object.entries(state.documents)) {
      if (docs[docId]) return { vid, doc: docs[docId] };
    }
    // The recorded shape keys documents by id at the top level.
    if (state.documents[docId]) return { vid: null, doc: state.documents[docId] };
    return null;
  }

  function summaryFor(vid) {
    return state.validations[vid];
  }

  function refinalise(vid) {
    // Validated status is the output of the gate. Re-run it: with every document
    // approved, the approvals blocker goes and, in this record, nothing else
    // stands in the way.
    const v = state.validations[vid];
    const outstanding = v.documents.filter((d) => !d.signatures_required_met).length;
    v.readiness.blockers = v.readiness.blockers.filter((b) => !/lack the required approvals/.test(b));
    if (outstanding) {
      const ids = v.documents.filter((d) => !d.signatures_required_met).map((d) => d.doc_id);
      v.readiness.blockers.unshift(
        `${outstanding} document(s) lack the required approvals: ${ids.slice(0, 5).join(', ')}${ids.length > 5 ? '...' : ''}.`);
      v.readiness.ready = false;
      v.status = 'in_validation';
      v.validated_at = null;
    } else {
      v.readiness.ready = v.readiness.blockers.length === 0;
      if (v.readiness.ready) {
        if (!v.readiness.satisfied.some((s) => /required valid approvals/.test(s))) {
          v.readiness.satisfied.push('Every document carries the required valid approvals.');
        }
        v.status = 'validated';
        v.validated_at = v.validated_at || now();
      }
    }
  }

  // ----------------------------------------------------------------- routes

  const routes = [
    ['GET', /^\/readyz$/, () => json(FX.readyz)],
    ['GET', /^\/api\/v1\/audit\/verify$/, () =>
      json({ ...FX.audit_verify, checked: state.audit.length,
        detail: { ...FX.audit_verify.detail, chain_digest: state.audit[state.audit.length - 1].row_hash } })],
    ['GET', /^\/api\/v1\/evidence\/verify$/, () => json(FX.evidence_verify)],

    ['GET', /^\/api\/v1\/validations$/, () => json(Object.keys(state.validations).sort())],
    ['GET', /^\/api\/v1\/validations\/([^/]+)$/, (m) => {
      const v = summaryFor(decodeURIComponent(m[1]));
      return v ? json(v) : error(404, `no validation with identifier '${decodeURIComponent(m[1])}'`);
    }],
    ['GET', /^\/api\/v1\/validations\/([^/]+)\/rtm$/, (m) => {
      const rtm = FX.rtm[decodeURIComponent(m[1])] || FX.rtm[Object.keys(FX.rtm)[0]];
      return rtm ? json(rtm) : error(404, 'no traceability matrix');
    }],
    ['GET', /^\/api\/v1\/validations\/([^/]+)\/run$/, (m) => {
      const v = summaryFor(decodeURIComponent(m[1]));
      return v && v.run ? json(v.run) : error(404, 'this validation has no evaluation run');
    }],

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
        { spec_sha256: FX.spec_ingest.spec_sha256, warnings: FX.spec_ingest.warnings });
      return json(FX.spec_ingest, 201);
    }],
    ['POST', /^\/api\/v1\/validations$/, async (m, init) => {
      const who = actorOf(init);
      if (!who) return error(422, 'X-ValKit-Actor is required', 'RequestValidationError');
      state.valCounter += 1;
      const vid = `VAL-${String(state.valCounter).padStart(4, '0')}`;
      const source = FX.validations[Object.keys(FX.validations)[0]];
      const copy = JSON.parse(JSON.stringify(source));
      copy.validation_id = vid;
      copy.created_at = now();
      copy.validated_at = null;
      copy.status = 'in_validation';
      for (const d of copy.documents) { d.signature_count = 0; d.signatures_required_met = false; d.status = 'draft'; }
      state.validations[vid] = copy;
      state.documents[vid] = JSON.parse(JSON.stringify(FX.documents));
      for (const d of Object.values(state.documents[vid])) { d.signatures = []; d.status = 'draft'; }
      refinalise(vid);
      await appendAudit(who, 'validation.executed', 'validation', vid,
        { agent: FX.spec_ingest.ref, run_id: copy.run.run_id, ready: copy.readiness.ready });
      return json(copy, 201);
    }],

    ['GET', /^\/api\/v1\/documents\/([^/?]+)$/, (m, init, url) => {
      const docId = decodeURIComponent(m[1]);
      const format = url.searchParams.get('format') || 'markdown';
      const found = findDocument(docId);
      if (!found) return error(404, `no document with identifier '${docId}'`);
      if (format === 'json') return json(found.doc);
      if (format === 'html') {
        let html = FX.document_html[docId] || '';
        if (found.doc.signatures.length) {
          const rows = found.doc.signatures.map((s) =>
            `<tr><td>Printed name</td><td>${s.printed_name}</td></tr><tr><td>Meaning</td><td>${s.meaning}</td></tr><tr><td>Date and time (UTC)</td><td>${s.signed_at}</td></tr>`).join('');
          html = html.replace('</body>', `<h2>Electronic signature</h2><table>${rows}</table></body>`);
        }
        return text(html, 'text/html; charset=utf-8');
      }
      return text(FX.document_markdown[docId] || found.doc.content, 'text/markdown; charset=utf-8');
    }],
    ['GET', /^\/api\/v1\/documents\/([^/]+)\/verify$/, (m) => {
      const docId = decodeURIComponent(m[1]);
      const found = findDocument(docId);
      if (!found) return error(404, `no document with identifier '${docId}'`);
      return json({ ok: true, reason: '', checked: found.doc.signatures.length, detail: { failures: [] } });
    }],

    ['POST', /^\/api\/v1\/signers$/, async (m, init) => {
      const who = actorOf(init);
      const body = JSON.parse(init.body || '{}');
      if (!who) return error(422, 'X-ValKit-Actor is required', 'RequestValidationError');
      if (state.signers.has(body.user_id)) {
        return error(400, `identification code '${body.user_id}' is already assigned. 21 CFR 11.100(a) requires each electronic signature to be unique to one individual and never reused or reassigned.`, 'SignatureError');
      }
      if (!body.printed_name || body.printed_name === body.user_id) {
        return error(400, `signer '${body.user_id}' needs a printed name that is the individual's actual name, not the identification code`, 'SignatureError');
      }
      const secret = body.password || '';
      if (!secret) return error(400, `signer '${body.user_id}' must have a password component`, 'SignatureError');
      state.signers.set(body.user_id, { printed_name: body.printed_name, hash: await sha256(`valkit-demo:${body.user_id}:${secret}`) });
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
      const signer = state.signers.get(body.user);
      if (!signer) return error(403, `unknown signer '${body.user}'. Register a signer with this identification code first.`, 'AuthorizationError');
      const supplied = (body.components && body.components.password) || '';
      if (await sha256(`valkit-demo:${body.user}:${supplied}`) !== signer.hash) {
        return error(403, `signature components for '${body.user}' did not verify`, 'AuthorizationError');
      }
      const approvers = ['qa_lead'];
      const reviewers = ['csv_lead'];
      if (body.meaning === 'approved' && !approvers.includes(body.user)) {
        return error(403, `'${body.user}' is not among the approvers the specification names (${approvers.join(', ')}). Reviewers: ${reviewers.join(', ')}.`, 'AuthorizationError');
      }
      const found = findDocument(docId);
      if (!found) return error(404, `no document with identifier '${docId}'`);

      state.sigCounter += 1;
      const signature = {
        signature_id: `SIG-${String(state.sigCounter).padStart(6, '0')}`,
        document_id: docId,
        document_sha256: found.doc.content_sha256,
        printed_name: signer.printed_name,
        signed_at: now(),
        meaning: body.meaning,
        components_used: ['password', 'user_id'],
      };
      signature.manifest = [
        '| Electronic signature | |', '| --- | --- |',
        `| Printed name | ${signature.printed_name} |`,
        `| Identification code | ${body.user} |`,
        `| Date and time (UTC) | ${signature.signed_at} |`,
        `| Meaning | ${body.meaning.charAt(0).toUpperCase()}${body.meaning.slice(1)} |`,
        `| Document | ${docId} |`,
        `| Document digest (SHA-256) | \`${signature.document_sha256}\` |`,
        `| Signature identifier | ${signature.signature_id} |`,
        `| Components used | password, user_id |`,
        ...(body.reason ? [`| Reason | ${body.reason} |`] : []),
      ].join('\n');

      found.doc.signatures.push({ signature_id: signature.signature_id, printed_name: signature.printed_name,
        meaning: body.meaning, signed_at: signature.signed_at, components_used: signature.components_used });
      found.doc.status = body.meaning === 'rejected' ? 'rejected' : 'approved';

      for (const [vid, v] of Object.entries(state.validations)) {
        const docs = state.documents[vid] || state.documents;
        if (!docs[docId]) continue;
        const summary = v.documents.find((d) => d.doc_id === docId);
        if (summary) {
          summary.signature_count = found.doc.signatures.length;
          summary.signatures_required_met = body.meaning === 'approved';
          summary.status = found.doc.status;
        }
        refinalise(vid);
      }
      await appendAudit(body.user, 'document.signed', 'document', docId,
        { signature_id: signature.signature_id, meaning: body.meaning, document_sha256: signature.document_sha256,
          components_used: signature.components_used });
      return json(signature, 201);
    }],

    ['GET', /^\/api\/v1\/audit$/, (m, init, url) => {
      let records = state.audit;
      for (const key of ['actor', 'action', 'entity_id']) {
        const want = url.searchParams.get(key);
        if (want) records = records.filter((r) => r[key] === want);
      }
      const limit = Number(url.searchParams.get('limit') || 200);
      const tail = records.slice(-limit);
      return json({ total: records.length, returned: tail.length,
        chain_digest: state.audit[state.audit.length - 1].row_hash, records: tail });
    }],
    ['GET', /^\/api\/v1\/audit\/export$/, (m, init, url) => {
      const format = url.searchParams.get('format') || 'text';
      if (format === 'jsonl') return text(state.audit.map((r) => JSON.stringify(r)).join('\n'), 'application/x-ndjson');
      return text(state.audit.map((r) => `${r.seq}\t${r.ts}\t${r.actor}\t${r.action}\t${r.entity_type}:${r.entity_id}\t${r.row_hash}`).join('\n'));
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
      return json(agent ? state.changeControls.filter((c) => c.agent_id === agent) : state.changeControls);
    }],
    ['GET', /^\/api\/v1\/change-controls\/([^/]+)$/, (m) => {
      const hit = state.changeControls.find((c) => c.cc_id === decodeURIComponent(m[1]));
      return hit ? json(hit) : error(404, 'no such change control');
    }],
  ];

  globalThis.__valkitTransport = async (input, init = {}) => {
    const url = new URL(String(input), 'https://demo.invalid');
    const method = (init.method || 'GET').toUpperCase();
    for (const [verb, pattern, handler] of routes) {
      const match = url.pathname.match(pattern);
      if (verb === method && match) return handler(match, init, url);
    }
    return error(404, `no route for ${method} ${url.pathname}`, 'HTTPError');
  };
})();
