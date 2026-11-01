import { demRequest, fetchDem, corridorHalfM, outlineInputs, type DemGrid } from "./outline.ts";
import type { Analysis, DamRecord, ReceptorSeed } from "./types.ts";

/**
 * Pads for receptors made from ND 911 address points (Nick, 2026-10-31, after DWR-2801's 14751 30th St SE read 9.6 ft
 * deep on the channel bed while the house sits about 11 ft above it).
 *
 * pad = DEM ground at the point - channel bed at its station, never negative. The bed is the lowest DEM cell across
 * the valley cross-section at the point's distance downstream (the outline's corridor width). If that section has no
 * terrain, the bed is the streambed at the dam lowered along the valley slope fitted from the sections that do (no
 * slope: the dam's streambed, which keeps the pad small). If the ground at the point cannot be read, pad stays 0 and
 * the seed is marked "estimated" so the screen asks for review. The engine already screens depth = channel depth - pad,
 * clamped at 0, so a point above the water is dry.
 */

export const FT_PER_M = 1 / 0.3048;
const M_PER_DEG_LAT = 110574;
const M_PER_DEG_LON_EQ = 111320;
const NODATA = -9999;
/** Section sample spacing (m). */
const STEP_M = 5;

export type PadPoint = { id: string; lon: number; lat: number; distanceKm: number };
export type PadResult = { id: string; padFt: number; padSource: "dem" | "dem-slope" | "estimated"; groundFt: number | null; bedFt: number | null; note: string };

/** Bilinear DEM (metres NAVD88) at lon/lat; null outside or on nodata. */
export function demAt(g: DemGrid, lon: number, lat: number): number | null {
  const fx = ((lon - g.bbox[0]) / (g.bbox[2] - g.bbox[0])) * g.width - 0.5;
  const fy = ((g.bbox[3] - lat) / (g.bbox[3] - g.bbox[1])) * g.height - 0.5;
  if (!(fx >= -0.5 && fy >= -0.5 && fx <= g.width - 0.5 && fy <= g.height - 0.5)) return null;
  const i0 = Math.max(0, Math.min(g.width - 1, Math.floor(fx)));
  const j0 = Math.max(0, Math.min(g.height - 1, Math.floor(fy)));
  const i1 = Math.min(g.width - 1, i0 + 1);
  const j1 = Math.min(g.height - 1, j0 + 1);
  const tx = Math.max(0, Math.min(1, fx - i0));
  const ty = Math.max(0, Math.min(1, fy - j0));
  const z = (i: number, j: number) => g.z[j * g.width + i];
  const v = [z(i0, j0), z(i1, j0), z(i0, j1), z(i1, j1)];
  if (v.some((x) => !Number.isFinite(x) || x <= NODATA + 1 || x > 9000)) return null;
  return (v[0] * (1 - tx) + v[1] * tx) * (1 - ty) + (v[2] * (1 - tx) + v[3] * tx) * ty;
}

type Frame = { loc: { lon: number; lat: number }; u: [number, number]; halfAt: (xM: number) => number; streambedFt: number };

export function padFrame(dam: DamRecord, result: Analysis): Frame | null {
  const io = outlineInputs(dam, result);
  if (!io.ok) return null;
  const { loc, u, st } = io.inputs;
  const halfAt = (x: number) => {
    if (x <= st[0].x) return st[0].h;
    for (let i = 1; i < st.length; i++) if (st[i].x >= x) return st[i - 1].h + ((x - st[i - 1].x) / Math.max(st[i].x - st[i - 1].x, 1e-9)) * (st[i].h - st[i - 1].h);
    return st[st.length - 1].h;
  };
  return { loc, u: u as [number, number], halfAt, streambedFt: result.datum.streambedNavdFt ?? Number.NaN };
}

/** Lowest DEM (m) across the section at xM downstream; null if the section has no terrain. */
export function sectionBedM(g: DemGrid, f: Frame, xM: number): number | null {
  const kx = M_PER_DEG_LON_EQ * Math.cos((f.loc.lat * Math.PI) / 180);
  const n: [number, number] = [-f.u[1], f.u[0]];
  const w = corridorHalfM(f.halfAt(xM));
  let best: number | null = null;
  for (let t = -w; t <= w + 1e-9; t += STEP_M) {
    const x = f.u[0] * xM + n[0] * t;
    const y = f.u[1] * xM + n[1] * t;
    const z = demAt(g, f.loc.lon + x / kx, f.loc.lat + y / M_PER_DEG_LAT);
    if (z != null && (best == null || z < best)) best = z;
  }
  return best;
}

