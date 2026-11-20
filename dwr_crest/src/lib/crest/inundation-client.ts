/**
 * Client side of the inundation build: runs buildInundationReach (inundation.ts) in a Web Worker (inundation.worker.ts,
 * inlined like the results worker so the single-file Crest.html needs no extra file) and falls back to the main
 * thread when a worker cannot start (no `Worker`, a locked-down page, Node).
 *
 * Measured on the committed ND00199 fixture upsampled to 10 m (1.09 M cells), the build was 4.7 s on the main thread
 * with a longest unyielded gap of 2 s (spreadVolume) and 0.7 s (polygonizeDepth): the page froze while it ran. In a
 * worker the page stays live, the status lines still arrive (onStatus), and a build can be cancelled: the worker is
 * terminated and a fresh one started for the next build.
 *
 * Terrain: by default the worker fetches 3DEP itself (fetch works in a worker, and the grid then never crosses the
 * thread boundary). When the desk has a terrain override (the desktop's local DEM, which needs the page's API token
 * and the pywebview bridge), the worker asks the page for each grid and the Float32Array is TRANSFERRED, not copied.
 *
 * The batch uses a small pool of these workers (InundationPool): one per structure in flight.
 */
import type { InundationResult, InundationReachOptions } from "./inundation.ts";
import type { InundationIn, InundationOut, InundationReq } from "./inundation-protocol.ts";
import { demSourceLabel, fetchDem, type DemGrid, type DemRequest } from "./outline.ts";
import type { Analysis, DamRecord } from "./types.ts";

export type BuildOptions = {
  onStatus?: (text: string) => void;
  /** Extension rounds allowed (reach.ts REACH_MAX_ROUNDS by default). */
  maxRounds?: number;
  /** Aborting it cancels the build: the worker is terminated and the promise rejects with "cancelled". */
  signal?: AbortSignal;
  /** The terrain fetch when running in-thread (tests); in a worker the page serves it when it is not 3DEP. */
  fetch?: (req: DemRequest) => Promise<DemGrid>;
  /** Yield hook for the in-thread fallback (the desk passes a setTimeout(0)). */
  yieldFn?: () => Promise<void>;
};

export const CANCELLED = "cancelled";

export let backendKind: "worker" | "main thread" | null = null;

/** Thrown when the worker could not start or crashed (not a build failure): the client then builds in-thread. */
class WorkerBroke extends Error {}

type WorkerCtor = new () => Worker;
let ctor: Promise<WorkerCtor | null> | null = null;

/** The inlined worker constructor, or null where a worker cannot be made (Node, no Worker, a blocked blob URL). */
async function workerCtor(): Promise<WorkerCtor | null> {
  if (typeof Worker === "undefined") return null;
  ctor ??= import("./inundation.worker.ts?worker&inline").then(
    (m) => m.default as unknown as WorkerCtor,
    () => null,
  );
  return ctor;
}

/** Whether a build posted now would run in a worker (the batch sizes its pool on this). */
export async function workerAvailable(): Promise<boolean> {
  return (await workerCtor()) != null;
}

/** One worker that runs one build at a time; `cancel` ends the build and the worker with it. */
export class InundationWorker {
  private w: Worker | null = null;
  private seq = 0;
  private pending: { id: number; resolve: (r: InundationResult) => void; reject: (e: Error) => void; onStatus?: (t: string) => void } | null = null;
  private fetchFn: (req: DemRequest) => Promise<DemGrid>;
  private readonly make: WorkerCtor;

  constructor(make: WorkerCtor, fetchFn?: (req: DemRequest) => Promise<DemGrid>) {
    this.make = make;
    this.fetchFn = fetchFn ?? ((r) => fetchDem(r));
  }

  get busy(): boolean {
    return this.pending != null;
  }

  private start(): Worker {
    const w = new this.make();
    w.onmessage = (e: MessageEvent<InundationOut>) => void this.onMessage(w, e.data);
    w.onerror = (e) => this.fail(new WorkerBroke(e.message || "The inundation worker failed."));
    return w;
  }

  private async onMessage(w: Worker, m: InundationOut): Promise<void> {
    const p = this.pending;
    if (!p || p.id !== m.id) return;
    if ("progress" in m) return p.onStatus?.(m.progress);
    if ("fetch" in m) {
      // The page serves the terrain (local DEM, or a test's stand-in); the grid's buffer is transferred, not copied.
      try {
        const grid = await this.fetchFn(m.fetch);
        if (this.w !== w || this.pending !== p) return; // cancelled meanwhile
        const z = grid.z.buffer instanceof ArrayBuffer && grid.z.byteOffset === 0 && grid.z.byteLength === grid.z.buffer.byteLength ? grid.z : Float32Array.from(grid.z);
        w.postMessage({ id: m.id, dem: { seq: m.seq, grid: { ...grid, z } } } satisfies InundationIn, [z.buffer as ArrayBuffer]);
      } catch (err) {
        if (this.w !== w || this.pending !== p) return;
        w.postMessage({ id: m.id, dem: { seq: m.seq, error: (err as Error).message } } satisfies InundationIn);
      }
      return;
    }
    this.pending = null;
    if (m.ok) p.resolve(m.out);
    else p.reject(new Error(m.error));
    // A result unknown to this build id was already filtered above; a worker that answers is kept for the next build.
  }

  private fail(e: Error): void {
    const p = this.pending;
    this.pending = null;
    this.w?.terminate();
    this.w = null;
    p?.reject(e);
  }

