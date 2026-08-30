/* The signing queue.
 *
 * The one screen in the console that writes something consequential. Every
 * design choice here is a constraint rather than a convenience:
 *
 *   - Nothing is selected by default. Fifteen pre-ticked boxes and one password
 *     entry would be an attestation nobody made.
 *   - A document can be selected only once it has been opened in this tab. That
 *     is a local aid, never sent to the API and never printed, because the
 *     console cannot observe that anyone read anything and will not assert it.
 *   - The credential is read by sign.js and by nothing else here.
 *   - Rejection takes one document, a mandatory reason, and a typed
 *     confirmation, because it cannot be undone.
 */

import { api, cached, invalidate } from '../api.js';
import { COPY } from '../copy.js';
import { announce, consoleNote, dataRegion, el, errorBlock, section, tok } from '../dom.js';
import { applyBatch, registerSigner } from '../sign.js';
import { afterWrite, identity, opened } from '../app.js';

const MEANINGS = ['approved', 'reviewed', 'verified', 'authored'];

export async function renderSign(validationId) {
  const host = el('div');
  const root = el('div', {}, [
    el('h1', { class: 'screen-title', text: COPY.SIGN_H }),
    el('p', { class: 'lede', text: COPY.SIGN_LEDE }),
    host,
  ]);

  const who = identity.value();
  if (!who) {
    host.append(el('p', { class: 'note', text: COPY.SIGN_NO_IDENTITY }));
    return root;
  }

  await dataRegion(host, async () => {
    const record = await cached(
      `validation:${validationId}`,
      () => api.validation(validationId),
      { now: Date.now() },
    );
    return queue(record, validationId, who);
  }, { noun: 'the signing queue' });

  return root;
}

function queue(record, validationId, who) {
  const wrap = el('div');
  const read = opened.all(validationId);
  const outstanding = record.documents.filter((d) => !d.signatures_required_met);

  wrap.append(consoleNote(COPY.SIGN_COMPONENTS));
  wrap.append(consoleNote(COPY.SIGN_READ_GATE));

  if (!outstanding.length) {
    wrap.append(el('p', { class: 'note', text: COPY.SIGN_QUEUE_EMPTY }));
    return wrap;
  }

  const selected = new Set();
  const rows = el('div', { class: 'cards' });

  for (const doc of outstanding) {
    const isRead = Boolean(read[doc.doc_id]);
    const id = `pick-${doc.doc_id}`;
    const box = el('input', {
      type: 'checkbox',
      id,
      'aria-disabled': isRead ? null : 'true',
      onchange: (event) => {
        if (!isRead) { event.target.checked = false; return; }
        if (event.target.checked) selected.add(doc.doc_id);
        else selected.delete(doc.doc_id);
        refresh();
      },
    });
    if (!isRead) box.disabled = true;

    rows.append(el('div', { class: 'queue-row', dataset: { disabled: String(!isRead) } }, [
      box,
      el('div', { class: 'grow' }, [
        el('label', { for: id }, [
          el('span', { class: 'id', text: doc.doc_id }),
          ` — ${doc.title}`,
        ]),
        el('p', { class: 'meta' }, [
          isRead
            ? `Opened in this tab at ${read[doc.doc_id]}`
            : 'Not yet opened in this tab. ',
          isRead ? null : el('a', {
            href: `#/doc/${encodeURIComponent(doc.doc_id)}?v=${encodeURIComponent(validationId)}`,
            text: 'Open the record',
          }),
        ]),
      ]),
      el('span', { class: 'id', text: `${doc.content_sha256.slice(0, 12)}…` }),
    ]));
  }
  wrap.append(section(`Outstanding — ${outstanding.length}`, rows));

  // -- the attestation ---------------------------------------------------

  const form = el('form', { id: 'sign-form', onsubmit: (e) => e.preventDefault() });
  const meaningSet = el('fieldset', {}, [el('legend', { text: 'Meaning of the signature' })]);
  const radios = el('div', { class: 'radios' });
  for (const meaning of MEANINGS) {
    radios.append(el('label', {}, [
      el('input', {
        type: 'radio', name: 'meaning', value: meaning,
        onchange: () => refresh(),
      }),
      meaning,
    ]));
  }
  meaningSet.append(radios);
  meaningSet.append(el('p', {
    class: 'note',
    text: '21 CFR 11.50(a)(3): the meaning is part of the signed record and appears in the manifest.',
  }));

  const password = el('input', {
    type: 'password',
    id: 'sign-pw',
    autocomplete: 'off',
    required: true,
  });

  const apply = el('button', { type: 'button', class: 'primary', text: 'Apply signature' });
  apply.disabled = true;

  form.append(
    meaningSet,
    el('div', { class: 'form-row' }, [
      el('div', { class: 'field' }, [
        el('label', { for: 'sign-pw', text: 'Password (signature component)' }),
        password,
      ]),
      apply,
    ]),
    el('p', { class: 'note', text: COPY.SIGN_CREDENTIAL_NOTE }),
    el('p', { class: 'note', text: COPY.SIGN_PROBE }),
  );
  wrap.append(section('Apply', form));

  const results = el('div', {}, [el('p', { class: 'note', text: COPY.SIGN_APPLIED_EMPTY })]);
  wrap.append(el('section', { class: 'block' }, [
    el('h2', { id: 'sign-results', tabindex: '-1', text: 'Applied' }),
    results,
  ]));

  function meaning() {
    return form.querySelector('input[name="meaning"]:checked')?.value || '';
  }
  function refresh() {
    apply.disabled = !(selected.size && meaning());
  }

  apply.addEventListener('click', async () => {
    const docIds = outstanding.map((d) => d.doc_id).filter((id) => selected.has(id));
    apply.disabled = true;
    results.replaceChildren(el('p', { class: 'pending', role: 'status', text: 'Applying.' }));

    let applied = 0;
    const list = el('div', { class: 'cards' });
    results.replaceChildren(list);

    try {
      await applyBatch({
        docIds,
        meaning: meaning(),
        reason: '',
        actor: who,
        form,
        onResult: (outcome) => {
          if (outcome.ok) applied += 1;
          list.append(resultCard(outcome));
          announce(COPY.SIGN_APPLIED(applied, docIds.length));
        },
      });
    } catch (err) {
      list.append(errorBlock(err));
    }

    list.append(el('p', { class: 'note', text: COPY.SIGN_NO_RETRY }));
    invalidate(`validation:${validationId}`);
    await afterWrite();
    document.getElementById('sign-results')?.focus();
  });

  wrap.append(registrationPanel(who));
  return wrap;
}

