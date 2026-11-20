/**
 * Cut cross-sections from a DEM along a traced channel centreline (step 3 of the terrain-inundation plan, Nick,
 * 2026-11-01), so the routing module (routing.ts) can build a hydraulic table at each one and route the breach
 * hydrograph down the real valley.
 *
 * The centreline is the drainage path outline.ts already traces from the breach (a run of DEM cell centres, ordered
 * downstream, each with its distance downstream along the bends). That path is one cell wide and stair-steps, so the
 * cutter resamples it to even stations, takes a smoothed tangent at each (a straight one-cell step has a noisy
 * normal), and samples the DEM along the perpendicular out to a half-width each side. The result is a `Section`
 * (routing.ts): offsets in metres across the valley, left to right, with ground elevations, at `x` m downstream.
 *
 * Pure module (no DOM, no fetch). Metres throughout; the DEM is metres NAVD 88. The local frame (east/north metres
 * from the dam) is the same equirectangular one as aoi.ts and outline.ts.
 */

import type { DemGrid } from "./outline.ts";
import type { Section } from "./routing.ts";

const M_PER_DEG_LAT = 110574;
const M_PER_DEG_LON_EQ = 111320;
const kxOf = (lat: number) => M_PER_DEG_LON_EQ * Math.cos((lat * Math.PI) / 180);
const NODATA = -9999;

/** A traced channel: local east/north metres from `loc`, ordered downstream, each with its distance downstream (m). */
export type Centerline = {
  loc: { lon: number; lat: number };
  pts: { e: number; n: number; x: number }[];
};

export type CutOptions = {
  /** Station spacing down the channel (m). Default 50. */
  stationM?: number;
  /** Half-width sampled each side of the centreline (m). Default 400. */
  halfWidthM?: number;
  /** Spacing of ground samples across a section (m). Default the DEM cell size. */
  sampleM?: number;
  /** Half-length (m) of the centreline window the tangent is averaged over, to steady the normal. Default 60. */
  tangentWindowM?: number;
  /** A section is dropped if more than this fraction of its samples are off the DEM or NoData. Default 0.5. */
  maxMissingFraction?: number;
};

/** Bilinear ground elevation (m) at a point in the local frame, or null off the grid / on NoData. */
export function elevAt(g: DemGrid, loc: { lon: number; lat: number }, e: number, n: number): number | null {
  const kx = kxOf(loc.lat);
  const lon = loc.lon + e / kx;
  const lat = loc.lat + n / M_PER_DEG_LAT;
  const dlon = (g.bbox[2] - g.bbox[0]) / g.width;
  const dlat = (g.bbox[3] - g.bbox[1]) / g.height;
  // Fractional cell-centre coordinates (cell centre i sits at i + 0.5).
  const fx = (lon - g.bbox[0]) / dlon - 0.5;
  const fy = (g.bbox[3] - lat) / dlat - 0.5;
  const i0 = Math.floor(fx);
  const j0 = Math.floor(fy);
  const tx = fx - i0;
  const ty = fy - j0;
  const ok = (z: number) => Number.isFinite(z) && z > NODATA + 1 && z < 9000;
  const at = (i: number, j: number) => {
    if (i < 0 || j < 0 || i >= g.width || j >= g.height) return null;
    const z = g.z[j * g.width + i];
    return ok(z) ? z : null;
  };
  // Four surrounding cell centres; if any is missing, fall back to the nearest valid of the four.
  const z00 = at(i0, j0);
  const z10 = at(i0 + 1, j0);
  const z01 = at(i0, j0 + 1);
  const z11 = at(i0 + 1, j0 + 1);
  if (z00 != null && z10 != null && z01 != null && z11 != null) {
    const a = z00 * (1 - tx) + z10 * tx;
    const b = z01 * (1 - tx) + z11 * tx;
    return a * (1 - ty) + b * ty;
  }
  // Partial coverage near the edge or a NoData hole: nearest of the valid corners by bilinear weight.
  const cands: { z: number; w: number }[] = [];
  if (z00 != null) cands.push({ z: z00, w: (1 - tx) * (1 - ty) });
  if (z10 != null) cands.push({ z: z10, w: tx * (1 - ty) });
  if (z01 != null) cands.push({ z: z01, w: (1 - tx) * ty });
  if (z11 != null) cands.push({ z: z11, w: tx * ty });
  if (cands.length === 0) return null;
  let best = cands[0];
  for (const c of cands) if (c.w > best.w) best = c;
  return best.z;
}

