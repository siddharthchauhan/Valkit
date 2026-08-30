/* The credential path, in full.
 *
 * NO OTHER FILE IN THIS CONSOLE READS, RECEIVES, HOLDS OR FORWARDS A SIGNATURE
 * COMPONENT. That is a design decision rather than tidiness: credential
 * containment becomes a review of this one file, which is what a supplier
 * assessment will actually ask to do.
 *
 * What a reviewer should check here, and can:
 *
 *   - The value is read once, at submit, into a `const` in one stack frame, and
 *     the field is cleared in the same tick. The frame ends when the batch ends.
 *   - It is never assigned to module state, to an object that outlives the call,
 *     to sessionStorage or localStorage, to a URL, to a header, or to a data
 *     attribute.
 *   - There is no `console.*` call anywhere in this file. Not one.
 *   - The form has no `action` and the input has no `name`, so no form
 *     submission can serialise the value into a query string.
 *   - `autocomplete="off"`, not `current-password`: a Part 11 component that a
 *     browser can store, sync and replay without the individual weakens the
 *     basis of 11.200(a)(1)(i).
 *
 * What is deliberately NOT offered is a runtime assertion sweeping application
 * state for credential-shaped keys. Such a check could never fire, because the
 * value is never placed there, and a control incapable of failing is not
 * evidence of containment.
 */

import { ApiError, IntegrityFailure } from './api.js';

const BASE = '/api/v1';

/** One signing. All components, every time.
 *
 * `session_id` is never sent: SignatureService.open_session is unreachable over
 * HTTP, so every signing here is a signing outside a continuous session and
 * carries all components, which is what 21 CFR 11.200(a)(1)(ii) requires.
 */
async function postSignature(docId, body, actor) {
  let response;
  try {
    response = await fetch(`${BASE}/documents/${encodeURIComponent(docId)}/signatures`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
        'X-ValKit-Actor': actor,
      },
      body: JSON.stringify(body),
    });
  } catch {
    throw new ApiError({
      status: 0,
      error: 'The request did not complete.',
      error_type: 'NetworkError',
    });
  }

  const payload = await response.json().catch(() => null);
  if (response.ok) return payload;

  const shape = payload && typeof payload === 'object' ? payload : {};
  const built = {
    status: response.status,
    error: shape.error || `HTTP ${response.status}`,
    error_type: shape.error_type || 'HTTPError',
    path: shape.path,
    detail: shape.detail,
  };
  if (['IntegrityError', 'AuditError', 'VaultError'].includes(built.error_type)) {
    throw new IntegrityFailure(built);
  }
  throw new ApiError(built);
}

/** Apply one signing act, by one individual, to a chosen set of documents.
 *
 * Sequential and awaited one at a time, so the audit sequence matches the
 * on-screen ledger. The first document is attempted alone: if the caller is not
 * among the approvers the specification names, learning that costs one refusal
 * rather than fifteen.
 *
 * `onResult` is called as each response lands, so a batch that stops halfway
 * leaves an accurate record on screen rather than an optimistic one.
 */
export async function applyBatch({ docIds, meaning, reason, actor, form, onResult }) {
  const field = form.elements.namedItem('sign-pw');

  // Read once, clear in the same tick, before any await.
  const credential = field.value;
  field.value = '';
  field.disabled = true;

  const results = [];
  try {
    for (let index = 0; index < docIds.length; index += 1) {
      const docId = docIds[index];
      const body = {
        user: actor,
        meaning,
        reason: reason || '',
        components: { user_id: actor, password: credential },
      };

      try {
        const signature = await postSignature(docId, body, actor);
        results.push({ docId, ok: true, signature });
        onResult({ docId, ok: true, signature, index });
      } catch (err) {
        results.push({ docId, ok: false, err });
        onResult({ docId, ok: false, err, index });

        if (err.name === 'IntegrityFailure') throw err;

        // A credential problem, a malformed request or a lost connection will
        // refuse the rest, so stop. A 403 is document-specific — segregation of
        // duties — and only stops the batch when it is the probe.
        const stops =
          err.status === 400 ||
          err.status === 422 ||
          err.status === 0 ||
          index === 0;
        if (stops) {
          for (const skipped of docIds.slice(index + 1)) {
            results.push({ docId: skipped, ok: false, notAttempted: true });
            onResult({ docId: skipped, ok: false, notAttempted: true });
          }
          break;
        }
      }
    }
    return results;
  } finally {
    field.value = '';
    field.disabled = false;
    // `credential` is a const in this frame, and the frame ends here.
  }
}

/** Register a signer.
 *
 * Two distinct labelled inputs, because the identification code and the printed
 * name are different regulated fields: sending them equal is refused by the
 * identity store, and a manifest whose printed-name row carried the code would
 * be a defective signed record.
 */
export async function registerSigner({ userId, printedName, form, actor }) {
  const field = form.elements.namedItem('reg-pw');
  const credential = field.value;
  field.value = '';
  field.disabled = true;

  try {
    const response = await fetch(`${BASE}/signers`, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
        'X-ValKit-Actor': actor,
      },
      body: JSON.stringify({
        user_id: userId,
        printed_name: printedName,
        password: credential,
        roles: [],
      }),
    });
    const payload = await response.json().catch(() => null);
    if (response.ok) return payload;
    const shape = payload && typeof payload === 'object' ? payload : {};
    throw new ApiError({
      status: response.status,
      error: shape.error || `HTTP ${response.status}`,
      error_type: shape.error_type || 'HTTPError',
      detail: shape.detail,
    });
  } finally {
    field.value = '';
    field.disabled = false;
  }
}

export default { applyBatch, registerSigner };
