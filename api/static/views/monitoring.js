/* Drift and change control.
 *
 * No control chart is drawn. Drawing one would mean a second, unqualified
 * implementation of a regulated SPC computation that already exists in
 * valkit.drift.spc, and two implementations of a regulated calculation is one
 * too many. The observed values are listed as recorded; the control rules are
 * evaluated server-side and their violations are reported verbatim.
 */

import { api } from '../api.js';
import { COPY } from '../copy.js';
import { consoleNote, dataRegion, el, ledger, proseList, section } from '../dom.js';
import { label, proportion } from '../fmt.js';

export async function renderMonitoring(agentId) {
  const drift = el('div');
  const changes = el('div');

  const root = el('div', {}, [
    el('h1', { class: 'screen-title', text: COPY.MON_H }),
    el('p', { class: 'note mono', text: agentId }),
    el('p', { class: 'lede', text: COPY.MON_LEDE }),
    section('Observations', drift),
    section(COPY.MON_CC_H, changes),
  ]);

  await Promise.all([
    dataRegion(drift, async () => {
      const payload = await api.drift(agentId, 20);
      const wrap = el('div');

      if (!payload.points.length) {
        wrap.append(el('p', { class: 'note', text: COPY.MON_EMPTY }));
      } else {
        wrap.append(ledger({
          caption: 'Observed values, most recent last',
          enumerate: true,
          columns: [
            { label: 'Metric' },
            { label: 'Observed (UTC)' },
            { label: 'Value', numeric: true },
            { label: 'Cases', numeric: true },
            { label: 'Run' },
          ],
          rows: payload.points.map((p) => [
            p.metric,
            el('span', { class: 'mono', text: p.observed_at }),
            proportion(p.value),
            String(p.n),
            el('span', { class: 'id', text: p.run_id || '' }),
          ]),
        }));
      }
      wrap.append(consoleNote(COPY.MON_NO_CHART));

      wrap.append(proseList({
        heading: 'Control rules tripped',
        items: payload.violations.map((v) => v.description),
        token: 'OBL',
        emptyCopy: 'None. No control rule has tripped in the window shown.',
        notesFor: (description) => {
          const violation = payload.violations.find((v) => v.description === description);
          return violation
            ? `Console note. ${violation.metric} · rule ${violation.rule} · severity ` +
              `${label('AlertSeverity', violation.severity).text} · observed ${proportion(violation.value)}.`
            : null;
        },
      }));
      return wrap;
    }, { noun: 'the drift record' }),

    dataRegion(changes, async () => {
      const records = await api.changeControls(agentId);
      if (!records.length) return el('p', { class: 'note', text: COPY.MON_CC_EMPTY });
      const wrap = el('div');
      for (const record of records) {
        wrap.append(el('div', { class: 'card' }, [
          el('h3', {}, [el('span', { class: 'id', text: record.cc_id })]),
          el('p', {
            class: 'meta',
            text: `${label('ChangeControlStatus', record.status).text} · ` +
              `${label('ChangeTrigger', record.trigger).text} · opened ${record.opened_at} (UTC)`,
          }),
          el('p', { class: 'server', text: record.reason }),
          el('p', { class: 'server', text: record.impact }),
          el('p', {
            class: 'note',
            text: `Required before validated status returns: ${record.required_scope.join(', ') || 'none'}`,
          }),
        ]));
      }
      return wrap;
    }, { noun: 'the change controls' }),
  ]);

  return root;
}

export default renderMonitoring;
