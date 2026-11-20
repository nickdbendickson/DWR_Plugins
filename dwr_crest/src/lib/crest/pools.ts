/**
 * Lakes on the traced channel, routed as level pools (Nick, 2026-11-18).
 *
 * 3DEP lidar shows a lake or reservoir as its water surface, hydro-flattened to one elevation. Muskingum-Cunge on
 * cross-sections cut across that flat treats the lake as a very wide, very flat river: normal depth needs little rise
 * to pass the peak, so the routing finds little storage and the wave goes through almost undiminished. That is not how
 * a lake behaves. The water entering it rises the whole lake together and leaves over its outlet: a level pool. On
 * Edenville (2020) Crest's peak fell only 19 % over the 21 km to Sanford against ~35 % in the ASCE 2-D model, because
 * Sanford Lake's storage was missed.
 *
 * So a run of sections whose bed is the same flat surface for at least POOL_MIN_LEN_M, and wide enough to be a lake
 * (POOL_MIN_WIDTH_M at 1 m above the surface), is routed as one level pool: storage from the sections (cut wide), and
 * outflow over the outlet, the section just below the flat that passes the least water at a reference rise (the dam,
 * a narrows or a road fill), as a broad-crested weir over that section's ground clamped at the lake surface (the lake
 * already stands at its spill level, so nothing leaves until it rises). The routing (routing.ts) steps the pool by
 * storage-indication; the sections inside report the pool level.
 *
 * Screening only: no gates, no tailwater, no breach of the downstream dam. When the rise tops the outlet's embankment
 * (the lower of the highest ground within POOL_EMBANKMENT_M each side of the outlet's low point) the note says the
 * dam there is overtopped, and that only the Runner can say whether it fails.
 *
 * Pure module (no DOM, no fetch). SI inside.
 */

import type { Section, SectionTable } from "./routing.ts";
import { atDepth } from "./routing.ts";

/** A run of sections counts as a lake when its bed stays within this of its lowest (m). Hydro-flattened lidar water is flat to ~mm; a river drops more than this over POOL_MIN_LEN_M. */
export const POOL_FLAT_TOL_M = 0.03;
/** ...or this much per metre down it (the hydro-flattened backwater of a river above a lake falls in small steps). */
export const POOL_MAX_SLOPE = 5e-5;
/** ...for at least this far down the channel (m). */
export const POOL_MIN_LEN_M = 800;
/** Sections in the running median the lake test reads the bed through (odd). */
export const POOL_MEDIAN_SECTIONS = 9;
/** ...and is at least this wide 1 m above its surface (m, median over the run). */
export const POOL_MIN_WIDTH_M = 200;
/** How far below the end of the flat the outlet is looked for (m). */
export const POOL_OUTLET_SEARCH_M = 400;
/** The reference rise (m) the outlet candidates are compared at: the one that passes least is the control. */
export const POOL_REF_RISE_M = 2;
/** Broad-crested weir coefficient (SI, m^0.5/s): Q = C·L·H^1.5. */
export const POOL_WEIR_C = 1.7;
/** Highest rise the pool's tables reach (m). */
export const POOL_MAX_RISE_M = 30;
/** Rise step of the pool's tables (m). */
export const POOL_RISE_STEP_M = 0.05;
/** Half-width (m) the lake's sections are cut at for its storage. */
export const POOL_WIDE_HALF_M = 2000;
/** Each side of the outlet's low point, how far the embankment top is looked for (m). */
export const POOL_EMBANKMENT_M = 400;

export type Pool = {
  /** First section of the lake and the outlet section (indices into the routed sections). */
  from: number;
  outlet: number;
  /** The lake surface (m): the flat the lidar shows. */
  surfaceM: number;
  /** Lake reach along the channel (m), first flat section to the outlet. */
  lengthM: number;
  /** Rise above the surface (m), the storage held at that rise (m³) and the outflow over the outlet (m³/s). */
  rise: Float64Array;
  volume: Float64Array;
  outflow: Float64Array;
  /** Top of the embankment (or ground) at the outlet (m), the lower side; null when the profile does not show one. */
  embankmentM: number | null;
  /** Surface area at the lake level (ac), from the sections. */
  surfaceAc: number;
};

const ACRE_M2 = 4046.8564224;

