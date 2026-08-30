/* The verdict — the validated gate.
 *
 * What the accountable signer must answer in under ten seconds: can this be
 * signed, what is stopping it, and what would I be attesting to.
 *
 * The banner is derived from `readiness.ready` and nothing else. Not from
 * `run.passed`, which is true in a record that is not validated; and not from
 * `status`, which is a snapshot fixed when the pipeline last finalised. The
 * gate recomputed on this request is the honest answer.
 *
 * The three lists never share a heading, a count or a token. A blocker
 * withholds validated status; an outstanding obligation does not, and depends
 * on live operation. Merging them would misstate the record in the direction
 * that matters.
 */

import { api, cached } from '../api.js';
import { COPY } from '../copy.js';
import { consoleNote, dataRegion, defs, digest, el, proseList, section, widthFor } from '../dom.js';
import { label, plural } from '../fmt.js';

const REMEDIES = [
  [/^\d+ document\(s\) lack the required approvals/, (id, m) => ({
    href: `#/v/${id}/sign`,
    text: `Open the signing queue — ${m[0].match(/^\d+/)[0]} outstanding`,
  })],
  [/^(Critical acceptance criteria not met|No critical acceptance criterion|Judge calibration|A judge is configured)/,
    (id) => ({ href: `#/v/${id}/acceptance`, text: 'Open the acceptance evidence' })],
  [/^(Critical-requirement coverage is incomplete|The traceability graph|Traceability: )/,
    (id) => ({ href: `#/v/${id}/chain`, text: 'Open the traceability matrix' })],
  [/^(Evidence vault verification failed|Audit chain verification failed)/,
    () => ({ escalated: true, text: COPY.GATE_INTEGRITY_BLOCKER })],
  [/open change control\(s\) prevent validated status/, (id, m, record) => ({
    href: `#/agent/${encodeURIComponent(record.agent_id)}/monitoring`,
    text: 'Open monitoring and change control',
  })],
  [/^No documents have been generated/, (id) => ({
    href: `#/v/${id}/package`, text: 'Open the package',
  })],
  [/^The specification requires Part 11 signatures, but no signature service/,
    () => ({ text: COPY.NO_REMEDY_DEPLOYMENT })],
];

function remedyFor(record, validationId) {
  return (text) => {
    for (const [pattern, build] of REMEDIES) {
      const match = text.match(pattern);
      if (match) return build(encodeURIComponent(validationId), match, record);
    }
    return { text: COPY.NO_REMEDY };
  };
}

export async function renderVerdict(validationId) {
  const root = el('div');
  const host = el('div');
  root.append(host);

  await dataRegion(host, async () => {
    const record = await cached(
      `validation:${validationId}`,
      () => api.validation(validationId),
      { now: Date.now() },
    );
    return body(record, validationId);
  }, { noun: 'the validation' });

  return root;
}

