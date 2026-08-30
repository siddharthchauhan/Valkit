/* The record index — the auditor's entry, cold, with no credentials.
 *
 * It answers two questions in the order they are actually asked: can this
 * record be trusted, and what does this instance hold?
 */

import { api } from '../api.js';
import { COPY } from '../copy.js';
import { consoleNote, dataRegion, el, ledger, section } from '../dom.js';

export async function renderIndex() {
  const root = el('div', {}, [
    el('h1', { class: 'screen-title', text: COPY.IDX_H }),
    el('p', { class: 'lede', text: COPY.IDX_LEDE }),
  ]);

  const validations = el('div');
  const specs = el('div');
  root.append(
    section(COPY.IDX_VAL_H, validations),
    section(COPY.IDX_SPEC_H, specs),
    consoleNote(COPY.IDX_TYPEFACE),
  );

  await Promise.all([
    dataRegion(validations, async () => {
      const ids = await api.listValidations();
      if (!ids.length) return el('p', { class: 'note', text: COPY.IDX_VAL_EMPTY });

      const rows = await Promise.all(ids.map(async (id) => {
        const record = await api.validation(id);
        const ready = record.readiness.ready;
        return [
          el('a', { href: `#/v/${encodeURIComponent(id)}`, class: 'id', text: id }),
          `${record.agent_id} v${record.agent_version}`,
          ready ? COPY.GATE_READY : COPY.GATE_HOLD,
          String(record.readiness.blockers.length),
          String(record.readiness.conditions.length),
          String(record.documents.length),
        ];
      }));

      return ledger({
        caption: 'Validations held in this instance',
        columns: [
          { label: 'Validation' },
          { label: 'Agent' },
          { label: 'Gate' },
          { label: 'Blocking', numeric: true },
          { label: 'Outstanding', numeric: true },
          { label: 'Documents', numeric: true },
        ],
        rows,
      });
    }, { noun: 'the validations' }),

    dataRegion(specs, async () => {
      const refs = await api.listSpecs();
      if (!refs.length) return el('p', { class: 'note', text: COPY.IDX_SPEC_EMPTY });
      return ledger({
        caption: 'Specifications ingested in this instance',
        columns: [{ label: 'Reference' }],
        rows: refs.map((ref) => [el('span', { class: 'id', text: ref })]),
      });
    }, { noun: 'the specifications' }),
  ]);

  return root;
}

export default renderIndex;
