/* One document record.
 *
 * Markdown is the bytes the digest covers, so it is offered and labelled as
 * such. The rendering goes into a sandboxed iframe with no same-origin access:
 * it is generated content, and it has no business sharing a DOM with the screen
 * that holds a signature field.
 *
 * Opening a document here records it as opened in this tab, which is what
 * unlocks its row in the signing queue.
 */

import { api } from '../api.js';
import { COPY } from '../copy.js';
import { consoleNote, dataRegion, defs, digest, el, ledger, section, tok } from '../dom.js';
import { label } from '../fmt.js';
import { opened } from '../session.js';

export async function renderDocument(docId, validationId = null) {
  const host = el('div');
  const root = el('div', {}, [
    el('h1', { class: 'screen-title' }, [el('span', { class: 'id', text: docId })]),
    el('p', { class: 'lede', text: COPY.DOC_LEDE }),
    host,
  ]);

  if (validationId) {
    root.insertBefore(el('p', {}, [
      el('a', {
        href: `#/v/${encodeURIComponent(validationId)}/package`,
        text: '← the package',
      }),
    ]), root.firstChild);
  }

  await dataRegion(host, async () => {
    const [doc, verification] = await Promise.all([
      api.document(docId, 'json'),
      api.verifyDocument(docId).catch(() => null),
    ]);
    return body(doc, verification, validationId);
  }, { noun: 'the document' });

  return root;
}

function body(doc, verification, validationId) {
  const wrap = el('div');

  // Record it as opened, against the validation the reader came from. Local
  // only: never sent to the API, never printed. Without a validation in the
  // route the queue simply stays locked, which is the safe direction.
  if (validationId) opened.record(validationId, doc.doc_id);

  wrap.append(section('Record', defs([
    ['Title', doc.title],
    ['Type', doc.doc_type],
    ['Version', doc.version],
    ['Status', label('DocumentStatus', doc.status).text],
    ['Generated', doc.generated_at],
    ['Template', el('span', { class: 'mono', text: doc.template })],
    ['Digest of the bytes', digest(doc.content_sha256)],
  ]), consoleNote(COPY.DOC_DIGEST_NOTE)));

  // -- approvals, authoritative -------------------------------------------
  const approvals = el('div');
  if (doc.signatures.length) {
    approvals.append(ledger({
      caption: 'Signatures applied to this document',
      enumerate: true,
      columns: [
        { label: 'Signature' },
        { label: 'Printed name' },
        { label: 'Meaning' },
        { label: 'Signed at (UTC)' },
        { label: 'Components used' },
      ],
      rows: doc.signatures.map((s) => [
        el('span', { class: 'id', text: s.signature_id }),
        s.printed_name,
        label('SignatureMeaning', s.meaning).text,
        s.signed_at,
        s.components_used.join(', '),
      ]),
    }));
    if (verification) {
      approvals.append(el('p', { class: 'note' }, [
        tok(verification.ok ? 'OK' : 'INT'),
        ' ',
        verification.ok
          ? `${verification.checked} signature(s) verified against the content they were bound to.`
          : verification.reason,
      ]));
    }
    approvals.append(consoleNote(COPY.DOC_UNSIGNED_NOTE));
  } else {
    approvals.append(el('p', { class: 'note', text: 'None. This document is unsigned.' }));
    approvals.append(el('p', { class: 'note', text: COPY.DOC_VERIFY_NONE }));
  }
  wrap.append(section('Approvals', approvals));

  // -- the content --------------------------------------------------------
  const view = el('div');
  const switcher = el('div', { class: 'form-row', role: 'group', 'aria-label': 'Format' });

  const showMarkdown = () => {
    view.replaceChildren(
      el('p', { class: 'note', text: 'Markdown — the exact bytes the digest above covers.' }),
      el('pre', { class: 'record', text: doc.content }),
    );
  };
  const showRendered = () => {
    const frame = el('iframe', {
      class: 'doc',
      sandbox: '',
      title: `${doc.doc_id} rendered`,
    });
    view.replaceChildren(
      el('p', { class: 'note', text: 'Rendered — the human-readable form 21 CFR 11.50(b) asks for.' }),
      frame,
    );
    api.document(doc.doc_id, 'html').then((html) => { frame.srcdoc = html; });
  };

  const mdButton = el('button', { type: 'button', text: 'Markdown', onclick: showMarkdown });
  const htmlButton = el('button', { type: 'button', text: 'Rendered', onclick: showRendered });
  switcher.append(mdButton, htmlButton);

  wrap.append(section('Content', switcher, view));
  showMarkdown();

  return wrap;
}

export default renderDocument;
