/** Port of the shared ACER TM 11 / REG_05.2026 depth-velocity charts from damsuite/tm11_curves.py,
 * and of the ND class rules in damsuite/classification.py (classify_nd).
 *
 * Zone rules follow tm11_curves.zone(): linear interpolation of the low and high boundaries at the
 * velocity, a depth on a boundary takes the higher zone, and a point BEYOND EITHER chart axis is high
 * danger ("the figure does not say a faster or deeper flood is safer"): a depth past the depth axis, or
 * a velocity past the velocity axis (house 25 ft/s, mobile home 16 ft/s). Until 2026-11-07 this port
 * read a velocity past the axis at the last chart column instead (and its header claimed the suite had
 * no such rule, which was false), so a shallow fast flood screened judgment/Medium where the suite's
 * Helper and Runner screen High (Grok audit 2026-11-07, 3.3).
 */

import type { RoadClass } from "./types.ts";

export type ChartKind = "house" | "mobile";
export type Zone = "low" | "judgment" | "high";
export type NdClass = "High" | "Medium" | "Low";

/** (velocity ft/s, depth ft) chart maxima. */
export const AXES: Record<ChartKind, [number, number]> = {
  house: [25.0, 10.0],
  mobile: [16.0, 3.0],
};

// Full tables copied from damsuite tm11_curves.py CURVES.
const CURVES: Record<ChartKind, { v: number[]; low: number[]; high: number[] }> = {
  house: {
    v: [0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5, 5.5, 6, 6.5, 7, 7.5, 8, 8.5, 9, 9.5, 10, 10.5, 11, 11.5, 12, 12.5, 13, 13.5, 14, 14.5, 15, 15.5, 16, 16.5, 17, 17.5, 18, 18.5, 19, 19.5, 20, 20.5, 21, 21.5, 22, 22.5, 23, 23.5, 24, 24.5, 25],
    low: [2.94, 2.94, 2.916, 2.866, 2.816, 2.767, 2.711, 2.618, 2.568, 2.469, 2.417, 2.32, 2.246, 2.146, 2.049, 1.95, 1.873, 1.774, 1.675, 1.576, 1.476, 1.377, 1.278, 1.179, 1.091, 1.005, 0.906, 0.806, 0.732, 0.633, 0.558, 0.484, 0.429, 0.355, 0.285, 0.236, 0.136, 0.062, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    high: [5.968, 5.968, 5.968, 5.918, 5.893, 5.819, 5.769, 5.695, 5.616, 5.521, 5.422, 5.298, 5.174, 5.074, 4.95, 4.804, 4.677, 4.529, 4.38, 4.231, 4.082, 3.917, 3.759, 3.61, 3.448, 3.288, 3.128, 2.965, 2.807, 2.659, 2.494, 2.345, 2.196, 2.072, 1.923, 1.797, 1.65, 1.55, 1.427, 1.304, 1.203, 1.129, 1.03, 0.955, 0.881, 0.839, 0.782, 0.757, 0.732, 0.732, 0.732],
  },
  mobile: {
    v: [0, 0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.25, 2.5, 2.75, 3, 3.25, 3.5, 3.75, 4, 4.25, 4.5, 4.75, 5, 5.25, 5.5, 5.75, 6, 6.25, 6.5, 6.75, 7, 7.25, 7.5, 7.75, 8, 8.25, 8.5, 8.75, 9, 9.25, 9.5, 9.75, 10, 10.25, 10.5, 10.75, 11, 11.25, 11.5, 11.75, 12, 12.25, 12.5, 12.75, 13, 13.25, 13.5, 13.75, 14, 14.25, 14.5, 14.75, 15, 15.25, 15.5, 15.75, 16],
    low: [1.905, 1.905, 1.905, 1.905, 1.905, 1.905, 1.905, 1.905, 1.904, 1.897, 1.89, 1.882, 1.882, 1.875, 1.868, 1.86, 1.853, 1.838, 1.83, 1.823, 1.816, 1.801, 1.793, 1.778, 1.771, 1.756, 1.741, 1.734, 1.719, 1.704, 1.697, 1.682, 1.667, 1.652, 1.645, 1.63, 1.615, 1.6, 1.593, 1.578, 1.563, 1.548, 1.541, 1.526, 1.519, 1.504, 1.489, 1.481, 1.472, 1.459, 1.452, 1.439, 1.429, 1.422, 1.415, 1.407, 1.406, 1.392, 1.392, 1.385, 1.377, 1.377, 1.377, 1.377, 1.37],
    high: [2.499, 2.499, 2.499, 2.499, 2.499, 2.499, 2.499, 2.499, 2.499, 2.491, 2.491, 2.484, 2.476, 2.475, 2.469, 2.462, 2.454, 2.442, 2.432, 2.425, 2.417, 2.403, 2.395, 2.387, 2.373, 2.365, 2.35, 2.343, 2.328, 2.313, 2.298, 2.291, 2.276, 2.261, 2.249, 2.239, 2.224, 2.209, 2.194, 2.179, 2.165, 2.15, 2.135, 2.12, 2.106, 2.098, 2.083, 2.068, 2.053, 2.038, 2.024, 2.011, 1.994, 1.986, 1.972, 1.957, 1.949, 1.934, 1.92, 1.912, 1.897, 1.89, 1.876, 1.868, 1.86],
  },
};

function interp(x: number, xs: number[], ys: number[]): number {
  if (x <= xs[0]) return ys[0];
  if (x >= xs[xs.length - 1]) return ys[ys.length - 1];
  let lo = 0;
  let hi = xs.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xs[mid] <= x) lo = mid;
    else hi = mid;
  }
  const t = (x - xs[lo]) / (xs[hi] - xs[lo]);
  return ys[lo] + t * (ys[hi] - ys[lo]);
}

