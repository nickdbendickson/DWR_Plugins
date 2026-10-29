/**
 * Port of the shared ACER TM 11 / REG_05.2026 depth-velocity danger curves
 * from damsuite/tm11_curves.py. Linear interpolation between tabulated
 * velocities. Depth on a boundary takes the higher zone (ND 5.3).
 * Points beyond chart axes are treated as high danger.
 */

export type ChartKind = "house" | "mobile";
export type Zone = "low" | "judgment" | "high";

const AXES: Record<ChartKind, [number, number]> = {
  house: [25.0, 10.0],
  mobile: [16.0, 3.0],
};

// Condensed from the full table for the port (key points; full table can be expanded).
// Full CURVES from tm11_curves.py should be used in production; this is the logic skeleton.
const CURVES: Record<ChartKind, { v: number[]; low: number[]; high: number[] }> = {
  house: {
    v: [0, 5, 10, 15, 20, 25],
    low: [2.94, 2.42, 1.48, 0.56, 0, 0],
    high: [5.97, 5.42, 4.08, 2.49, 1.20, 0.73],
  },
  mobile: {
    v: [0, 4, 8, 12, 16],
    low: [1.91, 1.85, 1.67, 1.47, 1.37],
    high: [2.50, 2.45, 2.28, 2.05, 1.86],
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

export function thresholds(kind: ChartKind, velocityFps: number): [number, number] {
  const c = CURVES[kind];
  const v = Math.max(0, velocityFps);
  return [interp(v, c.v, c.low), interp(v, c.v, c.high)];
}

export function zone(kind: ChartKind, depthFt: number, velocityFps: number): Zone {
  const [lo, hi] = thresholds(kind, velocityFps);
  const d = depthFt;
  // Beyond axes: high (nd_hazard rule)
  const [vmax, dmax] = AXES[kind];
  if (velocityFps > vmax || depthFt > dmax || velocityFps < 0 || depthFt < 0) return "high";
  if (d >= hi) return "high";
  if (d >= lo && d > 0) return "judgment";
  return "low";
}

export function rankReceptor(
  kind: string,
  depthFt: number,
  velocityFps: number,
  aadt?: number,
): { zone: Zone; class: "High" | "Medium" | "Low"; reason: string } {
  const chartKind: ChartKind = kind === "mobile" || kind === "trailer" ? "mobile" : "house";
  const z = zone(chartKind, depthFt, velocityFps);
  if (kind === "road") {
    if (aadt != null && aadt > 2000) return { zone: z, class: "High", reason: "AADT > 2000" };
    if (aadt != null && aadt >= 500) return { zone: z, class: "Medium", reason: "AADT 500-2000" };
    if (depthFt >= 1) return { zone: z, class: "Medium", reason: "road overtopped" };
    return { zone: z, class: "Low", reason: "road dry or low AADT" };
  }
  if (z === "high" || z === "judgment") return { zone: z, class: "High", reason: `ACER ${z} zone` };
  if (depthFt > 0.1) return { zone: z, class: "Medium", reason: "low zone (economic)" };
  return { zone: z, class: "Low", reason: "dry" };
}