function resultCard(outcome) {
  if (outcome.notAttempted) {
    return el('div', { class: 'card' }, [
      el('h3', { class: 'id', text: outcome.docId }),
      el('p', { class: 'note', text: COPY.SIGN_NOT_ATTEMPTED }),
    ]);
  }
  if (!outcome.ok) {
    return el('div', { class: 'card' }, [
      el('h3', { class: 'id', text: outcome.docId }),
      errorBlock(outcome.err),
    ]);
  }

  const s = outcome.signature;
  return el('div', { class: 'card' }, [
    el('h3', {}, [tok('OK'), ' ', el('span', { class: 'id', text: outcome.docId })]),
    el('p', { class: 'meta', text: `${s.signature_id} · ${s.printed_name} · ${s.signed_at} (UTC)` }),
    el('pre', { class: 'record', text: s.manifest }),
  ]);
}

/* Registering a signer, in the same file discipline: two distinct labelled
   inputs, because the identification code and the printed name are different
   regulated fields and sending them equal is refused by the identity store. */
function registrationPanel(who) {
  const form = el('form', { id: 'reg-form', onsubmit: (e) => e.preventDefault() });
  const outcome = el('div');

  const printed = el('input', { type: 'text', id: 'reg-name', autocomplete: 'off', required: true });
  const password = el('input', { type: 'password', id: 'reg-pw', autocomplete: 'off', required: true });
  const submit = el('button', { type: 'button', text: 'Register this signer' });

  submit.addEventListener('click', async () => {
    outcome.replaceChildren();
    try {
      const signer = await registerSigner({
        userId: who,
        printedName: printed.value.trim(),
        form,
        actor: who,
      });
      outcome.append(el('p', { class: 'note', text: `Registered ${signer.printed_name} (${signer.user_id}).` }));
    } catch (err) {
      outcome.append(errorBlock(err));
    }
  });

  form.append(
    el('div', { class: 'form-row' }, [
      el('div', { class: 'field' }, [
        el('label', { for: 'reg-id', text: 'Identification code' }),
        el('input', { type: 'text', id: 'reg-id', value: who, readonly: true }),
      ]),
      el('div', { class: 'field' }, [
        el('label', { for: 'reg-name', text: 'Printed name' }),
        printed,
      ]),
      el('div', { class: 'field' }, [
        el('label', { for: 'reg-pw', text: 'Password (signature component)' }),
        password,
      ]),
      submit,
    ]),
    el('p', { class: 'note', text: COPY.SIGN_PRINTED_NAME_NOTE }),
    outcome,
  );

  return section('Register a signer', form);
}

export default renderSign;
