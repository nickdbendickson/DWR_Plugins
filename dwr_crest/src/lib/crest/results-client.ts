/**
 * Client side of the results engine: runs raster-engine.ts in a Web Worker (inlined, so the single-file
 * Crest.html needs no extra file) and falls back to the main thread when a worker cannot start.
 */
import type { Req, RenderOut, RunSummary } from "./raster-engine.ts";
import type { FloodStrip } from "./floodstrip.ts";
import type { ReceptorSample } from "./types.ts";

type Pending = { resolve: (v: unknown) => void; reject: (e: Error) => void; progress?: (t: string) => void };
type Backend = { send: (req: Req, p: Pending, transfer: Transferable[]) => void };

let backend: Promise<Backend> | null = null;
let seq = 0;
export let backendKind: "worker" | "main thread" | null = null;

async function mainThread(): Promise<Backend> {
  const { createEngine } = await import("./raster-engine.ts");
  const engine = createEngine();
  backendKind = "main thread";
  return {
    send: (req, p) => {
      engine.handle(req, p.progress).then(p.resolve, (e: Error) => p.reject(e));
    },
  };
}

async function start(): Promise<Backend> {
  if (typeof Worker === "undefined") return mainThread();
  try {
    const { default: RasterWorker } = await import("./raster.worker.ts?worker&inline");
    const w: Worker = new RasterWorker();
    const pending = new Map<number, Pending>();
    w.onmessage = (e: MessageEvent<{ id: number; ok?: boolean; out?: unknown; error?: string; progress?: string }>) => {
      const p = pending.get(e.data.id);
      if (!p) return;
      if (e.data.progress != null) return p.progress?.(e.data.progress);
      pending.delete(e.data.id);
      if (e.data.ok) p.resolve(e.data.out);
      else p.reject(new Error(e.data.error ?? "results worker failed"));
    };
    w.onerror = (e) => {
      for (const p of pending.values()) p.reject(new Error(e.message || "results worker failed"));
      pending.clear();
    };
    backendKind = "worker";
    return {
      send: (req, p, transfer) => {
        const id = ++seq;
        pending.set(id, p);
        w.postMessage({ id, req }, transfer);
      },
    };
  } catch {
    return mainThread();
  }
}

function call<T>(req: Req, progress?: (t: string) => void, transfer: Transferable[] = []): Promise<T> {
  backend ??= start();
  return backend.then(
    (b) =>
      new Promise<T>((resolve, reject) =>
        b.send(req, { resolve: resolve as (v: unknown) => void, reject, progress }, transfer),
      ),
  );
}

export const results = {
  import: (req: Extract<Req, { type: "import" }>, progress?: (t: string) => void) =>
    call<RunSummary>(req, progress, req.files.map((f) => f.buf)),
  rescan: (req: Extract<Req, { type: "rescan" }>) => call<RunSummary>(req),
  sample: (req: Extract<Req, { type: "sample" }>) => call<ReceptorSample[]>(req),
  render: (req: Extract<Req, { type: "render" }>) => call<RenderOut>(req),
  strip: (req: Extract<Req, { type: "strip" }>) => call<FloodStrip | null>(req),
  drop: (key: string) => call<boolean>({ type: "drop", key }),
};
