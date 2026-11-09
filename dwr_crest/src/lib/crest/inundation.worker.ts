/// <reference lib="webworker" />
/**
 * The inundation build off the main thread: a thin wrapper around buildInundationReach (inundation.ts) with the same
 * hand-rolled message protocol as raster.worker.ts / results-client.ts, so the single-file Crest.html inlines it the
 * same way (`?worker&inline`). The pure functions it calls (trace, sections, routing, spread, settle, polygonize) are
 * unchanged and still import and test on the main thread; only where they run moves.
 *
 * Messages in: { id, req: InundationReq }. Out: { id, progress: text } while it runs (the desk's onStatus lines), then
 * { id, ok: true, out: InundationResult } or { id, ok: false, error }. The terrain is fetched here too, so the DEM
 * never crosses the thread boundary on the way in; a caller that must fetch on the main thread (the desktop's local
 * DEM, which needs the page's API token) posts the grid as a `dem` message, transferring the Float32Array's buffer,
 * and the worker's fetch resolves on it. Cancel is terminate + a fresh worker (inundation-client.ts).
 */
import { buildInundationReach } from "./inundation.ts";
import { type InundationIn, type InundationOut, lightResult } from "./inundation-protocol.ts";
import type { DemGrid, DemRequest } from "./outline.ts";

const scope = self as unknown as DedicatedWorkerGlobalScope;
const waiting = new Map<number, { resolve: (g: DemGrid) => void; reject: (e: Error) => void }>();
let seq = 0;

scope.onmessage = async (e: MessageEvent<InundationIn>) => {
  const msg = e.data;
  if ("dem" in msg) {
    const w = waiting.get(msg.dem.seq);
    if (!w) return;
    waiting.delete(msg.dem.seq);
    if (msg.dem.grid) w.resolve(msg.dem.grid);
    else w.reject(new Error(msg.dem.error ?? "The terrain could not be read."));
    return;
  }
  const { id, req } = msg;
  try {
    // A stand-in for fetchDem that asks the page: the DEM arrives as a transferred buffer, not a copy.
    const fetchOnMain = (r: DemRequest) =>
      new Promise<DemGrid>((resolve, reject) => {
        const s = ++seq;
        waiting.set(s, { resolve, reject });
        scope.postMessage({ id, fetch: r, seq: s } satisfies InundationOut);
      });
    const out = await buildInundationReach(req.dam, req.result, {
      maxRounds: req.maxRounds,
      onStatus: (text) => scope.postMessage({ id, progress: text } satisfies InundationOut),
      ...(req.fetchOnMain ? { fetch: fetchOnMain } : {}),
    });
    scope.postMessage({ id, ok: true, out: lightResult(out) } satisfies InundationOut);
  } catch (err) {
    scope.postMessage({ id, ok: false, error: (err as Error).message } satisfies InundationOut);
  }
};