/** Pads for the points from a DEM grid (null grid = the lookup failed). */
export function computePads(points: PadPoint[], g: DemGrid | null, f: Frame): PadResult[] {
  const rows = points.map((p) => {
    const ground = g ? demAt(g, p.lon, p.lat) : null;
    const bed = g ? sectionBedM(g, f, p.distanceKm * 1000) : null;
    return { p, groundFt: ground == null ? null : ground * FT_PER_M, bedFt: bed == null ? null : bed * FT_PER_M };
  });
  // valley slope (ft/m) from the sections with terrain, least squares through the dam's streambed
  const fit = rows.filter((r) => r.bedFt != null && r.p.distanceKm > 0);
  let slope = 0;
  if (fit.length) {
    const num = fit.reduce((s, r) => s + (f.streambedFt - r.bedFt!) * r.p.distanceKm * 1000, 0);
    const den = fit.reduce((s, r) => s + (r.p.distanceKm * 1000) ** 2, 0);
    slope = Math.max(0, num / den);
  }
  return rows.map(({ p, groundFt, bedFt }): PadResult => {
    if (groundFt == null)
      return { id: p.id, padFt: 0, padSource: "estimated", groundFt: null, bedFt, note: "pad estimated: no terrain at the point; kept on the channel bed (pad 0)" };
    if (bedFt != null) {
      const pad = Math.max(0, groundFt - bedFt);
      return { id: p.id, padFt: Math.round(pad * 10) / 10, padSource: "dem", groundFt, bedFt, note: `ground ${groundFt.toFixed(1)} ft - bed ${bedFt.toFixed(1)} ft (3DEP)` };
    }
    const b = f.streambedFt - slope * p.distanceKm * 1000;
    if (!Number.isFinite(b))
      return { id: p.id, padFt: 0, padSource: "estimated", groundFt, bedFt: null, note: "pad estimated: no channel bed at the station; kept on the channel bed (pad 0)" };
    const pad = Math.max(0, groundFt - b);
    return { id: p.id, padFt: Math.round(pad * 10) / 10, padSource: "dem-slope", groundFt, bedFt: b, note: `ground ${groundFt.toFixed(1)} ft - bed ${b.toFixed(1)} ft (streambed at the dam along the valley slope)` };
  });
}

/** The 911 seeds that still need a pad, with their points' lon/lat from the saved lookup. */
export function padPoints(dam: DamRecord): PadPoint[] {
  const pts = new Map((dam.nd911?.points ?? []).map((p) => [p.nguid, p]));
  return dam.receptors
    .filter((r) => r.source === "nd911" && r.padSource == null)
    .flatMap((r) => {
      const p = pts.get(r.sourceRef ?? "");
      return p ? [{ id: r.id, lon: p.lon, lat: p.lat, distanceKm: r.distanceKm ?? 0 }] : [];
    });
}

/** Receptors with the pads applied. */
export function applyPads(receptors: ReceptorSeed[], pads: PadResult[]): ReceptorSeed[] {
  const by = new Map(pads.map((p) => [p.id, p]));
  return receptors.map((r) => {
    const p = by.get(r.id);
    return p ? { ...r, padFt: p.padFt, padSource: p.padSource, padNote: p.note } : r;
  });
}

/** Fetch 3DEP terrain for the outline corridor and compute the pads (a failed lookup gives "estimated" pads). */
export async function lookupPads(dam: DamRecord, result: Analysis, fetchFn?: Parameters<typeof fetchDem>[1]): Promise<ReceptorSeed[] | null> {
  const f = padFrame(dam, result);
  const points = padPoints(dam);
  if (!f || points.length === 0) return null;
  const io = outlineInputs(dam, result);
  let g: DemGrid | null = null;
  if (io.ok) {
    try {
      g = await fetchDem(demRequest(io.inputs, 10, 1500), fetchFn);
    } catch {
      g = null;
    }
  }
  return applyPads(dam.receptors, computePads(points, g, f));
}
