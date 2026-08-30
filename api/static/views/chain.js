/* The traceability matrix, and — mainly — its gaps.
 *
 * Requirement text is never clamped, ellipsised or truncated. It is
 * server-authored prose, it is what the requirement says, and a console that
 * shortened it would be showing a reader something other than the record.
 *
 * "Covered" and "verified" are kept apart throughout. Coverage can be complete
 * while requirements remain unverified, which is exactly the case in the
 * example record, and eliding the two would overstate the evidence.
 */

import { api, cached } from '../api.js';
import { COPY } from '../copy.js';
import { consoleNote, dataRegion, defs, digest, el, ledger, proseList, section, tok, widthFor } from '../dom.js';
import { label, proportion } from '../fmt.js';

const VERDICT_TOKEN = {
  verified: 'VER',
  'not verified': 'NVF',
  'not executed': 'NEX',
  'no test': 'NOT',
};

export async function renderChain(validationId, requirementId = null) {
  const host = el('div');
  const root = el('div', {}, [
    el('h1', { class: 'screen-title', text: COPY.CHAIN_H }),
    el('p', { class: 'lede', text: COPY.CHAIN_LEDE }),
    host,
  ]);

  await dataRegion(host, async () => {
    const rtm = await cached(`rtm:${validationId}`, () => api.rtm(validationId), {
      ttl: 600_000, now: 0,
    });
    return requirementId
      ? one(rtm, requirementId, validationId)
      : all(rtm, validationId);
  }, { noun: 'the traceability matrix' });

  return root;
}

function all(rtm, validationId) {
  const wrap = el('div');
  const coverage = rtm.coverage;

  wrap.append(section('Coverage', defs([
    ['Requirements', `${coverage.requirements_covered} of ${coverage.requirements_total} covered`],
    ['Critical requirements', `${coverage.critical_covered} of ${coverage.critical_total} covered`],
    ['Critical coverage', proportion(coverage.critical_coverage)],
    ['Risks mitigated', `${coverage.risks_mitigated} of ${coverage.risks_total}`],
    ['Tests executed', `${coverage.tests_executed} of ${coverage.tests_total}`],
  ]), consoleNote(COPY.CHAIN_COVERED_VS_VERIFIED)));

  const digests = rtm.rows.flatMap((row) => row.evidence);
  const width = widthFor(digests);

  wrap.append(section('Requirements', ledger({
    caption: 'Requirements to test traceability',
    enumerate: true,
    columns: [
      { label: 'Requirement' },
      { label: 'Verdict' },
      { label: 'Text' },
      { label: 'Kind' },
      { label: 'Risks' },
      { label: 'Tests' },
      { label: 'Executed' },
      { label: 'Evidence' },
    ],
    rows: rtm.rows.map((row) => [
      el('span', {}, [
        el('a', {
          class: 'id',
          href: `#/v/${encodeURIComponent(validationId)}/chain/${encodeURIComponent(row.requirement_id)}`,
          text: row.requirement_id,
        }),
        row.critical ? tok('CRT') : null,
      ]),
      el('span', {}, [
        tok(VERDICT_TOKEN[row.verdict] || 'NOT'),
        ' ',
        label('RtmVerdict', row.verdict).text,
      ]),
      // Full text, wrapping to as many lines as it needs.
      el('span', { class: 'server', text: row.text }),
      label('RequirementKind', row.kind).text,
      row.risks.join(', '),
      row.tests.join(', '),
      String(row.executions.length),
      row.evidence.length
        ? el('span', {}, row.evidence.map((d) => digest(d, width)))
        : '',
    ]),
  })));

  wrap.append(proseList({
    heading: COPY.CHAIN_FINDINGS_H,
    items: (rtm.findings || []).map((f) => f.message),
    token: 'OBL',
    emptyCopy: COPY.CHAIN_FINDINGS_EMPTY,
    notesFor: (message) => {
      const finding = rtm.findings.find((f) => f.message === message);
      if (!finding) return null;
      return `Console note. Severity ${finding.severity}; ` +
        `${finding.blocking ? 'blocking the validated gate' : 'not blocking the validated gate'}.`;
    },
  }));

  return wrap;
}

function one(rtm, requirementId, validationId) {
  const row = rtm.rows.find((r) => r.requirement_id === requirementId);
  const wrap = el('div');
  wrap.append(el('p', {}, [
    el('a', { href: `#/v/${encodeURIComponent(validationId)}/chain`, text: '← the whole matrix' }),
  ]));

  if (!row) {
    wrap.append(el('p', { class: 'note', text: `No requirement ${requirementId} in this matrix.` }));
    return wrap;
  }

  const width = widthFor(row.evidence);
  wrap.append(el('h2', { class: 'screen-title' }, [
    el('span', { class: 'id', text: row.requirement_id }),
    ' ',
    tok(VERDICT_TOKEN[row.verdict] || 'NOT'),
    ' ',
    label('RtmVerdict', row.verdict).text,
  ]));
  wrap.append(el('p', { class: 'server', text: row.text }));

  wrap.append(section('Traced', defs([
    ['Kind', label('RequirementKind', row.kind).text],
    ['Critical', row.critical ? 'Yes' : 'No'],
    ['Risks', row.risks.join(', ') || 'none'],
    ['Tests', row.tests.join(', ') || 'none'],
    ['Executions', row.executions.join(', ') || 'none'],
    ['Runs', row.runs.join(', ') || 'none'],
    ['Evidence', row.evidence.length
      ? el('span', {}, row.evidence.map((d) => digest(d, width)))
      : 'none'],
    ['Documents', el('span', {}, row.documents.flatMap((docId, index) => [
      index ? ', ' : '',
      el('a', { class: 'id', href: `#/doc/${encodeURIComponent(docId)}?v=${encodeURIComponent(validationId)}`, text: docId }),
    ]))],
  ])));

  return wrap;
}

export default renderChain;
