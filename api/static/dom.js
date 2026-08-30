/* Element helpers and the shared components.
 *
 * Everything here builds DOM nodes rather than HTML strings. Server-authored
 * prose reaches the page through `textContent` and never through `innerHTML`,
 * so a specification, a requirement or an error body cannot inject markup.
 */

import { COPY } from './copy.js';
import { digestWidth, isDigest, label, plural } from './fmt.js';

export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
    else node.setAttribute(key, value === true ? '' : String(value));
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

export const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); };

/** A state token: real text, colour-independent, with an accessible expansion. */
const TOKEN_MEANING = {
  BLK: 'blocking the validated gate',
  OBL: 'outstanding obligation',
  OK: 'satisfied',
  MET: 'acceptance criterion met',
  NMT: 'acceptance criterion not met',
  CRT: 'critical acceptance criterion',
  ADV: 'advisory',
  VER: 'verified',
  NVF: 'not verified',
  NEX: 'not executed',
  NOT: 'no test',
  APC: 'approvals complete',
  APO: 'approvals outstanding',
  SYS: 'attributed to the system',
  PSN: 'attributed to a person',
  INT: 'integrity failure',
};

export function tok(name) {
  return el('span', { class: 'tok', dataset: { t: name } }, [
    name,
    el('span', { class: 'vh', text: ` — ${TOKEN_MEANING[name] || name}` }),
  ]);
}

/** Prose the API wrote, rendered verbatim in the record's typeface. */
export const server = (text) => el('p', { class: 'server', text });

export const consoleNote = (text) => el('p', { class: 'console-note', text });

/** A digest: markable, resolvable, full in print, never computed here. */
export function digest(value, width = 12) {
  if (!isDigest(value)) return el('span', { class: 'mono', text: String(value) });
  const wrap = el('span', { class: 'digest', dataset: { digest: value } });
  const button = el('button', {
    type: 'button',
    class: 'digest-mark',
    'aria-pressed': 'false',
    'aria-label': `Mark every occurrence of digest ${value.slice(0, 12)}`,
    onclick: () => markDigest(value),
  }, [
    el('span', { class: 'short', text: `${value.slice(0, width)}…` }),
    el('span', { class: 'full', text: value }),
  ]);
  wrap.append(button, el('a', {
    class: 'digest-open',
    href: `#/digest/${value}`,
    'aria-label': `Resolve digest ${value.slice(0, 12)}`,
    text: '↗',
  }));
  return wrap;
}

function markDigest(value) {
  const nodes = [...document.querySelectorAll(`.digest[data-digest="${value}"]`)];
  const on = !nodes.some((n) => n.classList.contains('marked'));
  document.querySelectorAll('.digest.marked').forEach((n) => {
    n.classList.remove('marked');
    n.querySelector('.digest-mark')?.setAttribute('aria-pressed', 'false');
  });
  if (on) {
    nodes.forEach((n) => {
      n.classList.add('marked');
      n.querySelector('.digest-mark')?.setAttribute('aria-pressed', 'true');
    });
    announce(`Marked ${nodes.length} ${plural(nodes.length, 'occurrence', 'occurrences')} of this digest.`);
  } else {
    announce('Digest marking cleared.');
  }
}

export function announce(text) {
  const region = document.getElementById('announce');
  if (region) region.textContent = text;
}

/** Apply the auto-lengthening rule across every digest a screen will render. */
export function widthFor(values) {
  return digestWidth(values.filter(isDigest));
}

/** A list of server-authored statements under a permanent heading.
 *
 * The heading and intro are always rendered. An absent heading looks like an
 * absent question, and the three gate lists must never share a heading, a
 * count or a token.
 */
export function proseList({ heading, intro, items, token, emptyCopy, notesFor, remedyFor }) {
  const section = el('section', { class: 'prose-list', dataset: { token: token || '' } }, [
    el('h3', { text: heading }),
    intro ? el('p', { class: 'intro', text: intro }) : null,
  ]);

  if (!items.length) {
    section.append(el('p', { class: 'empty', text: emptyCopy || COPY.NONE }));
    return section;
  }

  const list = el('ol', { class: 'items' });
  for (const item of items) {
    const remedy = remedyFor ? remedyFor(item) : null;
    const escalated = remedy && remedy.escalated;
    const body = el('div', {}, [server(item)]);
    const note = notesFor ? notesFor(item) : null;
    if (note) body.append(consoleNote(note));
    if (remedy) {
      body.append(el('p', { class: 'remedy' }, [
        remedy.href
          ? el('a', { href: remedy.href, text: remedy.text })
          : el('span', { class: 'note', text: remedy.text }),
      ]));
    }
    list.append(el('li', { dataset: escalated ? { escalated: 'true' } : {} }, [
      tok(escalated ? 'INT' : token),
      body,
    ]));
  }
  section.append(list);
  return section;
}

