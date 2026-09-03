/* Per-tab session state, and the hooks the shell registers on it.
 *
 * Separate from app.js so that a view never imports the shell. app.js imports
 * every view; a view importing app.js back would be a cycle, which ES modules
 * tolerate and a single-file bundle of them does not.
 *
 * Nothing here ever holds a credential. The identity is the acting-as value —
 * the X-ValKit-Actor header on every POST and the identification code a
 * signature is claimed for — and the read record is which documents this tab
 * has opened. Both live in sessionStorage, not localStorage: on a shared
 * validation workstation a persisted identity misattributes the next person's
 * work in the audit trail.
 */

const ACTOR_KEY = 'valkit.actor';

export const hooks = {
  /** Re-verify integrity and re-render the shell after a write. Set by app.js. */
  afterWrite: async () => {},
  /** Re-render the identity block. Set by app.js. */
  identityChanged: () => {},
};

export const identity = {
  get() {
    try {
      const raw = sessionStorage.getItem(ACTOR_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch {
      return null;
    }
  },
  set(value) {
    try {
      if (!value) sessionStorage.removeItem(ACTOR_KEY);
      else sessionStorage.setItem(ACTOR_KEY, JSON.stringify({
        value,
        set_at: new Date().toISOString().replace(/\.\d+Z$/, 'Z'),
      }));
    } catch { /* a browser with storage disabled still works, read-only */ }
    hooks.identityChanged();
  },
  value() {
    return this.get()?.value || '';
  },
};

export const opened = {
  key: (validationId) => `valkit.opened.${validationId}`,
  record(validationId, docId) {
    try {
      const all = this.all(validationId);
      all[docId] = new Date().toISOString();
      sessionStorage.setItem(this.key(validationId), JSON.stringify(all));
    } catch { /* storage disabled: the queue simply stays locked */ }
  },
  all(validationId) {
    try {
      return JSON.parse(sessionStorage.getItem(this.key(validationId)) || '{}');
    } catch {
      return {};
    }
  },
};

export async function afterWrite() {
  return hooks.afterWrite();
}
