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
import { consoleNote, dataRegion, defs, digest, el, proseList, section, tok, widthFor } from '../dom.js';
import { label, proportion } from '../fmt.js';

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
  const wrap = el('div', { class: 'validation-workspace' });
  const remedies = remedyFor(record, validationId);
  const action = nextAction(record, validationId, remedies);

  wrap.append(validationHero(record, action), lifecycleRail(record, validationId));

  if (run?.metrics?.length) wrap.append(evidenceSnapshot(run, validationId));

  wrap.append(el('nav', { class: 'workspace-links', 'aria-label': 'Validation workspace' }, [
    el('a', { href: `#/v/${encodeURIComponent(validationId)}/acceptance`, text: COPY.OVERVIEW_VIEW_RESULTS }),
    el('a', { href: `#/v/${encodeURIComponent(validationId)}/chain`, text: COPY.OVERVIEW_VIEW_TRACEABILITY }),
    el('a', { href: `#/v/${encodeURIComponent(validationId)}/package`, text: COPY.OVERVIEW_VIEW_DOCUMENTS }),
    el('a', {
      href: `#/agent/${encodeURIComponent(record.agent_id)}/monitoring`,
      text: COPY.OVERVIEW_VIEW_MONITORING,
    }),
  ]));

  const review = el('section', { class: 'decision-record' }, [
    el('div', { class: 'section-heading' }, [
      el('div', {}, [
        el('p', { class: 'eyebrow', text: 'Readiness record' }),
        el('h2', { text: 'What the record says' }),
      ]),
      el('p', { class: 'section-lede', text: COPY.GATE_CONJUNCTIVE }),
    ]),
  ]);

  review.append(proseList({
    heading: COPY.BLOCKERS_H,
    intro: COPY.BLOCKERS_INTRO,
    items: readiness.blockers,
    token: 'BLK',
    remedyFor: remedies,
  }));

  review.append(proseList({
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
  review.append(satisfied);
  wrap.append(review);

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

function validationHero(record, action) {
  const readiness = record.readiness;
  const run = record.run;
  const ready = readiness.ready;
  const headline = ready
    ? COPY.OVERVIEW_READY_H
    : run ? COPY.OVERVIEW_HOLD_H : COPY.OVERVIEW_NOT_RUN_H;
  const summary = ready
    ? COPY.OVERVIEW_READY_LEDE
    : run
      ? COPY.OVERVIEW_HOLD_LEDE(readiness.blockers.length)
      : COPY.OVERVIEW_NOT_RUN_LEDE;
  const state = ready ? 'ready' : run ? 'hold' : 'draft';

  return el('section', { class: 'validation-hero', dataset: { state } }, [
    el('div', { class: 'validation-hero-copy' }, [
      el('p', { class: 'eyebrow', text: COPY.OVERVIEW_EYEBROW }),
      el('p', { class: 'validation-ref', text: `${record.validation_id} · opened ${record.created_at} (UTC)` }),
      el('h1', { text: record.agent_id }),
      el('p', { class: 'agent-version', text: `Version ${record.agent_version}` }),
      el('h2', { class: 'validation-headline', text: headline }),
      el('p', { class: 'hero-lede', text: summary }),
      el('div', { class: 'hero-actions' }, [
        action.href
          ? el('a', { class: 'button-link primary-link', href: action.href, text: action.text })
          : el('p', { class: 'note', text: action.text }),
      ]),
    ]),
    el('aside', { class: 'readiness-card', 'aria-label': COPY.OVERVIEW_READINESS }, [
      el('p', { class: 'journey-label', text: COPY.OVERVIEW_READINESS }),
      el('span', { class: 'status-pill', dataset: { state } }, [
        tok(ready ? 'OK' : readiness.blockers.length ? 'BLK' : 'ADV'),
        el('span', { text: ready ? COPY.GATE_READY : COPY.HOME_STATUS_HOLD }),
      ]),
      el('dl', { class: 'readiness-facts' }, [
        ...fact(COPY.OVERVIEW_EVIDENCE, run
          ? run.passed ? COPY.HOME_EVIDENCE_MET : COPY.HOME_EVIDENCE_NOT_MET
          : COPY.OVERVIEW_EVIDENCE_PENDING),
        ...fact(COPY.OVERVIEW_DOCUMENTS, record.documents.length
          ? COPY.OVERVIEW_DOCUMENTS_READY(record.documents.length)
          : COPY.OVERVIEW_DOCUMENTS_PENDING),
        ...fact(COPY.OVERVIEW_APPROVALS, ready
          ? COPY.OVERVIEW_APPROVALS_READY : COPY.OVERVIEW_APPROVALS_HOLD),
      ]),
      el('p', {
        class: 'note',
        text: record.validated_at
          ? COPY.GATE_LIFECYCLE_SET(label('ValidationStatus', record.status).text, record.validated_at)
          : COPY.GATE_LIFECYCLE_NULL(label('ValidationStatus', record.status).text),
      }),
    ]),
  ]);
}

function lifecycleRail(record, validationId) {
  const ready = record.readiness.ready;
  const blockers = record.readiness.blockers.length;
  const run = record.run;
  const docs = record.documents.length;
  const stages = [
    ['complete', COPY.OVERVIEW_STAGES[0], 'Specification and controls recorded.', `#/v/${encodeURIComponent(validationId)}/chain`],
    [run ? 'complete' : 'waiting', COPY.OVERVIEW_STAGES[1], run
      ? COPY.OVERVIEW_EVIDENCE_COMPLETE : COPY.OVERVIEW_EVIDENCE_PENDING,
    `#/v/${encodeURIComponent(validationId)}/acceptance`],
    [docs ? 'complete' : 'waiting', COPY.OVERVIEW_STAGES[2], docs
      ? COPY.OVERVIEW_DOCUMENTS_READY(docs) : COPY.OVERVIEW_DOCUMENTS_PENDING,
    `#/v/${encodeURIComponent(validationId)}/package`],
    [ready ? 'complete' : blockers ? 'attention' : 'waiting', COPY.OVERVIEW_STAGES[3], ready
      ? COPY.OVERVIEW_APPROVALS_READY : COPY.OVERVIEW_APPROVALS_HOLD,
    `#/v/${encodeURIComponent(validationId)}/sign`],
    [ready ? 'current' : 'waiting', COPY.OVERVIEW_STAGES[4], ready
      ? COPY.OVERVIEW_MONITORING_READY : COPY.OVERVIEW_MONITORING_PENDING,
    `#/agent/${encodeURIComponent(record.agent_id)}/monitoring`],
  ];

  return el('section', { class: 'lifecycle-section', 'aria-labelledby': 'lifecycle-heading' }, [
    el('div', { class: 'section-heading' }, [
      el('div', {}, [
        el('p', { class: 'eyebrow', text: COPY.OVERVIEW_LIFECYCLE }),
        el('h2', { id: 'lifecycle-heading', text: 'Where this validation is now' }),
      ]),
    ]),
    el('ol', { class: 'lifecycle-rail' }, stages.map(([state, labelText, detail, href], index) =>
      el('li', { dataset: { state } }, [
        el('span', { class: 'lifecycle-index', text: `0${index + 1}` }),
        el('div', {}, [
          el('h3', {}, [el('a', { href, text: labelText })]),
          el('p', { text: detail }),
        ]),
      ]))),
  ]);
}

function evidenceSnapshot(run, validationId) {
  return el('section', { class: 'evidence-snapshot' }, [
    el('div', { class: 'section-heading' }, [
      el('div', {}, [
        el('p', { class: 'eyebrow', text: 'Qualification result' }),
        el('h2', { text: COPY.OVERVIEW_EVIDENCE_H }),
      ]),
      el('a', {
        class: 'button-link secondary-link',
        href: `#/v/${encodeURIComponent(validationId)}/acceptance`,
        text: COPY.OVERVIEW_VIEW_RESULTS,
      }),
    ]),
    el('p', { class: 'section-lede', text: COPY.OVERVIEW_EVIDENCE_LEDE }),
    el('div', { class: 'metric-glance-list' }, run.metrics.map((metric) =>
      el('article', { class: 'metric-glance', dataset: { state: metric.passed ? 'met' : 'nmt' } }, [
        el('div', { class: 'metric-glance-heading' }, [
          el('h3', { text: metric.name }),
          tok(metric.passed ? 'MET' : 'NMT'),
        ]),
        el('p', { class: 'metric-glance-bound', text: proportion(metric.lower_bound) }),
        el('p', { class: 'metric-glance-label', text: 'One-sided lower confidence bound' }),
        el('p', { class: 'metric-glance-target', text: `Target ${proportion(metric.target)}` }),
      ]))),
  ]);
}

function nextAction(record, validationId, remedies) {
  const blocker = record.readiness.blockers[0];
  if (blocker) return remedies(blocker);
  if (record.readiness.ready) {
    return {
      href: `#/agent/${encodeURIComponent(record.agent_id)}/monitoring`,
      text: COPY.OVERVIEW_VIEW_MONITORING,
    };
  }
  if (!record.run) return { href: '#/spec', text: COPY.HOME_CREATE };
  return { href: `#/v/${encodeURIComponent(validationId)}/acceptance`, text: COPY.OVERVIEW_VIEW_RESULTS };
}

function fact(term, value) {
  return [el('dt', { text: term }), el('dd', { text: value })];
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
