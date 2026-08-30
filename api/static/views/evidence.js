/* The evidence vault.
 *
 * Content-addressed: the identifier is the digest, so an identifier that
 * resolves is itself proof of integrity. The retention date is shown on every
 * object because it is the thing a reader most often needs and most often
 * cannot find.
 */

import { api } from '../api.js';
import { COPY } from '../copy.js';
import { consoleNote, dataRegion, digest, el, ledger, widthFor } from '../dom.js';
import { bytes } from '../fmt.js';

export async function renderEvidence(query = {}) {
  const host = el('div');
  const root = el('div', {}, [
    el('h1', { class: 'screen-title', text: COPY.EV_H }),
    el('p', { class: 'lede', text: COPY.EV_LEDE }),
    host,
  ]);

  await dataRegion(host, async () => {
    const payload = await api.evidence({ agent_id: query.agent_id, limit: 500 });
    if (!payload.records.length) return el('p', { class: 'note', text: COPY.EV_EMPTY });

    const width = widthFor(payload.records.map((r) => r.evidence_id));
    return el('div', {}, [
      el('p', { class: 'note', text: `${payload.total} objects held.` }),
      ledger({
        caption: 'Evidence objects',
        enumerate: true,
        columns: [
          { label: 'Identifier (the digest)' },
          { label: 'Kind' },
          { label: 'Size', numeric: true },
          { label: 'Stored (UTC)' },
          { label: 'Retention until' },
          { label: 'Agent' },
          { label: 'Run' },
        ],
        rows: payload.records.map((r) => [
          digest(r.evidence_id, width),
          r.kind,
          bytes(r.size_bytes),
          el('span', { class: 'mono', text: r.stored_at }),
          el('span', { class: 'mono', text: r.retention_until }),
          r.agent_id,
          r.run_id,
        ]),
      }),
      consoleNote(
        'Console note. The identifier and the digest are the same value. There is no separate ' +
        'checksum to compare against, and no way to overwrite an object under a name it ' +
        'already has.'),
    ]);
  }, { noun: 'the evidence vault' });

  return root;
}

export default renderEvidence;
