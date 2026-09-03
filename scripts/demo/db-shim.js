/* A local stand-in for the artifact `db` capability, for the test harness ONLY.
 *
 * It is never bundled into the page. The harness injects it before the page's
 * own scripts so that the persistent code path — leases, segments, replay —
 * runs under Chromium here, against localStorage, before the page is published
 * to a store the harness cannot log into.
 *
 * It implements the subset of the contract the demo uses: doc/collection,
 * get/set/update/delete, acquire, orderBy/limit/where, get on a query, and
 * onSnapshot with a coarse poll. Last-writer-wins, like the real thing.
 */

(function installDbShim() {
  const KEY = 'valkit-db-shim';
  const load = () => JSON.parse(localStorage.getItem(KEY) || '{}');
  const save = (all) => localStorage.setItem(KEY, JSON.stringify(all));
  const leases = new Map();

  const snapshotOf = (path, body) => ({
    id: path.split('/').pop(),
    exists: body !== undefined,
    data: () => (body === undefined ? undefined : JSON.parse(JSON.stringify(body))),
    metadata: { fromCache: false, hasPendingWrites: false },
  });

  function docRef(path) {
    if (path.split('/').length % 2 !== 0) throw new TypeError(`document path needs an even number of segments: ${path}`);
    return {
      id: path.split('/').pop(),
      path,
      async get() { return snapshotOf(path, load()[path]); },
      async set(data) { const all = load(); all[path] = JSON.parse(JSON.stringify(data)); save(all); },
      async update(data) {
        const all = load();
        if (!all[path]) throw { code: 'invalid_argument', message: 'update requires the document to exist' };
        all[path] = { ...all[path], ...JSON.parse(JSON.stringify(data)) }; save(all);
      },
      async delete() { const all = load(); delete all[path]; save(all); },
      async acquire({ holder, ttlMs = 30000 }) {
        const now = Date.now();
        const held = leases.get(path);
        if (held && held.until > now && held.holder !== holder) {
          return { acquired: false, expiresAt: new Date(held.until).toISOString() };
        }
        const until = now + Math.min(Math.max(ttlMs || 30000, 1000), 600000);
        leases.set(path, { holder, until });
        return { acquired: true, holder, expiresAt: new Date(until).toISOString(), version: 1 };
      },
      onSnapshot(next) {
        let last = JSON.stringify(load()[path]);
        next(snapshotOf(path, load()[path]));
        const timer = setInterval(() => {
          const cur = JSON.stringify(load()[path]);
          if (cur !== last) { last = cur; next(snapshotOf(path, load()[path])); }
        }, 500);
        return () => clearInterval(timer);
      },
      collection(sub) { return collectionRef(`${path}/${sub}`); },
    };
  }

  function collectionRef(path, filters = [], order = null, max = null) {
    if (path.split('/').length % 2 !== 1) throw new TypeError(`collection path needs an odd number of segments: ${path}`);
    const matching = () => {
      const all = load();
      let docs = Object.entries(all)
        .filter(([p]) => p.startsWith(`${path}/`) && p.slice(path.length + 1).split('/').length === 1)
        .map(([p, body]) => ({ path: p, body }));
      for (const [field, op, value] of filters) {
        docs = docs.filter(({ body }) => {
          const v = body[field];
          return op === '==' ? v === value : op === '!=' ? v !== value
            : op === '<' ? v < value : op === '<=' ? v <= value
            : op === '>' ? v > value : op === '>=' ? v >= value : true;
        });
      }
      if (order) {
        docs.sort((a, b) => (a.body[order.field] > b.body[order.field] ? 1 : a.body[order.field] < b.body[order.field] ? -1 : 0));
        if (order.dir === 'desc') docs.reverse();
      } else {
        docs.sort((a, b) => (a.path > b.path ? 1 : -1));
      }
      if (max) docs = docs.slice(0, max);
      return docs;
    };
    const querySnap = () => {
      const docs = matching().map(({ path: p, body }) => snapshotOf(p, body));
      return { docs, size: docs.length, empty: docs.length === 0, docChanges: () => [],
        metadata: { fromCache: false, hasPendingWrites: false } };
    };
    return {
      path,
      doc(id) { return docRef(`${path}/${id || Math.random().toString(36).slice(2, 12)}`); },
      async add(data) { const ref = this.doc(); await ref.set(data); return ref; },
      where(field, op, value) { return collectionRef(path, [...filters, [field, op, value]], order, max); },
      orderBy(field, dir = 'asc') { return collectionRef(path, filters, { field, dir }, max); },
      limit(n) { return collectionRef(path, filters, order, n); },
      async get() { return querySnap(); },
      onSnapshot(next) {
        let last = JSON.stringify(matching());
        next(querySnap());
        const timer = setInterval(() => {
          const cur = JSON.stringify(matching());
          if (cur !== last) { last = cur; next(querySnap()); }
        }, 500);
        return () => clearInterval(timer);
      },
    };
  }

  const db = Object.freeze({ doc: docRef, collection: (p) => collectionRef(p) });
  globalThis.claude = {
    use: async (name) => {
      // Resolve later, as the real one does — never in the first synchronous run.
      await new Promise((r) => setTimeout(r, 30));
      return name === 'db' ? db : null;
    },
  };
})();
