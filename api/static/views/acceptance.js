/* Acceptance evidence.
 *
 * One block per metric, not one row: a row cannot carry the rationale sentence,
 * and the rationale is the audit-facing justification for the decision.
 *
 * The lower bound is the claim and is the only 34 px type in the console. The
 * observed rate is present, smaller, labelled as describing the sample. No
 * percentage appears anywhere, and no symmetric graphic: the bound is one-sided,
 * and an error bar or a ± would draw an upper limit that was never computed.
 */

import { api, cached } from '../api.js';
import { COPY } from '../copy.js';
import { consoleNote, dataRegion, defs, el, proseList, section, tok } from '../dom.js';
import { label, margin, proportion } from '../fmt.js';

export async function renderAcceptance(validationId) {
  const host = el('div');
  const root = el('div', {}, [
    el('h1', { class: 'screen-title', text: COPY.ACC_H }),
    el('p', { class: 'lede', text: COPY.ACC_LEDE }),
    host,
  ]);

  await dataRegion(host, async () => {
    const record = await cached(
      `validation:${validationId}`,
      () => api.validation(validationId),
      { now: Date.now() },
    );
    return body(record);
  }, { noun: 'the acceptance evidence' });

  return root;
}

function body(record) {
  const wrap = el('div');
  const run = record.run;

  if (!run) {
    wrap.append(el('p', { class: 'note', text: COPY.GATE_NO_RUN }));
    return wrap;
  }

  wrap.append(el('p', { class: 'note mono', text: `${run.run_id} · ${run.model}` }));

  if (!run.metrics.length) {
    wrap.append(el('p', { class: 'note', text: COPY.ACC_EMPTY }));
  }

  for (const metric of run.metrics) {
    wrap.append(metricBlock(metric));
  }

  wrap.append(calibration(run.calibration));

  wrap.append(proseList({
    heading: COPY.ACC_WARN_H,
    items: record.warnings || [],
    token: 'ADV',
    emptyCopy: COPY.GATE_WARNINGS_EMPTY,
  }));

  return wrap;
}

function metricBlock(metric) {
  const met = metric.passed;
  const block = el('article', { class: 'metric', dataset: { state: met ? 'met' : 'nmt' } });

  block.append(el('header', {}, [
    el('h2', { text: metric.name }),
    tok(metric.critical ? 'CRT' : 'ADV'),
    tok(met ? 'MET' : 'NMT'),
    el('span', { class: 'note', text: met ? 'met' : 'not met' }),
  ]));

  block.append(el('div', { class: 'claim' }, [
    el('p', { class: 'bound', text: proportion(metric.lower_bound) }),
    el('div', {}, [
      el('p', { class: 'bound-label', text: 'One-sided 95% lower confidence bound' }),
      el('p', { class: 'against', text: 'against target' }),
    ]),
    el('p', { class: 'target', text: proportion(metric.target) }),
  ]));

  const difference = metric.lower_bound - metric.target;
  block.append(defs([
    ['Observed in this sample', proportion(metric.point_estimate)],
    ['Cases meeting the criterion', `${metric.k} of ${metric.n}`],
    ['Denominator', COPY.ACC_DENOM(metric.n, metric.errors)],
    ['Method', label('BoundMethod', metric.method).text],
    ['Confidence', `${proportion(metric.confidence)}, one-sided`],
    ['Bound − target', el('span', {}, [
      margin(difference),
      el('span', { class: 'note', text: ` — ${COPY.ACC_MARGIN_NOTE}` }),
    ])],
  ]));

  block.append(el('div', { class: 'rationale' }, [
    el('p', { class: 'server', text: metric.rationale }),
  ]));
  block.append(consoleNote(COPY.ACC_ONE_SIDED));
  return block;
}

function calibration(cal) {
  if (!cal) {
    return section(COPY.ACC_CAL_H, el('p', { class: 'note', text: COPY.ACC_CAL_EMPTY }));
  }
  return section(COPY.ACC_CAL_H,
    el('p', { class: 'note', text: COPY.ACC_CAL_INTRO }),
    el('div', { class: 'metric', dataset: { state: cal.passed ? 'met' : 'nmt' } }, [
      el('header', {}, [
        el('h2', { text: 'Cohen’s kappa' }),
        tok(cal.passed ? 'MET' : 'NMT'),
        el('span', { class: 'note', text: cal.passed ? 'met' : 'not met' }),
      ]),
      el('div', { class: 'claim' }, [
        el('p', { class: 'bound', text: proportion(cal.cohen_kappa) }),
        el('div', {}, [
          el('p', { class: 'bound-label', text: 'Agreement corrected for chance' }),
          el('p', { class: 'against', text: 'against required minimum' }),
        ]),
        el('p', { class: 'target', text: proportion(cal.min_required) }),
      ]),
      defs([
        ['Raw agreement', proportion(cal.agreement)],
        ['Labelled cases', String(cal.n)],
      ]),
    ]),
  );
}

export default renderAcceptance;
