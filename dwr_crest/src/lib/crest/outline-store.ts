/**
 * Where a dam's built inundation layer is kept between visits.
 *
 * The desk state persists in localStorage (store.ts, key crest-desk-v2), which browsers cap at about 5 MB per site
 * (Safari counts it strictly). A routed terrain flood is a big GeoJSON: DWR-2801 / ND00199 alone is 5–6 MB, so saving
 * it there threw a QuotaExceededError out of the desk's update (Nick, 2026-11-05: "The quota has been exceeded." on
 * the Terrain layer step) and nothing was saved. So the layers live in IndexedDB instead (hundreds of MB), one record
 * per dam, and localStorage keeps only the light records. Where IndexedDB is missing (Node, a locked-down browser) the
 * layer simply isn't kept between visits; the desk still works.
 */
import type { OutlineCache } from "./outline.ts";
import type { DamRecord } from "./types.ts";

const DB = "crest-desk";
const STORE = "outlines";

let opening: Promise<IDBDatabase | null> | null = null;

function db(): Promise<IDBDatabase | null> {
  if (opening) return opening;
  opening = new Promise((resolve) => {
    try {
      const idb = (globalThis as { indexedDB?: IDBFactory }).indexedDB;
      if (!idb) return resolve(null);
      const req = idb.open(DB, 1);
      req.onupgradeneeded = () => {
        if (!req.result.objectStoreNames.contains(STORE)) req.result.createObjectStore(STORE);
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => resolve(null);
      req.onblocked = () => resolve(null);
    } catch {
      resolve(null);
    }
  });
  return opening;
}

function tx<T>(mode: IDBTransactionMode, run: (s: IDBObjectStore) => IDBRequest<T> | void): Promise<T | undefined> {
  return db().then(
    (d) =>
      new Promise<T | undefined>((resolve) => {
        if (!d) return resolve(undefined);
        try {
          const t = d.transaction(STORE, mode);
          const r = run(t.objectStore(STORE));
          t.oncomplete = () => resolve(r ? r.result : undefined);
          t.onerror = () => resolve(undefined);
          t.onabort = () => resolve(undefined);
        } catch {
          resolve(undefined);
        }
      }),
  );
}

/** Every kept layer, by dam id. Empty when IndexedDB is unavailable. */
export async function loadOutlines(): Promise<Map<string, OutlineCache>> {
  const out = new Map<string, OutlineCache>();
  const keys = await tx<IDBValidKey[]>("readonly", (s) => s.getAllKeys());
  const vals = await tx<OutlineCache[]>("readonly", (s) => s.getAll());
  if (keys && vals && keys.length === vals.length) keys.forEach((k, i) => out.set(String(k), vals[i]));
  return out;
}

export function saveOutline(id: string, cache: OutlineCache): Promise<unknown> {
  return tx("readwrite", (s) => s.put(cache, id));
}

export function deleteOutline(id: string): Promise<unknown> {
  return tx("readwrite", (s) => s.delete(id));
}

/** A dam record without its inundation layer: what goes to localStorage. */
export function withoutOutline(d: DamRecord): DamRecord {
  if (!d.outline) return d;
  const rest = { ...d };
  delete rest.outline;
  return rest;
}

/**
 * Keep IndexedDB in step with the desk's dams: write a layer when it is built or rebuilt, delete it when it is removed
 * (or its dam is gone). `saved` remembers which layer object each dam's record holds now, so an unchanged layer is not
 * written again on every keystroke.
 */
export function outlineSync(
  io: { save: typeof saveOutline; remove: typeof deleteOutline } = { save: saveOutline, remove: deleteOutline },
  /** Layers IndexedDB already holds, by dam id (not written again). */
  seed?: ReadonlyMap<string, OutlineCache>,
) {
  const saved = new Map<string, OutlineCache>(seed ?? []);
  return (dams: readonly DamRecord[]) => {
    const seen = new Set<string>();
    for (const d of dams) {
      seen.add(d.id);
      const prev = saved.get(d.id);
      if (d.outline === prev) continue;
      if (d.outline) {
        saved.set(d.id, d.outline);
        void io.save(d.id, d.outline);
      } else {
        saved.delete(d.id);
        void io.remove(d.id);
      }
    }
    for (const id of [...saved.keys()])
      if (!seen.has(id)) {
        saved.delete(id);
        void io.remove(id);
      }
  };
}
