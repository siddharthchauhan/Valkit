/* Specification and run — the only authoring screen.
 *
 * It writes to the audit trail, so it says up front that it needs an identity.
 * Running the battery generates the package and stops short of signing: a
 * pipeline that signed on the author's behalf would defeat the purpose of
 * requiring a signature, and the console does not paper over that.
 */

import { api, invalidate } from '../api.js';
import { COPY } from '../copy.js';
import { consoleNote, dataRegion, defs, digest, el, errorBlock, proseList, section } from '../dom.js';
import { afterWrite, identity } from '../app.js';

export async function renderSpec() {
  const root = el('div', {}, [
    el('h1', { class: 'screen-title', text: COPY.SPEC_H }),
    el('p', { class: 'lede', text: COPY.SPEC_LEDE }),
  ]);

  const who = identity.value();
  if (!who) {
    root.append(el('p', { class: 'note', text: COPY.SPEC_NEEDS_IDENTITY }));
    return root;
  }

  const editor = el('textarea', { id: 'spec-yaml', spellcheck: 'false' });
  const outcome = el('div');
  const runHost = el('div');

  const loadExample = el('button', {
    type: 'button',
    text: 'Load the example',
    onclick: async () => {
      try {
        editor.value = await api.exampleSpec();
      } catch (err) {
        outcome.replaceChildren(errorBlock(err));
      }
    },
  });

  const ingest = el('button', {
    type: 'button',
    class: 'primary',
    text: 'Ingest specification',
    onclick: async () => {
      outcome.replaceChildren(el('p', { class: 'pending', role: 'status', text: 'Ingesting.' }));
      try {
        const summary = await api.ingestSpec(editor.value, who);
        outcome.replaceChildren(summaryBlock(summary, who, runHost));
        await afterWrite();
      } catch (err) {
        outcome.replaceChildren(errorBlock(err));
      }
    },
  });

  root.append(section('Specification',
    el('div', { class: 'field' }, [
      el('label', { for: 'spec-yaml', text: 'valkit.yaml' }),
      editor,
    ]),
    el('div', { class: 'form-row' }, [loadExample, ingest]),
    outcome,
  ));
  root.append(section('Run', runHost));

  const existing = el('div');
  root.append(section('Already ingested', existing));
  await dataRegion(existing, async () => {
    const refs = await api.listSpecs();
    if (!refs.length) return el('p', { class: 'note', text: COPY.IDX_SPEC_EMPTY });
    const list = el('div', { class: 'cards' });
    for (const ref of refs) list.append(el('div', { class: 'card' }, [
      el('h3', { class: 'id', text: ref }),
      el('p', {}, [runButton(ref, who, runHost)]),
    ]));
    return list;
  }, { noun: 'the specifications' });

  return root;
}

function summaryBlock(summary, who, runHost) {
  const wrap = el('div');
  wrap.append(defs([
    ['Agent', `${summary.agent_id} v${summary.version}`],
    ['GAMP category', `Category ${summary.gamp_category}`],
    ['Risk class', summary.risk_class],
    ['Requirements', String(summary.requirements)],
    ['Risks', String(summary.risks)],
    ['Test cases', String(summary.tests)],
    ['Specification digest', digest(summary.spec_sha256)],
  ]));

  if (summary.risk_class !== summary.derived_risk_class) {
    wrap.append(consoleNote(COPY.SPEC_ESCALATED(summary.derived_risk_class, summary.risk_class)));
  }

  wrap.append(proseList({
    heading: COPY.SPEC_WARN_H,
    intro: COPY.SPEC_WARN_INTRO,
    items: summary.warnings || [],
    token: 'ADV',
    emptyCopy: 'None.',
  }));

  wrap.append(el('p', {}, [runButton(summary.ref, who, runHost)]));
  return wrap;
}

function runButton(specRef, who, runHost) {
  return el('button', {
    type: 'button',
    text: `Run the battery for ${specRef}`,
    onclick: async (event) => {
      event.target.disabled = true;
      event.target.textContent = 'Running the battery.';
      runHost.replaceChildren(el('p', { class: 'pending', role: 'status', text: 'Running.' }));
      try {
        const validation = await api.startValidation(specRef, who);
        invalidate('validation:');
        runHost.replaceChildren(el('div', {}, [
          el('p', { class: 'note', text: COPY.SPEC_RUN_NOTE }),
          el('p', {}, [
            'Opened ',
            el('a', {
              href: `#/v/${encodeURIComponent(validation.validation_id)}`,
              text: validation.validation_id,
            }),
            ` — ${validation.readiness.ready ? 'validated' : 'in validation'}, ` +
            `${validation.documents.length} documents generated.`,
          ]),
        ]));
        await afterWrite();
      } catch (err) {
        runHost.replaceChildren(errorBlock(err));
      } finally {
        event.target.disabled = false;
        event.target.textContent = `Run the battery for ${specRef}`;
      }
    },
  });
}

export default renderSpec;