/** Lowest point of a profile within `windowM` of the channel (offset 0), or the global lowest. */
function lowIndex(sec: Section, windowM = 60): number {
  let it = -1;
  for (let i = 0; i < sec.offsets.length; i++) {
    if (Math.abs(sec.offsets[i]) > windowM) continue;
    if (it < 0 || sec.z[i] < sec.z[it]) it = i;
  }
  if (it >= 0) return it;
  it = 0;
  for (let i = 1; i < sec.z.length; i++) if (sec.z[i] < sec.z[it]) it = i;
  return it;
}

/**
 * Weir outflow over a section for a pool at `levelM` standing on `floorM` (the lake surface): Σ C·w·h^1.5 over the
 * ground connected to the low point below the level, each point's crest taken no lower than the floor.
 */
export function weirOutflow(sec: Section, levelM: number, floorM: number, c = POOL_WEIR_C): number {
  const n = sec.offsets.length;
  const it = lowIndex(sec);
  const crest = (i: number) => Math.max(sec.z[i], floorM);
  if (levelM <= crest(it)) return 0;
  let q = 0;
  const w = (i: number) => {
    const a = i > 0 ? sec.offsets[i] - sec.offsets[i - 1] : sec.offsets[1] - sec.offsets[0];
    const b = i < n - 1 ? sec.offsets[i + 1] - sec.offsets[i] : a;
    return 0.5 * (Math.abs(a) + Math.abs(b));
  };
  for (const dir of [-1, 1]) {
    for (let i = dir < 0 ? it : it + 1; i >= 0 && i < n; i += dir) {
      const h = levelM - crest(i);
      if (h <= 0) break;
      q += c * w(i) * Math.pow(h, 1.5);
    }
  }
  return q;
}

/** The embankment top at an outlet: the lower of the highest ground within POOL_EMBANKMENT_M each side of the low. */
export function embankmentTop(sec: Section): number | null {
  const it = lowIndex(sec);
  const x0 = sec.offsets[it];
  let l = -Infinity;
  let r = -Infinity;
  for (let i = 0; i < sec.offsets.length; i++) {
    const d = sec.offsets[i] - x0;
    if (Math.abs(d) > POOL_EMBANKMENT_M) continue;
    if (d < 0) l = Math.max(l, sec.z[i]);
    else if (d > 0) r = Math.max(r, sec.z[i]);
  }
  const top = Math.min(l, r);
  return Number.isFinite(top) ? top : null;
}

/**
 * Flat runs of section beds: [first, last] index pairs, each POOL_MIN_LEN_M long or more. A run grows while each step
 * moves the bed by no more than `tolM` and the whole run stays within `tolM` plus POOL_MAX_SLOPE of its length: a lake
 * is dead flat, and the hydro-flattened backwater of a river above it falls in steps of a few centimetres over
 * kilometres (Tittabawassee above Sanford: 0.15 m in 6 km), far flatter than any free-flowing river.
 */
export function flatRuns(tables: Pick<SectionTable, "x" | "bedM">[], tolM = POOL_FLAT_TOL_M, minLenM = POOL_MIN_LEN_M): [number, number][] {
  const out: [number, number][] = [];
  let a = 0;
  while (a < tables.length) {
    let lo = tables[a].bedM;
    let hi = lo;
    let b = a;
    while (b + 1 < tables.length) {
      const z = tables[b + 1].bedM;
      if (Math.abs(z - tables[b].bedM) > tolM) break;
      const nlo = Math.min(lo, z);
      const nhi = Math.max(hi, z);
      if (nhi - nlo > tolM + POOL_MAX_SLOPE * (tables[b + 1].x - tables[a].x)) break;
      lo = nlo;
      hi = nhi;
      b++;
    }
    if (tables[b].x - tables[a].x >= minLenM) out.push([a, b]);
    a = b + 1;
  }
  return out;
}

/** Section beds through a running median of POOL_MEDIAN_SECTIONS, so a narrows or a bridge that lifts the thalweg of
 *  one or two sections (the 30 m window reaching a bank) does not split a lake. */
export function medianBeds(tables: Pick<SectionTable, "x" | "bedM">[]): { x: number; bedM: number }[] {
  const h = POOL_MEDIAN_SECTIONS >> 1;
  return tables.map((t, j) => {
    const w: number[] = [];
    for (let k = Math.max(0, j - h); k <= Math.min(tables.length - 1, j + h); k++) w.push(tables[k].bedM);
    w.sort((p, q) => p - q);
    return { x: t.x, bedM: w[w.length >> 1] };
  });
}

/**
 * The lakes on the channel, ready for the routing. `tables` are the routing's own tables (same indices as
 * `sections`); `wide(j)` gives section j cut wide with its table (spilling over the banks), for the lake's storage, or
 * null to use the routing's own. Lakes with no outlet inside the traced reach are left to Muskingum-Cunge.
 */