function body(record, validationId) {
  const readiness = record.readiness;
  const ready = readiness.ready;
  const run = record.run;
  const wrap = el('div');

  wrap.append(el('h1', { class: 'vh', text: `Validation ${record.validation_id}` }));

  const banner = el('section', { class: 'banner', dataset: { state: ready ? 'met' : 'hold' } }, [
    el('h2', { class: 'banner-h', text: ready ? COPY.GATE_READY : COPY.GATE_HOLD }),
    el('p', {
      class: 'line-1',
      text: `${record.agent_id} v${record.agent_version} · ${record.validation_id} · opened ${record.created_at} (UTC)`,
    }),
    el('p', {
      class: 'line-2',
      text: ready
        ? COPY.GATE_SUMMARY_READY(readiness.satisfied.length)
        : COPY.GATE_SUMMARY_HOLD(
          readiness.blockers.length, readiness.satisfied.length, readiness.conditions.length),
    }),
    el('p', { class: 'note', text: COPY.GATE_CONJUNCTIVE }),
  ]);
  if (ready && readiness.conditions.length) {
    banner.append(el('p', { class: 'note', text: COPY.GATE_READY_CONDITIONS(readiness.conditions.length) }));
  }
  if (ready) banner.append(el('p', { class: 'note', text: COPY.GATE_READY_LIMIT }));
  banner.append(el('p', {
    class: 'note lifecycle',
    text: record.validated_at
      ? COPY.GATE_LIFECYCLE_SET(label('ValidationStatus', record.status).text, record.validated_at)
      : COPY.GATE_LIFECYCLE_NULL(label('ValidationStatus', record.status).text),
  }));
  wrap.append(banner);

  wrap.append(proseList({
    heading: COPY.BLOCKERS_H,
    intro: COPY.BLOCKERS_INTRO,
    items: readiness.blockers,
    token: 'BLK',
    remedyFor: remedyFor(record, validationId),
  }));

  wrap.append(proseList({
    heading: COPY.CONDITIONS_H,
    intro: COPY.CONDITIONS_INTRO,
    items: readiness.conditions,
    token: 'OBL',
  }));

  const satisfied = proseList({
    heading: COPY.SATISFIED_H,
    intro: COPY.SATISFIED_INTRO,
    items: readiness.satisfied,
    token: 'OK',
  });
  wrap.append(satisfied);

  // The server's satisfied sentence about coverage is true and incomplete:
  // covered means a test is linked, verified means it ran. Where the RTM shows
  // unexecuted tests, that gap is stated beside the sentence rather than by
  // editing it.
  attachCoverageNote(satisfied, validationId);

  if (run) {
    wrap.append(section('What produced this', defs([
      // The validation summary carries no spec digest, so it is not invented
      // here: the audit trail holds it, and the link below goes to the record.
      ['Specification', el('span', { class: 'id', text: `${record.agent_id}@${record.agent_version}` })],
      ['Run', `${run.run_id} · ${label('RunStatus', run.status).text} · started ${run.started_at} (UTC)`],
      ['Model', el('span', { class: 'mono', text: run.model })],
      ['Qualification set', digest(run.dataset_sha256, widthFor([run.dataset_sha256, run.transcripts_ref]))],
      ['Transcripts', run.transcripts_ref
        ? digest(run.transcripts_ref, widthFor([run.dataset_sha256, run.transcripts_ref]))
        : null],
    ]), el('p', { class: 'note' }, [
      COPY.GATE_SPEC_NOTE,
      ' ',
      el('a', { href: '#/audit?action=spec.ingested', text: 'Filter the trail on spec.ingested.' }),
    ])));
  } else {
    wrap.append(section('What produced this', el('p', { class: 'note', text: COPY.GATE_NO_RUN })));
  }

  const skipped = Object.entries(record.skipped_documents || {});
  wrap.append(section('Not generated, and why',
    skipped.length
      ? el('dl', { class: 'defs' }, skipped.flatMap(([type, reason]) => [
        el('dt', { class: 'mono', text: type }),
        el('dd', {}, [el('span', { class: 'server', text: reason })]),
      ]))
      : el('p', { class: 'note', text: COPY.GATE_SKIPPED_EMPTY })));

  wrap.append(proseList({
    heading: 'Warnings recorded when the run was assembled',
    items: record.warnings || [],
    token: 'ADV',
    emptyCopy: COPY.GATE_WARNINGS_EMPTY,
  }));

  return wrap;
}

async function attachCoverageNote(container, validationId) {
  try {
    const rtm = await cached(`rtm:${validationId}`, () => api.rtm(validationId), { ttl: 600_000, now: 0 });
    const unexecuted = rtm.rows
      .filter((row) => row.critical && row.verdict !== 'verified')
      .flatMap((row) => row.tests.filter((t) => !row.executions.some((e) => e.startsWith(`${t}@`))));
    const ids = [...new Set(unexecuted)].sort();
    if (!ids.length) return;

    const items = [...container.querySelectorAll('.items > li')];
    const target = items.find((li) =>
      li.querySelector('.server')?.textContent.startsWith('Every critical requirement is verified'));
    if (target) {
      target.querySelector('div').append(
        consoleNote(COPY.GATE_COVERAGE_NOTE(ids.length, ids.join(', '))));
    }
  } catch { /* the chain screen states it too; a failure here is not worth a box */ }
}

export default renderVerdict;
