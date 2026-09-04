/* The guided validation launch.
 *
 * valkit.yaml remains the source of truth, but it now sits in a workflow that
 * explains what the file controls and what will happen when a qualification is
 * run. The screen still performs exactly the same two append-only writes.
 */

import { api, invalidate } from '../api.js';
import { COPY } from '../copy.js';
import {
  announce, consoleNote, dataRegion, defs, digest, el, errorBlock, proseList,
} from '../dom.js';
import { afterWrite, identity } from '../session.js';

export async function renderSpec({ loadExample = false } = {}) {
  const root = el('div', { class: 'launch-workspace' });
  const who = identity.value();

  if (!who) {
    root.append(identityGate(loadExample));
    return root;
  }

  const editor = el('textarea', { id: 'spec-yaml', spellcheck: 'false' });
  const outcome = el('div', { class: 'launch-outcome' });
  const runHost = el('div', { class: 'launch-run-result' });

  const useExample = el('button', {
    type: 'button',
    text: COPY.LAUNCH_USE_EXAMPLE,
    onclick: async () => {
      try {
        editor.value = await api.exampleSpec();
        announce('The example validation source is ready to review.');
      } catch (err) {
        outcome.replaceChildren(errorBlock(err));
      }
    },
  });

  const createPlan = el('button', {
    type: 'button',
    class: 'primary',
    text: COPY.LAUNCH_CREATE_PLAN,
    onclick: async () => {
      outcome.replaceChildren(el('p', {
        class: 'pending', role: 'status', text: 'Creating the validation plan…',
      }));
      try {
        const summary = await api.ingestSpec(editor.value, who);
        outcome.replaceChildren(summaryBlock(summary, who, runHost));
        await afterWrite();
      } catch (err) {
        outcome.replaceChildren(errorBlock(err));
      }
    },
  });

  root.append(launchHero(), el('div', { class: 'launch-layout' }, [
    el('section', { class: 'launch-source' }, [
      el('p', { class: 'eyebrow', text: COPY.LAUNCH_SOURCE_EYEBROW }),
      el('h2', { text: COPY.LAUNCH_SOURCE_H }),
      el('p', { class: 'section-lede', text: COPY.LAUNCH_SOURCE_LEDE }),
      el('div', { class: 'field' }, [
        el('label', { for: 'spec-yaml', text: 'valkit.yaml' }),
        editor,
      ]),
      el('div', { class: 'form-row launch-actions' }, [useExample, createPlan]),
      outcome,
    ]),
    el('aside', { class: 'launch-next', 'aria-label': COPY.LAUNCH_NEXT_EYEBROW }, [
      el('p', { class: 'eyebrow', text: COPY.LAUNCH_NEXT_EYEBROW }),
      el('ol', { class: 'next-steps' }, COPY.LAUNCH_NEXT.map(([heading, body], index) =>
        el('li', {}, [
          el('span', { class: 'next-index', text: `0${index + 1}` }),
          el('div', {}, [el('h3', { text: heading }), el('p', { text: body })]),
        ]))),
      el('p', { class: 'note', text: COPY.LAUNCH_RUN_NOTE }),
    ]),
  ]), runHost);

  const existing = el('div');
  root.append(el('section', { class: 'ready-plans' }, [
    el('div', { class: 'section-heading' }, [
      el('div', {}, [
        el('p', { class: 'eyebrow', text: 'Existing plans' }),
        el('h2', { text: COPY.LAUNCH_READY_H }),
      ]),
    ]),
    existing,
  ]));

  await dataRegion(existing, async () => {
    const refs = await api.listSpecs();
    if (!refs.length) return el('p', { class: 'note', text: COPY.LAUNCH_READY_EMPTY });
    return el('div', { class: 'plan-list' }, refs.map((ref) => planRow(ref, who, runHost)));
  }, { noun: 'the validation plans' });

  if (loadExample) {
    try {
      editor.value = await api.exampleSpec();
      announce('The example validation source is ready to review.');
    } catch (err) {
      outcome.replaceChildren(errorBlock(err));
    }
  }

  return root;
}

function launchHero() {
  return el('section', { class: 'launch-hero' }, [
    el('p', { class: 'eyebrow', text: COPY.LAUNCH_EYEBROW }),
    el('h1', { text: COPY.LAUNCH_H }),
    el('p', { class: 'hero-lede', text: COPY.LAUNCH_LEDE }),
  ]);
}

function identityGate(loadExample) {
  return el('section', { class: 'identity-gate', 'data-needs-identity': 'true' }, [
    el('p', { class: 'eyebrow', text: COPY.LAUNCH_IDENTITY_EYEBROW }),
    el('h1', { text: COPY.LAUNCH_IDENTITY_H }),
    el('p', { class: 'hero-lede', text: COPY.LAUNCH_IDENTITY_LEDE }),
    el('button', {
      type: 'button',
      class: 'primary',
      text: COPY.LAUNCH_IDENTITY_ACTION,
      onclick: () => {
        if (!identity.value()) {
          announce(COPY.LAUNCH_IDENTITY_MISSING);
          document.getElementById('actor-input')?.focus();
          return;
        }
        location.hash = loadExample ? '#/spec?setup=1&example=1' : '#/spec?setup=1';
      },
    }),
  ]);
}

function summaryBlock(summary, who, runHost) {
  const wrap = el('section', { class: 'preflight-panel' }, [
    el('p', { class: 'eyebrow', text: COPY.LAUNCH_PLAN_EYEBROW }),
    el('h3', { text: `${summary.agent_id} v${summary.version}` }),
  ]);
  wrap.append(defs([
    ['GAMP category', `Category ${summary.gamp_category}`],
    ['Risk class', summary.risk_class],
    ['Requirements derived', String(summary.requirements)],
    ['Risks assessed', String(summary.risks)],
    ['Qualification tests', String(summary.tests)],
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
  wrap.append(el('div', { class: 'preflight-actions' }, [runButton(summary.ref, who, runHost)]));
  return wrap;
}

function planRow(specRef, who, runHost) {
  return el('article', { class: 'plan-row' }, [
    el('div', {}, [
      el('p', { class: 'id', text: specRef }),
      el('p', { class: 'note', text: 'A validation source is ready for qualification.' }),
    ]),
    runButton(specRef, who, runHost),
  ]);
}

function runButton(specRef, who, runHost) {
  return el('button', {
    type: 'button',
    class: 'primary',
    text: COPY.LAUNCH_RUN,
    onclick: async (event) => {
      event.target.disabled = true;
      event.target.textContent = COPY.LAUNCH_RUNNING;
      runHost.replaceChildren(el('p', {
        class: 'pending', role: 'status', text: COPY.LAUNCH_RUNNING,
      }));
      try {
        const validation = await api.startValidation(specRef, who);
        invalidate('validation:');
        runHost.replaceChildren(el('section', { class: 'run-complete' }, [
          el('p', { class: 'eyebrow', text: COPY.LAUNCH_RESULT_EYEBROW }),
          el('h2', { text: COPY.LAUNCH_RESULT_H }),
          el('p', { class: 'section-lede', text: COPY.LAUNCH_RESULT_LEDE(validation.documents.length) }),
          el('a', {
            class: 'button-link primary-link',
            href: `#/v/${encodeURIComponent(validation.validation_id)}`,
            text: COPY.HOME_OPEN,
          }),
        ]));
        await afterWrite();
      } catch (err) {
        runHost.replaceChildren(errorBlock(err));
      } finally {
        event.target.disabled = false;
        event.target.textContent = COPY.LAUNCH_RUN;
      }
    },
  });
}

export default renderSpec;
