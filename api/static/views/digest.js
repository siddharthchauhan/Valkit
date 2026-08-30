/* The digest resolver.
 *
 * The API has no digest lookup, so this searches client-side over a window and
 * says exactly what that window was. "Not found here" is not the same statement
 * as "does not exist", and the copy keeps them apart.
 */

import { api } from '../api.js';
import { COPY } from '../copy.js';
import { dataRegion, digest, el, ledger, section } from '../dom.js';
import { bytes, isDigest } from '../fmt.js';

const AUDIT_WINDOW = 5000;
const EVIDENCE_WINDOW = 5000;

export async function renderDigest(value) {
  const host = el('div');
  const root = el('div', {}, [
    el('h1', { class: 'screen-title', text: COPY.DIG_H }),
    el('p', { class: 'mono', text: value }),
    el('p', { class: 'lede', text: COPY.DIG_LEDE }),
    host,
  ]);

  if (!isDigest(value)) {
    host.append(el('p', { class: 'note', text: COPY.DIG_INVALID }));
    return root;
  }

  await dataRegion(host, async () => {
    const [evidence, audit] = await Promise.all([
      api.evidence({ limit: EVIDENCE_WINDOW }),
      api.audit({ limit: AUDIT_WINDOW }),
    ]);

    const object = evidence.records.find((r) => r.evidence_id === value);
    const records = audit.records.filter((r) =>
      r.row_hash === value || r.prev_hash === value || JSON.stringify(r.payload).includes(value));

    const wrap = el('div');
    wrap.append(el('p', { class: 'note', text: COPY.DIG_WINDOW(evidence.records.length, audit.returned) }));

    wrap.append(section('In the evidence vault', object
      ? el('dl', { class: 'defs' }, [
        el('dt', { text: 'Kind' }), el('dd', { text: object.kind }),
        el('dt', { text: 'Size' }), el('dd', { text: bytes(object.size_bytes) }),
        el('dt', { text: 'Stored' }), el('dd', { class: 'mono', text: object.stored_at }),
        el('dt', { text: 'Retention until' }), el('dd', { class: 'mono', text: object.retention_until }),
        el('dt', { text: 'Agent' }), el('dd', { text: object.agent_id || 'none' }),
        el('dt', { text: 'Run' }), el('dd', { text: object.run_id || 'none' }),
      ])
      : el('p', { class: 'note', text: COPY.DIG_NONE })));

    wrap.append(section('In the audit trail', records.length
      ? ledger({
        caption: 'Audit records referencing this digest',
        columns: [
          { label: 'Seq', numeric: true },
          { label: 'Time (UTC)' },
          { label: 'Actor' },
          { label: 'Action' },
          { label: 'Role of the digest' },
        ],
        rows: records.map((r) => [
          el('span', { class: 'id', text: String(r.seq) }),
          el('span', { class: 'mono', text: r.ts }),
          r.actor,
          el('span', { class: 'mono', text: r.action }),
          r.row_hash === value ? 'this record’s own digest'
            : r.prev_hash === value ? 'the digest of the record before it'
            : 'referenced in the payload',
        ]),
      })
      : el('p', { class: 'note', text: COPY.DIG_NONE })));

    return wrap;
  }, { noun: 'the digest' });

  return root;
}

export default renderDigest;