/** A table inside a scroller, with the labels the narrow and print reflows use. */
export function ledger({ caption, columns, rows, enumerate = false }) {
  const table = el('table');
  table.append(el('caption', { class: 'vh', text: caption }));

  const headRow = el('tr');
  if (enumerate) headRow.append(el('th', { scope: 'col', text: '#' }));
  for (const column of columns) {
    headRow.append(el('th', {
      scope: 'col',
      class: column.numeric ? 'num' : null,
      text: column.label,
    }));
  }
  table.append(el('thead', {}, [headRow]));

  const body = el('tbody');
  rows.forEach((row, index) => {
    const tr = el('tr');
    if (enumerate) {
      tr.append(el('td', { class: 'enum', 'data-label': 'Row' },
        [`${index + 1} of ${rows.length}`]));
    }
    columns.forEach((column, columnIndex) => {
      const value = row[columnIndex];
      const cell = el(columnIndex === 0 && !enumerate ? 'th' : 'td', {
        scope: columnIndex === 0 && !enumerate ? 'row' : null,
        class: column.numeric ? 'num' : null,
        'data-label': column.label,
      });
      if (value === null || value === undefined || value === '') {
        cell.append(el('span', { class: 'note', text: 'none' }));
      } else if (value.nodeType) {
        cell.append(value);
      } else {
        cell.textContent = String(value);
      }
      tr.append(cell);
    });
    body.append(tr);
  });
  table.append(body);

  return el('div', {
    class: 'scroller',
    role: 'region',
    tabindex: '0',
    'aria-label': caption,
  }, [table]);
}

/** Renders `error` and `error_type` verbatim. Never renders `detail[].input`:
 *  for a credential field its value is the literal string ***REDACTED***, and a
 *  console that echoed it would undo the server's redaction. */
export function errorBlock(err) {
  const integrity = err && err.name === 'IntegrityFailure';
  const box = el('div', {
    class: 'error',
    role: 'alert',
    dataset: { kind: integrity ? 'integrity' : 'ordinary' },
  }, [server(err.error || err.message || COPY.REQUEST_FAILED)]);

  const meta = [err.error_type, err.path ? `path: ${err.path}` : null]
    .filter(Boolean).join(' · ');
  if (meta) box.append(el('p', { class: 'meta', text: meta }));

  if (Array.isArray(err.detail)) {
    const list = el('ul', { class: 'detail' });
    for (const entry of err.detail) {
      const loc = Array.isArray(entry.loc) ? entry.loc.join('.') : '';
      list.append(el('li', { text: `${loc} — ${entry.msg || ''}`.trim() }));
    }
    box.append(list);
  } else if (typeof err.detail === 'string') {
    box.append(el('p', { class: 'note', text: err.detail }));
  }
  return box;
}

/** Three states and no fourth. A region never renders a default, an assumed
 *  pass, or a zero it did not receive. */
export async function dataRegion(host, loader, { noun }) {
  clear(host);
  host.append(el('p', { class: 'pending', role: 'status', text: COPY.PENDING(noun) }));
  try {
    const content = await loader();
    clear(host);
    host.append(content);
  } catch (err) {
    if (err && err.name === 'IntegrityFailure') throw err;
    clear(host);
    host.append(errorBlock(err), consoleNote(COPY.REGION_FAILED(
      noun.charAt(0).toUpperCase() + noun.slice(1))));
  }
}

/** An enum rendered as prose, with the record's own value when unknown. */
export function enumText(kind, value) {
  const { text, known } = label(kind, value);
  if (known) return document.createTextNode(text);
  const span = el('span', {}, [el('span', { class: 'mono', text })]);
  span.append(consoleNote(
    'Console note. This console has no label for this value; the record’s own value is shown.'));
  return span;
}

export function section(heading, ...children) {
  return el('section', { class: 'block' }, [el('h2', { text: heading }), ...children]);
}

export function defs(pairs) {
  const list = el('dl', { class: 'defs' });
  for (const [term, value] of pairs) {
    if (value === null || value === undefined) continue;
    list.append(el('dt', { text: term }));
    const dd = el('dd');
    if (value.nodeType) dd.append(value); else dd.textContent = String(value);
    list.append(dd);
  }
  return list;
}

export default { el, clear, tok, server, consoleNote, digest, proseList, ledger, errorBlock,
  dataRegion, enumText, section, defs, announce, widthFor };
