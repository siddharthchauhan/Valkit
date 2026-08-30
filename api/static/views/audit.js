/* The audit trail.
 *
 * The scope of what is shown is stated on the face of the table, not in a
 * footnote. The API returns the tail, so on a long trail the genesis record is
 * not reachable here at all — a screen that quietly showed the last 200 records
 * under the heading "Audit trail" would be claiming to show the trail.
 */

import { api } from '../api.js';
import { COPY } from '../copy.js';
import { consoleNote, dataRegion, digest, el, ledger, section, tok, widthFor } from '../dom.js';
import { integer, isDigest } from '../fmt.js';

const LIMITS = [200, 1000, 5000];

export async function renderAudit(query = {}) {
  const host = el('div');
  const limit = Number(query.limit) || 200;

  const root = el('div', {}, [
    el('h1', { class: 'screen-title', text: COPY.AUD_H }),
    el('p', { class: 'lede', text: COPY.AUD_LEDE }),
    filters(query, limit),
    host,
  ]);

  await dataRegion(host, async () => {
    const payload = await api.audit({
      actor: query.actor,
      action: query.action,
      entity_id: query.entity_id,
      limit,
    });
    return body(payload, limit);
  }, { noun: 'the audit trail' });

  return root;
}

function filters(query, limit) {
  const form = el('form', { class: 'form-row', onsubmit: (e) => e.preventDefault() });

  const inputs = {};
  for (const [key, label] of [['actor', 'Actor'], ['action', 'Action'], ['entity_id', 'Entity']]) {
    const id = `flt-${key}`;
    inputs[key] = el('input', { type: 'text', id, value: query[key] || '', autocomplete: 'off' });
    form.append(el('div', { class: 'field' }, [el('label', { for: id, text: label }), inputs[key]]));
  }

  const limitSelect = el('select', { id: 'flt-limit' });
  for (const value of LIMITS) {
    limitSelect.append(el('option', { value, text: String(value), selected: value === limit }));
  }
  form.append(el('div', { class: 'field' }, [
    el('label', { for: 'flt-limit', text: 'Limit' }), limitSelect,
  ]));

  form.append(el('button', {
    type: 'button',
    text: 'Apply filter',
    onclick: () => {
      const params = new URLSearchParams();
      for (const [key, input] of Object.entries(inputs)) {
        if (input.value.trim()) params.set(key, input.value.trim());
      }
      params.set('limit', limitSelect.value);
      location.hash = `#/audit?${params}`;
    },
  }));
  form.append(el('a', { href: api.auditExportUrl('text'), text: 'Export as text' }));
  form.append(el('a', { href: api.auditExportUrl('jsonl'), text: 'Export as JSONL' }));
  return form;
}

function body(payload, limit) {
  const wrap = el('div');
  wrap.append(el('p', { class: 'note' }, [
    COPY.AUD_SCOPE(payload.returned, payload.total, limit),
  ]));
  wrap.append(el('p', { class: 'note' }, [
    'Chain digest ', digest(payload.chain_digest),
  ]));

  if (!payload.records.length) {
    wrap.append(el('p', { class: 'note', text: COPY.AUD_EMPTY }));
    return wrap;
  }

  const digests = payload.records.flatMap((r) => [r.row_hash, r.prev_hash]);
  const width = widthFor(digests);

  wrap.append(ledger({
    caption: 'Audit records, oldest first within the window shown',
    columns: [
      { label: 'Seq', numeric: true },
      { label: 'Time (UTC)' },
      { label: 'Actor' },
      { label: 'Action' },
      { label: 'Entity' },
      { label: 'Payload' },
      { label: 'Row digest' },
    ],
    rows: payload.records.map((r) => [
      el('span', { class: 'id', text: String(r.seq) }),
      el('span', { class: 'mono', text: r.ts }),
      el('span', {}, [tok(r.actor === 'system' ? 'SYS' : 'PSN'), ' ', r.actor]),
      el('span', { class: 'mono', text: r.action }),
      el('span', { class: 'mono', text: `${r.entity_type}:${r.entity_id}` }),
      payloadCell(r.payload, width),
      digest(r.row_hash, width),
    ]),
  }));

  wrap.append(consoleNote(
    'Console note. Each record’s digest covers its own contents and the digest of the record ' +
    'before it, which is what makes an insertion or a deletion detectable rather than merely ' +
    'discouraged.'));
  return wrap;
}

function payloadCell(payload, width) {
  const entries = Object.entries(payload || {});
  if (!entries.length) return '';
  const list = el('dl', { class: 'defs' });
  for (const [key, value] of entries) {
    list.append(el('dt', { text: key }));
    const dd = el('dd');
    if (isDigest(value)) dd.append(digest(value, width));
    else if (Array.isArray(value)) dd.append(el('span', { class: 'server', text: value.join('; ') }));
    else dd.textContent = typeof value === 'number' ? integer(value) : String(value);
    list.append(dd);
  }
  return list;
}

export default renderAudit;