/** (low boundary ft, high boundary ft) at a velocity. Negative velocity reads as 0, as in the suite. */
export function thresholds(kind: ChartKind, velocityFps: number): [number, number] {
  const c = CURVES[kind];
  const v = Math.max(0, Number.isFinite(velocityFps) ? velocityFps : 0);
  return [interp(v, c.v, c.low), interp(v, c.v, c.high)];
}

/**
 * "low", "judgment" or "high". A depth on a boundary takes the higher zone (ND 5.3); a point past either axis of the
 * chart is high (tm11_curves.zone: `if v > vmax or d > dmax: return HIGH`).
 */
export function zone(kind: ChartKind, depthFt: number, velocityFps: number): Zone {
  const [vmax, dmax] = AXES[kind];
  const d = depthFt;
  if (velocityFps > vmax || d > dmax) return "high"; // past the chart's velocity or depth axis
  const [lo, hi] = thresholds(kind, velocityFps);
  if (d >= hi) return "high";
  if (d >= lo && d > 0) return "judgment";
  return "low";
}

/** Depth (ft) at or above which a receptor counts as inundated (suite 0.1 ft wet threshold). */
export const WET_FT = 0.1;

export type RankInput = {
  /** house, mobile, barn, road, rail */
  kind: string;
  depthFt: number;
  velocityFps: number;
  /** Roads: annual average daily traffic. */
  aadt?: number;
  roadClass?: RoadClass;
  /** Structures: true = habitable. Barns and sheds default to false, houses to true. */
  habitable?: boolean;
  /** Old option: judgment zone counts as probable loss of life (High, 5.3). Default false (Nick, 2026-10-30): a
   *  habitable structure in the judgment zone is Medium by rule, as damsuite classify_nd now defaults. */
  judgmentAsProbable?: boolean;
};

export type Ranked = {
  zone: Zone | null;
  class: NdClass | null;
  reason: string;
  /** Needs an engineer: unknown AADT, unverified structure, or a judgment-zone call. */
  judgment: boolean;
};

