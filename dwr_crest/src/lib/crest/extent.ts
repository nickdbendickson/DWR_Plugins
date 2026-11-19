/**
 * Extent sensitivity of a terrain inundation layer: how far its footprint moves with a foot of depth error. The
 * confidence signal behind the per-dam triage (triage.ts, which documents why). Pure, no imports beyond the DEM type,
 * so outline.ts and inundation.ts can call it while building a layer without an import cycle.
 */
import type { DemGrid } from "./outline.ts";

const FT = 0.3048;
const ACRE_M2 = 4046.8564224;

/** Rise (and fall) of the flood surface the sensitivity tests (ft): Crest's depth error against the 2-D models. */
export const EXTENT_FRINGE_RISE_FT = 1;
/** How far from the wet edge the outer fringe is grown (m). A 1 ft rise on a 0.1 % plain spreads ~300 m. */
export const EXTENT_FRINGE_REACH_M = 500;
/** Wet threshold (ft) for the layer's own footprint (the desk's wet threshold). */
export const EXTENT_WET_FT = 0.1;

export type ExtentSensitivity = {
  /** Wet area of the layer (ac, wet at EXTENT_WET_FT). */
  wetAc: number;
  /** Dry ground a surface 1 ft higher would wet, connected to the flood (ac). */
  outerAc: number;
  /** Wet ground shallower than 1 ft, which a surface 1 ft lower would dry (ac). */
  innerAc: number;
  /** (outer + inner) / wet; 0 = rigid footprint, ≥ 1 = a foot of error can double or halve it. */
  sensitivity: number;
  /** Length of the footprint's wet edge (m): wet cell faces that meet dry valid ground inside the DEM, times the cell size. */
  edgeM: number;
  /** Mean distance (m) the edge moves outward / inward with a 1 ft rise / fall: fringe area ÷ edge length. */
  outShiftM: number;
  inShiftM: number;
  /** What the check was measured on: the routed layer itself, or a routed-spread probe beside a terrain outline. */
  basis?: "routed-layer" | "routed-probe";
  /** From the routed flood it was measured on: it ended in a closed basin, and its laid-down ÷ released volume. */
  stoppedInBasin?: boolean;
  /** The routed flood reached the terrain's edge with water still travelling (a probe is never grown downstream). */
  reachedEdge?: boolean;
  volumeRatio?: number;
  /** The outer fringe was cut short by the grow limit or the DEM edge on some cells (the true fringe is larger). */
  truncated: boolean;
};

const valid = (z: number) => Number.isFinite(z) && z > -9000;

/**
 * Extent sensitivity of a depth grid (ft, row-major from the NW, 0/NaN where dry) on the DEM it was built on. The outer
 * fringe is a multi-source flood-grow from the wet cells through 8-connected dry cells whose ground is within
 * EXTENT_FRINGE_RISE_FT of the surface carried from the wet edge, no farther than EXTENT_FRINGE_REACH_M.
 */
export function extentSensitivity(
  g: DemGrid,
  depthFt: ArrayLike<number>,
  opts: { riseFt?: number; reachM?: number; wetFt?: number } = {},
): ExtentSensitivity {
  const W = g.width;
  const H = g.height;
  const N = W * H;
  const rise = (opts.riseFt ?? EXTENT_FRINGE_RISE_FT) * FT;
  const wetFt = opts.wetFt ?? EXTENT_WET_FT;
  const maxSteps = Math.max(1, Math.ceil((opts.reachM ?? EXTENT_FRINGE_REACH_M) / g.cellM));
  const cellAc = (g.cellM * g.cellM) / ACRE_M2;
  // Carried surface (m) per cell: the wet cell's own, or the edge surface a fringe cell was reached from.
  const surf = new Float64Array(N).fill(NaN);
  const step = new Int32Array(N).fill(-1);
  const q = new Int32Array(N);
  let head = 0;
  let tail = 0;
  let wet = 0;
  let inner = 0;
  for (let k = 0; k < N; k++) {
    const d = depthFt[k];
    if (!(d >= wetFt) || !valid(g.z[k])) continue;
    wet++;
    if (d < EXTENT_FRINGE_RISE_FT) inner++;
    surf[k] = g.z[k] + d * FT;
    step[k] = 0;
    q[tail++] = k;
  }
  let outer = 0;
  let truncated = false;
  while (head < tail) {
    const k = q[head++];
    const r = (k / W) | 0;
    const c = k - r * W;
    const s = step[k];
    for (let dr = -1; dr <= 1; dr++)
      for (let dc = -1; dc <= 1; dc++) {
        if (!dr && !dc) continue;
        const rr = r + dr;
        const cc = c + dc;
        if (rr < 0 || rr >= H || cc < 0 || cc >= W) {
          if (s > 0) truncated = true;
          continue;
        }
        const j = rr * W + cc;
        if (step[j] >= 0) continue;
        const z = g.z[j];
        if (!valid(z) || z > surf[k] + rise) continue;
        if (s + 1 > maxSteps) {
          truncated = true;
          continue;
        }
        // The fringe carries the wet edge's surface, not its own ground, so it cannot creep uphill a foot per cell.
        step[j] = s + 1;
        surf[j] = surf[k];
        outer++;
        q[tail++] = j;
      }
  }
  // Edge faces: a wet cell beside valid ground that is not wet. The DEM border and missing cells are not edge: the
  // fringe cannot grow there, so counting them would only dilute the shift.
  const open = (j: number) => step[j] !== 0 && valid(g.z[j]);
  let faces = 0;
  for (let k = 0; k < N; k++) {
    if (step[k] !== 0) continue;
    const r = (k / W) | 0;
    const c = k - r * W;
    if (c > 0 && open(k - 1)) faces++;
    if (c < W - 1 && open(k + 1)) faces++;
    if (r > 0 && open(k - W)) faces++;
    if (r < H - 1 && open(k + W)) faces++;
  }
  const edgeM = faces * g.cellM;
  const cellM2 = g.cellM * g.cellM;
  const r1 = (v: number) => Math.round(v * 10) / 10;
  return {
    wetAc: r1(wet * cellAc),
    outerAc: r1(outer * cellAc),
    innerAc: r1(inner * cellAc),
    edgeM: Math.round(edgeM),
    outShiftM: edgeM > 0 ? r1((outer * cellM2) / edgeM) : 0,
    inShiftM: edgeM > 0 ? r1((inner * cellM2) / edgeM) : 0,
    sensitivity: wet > 0 ? Math.round(((outer + inner) / wet) * 100) / 100 : 0,
    truncated,
  };
}

