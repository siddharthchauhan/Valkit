/* The data layer.
 *
 * Two rules are load-bearing.
 *
 * THE BODY IS READ BEFORE `response.ok` IS INSPECTED. ValKit returns a
 * well-formed body on its failures — the dotted path of a bad specification
 * field, the redacted detail of a malformed request, the reason a chain did not
 * verify — and a wrapper that throws on the status code before reading it
 * discards exactly the information the user needs.
 *
 * AN INTEGRITY FAILURE IS NOT AN ERROR TO DISPLAY IN PLACE. It becomes an
 * IntegrityFailure, which the router turns into a full-screen interdiction,
 * because "the evidence cannot be trusted" is not a message to show in a red
 * box beside content that is still on screen.
 *
 * The two verification endpoints are the exception to both: they answer HTTP
 * 500 with a valid result body when they find a problem. That is a result, not
 * a transport failure, and is returned as data.
 */

const BASE = '/api/v1';

const INTEGRITY_TYPES = new Set(['IntegrityError', 'AuditError', 'VaultError']);

export class ApiError extends Error {
  constructor({ status, error, error_type, path, detail }) {
    super(error || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.error = error;
    this.error_type = error_type;
    this.path = path || null;
    this.detail = detail || null;
  }
}

export class IntegrityFailure extends ApiError {
  constructor(payload) {
    super(payload);
    this.name = 'IntegrityFailure';
  }
}

async function request(path, options = {}) {
  const {
    method = 'GET',
    body,
    actor,
    accept = 'application/json',
    tolerate500WithBody = false,
  } = options;

  const headers = { Accept: accept };
  if (method === 'POST') {
    if (!actor) throw new Error('a POST needs an acting identity');
    headers['X-ValKit-Actor'] = actor;
    headers['Content-Type'] = 'application/json';
  }

  let response;
  try {
    response = await fetch(path.startsWith('/') ? path : `${BASE}/${path}`, {
      method,
      headers,
      credentials: 'same-origin',
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (cause) {
    throw new ApiError({
      status: 0,
      error: 'The request did not complete.',
      error_type: 'NetworkError',
    });
  }

  // Read first. The body is the useful part of a ValKit failure.
  const type = response.headers.get('content-type') || '';
  let payload;
  try {
    payload = type.includes('application/json') ? await response.json() : await response.text();
  } catch {
    payload = null;
  }

  if (response.ok) return payload;

  if (tolerate500WithBody && response.status === 500 && payload && typeof payload === 'object'
      && 'ok' in payload) {
    return payload;
  }

  const shape = payload && typeof payload === 'object'
    ? payload
    : { error: `The request returned HTTP ${response.status} and the body could not be read.` };
  const built = {
    status: response.status,
    error: shape.error || `HTTP ${response.status}`,
    error_type: shape.error_type || 'HTTPError',
    path: shape.path,
    detail: shape.detail,
  };
  throw INTEGRITY_TYPES.has(built.error_type)
    ? new IntegrityFailure(built)
    : new ApiError(built);
}

const q = (params) => {
  const search = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v !== undefined && v !== null && v !== '') search.set(k, v);
  }
  const s = search.toString();
  return s ? `?${s}` : '';
};

export const api = {
  readyz: () => request('/readyz'),

  verifyAudit: () => request('audit/verify', { tolerate500WithBody: true }),
  verifyEvidence: () => request('evidence/verify', { tolerate500WithBody: true }),

  listValidations: () => request('validations'),
  validation: (id) => request(`validations/${encodeURIComponent(id)}`),
  rtm: (id) => request(`validations/${encodeURIComponent(id)}/rtm`),
  run: (id) => request(`validations/${encodeURIComponent(id)}/run`),

  listSpecs: () => request('specs'),
  spec: (ref) => request(`specs/${encodeURIComponent(ref)}`),
  exampleSpec: () => request('example-spec', { accept: 'text/yaml' }),
  ingestSpec: (yaml, actor) =>
    request('specs', { method: 'POST', body: { yaml, strict: true }, actor }),
  startValidation: (specRef, actor) =>
    request('validations', { method: 'POST', body: { spec_ref: specRef }, actor }),

  document: (docId, format = 'json') =>
    request(`documents/${encodeURIComponent(docId)}${q({ format })}`, {
      accept: format === 'json' ? 'application/json' : 'text/plain',
    }),
  verifyDocument: (docId) => request(`documents/${encodeURIComponent(docId)}/verify`),

  audit: (params) => request(`audit${q(params)}`),
  auditExportUrl: (format) => `${BASE}/audit/export${q({ format })}`,
  evidence: (params) => request(`evidence${q(params)}`),

  drift: (agentId, window) =>
    request(`agents/${encodeURIComponent(agentId)}/drift${q({ window })}`),
  changeControls: (agentId) => request(`change-controls${q({ agent_id: agentId })}`),
};

/* The only two writes in the console that are not signing. Signing lives
   entirely in sign.js and does not pass through here, so that the credential
   path is a review of one file. */
export const writes = {
  registerSigner: (payload, actor) =>
    request('signers', { method: 'POST', body: payload, actor }),
};

/* Caching. GET /validations/{id} re-derives the whole traceability chain and
   verifies the vault server-side on every request, so it is fetched once per
   entry into a validation and re-fetched after a signature — never polled. */
const cache = new Map();
const TTL_MS = 30_000;

export function cached(key, loader, { ttl = TTL_MS, now = 0 } = {}) {
  const hit = cache.get(key);
  if (hit && now - hit.at < ttl) return hit.value;
  const value = loader();
  cache.set(key, { at: now, value });
  value.catch(() => cache.delete(key));
  return value;
}

export function invalidate(prefix) {
  for (const key of [...cache.keys()]) {
    if (key.startsWith(prefix)) cache.delete(key);
  }
}

export default api;