/** ND class contribution of one receptor (damsuite classification.classify_nd rules). */
export function rankReceptor(input: RankInput): Ranked {
  const { kind, depthFt, velocityFps } = input;
  // A non-finite depth is unknown, not dry. depthFt >= WET_FT is false for NaN, which would otherwise
  // read as "not inundated" and drop the receptor from the screen — the opposite of the codebase's
  // discipline, where an unknown forces judgment. The engine coerces NaN depths to 0 before it reaches
  // here, so this is defensive, but rankReceptor is exported and must not silently swallow a bad depth.
  if (!Number.isFinite(depthFt)) {
    return { zone: null, class: null, reason: "depth is unknown (not finite): needs judgment", judgment: true };
  }
  const wet = depthFt >= WET_FT;
  if (kind === "road") {
    if (!wet) return { zone: null, class: null, reason: "road not inundated", judgment: false };
    // A zero or negative AADT is not a count (nd_hazard.aadt_value). A private road with no
    // count is Low. A public road with no count is Medium, the suite's unknown-AADT assumption.
    const aadt = input.aadt != null && Number.isFinite(input.aadt) && input.aadt > 0 ? input.aadt : null;
    if (input.roadClass === "interstate")
      return { zone: null, class: "High", reason: "Interstate inundated (5.2.1.2(a))", judgment: false };
    if (aadt != null && aadt > 2000)
      return { zone: null, class: "High", reason: `road AADT ${aadt} > 2,000 (5.2.1.2(a))`, judgment: false };
    if (input.roadClass === "us_state")
      return { zone: null, class: "Medium", reason: "US or State highway inundated (5.2.1.2(b), at least Medium)", judgment: false };
    if (aadt != null && aadt >= 500)
      return { zone: null, class: "Medium", reason: `road AADT ${aadt}, 500–2,000 (5.2.1.2(b))`, judgment: false };
    if (input.roadClass === "private")
      return {
        zone: null,
        class: "Low",
        reason: "private road inundated (AADT under 500 assumed when none is given)",
        judgment: false,
      };
    if (aadt != null)
      return { zone: null, class: "Low", reason: `road AADT ${aadt} < 500`, judgment: false };
    return {
      zone: null,
      class: "Medium",
      reason: "public road inundated, no AADT: assumed Medium (verify traffic)",
      judgment: true,
    };
  }
  if (kind === "rail") {
    if (!wet) return { zone: null, class: null, reason: "rail not inundated", judgment: false };
    return { zone: null, class: "Medium", reason: "railroad inundated (at least Medium; High if passenger or ≥ 10 trains/day)", judgment: true };
  }
  const habitable = input.habitable ?? (kind === "house" || kind === "mobile");
  const chart: ChartKind = kind === "mobile" ? "mobile" : "house";
  const z = zone(chart, depthFt, velocityFps);
  if (!habitable) {
    return {
      zone: wet ? z : null,
      class: null,
      reason: wet ? `${kind} is not habitable; not counted` : "dry",
      judgment: false,
    };
  }
  if (!wet) return { zone: z, class: null, reason: "dry", judgment: false };
  if (z === "high") return { zone: z, class: "High", reason: `ACER TM 11 high-danger zone (${chart} chart)`, judgment: false };
  if (z === "judgment") {
    // Nick (2026-10-30): any house in the judgment zone is automatically Medium (not probable loss of life,
    // not an engineer's call). The old setting (judgment zone counted as probable loss of life, High) is an
    // option, off by default.
    const asHigh = input.judgmentAsProbable ?? false;
    return {
      zone: z,
      class: asHigh ? "High" : "Medium",
      reason: asHigh
        ? `ACER TM 11 judgment zone (${chart} chart), counted as probable loss of life (old option, 5.3)`
        : `ACER TM 11 judgment zone (${chart} chart): Medium by rule (judgment-zone homes are Medium, not probable loss of life)`,
      judgment: asHigh,
    };
  }
  return { zone: z, class: "Medium", reason: `inundated, ACER TM 11 low-danger zone (${chart} chart): economic loss (5.2.2)`, judgment: false };
}
