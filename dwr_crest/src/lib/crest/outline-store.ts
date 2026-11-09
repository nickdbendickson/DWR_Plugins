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
/** Shared with batch-store.ts: version 2 adds its `batch-rows` store. Both open at this version and create both
 *  stores, so whichever opens first upgrades the database for the other (a lower version would fail to open). */
export const DESK_DB_VERSION = 2;
export const DESK_DB_STORES = ["outlines", "batch-rows"] as const;

let opening: Promise<IDBDatabase | null> | null = null;

function db(): Promise<IDBDatabase | null> {
  if (opening) return opening;
  opening = new Promise((resolve) => {
    try {
      const idb = (globalThis as { indexedDB?: IDBFactory }).indexedDB;
      if (!idb) return resolve(null);
      const req = idb.open(DB, DESK_DB_VERSION);
      req.onupgradeneeded = () => {
        for (const name of DESK_DB_STORES) if (!req.result.objectStoreNames.contains(name)) req.result.createObjectStore(name);
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

type TxResult<T> = { ok: true; value: T | undefined } | { ok: false; error: string };

/**
 * One transaction. Never rejects: a missing IndexedDB, a failed open, an aborted transaction (quota, a closed
 * database, a private window) all resolve { ok: false, error } so the caller can say what did NOT happen; the old code
 * resolved undefined for every one of those and the desk went on saying "saved with this dam".
 */
function tx<T>(mode: IDBTransactionMode, run: (s: IDBObjectStore) => IDBRequest<T> | void): Promise<TxResult<T>> {
  return db().then(
    (d) =>
      new Promise<TxResult<T>>((resolve) => {
        if (!d) return resolve({ ok: false, error: "IndexedDB is not available in this browser" });
        try {
          const t = d.transaction(STORE, mode);
          const r = run(t.objectStore(STORE));
          const err = () => resolve({ ok: false, error: t.error?.message ?? r?.error?.message ?? "the IndexedDB transaction failed" });
          t.oncomplete = () => resolve({ ok: true, value: r ? r.result : undefined });
          t.onerror = err;
          t.onabort = err;
        } catch (e) {
          resolve({ ok: false, error: e instanceof Error ? e.message : String(e) });
        }
      }),
  );
}

/** Every kept layer, by dam id. Empty when IndexedDB is unavailable. */
export async function loadOutlines(): Promise<Map<string, OutlineCache>> {
  const out = new Map<string, OutlineCache>();
  const keys = await tx<IDBValidKey[]>("readonly", (s) => s.getAllKeys());
  const vals = await tx<OutlineCache[]>("readonly", (s) => s.getAll());
  if (!keys.ok || !vals.ok) {
    const error = !keys.ok ? keys.error : !vals.ok ? vals.error : "";
    // Node and a locked-down browser land here on every load; say so once, quietly, and run without saved layers.
    if (!/not available/.test(error)) console.warn(`Crest: the saved inundation layers could not be read (${error}).`);
    return out;
  }
  const k = keys.value ?? [];
  const v = vals.value ?? [];
  if (k.length === v.length) k.forEach((key, i) => out.set(String(key), v[i]));
  return out;
}

/** Why the last save or delete failed (for the desk's note), by dam id; cleared by the next success. */
const failures = new Map<string, string>();
const listeners = new Set<() => void>();

/** The storage error for a dam's layer, or null when its last write succeeded (or none was tried). */
export function outlineSaveError(id: string): string | null {
  return failures.get(id) ?? null;
}

/** Subscribe to save-failure changes (useSyncExternalStore). */
export function onOutlineSaveChange(fn: () => void): () => void {
  listeners.add(fn);
  return () => void listeners.delete(fn);
}

function setFailure(id: string, error: string | null) {
  const had = failures.get(id) ?? null;
  if (error) failures.set(id, error);
  else failures.delete(id);
  if (had !== error) for (const fn of listeners) fn();
}

/** Write a dam's layer. Resolves false, logs a warning and records the error (outlineSaveError) when it was NOT saved. */
export async function saveOutline(id: string, cache: OutlineCache): Promise<boolean> {
  const r = await tx("readwrite", (s) => s.put(cache, id));
  if (!r.ok) console.warn(`Crest: the inundation layer of ${id} was not saved (${r.error}). It stays on the desk for this visit only.`);
  setFailure(id, r.ok ? null : r.error);
  return r.ok;
}

export async function deleteOutline(id: string): Promise<boolean> {
  const r = await tx("readwrite", (s) => s.delete(id));
  if (!r.ok) console.warn(`Crest: the saved inundation layer of ${id} could not be removed (${r.error}).`);
  setFailure(id, null);
  return r.ok;
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
  io: { save: (id: string, cache: OutlineCache) => Promise<unknown>; remove: (id: string) => Promise<unknown> } = { save: saveOutline, remove: deleteOutline },
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
