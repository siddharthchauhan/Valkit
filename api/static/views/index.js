/* The validation workspace home.
 *
 * The former record index was accurate but made a first-time user reverse
 * engineer the product from its audit artefacts. This screen starts with the
 * job ValKit helps a team do, then presents the records that support it.
 */

import { api } from '../api.js';
import { COPY } from '../copy.js';
import { dataRegion, el, tok } from '../dom.js';

export async function renderIndex() {
  const root = el('div', { class: 'workspace-home' });
  const validations = el('div');

  root.append(homeHero(), el('section', { class: 'workspace-section' }, [
    el('div', { class: 'section-heading' }, [
      el('div', {}, [
        el('p', { class: 'eyebrow', text: 'Validation records' }),
        el('h2', { text: COPY.HOME_VALIDATIONS_H }),
      ]),
      el('p', { class: 'section-lede', text: COPY.HOME_VALIDATIONS_LEDE }),
    ]),
    validations,
  ]));

  await dataRegion(validations, async () => {
    const ids = await api.listValidations();
    if (!ids.length) return emptyWorkspace();
    const records = await Promise.all(ids.map((id) => api.validation(id)));
    return el('div', { class: 'validation-list' }, records.map(validationRow));
  }, { noun: 'the validations' });

  return root;
}

function homeHero() {
  return el('section', { class: 'workspace-hero' }, [
    el('div', { class: 'workspace-hero-copy' }, [
      el('p', { class: 'eyebrow', text: COPY.HOME_EYEBROW }),
      el('h1', { text: COPY.HOME_H }),
      el('p', { class: 'hero-lede', text: COPY.HOME_LEDE }),
      el('div', { class: 'hero-actions' }, [
        el('a', { class: 'button-link primary-link', href: '#/spec', text: COPY.HOME_CREATE }),
        el('a', {
          class: 'button-link secondary-link',
          href: '#/spec?example=1',
          text: COPY.HOME_EXAMPLE,
        }),
      ]),
      el('p', { class: 'hero-note', text: COPY.HOME_NOTE }),
    ]),
    el('aside', { class: 'hero-journey', 'aria-label': COPY.HOME_JOURNEY }, [
      el('p', { class: 'journey-label', text: COPY.HOME_JOURNEY }),
      el('ol', { class: 'journey-stages' }, COPY.HOME_STAGES.map(([number, heading, body]) =>
        el('li', {}, [
          el('span', { class: 'stage-number', text: number }),
          el('div', {}, [
            el('h2', { text: heading }),
            el('p', { text: body }),
          ]),
        ]))),
    ]),
  ]);
}

function emptyWorkspace() {
  return el('section', { class: 'empty-workspace' }, [
    el('div', {}, [
      el('p', { class: 'eyebrow', text: COPY.HOME_EMPTY_EYEBROW }),
      el('h3', { text: COPY.HOME_EMPTY_H }),
      el('p', { class: 'empty-lede', text: COPY.HOME_EMPTY_LEDE }),
      el('a', {
        class: 'button-link primary-link',
        href: '#/spec',
        text: COPY.HOME_EMPTY_ACTION,
      }),
      el('p', { class: 'note', text: COPY.HOME_EMPTY_NOTE }),
    ]),
  ]);
}

function validationRow(record) {
  const ready = record.readiness.ready;
  const blockers = record.readiness.blockers.length;
  const conditions = record.readiness.conditions.length;
  const run = record.run;
  const approvals = approvalState(record.documents);
  const status = ready ? 'ready' : run ? 'hold' : 'draft';
  const evidence = !run
    ? COPY.HOME_EVIDENCE_PENDING
    : run.passed ? COPY.HOME_EVIDENCE_MET : COPY.HOME_EVIDENCE_NOT_MET;
  const summary = ready
    ? COPY.HOME_READY_SUMMARY
    : COPY.HOME_HOLD_SUMMARY(blockers);

  return el('article', { class: 'validation-row', dataset: { state: status } }, [
    el('div', { class: 'validation-row-main' }, [
      el('div', { class: 'validation-row-title' }, [
        el('p', { class: 'id', text: record.validation_id }),
        el('h3', {}, [el('a', {
          href: `#/v/${encodeURIComponent(record.validation_id)}`,
          text: `${record.agent_id} v${record.agent_version}`,
        })]),
      ]),
      el('p', { class: 'validation-summary', text: summary }),
    ]),
    el('div', { class: 'validation-row-status' }, [
      el('span', { class: 'status-pill', dataset: { state: status } }, [
        tok(ready ? 'OK' : blockers ? 'BLK' : 'ADV'),
        el('span', { text: ready ? COPY.HOME_STATUS_READY : run ? COPY.HOME_STATUS_HOLD : COPY.HOME_STATUS_DRAFT }),
      ]),
      el('p', { class: 'validation-evidence', text: evidence }),
    ]),
    el('dl', { class: 'validation-facts' }, [
      ...fact('Documents', String(record.documents.length)),
      ...fact('Approvals', approvals.pending
        ? 'not available'
        : approvals.complete ? 'complete' : COPY.HOME_APPROVALS(approvals.outstanding)),
      ...fact('Ongoing', conditions ? COPY.HOME_CONDITIONS(conditions) : 'none'),
    ]),
    el('a', {
      class: 'button-link secondary-link validation-open',
      href: `#/v/${encodeURIComponent(record.validation_id)}`,
      text: COPY.HOME_OPEN,
    }),
  ]);
}

function approvalState(documents) {
  const outstanding = documents.filter((doc) => !doc.signatures_required_met).length;
  return {
    pending: documents.length === 0,
    complete: documents.length > 0 && outstanding === 0,
    outstanding,
  };
}

function fact(term, value) {
  return [el('dt', { text: term }), el('dd', { text: value })];
}

export default renderIndex;
