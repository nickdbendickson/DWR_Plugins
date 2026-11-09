/**
 * Where the batch screen's result rows are kept between visits.
 *
 * They used to go to localStorage as one JSON string rewritten on EVERY row (BatchScreen saveRows): on a county of
 * 400 structures that is 400 serialisations of a growing array on the main thread, and a statewide run would hit the
 * ~5 MB quota. The rows now live in IndexedDB (the same crest-desk database the inundation layers use, outline-store.ts,
 * in their own store), written in one transaction every FLUSH_ROWS rows or FLUSH_MS, whichever comes first, and once
 * more when the batch stops. localStorage keeps only a two-number summary for the Resume banner. Where IndexedDB is
 * missing the rows are kept for the tab's life only; the CSV still works.
 *
 * Pure apart from the IndexedDB calls: the flush scheduling (BatchRowBuffer) is testable with a stand-in writer.
 */
import type { BatchRow } from "./batch.ts";
import { DESK_DB_STORES, DESK_DB_VERSION } from "./outline-store.ts";

const DB = "crest-desk";
const STORE = "batch-rows";
const SUMMARY_KEY = "crest-batch-v2-summary";

export const FLUSH_ROWS = 10;
export const FLUSH_MS = 2000;

let opening: Promise<IDBDatabase | null> | null = null;

function db(): Promise<IDBDatabase | null> {
  if (opening) return opening;
  opening = new Promise((resolve) => {
    try {
      const idb = (globalThis as { indexedDB?: IDBFactory }).indexedDB;
      if (!idb) return resolve(null);
      const req = idb.open(DB, DESK_DB_VERSION);
      req.onupgradeneeded = () => {
        // The same upgrade outline-store.ts runs: both stores, whichever module opens the database first.
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

function tx<T>(mode: IDBTransactionMode, run: (s: IDBObjectStore) => IDBRequest<T> | void): Promise<T | undefined> {
  return db().then(
    (d) =>
      new Promise<T | undefined>((resolve) => {
        if (!d || !d.objectStoreNames.contains(STORE)) return resolve(undefined);
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

/** Every kept row, by structure id. Empty when IndexedDB is unavailable. */
export async function loadBatchRows(): Promise<BatchRow[]> {
  const vals = await tx<BatchRow[]>("readonly", (s) => s.getAll());
  return Array.isArray(vals) ? vals : [];
}

/** Write these rows (one transaction), replacing any with the same id. */
export function putBatchRows(rows: readonly BatchRow[]): Promise<unknown> {
  if (rows.length === 0) return Promise.resolve();
  return tx("readwrite", (s) => {
    for (const r of rows) s.put(r, r.id);
  });
}

export function clearBatchRows(): Promise<unknown> {
  return tx("readwrite", (s) => s.clear());
}

export type BatchSummary = { rows: number; done: number; at: string };

/** The light summary for the Resume banner (localStorage; a few dozen bytes). */
export function loadBatchSummary(): BatchSummary | null {
  try {
    const raw = globalThis.localStorage?.getItem(SUMMARY_KEY);
    const v = raw ? (JSON.parse(raw) as BatchSummary) : null;
    return v && typeof v.rows === "number" ? v : null;
  } catch {
    return null;
  }
}

export function saveBatchSummary(rows: readonly BatchRow[]): void {
  try {
    const s: BatchSummary = { rows: rows.length, done: rows.filter((r) => r.status === "done").length, at: new Date().toISOString() };
    globalThis.localStorage?.setItem(SUMMARY_KEY, JSON.stringify(s));
  } catch {
    // Storage full or blocked: the banner just has no count.
  }
}

export function clearBatchSummary(): void {
  try {
    globalThis.localStorage?.removeItem(SUMMARY_KEY);
  } catch {
    /* nothing to remove */
  }
}

/**
 * Buffers rows and writes them in batches: after FLUSH_ROWS rows, or FLUSH_MS after the first unwritten row, whichever
 * comes first; `flush` writes whatever is pending (the batch's end or Stop). `write` is putBatchRows by default; tests
 * pass their own. `setTimeout`/`clearTimeout` are injectable for the same reason.
 */
export class BatchRowBuffer {
  private pending: BatchRow[] = [];
  private timer: ReturnType<typeof setTimeout> | null = null;
  private readonly write: (rows: BatchRow[]) => Promise<unknown>;
  private readonly rows: number;
  private readonly ms: number;
  private readonly timers: { set: typeof setTimeout; clear: typeof clearTimeout };
  /** Writes started, for tests and the status line. */
  writes = 0;

  constructor(
    opts: {
      write?: (rows: BatchRow[]) => Promise<unknown>;
      rows?: number;
      ms?: number;
      timers?: { set: typeof setTimeout; clear: typeof clearTimeout };
    } = {},
  ) {
    this.write = opts.write ?? putBatchRows;
    this.rows = opts.rows ?? FLUSH_ROWS;
    this.ms = opts.ms ?? FLUSH_MS;
    this.timers = opts.timers ?? { set: setTimeout, clear: clearTimeout };
  }

  add(row: BatchRow): void {
    this.pending.push(row);
    if (this.pending.length >= this.rows) {
      void this.flush();
      return;
    }
    if (this.timer == null) this.timer = this.timers.set(() => void this.flush(), this.ms);
  }

  async flush(): Promise<void> {
    if (this.timer != null) {
      this.timers.clear(this.timer);
      this.timer = null;
    }
    if (this.pending.length === 0) return;
    const rows = this.pending;
    this.pending = [];
    this.writes++;
    await this.write(rows);
  }

  get unwritten(): number {
    return this.pending.length;
  }
}
