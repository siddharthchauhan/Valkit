/* The package and its approvals.
 *
 * A document with any signature is verified eagerly rather than trusted:
 * `signatures_required_met` calls verification first, so a document whose
 * existing signature no longer verifies would otherwise appear here as an
 * ordinary approvals gap, when it is an integrity problem.
 */

import { api, cached } from '../api.js';
import { COPY } from '../copy.js';
import { consoleNote, dataRegion, digest, el, ledger, tok, widthFor } from '../dom.js';
import { label } from '../fmt.js';

export async function renderPackage(validationId) {
  const host = el('div');
  const root = el('div', {}, [
    el('h1', { class: 'screen-title', text: COPY.PKG_H }),
    el('p', { class: 'lede', text: COPY.PKG_LEDE }),
    host,
  ]);

  await dataRegion(host, async () => {
    const record = await cached(
      `validation:${validationId}`,
      () => api.validation(validationId),
      { now: Date.now() },
    );
    const documents = record.documents;
    if (!documents.length) return el('p', { class: 'note', text: COPY.PKG_EMPTY });

    const verifications = await Promise.all(documents.map(async (doc) => {
      if (!doc.signature_count) return null;
      try {
        return await api.verifyDocument(doc.doc_id);
      } catch {
        return null;
      }
    }));

    const width = widthFor(documents.map((d) => d.content_sha256));
    const wrap = el('div');

    wrap.append(ledger({
      caption: 'Documents in this package and their approvals',
      enumerate: true,
      columns: [
        { label: 'Document' },
        { label: 'Type' },
        { label: 'Status' },
        { label: 'Signatures', numeric: true },
        { label: 'Approvals' },
        { label: 'Digest of the bytes signed' },
      ],
      rows: documents.map((doc, index) => {
        const verification = verifications[index];
        const void_ = verification && verification.ok === false;
        return [
          el('a', {
            class: 'id',
            href: `#/doc/${encodeURIComponent(doc.doc_id)}?v=${encodeURIComponent(validationId)}`,
            text: doc.doc_id,
          }),
          doc.doc_type,
          label('DocumentStatus', doc.status).text,
          String(doc.signature_count),
          el('span', {}, [
            tok(void_ ? 'INT' : doc.signatures_required_met ? 'APC' : 'APO'),
            ' ',
            void_ ? 'signature does not verify'
              : doc.signatures_required_met ? 'complete' : 'outstanding',
          ]),
          digest(doc.content_sha256, width),
        ];
      }),
    }));

    const voided = documents.filter((_, i) => verifications[i] && verifications[i].ok === false);
    for (const [index, doc] of documents.entries()) {
      const verification = verifications[index];
      if (verification && verification.ok === false) {
        wrap.append(consoleNote(`${doc.doc_id}: ${COPY.PKG_VOID(verification.reason || '')}`));
      }
    }
    if (!voided.length) {
      wrap.append(consoleNote(
        'Console note. Every signature on this package was re-verified against the content it ' +
        'was bound to when this screen loaded.'));
    }

    wrap.append(el('p', { class: 'note' }, [
      el('a', {
        href: `#/v/${encodeURIComponent(validationId)}/sign`,
        text: 'Open the signing queue',
      }),
    ]));

    return wrap;
  }, { noun: 'the package' });

  return root;
}

export default renderPackage;