  /** The worker itself broke (its script failed to load or run), as opposed to the build reporting a reason. */
  static broke(e: unknown): boolean {
    return e instanceof WorkerBroke;
  }

  /** Run one build. Rejects with Error(CANCELLED) when `signal` aborts first. */
  build(dam: DamRecord, result: Analysis, opts: BuildOptions = {}): Promise<InundationResult> {
    if (this.pending) return Promise.reject(new Error("This inundation worker is busy."));
    if (opts.signal?.aborted) return Promise.reject(new Error(CANCELLED));
    this.w ??= this.start();
    const w = this.w;
    const id = ++this.seq;
    return new Promise<InundationResult>((resolve, reject) => {
      const onAbort = () => this.cancel();
      opts.signal?.addEventListener("abort", onAbort, { once: true });
      const done = <T>(f: (v: T) => void) => (v: T) => {
        opts.signal?.removeEventListener("abort", onAbort);
        f(v);
      };
      this.pending = { id, resolve: done(resolve), reject: done(reject), onStatus: opts.onStatus };
      // 3DEP is fetched inside the worker; anything else (a local DEM through the desktop bridge, a test fetch) on the page.
      const fetchOnMain = opts.fetch != null || demSourceLabel() !== "3DEP";
      if (opts.fetch) this.fetchFn = opts.fetch;
      const req: InundationReq = { dam, result, maxRounds: opts.maxRounds, fetchOnMain };
      w.postMessage({ id, req } satisfies InundationIn);
    });
  }

  /** Stop the build under way (if any) and the worker; the next build starts a fresh worker. */
  cancel(): void {
    this.fail(new Error(CANCELLED));
  }

  /** Release the worker (idle or not). */
  terminate(): void {
    this.cancel();
  }
}

async function inThread(dam: DamRecord, result: Analysis, opts: BuildOptions): Promise<InundationResult> {
  const { buildInundationReach } = await import("./inundation.ts");
  backendKind = "main thread";
  if (opts.signal?.aborted) throw new Error(CANCELLED);
  const o: InundationReachOptions = { onStatus: opts.onStatus, maxRounds: opts.maxRounds, yieldFn: opts.yieldFn, fetch: opts.fetch };
  const r = await buildInundationReach(dam, result, o);
  // A cancelled in-thread build cannot be stopped, but its result is not handed back.
  if (opts.signal?.aborted) throw new Error(CANCELLED);
  return r;
}

let single: InundationWorker | null = null;

/**
 * The desk's build: in a worker when one can be made, else in-thread with the yield hook. One worker serves the desk;
 * a second build while one runs cancels the first (the desk's button is disabled meanwhile, so that is the guided
 * setup switching dams).
 */
export async function buildInundationInWorker(dam: DamRecord, result: Analysis, opts: BuildOptions = {}): Promise<InundationResult> {
  const make = await workerCtor();
  if (!make) return inThread(dam, result, opts);
  backendKind = "worker";
  single ??= new InundationWorker(make);
  if (single.busy) single.cancel();
  try {
    return await single.build(dam, result, opts);
  } catch (e) {
    // A worker that could not even start (blob: URL blocked by a CSP, out of memory): finish the build in-thread. A
    // build that failed for a reason of its own (3DEP down, no terrain) is reported as it is.
    if (!InundationWorker.broke(e)) throw e;
    return inThread(dam, result, opts);
  }
}

/** Pool size for the batch: one worker per spare core, at least 1, at most POOL_MAX. */
export const POOL_MAX = 4;
export function poolSize(hardwareConcurrency: number | undefined = typeof navigator !== "undefined" ? navigator.hardwareConcurrency : undefined): number {
  return Math.max(1, Math.min(POOL_MAX, (hardwareConcurrency ?? 2) - 1));
}

/**
 * A pool of inundation workers for the batch: `acquire` hands out an idle worker (waiting for one when all are busy),
 * `release` returns it. `terminateAll` ends every build (the batch's Stop).
 */
export class InundationPool {
  private workers: InundationWorker[] = [];
  private idle: InundationWorker[] = [];
  private queue: ((w: InundationWorker) => void)[] = [];
  private readonly make: WorkerCtor;
  private readonly fetchFn?: (req: DemRequest) => Promise<DemGrid>;
  readonly size: number;

  constructor(make: WorkerCtor, size: number, fetchFn?: (req: DemRequest) => Promise<DemGrid>) {
    this.make = make;
    this.size = size;
    this.fetchFn = fetchFn;
  }

  acquire(): Promise<InundationWorker> {
    const w = this.idle.pop();
    if (w) return Promise.resolve(w);
    if (this.workers.length < this.size) {
      const n = new InundationWorker(this.make, this.fetchFn);
      this.workers.push(n);
      return Promise.resolve(n);
    }
    return new Promise((resolve) => this.queue.push(resolve));
  }

  release(w: InundationWorker): void {
    const next = this.queue.shift();
    if (next) next(w);
    else this.idle.push(w);
  }

  terminateAll(): void {
    for (const w of this.workers) w.terminate();
    this.workers = [];
    this.idle = [];
    this.queue = [];
  }
}

/** A pool when workers are available here, else null (the batch then builds in-thread, as before). */
export async function makeInundationPool(size = poolSize(), fetchFn?: (req: DemRequest) => Promise<DemGrid>): Promise<InundationPool | null> {
  const make = await workerCtor();
  return make ? new InundationPool(make, size, fetchFn) : null;
}