/** Resample the centreline to even arc-length stations: point, unit downstream tangent (smoothed), distance. */
export function stations(line: Centerline, stationM: number, tangentWindowM: number) {
  const p = line.pts;
  if (p.length < 2) return [];
  // Cumulative arc length in the local frame (the pts carry `x` already, but recompute to be spacing-robust).
  const cum: number[] = [0];
  for (let i = 1; i < p.length; i++) cum.push(cum[i - 1] + Math.hypot(p[i].e - p[i - 1].e, p[i].n - p[i - 1].n));
  const total = cum[cum.length - 1];
  if (!(total > 0)) return [];
  const atArc = (s: number) => {
    const t = Math.max(0, Math.min(total, s));
    let i = 1;
    while (i < cum.length && cum[i] < t) i++;
    const a = p[i - 1];
    const b = p[Math.min(i, p.length - 1)];
    const seg = cum[Math.min(i, cum.length - 1)] - cum[i - 1];
    const f = seg > 0 ? (t - cum[i - 1]) / seg : 0;
    return { e: a.e + f * (b.e - a.e), n: a.n + f * (b.n - a.n), x: a.x + f * (b.x - a.x) };
  };
  const out: { e: number; n: number; x: number; te: number; tn: number }[] = [];
  const count = Math.max(2, Math.round(total / stationM) + 1);
  for (let s = 0; s < count; s++) {
    const arc = (s / (count - 1)) * total;
    const here = atArc(arc);
    // Tangent: vector between points a window ahead and behind, so a single stair step does not swing the normal.
    const ahead = atArc(arc + tangentWindowM);
    const behind = atArc(arc - tangentWindowM);
    let te = ahead.e - behind.e;
    let tn = ahead.n - behind.n;
    const L = Math.hypot(te, tn) || 1;
    te /= L;
    tn /= L;
    out.push({ e: here.e, n: here.n, x: here.x, te, tn });
  }
  return out;
}

/**
 * Cut sections along the centreline. The normal points to the left of the downstream direction, and the offsets run
 * from −half to +half, so the profile reads from the right bank (negative offsets) across to the left bank (positive)
 * looking downstream; offset 0 is the centreline point. The table reads the offsets symmetrically, so the order is a
 * labelling convention, not a value the routing depends on.
 */
export function cutSections(g: DemGrid, line: Centerline, opts: CutOptions = {}): Section[] {
  const stationM = opts.stationM ?? 50;
  const halfWidthM = opts.halfWidthM ?? 400;
  const dlon = (g.bbox[2] - g.bbox[0]) / g.width;
  const dlat = (g.bbox[3] - g.bbox[1]) / g.height;
  const cellM = Math.min((dlon * kxOf(line.loc.lat)), dlat * M_PER_DEG_LAT) || 10;
  const sampleM = opts.sampleM ?? cellM;
  const tangentWindowM = opts.tangentWindowM ?? 60;
  const maxMissing = opts.maxMissingFraction ?? 0.5;
  const sts = stations(line, stationM, tangentWindowM);
  const sections: Section[] = [];
  const half = Math.max(1, Math.round(halfWidthM / sampleM));
  for (const s of sts) {
    // Normal to the tangent: rotate the unit tangent +90° (points left of downstream). Marching from -half..+half then
    // runs from the right bank (negative offsets) across to the left bank (positive) looking downstream.
    const ne = -s.tn;
    const nn = s.te;
    const offsets: number[] = [];
    const z: number[] = [];
    for (let m = -half; m <= half; m++) {
      const off = m * sampleM;
      const e = s.e + ne * off;
      const n = s.n + nn * off;
      const el = elevAt(g, line.loc, e, n);
      offsets.push(off);
      z.push(el == null ? NaN : el);
    }
    // Fill short NoData gaps by linear interpolation between the nearest valid samples each side.
    fillGaps(z);
    const stillMissing = z.reduce((c, v) => c + (Number.isFinite(v) ? 0 : 1), 0);
    if (stillMissing > maxMissing * z.length || z.length < 3) continue;
    // Trim any NaN still left at the very ends so the profile is all-valid for the table.
    let lo = 0;
    let hi = z.length - 1;
    while (lo < hi && !Number.isFinite(z[lo])) lo++;
    while (hi > lo && !Number.isFinite(z[hi])) hi--;
    if (hi - lo < 2) continue;
    sections.push({ x: s.x, offsets: offsets.slice(lo, hi + 1), z: z.slice(lo, hi + 1) });
  }
  return dedupeByX(sections);
}

/** Linear-interpolate interior NaN runs in place from the valid samples on each side (ends left as NaN). */
function fillGaps(z: number[]) {
  let i = 0;
  while (i < z.length) {
    if (Number.isFinite(z[i])) {
      i++;
      continue;
    }
    let j = i;
    while (j < z.length && !Number.isFinite(z[j])) j++;
    if (i > 0 && j < z.length) {
      const a = z[i - 1];
      const b = z[j];
      for (let k = i; k < j; k++) z[k] = a + ((b - a) * (k - i + 1)) / (j - i + 1);
    }
    i = j;
  }
}

/** Sections must be strictly increasing in x for the routing reach slopes; drop any that don't advance. */
function dedupeByX(sections: Section[]): Section[] {
  const out: Section[] = [];
  for (const s of sections) if (out.length === 0 || s.x > out[out.length - 1].x + 1e-6) out.push(s);
  return out;
}