export function findPools(
  sections: Section[],
  tables: SectionTable[],
  wide: (j: number) => SectionTable | null = () => null,
): Pool[] {
  const pools: Pool[] = [];
  let after = 0;
  for (const [a, b] of flatRuns(medianBeds(tables))) {
    if (a < after) continue;
    const widths: number[] = [];
    for (let j = a; j <= b; j++) {
      const t = tables[j];
      widths.push(atDepth(t, t.topWidth, 1 + (tables[a].bedM - t.bedM)));
    }
    widths.sort((p, q) => p - q);
    if (!(widths[widths.length >> 1] >= POOL_MIN_WIDTH_M)) continue;
    const beds = tables.slice(a, b + 1).map((t) => t.bedM).sort((p, q) => p - q);
    const surfaceM = beds[beds.length >> 1];
    // The outlet: from the last flat section to POOL_OUTLET_SEARCH_M below it, the one passing least at the ref rise.
    let outlet = -1;
    let best = Infinity;
    for (let j = b; j < sections.length && sections[j].x - sections[b].x <= POOL_OUTLET_SEARCH_M; j++) {
      const q = weirOutflow(sections[j], surfaceM + POOL_REF_RISE_M, surfaceM);
      if (q < best) {
        best = q;
        outlet = j;
      }
    }
    // No outlet inside the reach (the lake runs off the end of the trace), or the "outlet" is the flat itself.
    if (outlet <= a || !(best > 0) || outlet === sections.length - 1) continue;
    const rows = Math.floor(POOL_MAX_RISE_M / POOL_RISE_STEP_M) + 1;
    const rise = new Float64Array(rows);
    const volume = new Float64Array(rows);
    const outflow = new Float64Array(rows);
    const st = [] as SectionTable[];
    for (let j = a; j <= outlet; j++) st.push(wide(j) ?? tables[j]);
    const areaAt = (t: SectionTable, level: number) => (level > t.bedM ? atDepth(t, t.area, level - t.bedM) : 0);
    const widthAt = (t: SectionTable, level: number) => (level > t.bedM ? atDepth(t, t.topWidth, level - t.bedM) : 0);
    // Storage above the surface: the sections' area above the lake level (the lidar has the water below it already).
    const base = st.map((t) => areaAt(t, surfaceM));
    for (let r = 0; r < rows; r++) {
      const h = r * POOL_RISE_STEP_M;
      const level = surfaceM + h;
      let v = 0;
      for (let k = 0; k + 1 < st.length; k++) {
        const dx = sections[a + k + 1].x - sections[a + k].x;
        const above = (m: number) => Math.max(0, areaAt(st[m], level) - base[m]);
        v += 0.5 * (above(k) + above(k + 1)) * dx;
      }
      rise[r] = h;
      volume[r] = v;
      outflow[r] = weirOutflow(sections[outlet], level, surfaceM);
    }
    // Storage must rise with level for the routing to invert it; a flat step (a section topped out) is nudged up.
    for (let r = 1; r < rows; r++) if (!(volume[r] > volume[r - 1])) volume[r] = volume[r - 1] + 1e-3;
    let surf = 0;
    for (let k = 0; k + 1 < st.length; k++) {
      const dx = sections[a + k + 1].x - sections[a + k].x;
      surf += 0.5 * (widthAt(st[k], surfaceM + 0.05) + widthAt(st[k + 1], surfaceM + 0.05)) * dx;
    }
    pools.push({
      from: a,
      outlet,
      surfaceM,
      lengthM: sections[outlet].x - sections[a].x,
      rise,
      volume,
      outflow,
      embankmentM: embankmentTop(sections[outlet]),
      surfaceAc: surf / ACRE_M2,
    });
    after = outlet;
  }
  return pools;
}

/** The rise (m) a pool's storage `v` (m³) stands at, by its table. */
export function riseAt(p: Pick<Pool, "rise" | "volume">, v: number): number {
  const n = p.volume.length;
  if (v <= 0) return 0;
  if (v >= p.volume[n - 1]) return p.rise[n - 1];
  let lo = 0;
  let hi = n - 1;
  while (hi - lo > 1) {
    const m = (lo + hi) >> 1;
    if (p.volume[m] <= v) lo = m;
    else hi = m;
  }
  const f = (v - p.volume[lo]) / (p.volume[hi] - p.volume[lo]);
  return p.rise[lo] + f * (p.rise[hi] - p.rise[lo]);
}
