/**
 * The message protocol between the inundation worker (inundation.worker.ts) and its client (inundation-client.ts),
 * and the one pure helper both sides share. Importable anywhere (no `self`), so the client and its tests never load
 * the worker module itself.
 */
import type { InundationResult } from "./inundation.ts";
import type { DemGrid, DemRequest } from "./outline.ts";
import type { Analysis, DamRecord } from "./types.ts";

export type InundationReq = {
  dam: DamRecord;
  result: Analysis;
  /** Extension rounds allowed (reach.ts REACH_MAX_ROUNDS by default). */
  maxRounds?: number;
  /** true: the worker asks the main thread for each terrain grid ({ id, fetch: req }) instead of calling 3DEP itself. */
  fetchOnMain?: boolean;
};

/** Worker → main: progress, a terrain request, or the result. */
export type InundationOut =
  | { id: number; progress: string }
  | { id: number; fetch: DemRequest; seq: number }
  | { id: number; ok: true; out: InundationResult }
  | { id: number; ok: false; error: string };

/** Main → worker: a build, or a terrain grid answering a `fetch` message (z transferred). */
export type InundationIn = { id: number; req: InundationReq } | { id: number; dem: { seq: number; grid?: DemGrid; error?: string } };

/**
 * What crosses back to the page: the cache (the polygonised GeoJSON and stats the desk saves) and the choice. The
 * routed build's per-cell grids (depth, WSE, velocity, arrival, ground: 5 × 4 MB at 10 m) stay in the worker;
 * nothing on the desk reads them after routedCache has summarised them, and the main-thread path keeps returning
 * them unchanged.
 */
export function lightResult(r: InundationResult): InundationResult {
  if (!r.ok) return r;
  const { routed: _routed, ...rest } = r;
  void _routed;
  return rest;
}
