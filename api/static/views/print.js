/* Print assembly.
 *
 * One page carrying everything needed to read this validation on paper: the
 * gate and its reasoning, the acceptance evidence, the traceability matrix, the
 * approvals, and a register of every digest in full so that a printed copy is
 * resolvable rather than merely readable.
 *
 * Assembled into the ordinary DOM and printed by the ordinary print stylesheet.
 * There is no separate print template to drift from the screen.
 */

import { api, cached } from '../api.js';
import { COPY } from '../copy.js';
import { consoleNote, dataRegion, defs, el, ledger, proseList, section, tok } from '../dom.js';
import { label, proportion } from '../fmt.js';

export async function renderPrint(validationId) {
  const host = el('div');
  const root = el('div', {}, [
    el('h1', { class: 'screen-title', text: COPY.PRINT_H }),
    el('p', { class: 'lede', text: COPY.PRINT_LEDE }),
    el('p', {}, [el('button', {
      type: 'button',
      class: 'primary',
      text: 'Print',
      onclick: () => window.print(),
    })]),
    host,
  ]);

  await dataRegion(host, async () => {
    const [record, rtm] = await Promise.all([
      cached(`validation:${validationId}`, () => api.validation(validationId), { now: Date.now() }),
      api.rtm(validationId).catch(() => null),
    ]);
    return sheet(record, rtm, validationId);
  }, { noun: 'the validation' });

  return root;
}

function sheet(record, rtm, validationId) {
  const wrap = el('div');
  const readiness = record.readiness;
  const run = record.run;
  const digests = [];

  wrap.append(section('Provenance', defs([
    ['Agent', `${record.agent_id} v${record.agent_version}`],
    ['Validation', record.validation_id],
    ['Opened (UTC)', record.created_at],
    ['Lifecycle status', label('ValidationStatus', record.status).text],
    ['Validated (UTC)', record.validated_at || 'not recorded'],
    ['Run', run ? run.run_id : 'none'],
    ['Model', run ? run.model : 'none'],
    ['Gate', readiness.ready ? COPY.GATE_READY : COPY.GATE_HOLD],
  ])));

  if (run) {
    digests.push(run.dataset_sha256);
    if (run.transcripts_ref) digests.push(run.transcripts_ref);
  }

  wrap.append(proseList({
    heading: COPY.BLOCKERS_H, intro: COPY.BLOCKERS_INTRO,
    items: readiness.blockers, token: 'BLK',
  }));
  wrap.append(proseList({
    heading: COPY.CONDITIONS_H, intro: COPY.CONDITIONS_INTRO,
    items: readiness.conditions, token: 'OBL',
  }));
  wrap.append(proseList({
    heading: COPY.SATISFIED_H, intro: COPY.SATISFIED_INTRO,
    items: readiness.satisfied, token: 'OK',
  }));

  if (run && run.metrics.length) {
    wrap.append(section('Acceptance', ledger({
      caption: 'Acceptance criteria',
      enumerate: true,
      columns: [
        { label: 'Metric' },
        { label: 'Result' },
        { label: 'Lower bound', numeric: true },
        { label: 'Target', numeric: true },
        { label: 'Observed', numeric: true },
        { label: 'Cases' },
        { label: 'Method' },
      ],
      rows: run.metrics.map((m) => [
        m.name,
        el('span', {}, [tok(m.passed ? 'MET' : 'NMT'), ' ', m.passed ? 'met' : 'not met']),
        proportion(m.lower_bound),
        proportion(m.target),
        proportion(m.point_estimate),
        `${m.k} of ${m.n}`,
        label('BoundMethod', m.method).text,
      ]),
    }), ...run.metrics.map((m) =>
      el('p', { class: 'server', text: `${m.name}: ${m.rationale}` }))));
  }

  wrap.append(section('Package', ledger({
    caption: 'Documents and approvals',
    enumerate: true,
    columns: [
      { label: 'Document' }, { label: 'Type' }, { label: 'Status' },
      { label: 'Signatures', numeric: true }, { label: 'Approvals' }, { label: 'Digest' },
    ],
    rows: record.documents.map((d) => {
      digests.push(d.content_sha256);
      return [
        d.doc_id, d.doc_type, label('DocumentStatus', d.status).text,
        String(d.signature_count),
        d.signatures_required_met ? 'complete' : 'outstanding',
        el('span', { class: 'mono', text: d.content_sha256 }),
      ];
    }),
  })));

  if (rtm) {
    wrap.append(section('Traceability', ledger({
      caption: 'Requirements to test traceability',
      enumerate: true,
      columns: [
        { label: 'Requirement' }, { label: 'Verdict' }, { label: 'Text' },
        { label: 'Tests' }, { label: 'Executions', numeric: true },
      ],
      rows: rtm.rows.map((row) => {
        row.evidence.forEach((d) => digests.push(d));
        return [
          row.requirement_id,
          label('RtmVerdict', row.verdict).text,
          el('span', { class: 'server', text: row.text }),
          row.tests.join(', '),
          String(row.executions.length),
        ];
      }),
    })));
  }

  const unique = [...new Set(digests)].sort();
  wrap.append(section(COPY.PRINT_REGISTER_H,
    el('p', { class: 'note', text: COPY.PRINT_REGISTER_INTRO }),
    ledger({
      caption: 'Digest register',
      enumerate: true,
      columns: [{ label: 'SHA-256' }],
      rows: unique.map((d) => [el('span', { class: 'mono', text: d })]),
    })));

  wrap.append(consoleNote(
    'Console note. Assembled by the ValKit console from the API responses named above. ' +
    'It is a reading aid, not a generated document: the validation summary report in the ' +
    'package is the record.'));

  return wrap;
}

export default renderPrint;
