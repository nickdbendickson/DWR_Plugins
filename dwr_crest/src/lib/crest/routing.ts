/**
 * Hydraulic routing for the terrain flood (step 3 and 4 of the terrain-inundation plan, Nick, 2026-11-01).
 *
 * The breach hydrograph (breach.ts routedHydrograph) is routed down the traced channel with Muskingum-Cunge
 * (Cunge 1969; Ponce & Yevjevich 1978) on cross-sections cut from the DEM. The parameters vary in space, reach to
 * reach with each section's geometry and celerity, but are held constant in time (computed once per cell at the
 * reference peak) — the practical, mass-conservative form. Recomputing them each step at the local flow is the full
 * variable-parameter scheme, which is not mass-conservative and manufactures ~20 % volume here; see the cell-build
 * note in routeMuskingumCunge. Each section's hydraulic table (area, top width, wetted perimeter, conveyance against stage) comes
 * from its ground profile with Manning's n split at a bankfull height: channel n below, floodplain n above, the
 * divided-channel conveyance sum. Only the wet region connected to the thalweg counts, so a pothole or a borrow
 * pit behind a levee does not add area. Stage at the routed peak is Manning normal depth on the section
 * (conveyance × √slope = Q), velocity is Q / A, and both go to the fill (step 5).
 *
 * Two slope floors, as the plan says: one keeps the routing stable on adverse or flat reaches, the other keeps
 * normal depth finite. A section whose table cannot carry the routed peak is flagged "capped" (the caller cuts a
 * taller or wider section and tries again); it is never clamped silently.
 *
 * SI inside (m, m³/s, s). The desk's ft and cfs are converted at the call site (breach.ts FT, CFS).
 * Pure module (no DOM). The routing scheme is the standard formulation (Ponce 1989, ch. 9); the tests check mass
 * conservation (to machine precision), a steady state, kinematic travel time and the attenuation ordering, not a
 * textbook table.
 */

export const G = 9.80665;

/** A cross-section: ground elevations (m) at offsets (m) across the valley, left to right, cut at `x` m downstream. */
export type Section = {
  x: number;
  offsets: ArrayLike<number>;
  z: ArrayLike<number>;
};

export type RoughnessOptions = {
  /** Manning's n in the channel (below bankfull) and on the floodplain. */
  nChannel: number;
  nFloodplain: number;
  /** Height above the thalweg (m) where the channel ends and the floodplain begins. */
  bankfullM: number;
};

export const DEFAULT_ROUGHNESS: RoughnessOptions = { nChannel: 0.035, nFloodplain: 0.06, bankfullM: 1.5 };

/** The range a typed Manning's n is held to: the shared defaults file's range for `manning_n` (settings.ts). */
export const MANNING_RANGE = { lo: 0.01, hi: 0.3 } as const;

/**
 * The roughness a record asks for: its own Manning's n values where they are usable numbers, the built-in ones
 * otherwise. A value outside MANNING_RANGE is held to the range. With nothing set this is DEFAULT_ROUGHNESS itself.
 */
export function roughnessOf(r?: { nChannel?: number; nFloodplain?: number } | null): RoughnessOptions {
  const pick = (v: number | undefined, d: number) =>
    typeof v === "number" && Number.isFinite(v) && v > 0 ? Math.min(MANNING_RANGE.hi, Math.max(MANNING_RANGE.lo, v)) : d;
  const nChannel = pick(r?.nChannel, DEFAULT_ROUGHNESS.nChannel);
  const nFloodplain = pick(r?.nFloodplain, DEFAULT_ROUGHNESS.nFloodplain);
  if (nChannel === DEFAULT_ROUGHNESS.nChannel && nFloodplain === DEFAULT_ROUGHNESS.nFloodplain) return DEFAULT_ROUGHNESS;
  return { nChannel, nFloodplain, bankfullM: DEFAULT_ROUGHNESS.bankfullM };
}

export type SectionTable = {
  x: number;
  /** Thalweg elevation (m). */
  bedM: number;
  /** Highest stage the table reaches (m above the bed): the lower of the two banks' tops, or the height cap. */
  maxDepthM: number;
  depth: Float64Array;
  area: Float64Array;
  topWidth: Float64Array;
  perimeter: Float64Array;
  /** Divided-channel conveyance K = Σ A_i R_i^(2/3) / n_i (m³/s per √slope). */
  conveyance: Float64Array;
};

/** Hydraulic properties of the wet region around the thalweg at one stage. */
export function wetAt(sec: Section, stage: number, bankM: number, rough: RoughnessOptions, thalweg?: number) {
  const n = sec.offsets.length;
  const it = thalweg ?? thalwegIndex(sec);
  if (stage <= sec.z[it]) return { area: 0, topWidth: 0, perimeter: 0, conveyance: 0 };
  // Channel part: ground below bankM; floodplain part: above. Each gets its own area and perimeter.
  let aC = 0, pC = 0, aF = 0, pF = 0, top = 0;
  const seg = (x0: number, z0: number, x1: number, z1: number) => {
    // Trapezoid between two ground points both under the stage; split at bankM when it crosses.
    const parts: [number, number, number, number][] = [];
    if ((z0 - bankM) * (z1 - bankM) < 0) {
      const t = (bankM - z0) / (z1 - z0);
      const xm = x0 + t * (x1 - x0);
      parts.push([x0, z0, xm, bankM], [xm, bankM, x1, z1]);
    } else parts.push([x0, z0, x1, z1]);
    for (const [xa, za, xb, zb] of parts) {
      const w = Math.abs(xb - xa);
      const a = w * (stage - (za + zb) / 2);
      const p = Math.hypot(w, zb - za);
      if ((za + zb) / 2 < bankM) {
        aC += a;
        pC += p;
      } else {
        aF += a;
        pF += p;
      }
      top += w;
    }
  };
  const walk = (dir: 1 | -1) => {
    let i = it;
    while (true) {
      const j = i + dir;
      if (j < 0 || j >= n) break;
      const x0 = sec.offsets[i], z0 = sec.z[i], x1 = sec.offsets[j], z1 = sec.z[j];
      if (z1 >= stage) {
        // The bank: water edge between i and j.
        const t = (stage - z0) / (z1 - z0);
        const xe = x0 + t * (x1 - x0);
        seg(x0, z0, xe, stage);
        return;
      }
      seg(x0, z0, x1, z1);
      i = j;
    }
  };
  walk(1);
  walk(-1);
  const k = (a: number, p: number, nn: number) => (a > 0 && p > 0 ? (a * Math.pow(a / p, 2 / 3)) / nn : 0);
  return { area: aC + aF, topWidth: top, perimeter: pC + pF, conveyance: k(aC, pC, rough.nChannel) + k(aF, pF, rough.nFloodplain) };
}

/**
 * Index of the section's thalweg: the lowest ground point, anywhere across the profile (default) or only within
 * `windowM` of the traced channel (offset 0). On a wide cut the global low can be a neighbouring valley or a pit
 * hundreds of metres off the channel, which leaves the table (and the routing's celerity there) degenerate.
 */
export function thalwegIndex(sec: Section, windowM?: number): number {
  const n = sec.offsets.length;
  let it = -1;
  for (let i = 0; i < n; i++) {
    if (windowM != null && Math.abs(sec.offsets[i]) > windowM) continue;
    if (it < 0 || sec.z[i] < sec.z[it]) it = i;
  }
  if (it >= 0) return it;
  it = 0;
  for (let i = 1; i < n; i++) if (sec.z[i] < sec.z[it]) it = i;
  return it;
}

export type TableOptions = {
  /** Bumps lower than this (m) don't end the table (DEM noise); 0 = the first descent ends it (as merged). */
  ridgeTolM?: number;
  /** Look for the thalweg only within this distance (m) of the traced channel; default anywhere. */
  thalwegWindowM?: number;
};

/**
 * The section's hydraulic table from the bed up to the lower bank top (or `capM` above the bed), every `stepM`.
 * The lower bank top is where the walk would leave the profile, so the table is the connected region only.
 */
export function sectionTable(
  sec: Section,
  rough: RoughnessOptions = DEFAULT_ROUGHNESS,
  capM = 30,
  stepM = 0.05,
  topts: TableOptions = {},
): SectionTable {
  const n = sec.offsets.length;
  if (n < 3) throw new Error("A section needs at least three points.");
  const ridgeTolM = topts.ridgeTolM ?? 0;
  const it = thalwegIndex(sec, topts.thalwegWindowM);
  const bed = sec.z[it];
  // The containing bank each side: walking out from the thalweg, the first ridge crest the water would spill over
  // (the elevation after which the ground descends into a disconnected low). Using the global max each side would
  // let the table rise past a levee into a pocket that is not connected to the thalweg at that stage.
  const crest = (dir: -1 | 1) => {
    let best = bed;
    for (let i = it; i >= 0 && i < n; i += dir) {
      if (sec.z[i] > best) best = sec.z[i];
      const j = i + dir;
      if (j < 0 || j >= n) break; // reached the profile edge
      // Descends beyond here: this ridge contains the wet region. With ridgeTolM > 0 a bump only counts once the ground
      // beyond it falls that far below the highest point so far, so DEM noise of a few centimetres doesn't end the table.
      if (ridgeTolM > 0 ? sec.z[j] < best - ridgeTolM : sec.z[i] > sec.z[j]) return best;
    }
    return best;
  };
  const maxDepth = Math.max(stepM, Math.min(capM, Math.min(crest(-1), crest(1)) - bed));
  const rows = Math.max(2, Math.floor(maxDepth / stepM) + 1);
  const depth = new Float64Array(rows);
  const area = new Float64Array(rows);
  const topWidth = new Float64Array(rows);
  const perimeter = new Float64Array(rows);
  const conveyance = new Float64Array(rows);
  const bank = bed + rough.bankfullM;
  for (let r = 1; r < rows; r++) {
    const h = Math.min(maxDepth, r * stepM);
    const w = wetAt(sec, bed + h, bank, rough, it);
    depth[r] = h;
    area[r] = w.area;
    topWidth[r] = w.topWidth;
    perimeter[r] = w.perimeter;
    conveyance[r] = w.conveyance;
  }
  return { x: sec.x, bedM: bed, maxDepthM: maxDepth, depth, area, topWidth, perimeter, conveyance };
}

/** Linear interpolation of a table column at a depth (clamped to the table). */
function atDepth(t: SectionTable, col: Float64Array, h: number) {
  const n = t.depth.length;
  if (h <= 0) return 0;
  if (h >= t.depth[n - 1]) return col[n - 1];
  let lo = 0;
  let hi = n - 1;
  while (hi - lo > 1) {
    const m = (lo + hi) >> 1;
    if (t.depth[m] <= h) lo = m;
    else hi = m;
  }
  const f = (h - t.depth[lo]) / (t.depth[hi] - t.depth[lo]);
  return col[lo] + f * (col[hi] - col[lo]);
}

export type NormalDepth = {
  depthM: number;
  areaM2: number;
  topWidthM: number;
  velocityMps: number;
  /** The table's top could not carry Q: the stage is the table's maximum and the section needs to be taller or wider. */
  capped: boolean;
};

/**
 * Manning normal depth on a section for discharge Q (m³/s) and friction slope S: K(h)·√S = Q. Conveyance rises
 * monotonically with depth, so the stage is found by a binary search on the conveyance column (no per-call
 * bisection of the hydraulics), then interpolated linearly within the bracketing rows.
 */
export function normalDepth(t: SectionTable, qCms: number, slope: number): NormalDepth {
  const n = t.depth.length;
  const s = Math.sqrt(Math.max(slope, 1e-12));
  if (qCms <= 0) return { depthM: 0, areaM2: 0, topWidthM: 0, velocityMps: 0, capped: false };
  const target = qCms / s; // conveyance needed
  if (target >= t.conveyance[n - 1]) {
    const a = t.area[n - 1];
    return { depthM: t.depth[n - 1], areaM2: a, topWidthM: t.topWidth[n - 1], velocityMps: a > 0 ? qCms / a : 0, capped: true };
  }
  let lo = 0;
  let hi = n - 1;
  while (hi - lo > 1) {
    const m = (lo + hi) >> 1;
    if (t.conveyance[m] <= target) lo = m;
    else hi = m;
  }
  const span = t.conveyance[hi] - t.conveyance[lo];
  const f = span > 0 ? (target - t.conveyance[lo]) / span : 0;
  const h = t.depth[lo] + f * (t.depth[hi] - t.depth[lo]);
  const a = t.area[lo] + f * (t.area[hi] - t.area[lo]);
  const top = t.topWidth[lo] + f * (t.topWidth[hi] - t.topWidth[lo]);
  return { depthM: h, areaM2: a, topWidthM: top, velocityMps: a > 0 ? qCms / a : 0, capped: false };
}

/** Depth, area, top width and kinematic celerity (dQ/dA at fixed slope) for a discharge, from the table. */
function propsAtQ(t: SectionTable, q: number, slope: number) {
  const nd = normalDepth(t, q, slope);
  const s = Math.sqrt(slope);
  const dh = Math.max(0.01, 0.02 * nd.depthM);
  const h1 = Math.max(0, nd.depthM - dh);
  const h2 = Math.min(t.depth[t.depth.length - 1], nd.depthM + dh);
  const dA = atDepth(t, t.area, h2) - atDepth(t, t.area, h1);
  const dQ = (atDepth(t, t.conveyance, h2) - atDepth(t, t.conveyance, h1)) * s;
  let c = dA > 0 ? dQ / dA : 0;
  if (!(c > 0)) c = nd.velocityMps > 0 ? (5 / 3) * nd.velocityMps : 0.1;
  return { ...nd, celerity: c };
}

/**
 * Muskingum routing coefficients from the Courant and cell-Reynolds numbers (Ponce 1989, eq. 9-56):
 *   C = c·Δt/Δx (Courant),  D = q/(B·S·c·Δx) (cell Reynolds),
 *   O₂ = C0·I₂ + C1·I₁ + C2·O₁,  C0=(−1+C+D)/(1+C+D), C1=(1+C−D)/(1+C+D), C2=(1−C+D)/(1+C+D).
 * C0+C1+C2 = 1 for any C, D, so the step conserves mass exactly whatever celerity and width are used. This is the
 * form the frozen scheme freezes at the peak and the MCT scheme recomputes each step at the local flow; q here is the
 * reach-referenced discharge per unit of the time step, B the top width and S the bed slope.
 */
function mcCoeffsCourant(c: number, B: number, slope: number, dx: number, dt: number, q: number) {
  const C = (c * dt) / dx;
  const D = q / (Math.max(1, B) * Math.max(1e-9, slope) * Math.max(1e-6, c) * dx);
  const denom = 1 + C + D;
  return { C0: (-1 + C + D) / denom, C1: (1 + C - D) / denom, C2: (1 - C + D) / denom };
}

export type RoutingOptions = {
  rough?: RoughnessOptions;
  /** Slope floors: for the routing parameters and for normal depth (the plan's two floors). */
  slopeFloorRouting?: number;
  slopeFloorStage?: number;
  /** Computational time step (s); default from the hydrograph's time to peak. */
  dtS?: number;
  /** Stop when the last section has receded to this fraction of its peak, or at maxHours. */
  recessionFraction?: number;
  maxHours?: number;
  /** Points kept per section hydrograph (0 = none). */
  keepPoints?: number;
  /**
   * Which Muskingum-Cunge variant routes each reach.
   *  - "mc"  : the frozen-coefficient scheme everywhere (parameters set once at the inflow peak). The default when
   *            unset; conserves mass exactly and is accurate on flat multi-thread reaches where the overbank wave
   *            translates rather than attenuating.
   *  - "mct" : the variable-parameter, mass-conservative Muskingum-Cunge-Todini scheme everywhere (parameters
   *            recomputed each step at the local reference discharge). It captures the real attenuation in a defined
   *            valley but over-damps a flat, shallow overbank (celerity collapses, the wave bogs).
   *  - "auto": MCT on reaches confined enough to carry the wave (bed slope ≥ `confineSlopeTrigger` and the section
   *            holding the peak within its banks), MC on flat plains and tiny pools. This is the terrain trigger:
   *            one dam can be confined upstream and a plain downstream, so the choice is per reach, not per dam.
   */
  router?: "mc" | "mct" | "auto";
  /** "auto" routes a reach with MCT when its bed slope (before the floor) is at least this; default 0.0015. */
  confineSlopeTrigger?: number;
};

export type RoutedSection = {
  x: number;
  bedM: number;
  /** Bed slope used for stage and for routing (after the floors). */
  slopeStage: number;
  slopeRouting: number;
  peakCms: number;
  /** Time of the peak (s) from the start of the breach hydrograph. */
  tPeakS: number;
  /** Time (s) the discharge first exceeds `arrivalCms` (routing's own arrival; the front formulas give an earlier one). */
  tArrivalS: number | null;
  stageM: number;
  /** Water surface (m) = bed + stage. */
  wseM: number;
  areaM2: number;
  topWidthM: number;
  velocityMps: number;
  capped: boolean;
  /**
   * Largest water volume (m³) held in the stretch from this section to the next at any one moment, and when (s). From
   * the routing's own continuity, S(t) = ∫ (Q_this − Q_next) dt, so it is exactly the storage the conserving scheme
   * carries; summed over stretches it is the 1-D max-envelope volume (each stretch at its own peak), not a release.
   * 0 for the last section (nothing below it in the reach).
   */
  storeMaxM3: number;
  tStoreMaxS: number;
  /** Thinned hydrograph (s, m³/s), when kept. */
  t?: number[];
  q?: number[];
};

export type RoutingResult = {
  sections: RoutedSection[];
  /** Volume in (the hydrograph) and out (at the last section) over the run (m³). */
  volumeInM3: number;
  volumeOutM3: number;
  dtS: number;
  steps: number;
  /** Sub-reaches per reach, for the Courant condition. */
  subReaches: number[];
  notes: string[];
};

/** Reach slopes from thalweg elevations, each floored; a reach below the floor is reported. */
export function reachSlopes(tables: SectionTable[], floor: number): { slopes: number[]; floored: number } {
  const slopes: number[] = [];
  let floored = 0;
  for (let j = 0; j + 1 < tables.length; j++) {
    const dx = tables[j + 1].x - tables[j].x;
    const s = dx > 0 ? (tables[j].bedM - tables[j + 1].bedM) / dx : 0;
    if (s < floor) floored++;
    slopes.push(Math.max(s, floor));
  }
  return { slopes, floored };
}

/** Linear interpolation of the inflow hydrograph (t ascending), 0 outside. */
function inflowAt(t: number[], q: number[], time: number) {
  if (time <= t[0]) return q[0];
  if (time >= t[t.length - 1]) return 0;
  let lo = 0;
  let hi = t.length - 1;
  while (hi - lo > 1) {
    const m = (lo + hi) >> 1;
    if (t[m] <= time) lo = m;
    else hi = m;
  }
  const f = (time - t[lo]) / (t[hi] - t[lo]);
  return q[lo] + f * (q[hi] - q[lo]);
}

/**
 * Route the hydrograph through the sections (ordered by x, the first at or near the breach). The inflow applies
 * at the first section. Returns the peak, its time, stage and velocity at every section.
 */
export function routeMuskingumCunge(
  inflow: { tS: number[]; qCms: number[] },
  tables: SectionTable[],
  opts: RoutingOptions = {},
  arrivalCms = 0.5,
): RoutingResult {
  if (tables.length < 1) throw new Error("Routing needs at least one section.");
  const rough = opts.rough ?? DEFAULT_ROUGHNESS;
  void rough;
  const floorR = opts.slopeFloorRouting ?? 0.0001;
  const floorS = opts.slopeFloorStage ?? 0.0005;
  const notes: string[] = [];
  const { slopes, floored } = reachSlopes(tables, floorR);
  if (floored) notes.push(`${floored} of ${slopes.length} reaches are flatter than the routing slope floor ${floorR} (or adverse) and use the floor.`);
  const n = inflow.qCms.length;
  let ip = 0;
  for (let i = 1; i < n; i++) if (inflow.qCms[i] > inflow.qCms[ip]) ip = i;
  const qPeakIn = inflow.qCms[ip];
  const tp = Math.max(60, inflow.tS[ip] - inflow.tS[0]);
  // Step so the clock lands exactly on the inflow peak: the first section then carries the breach peak itself (the
  // Flood step's "breach peak" equals the Breach step's routed peak), instead of the hydrograph sampled between steps.
  const dt0 = opts.dtS ?? Math.max(1, Math.min(60, tp / 30));
  const tpIn = inflow.tS[ip] - inflow.tS[0];
  const dt = opts.dtS == null && tpIn > 0 ? tpIn / Math.ceil(tpIn / dt0) : dt0;
  const maxT = (opts.maxHours ?? 48) * 3600;
  const frac = opts.recessionFraction ?? 0.02;

  // Sub-reaches and cell coefficients. Each reach is split so the computational Courant number c·Δt/Δx ≈ 1 at the
  // inflow peak (the grid the Muskingum-Cunge scheme is most accurate and stable on), then every cell's K, X and the
  // routing coefficients C0..C2 are computed once, at that reference discharge, and held constant for the whole run.
  //
  // This is the practical (constant-parameter per reach) Muskingum-Cunge of Ponce & Yevjevich: the parameters still
  // vary in space, cell to cell, with each section's own geometry and celerity, but not in time. Recomputing them
  // each step at the local flow (full variable-parameter MC) is not mass-conservative — the implied storage K[XI+(1-X)O]
  // is evaluated with different K,X on the two sides of a step, so the telescoping sum leaves a residual; on the toe
  // and recession the local celerity collapses, K blows up, C0 goes sharply negative and the reach manufactures ~20 %
  // of its own volume. Freezing the coefficients makes C0+C1+C2 = 1 a genuine volume balance and conserves mass to
  // machine precision, at the cost of the stage-dependent wave speed within one event (acceptable for a first layer;
  // the Todini mass-conservative correction is the route to full VPMC if it is ever wanted).
  // A reach is routed with MCT when the terrain trigger says it is confined enough to carry the wave: a real bed slope
  // and a section that holds the inflow peak within its banks (not a flat plain where the peak spills over). "mc" and
  // "mct" force the whole reach one way; "auto" (and the unset default of "mc") decide per reach.
  const router = opts.router ?? "mc";
  const trigger = opts.confineSlopeTrigger ?? 0.0015;
  const mctForReach = (j: number, rawSlope: number): boolean => {
    if (router === "mc") return false;
    if (router === "mct") return true;
    // "auto": confined = a real slope and the upstream section carries the peak within its cut (not capped/spilling).
    const confined = rawSlope >= trigger && !normalDepth(tables[j], qPeakIn, slopes[j]).capped;
    return confined;
  };
  // Raw (unfloored) slopes for the trigger; slopes[] is the floored version used for the hydraulics.
  const rawSlopes: number[] = [];
  for (let j = 0; j + 1 < tables.length; j++) {
    const dx = tables[j + 1].x - tables[j].x;
    rawSlopes.push(dx > 0 ? (tables[j].bedM - tables[j + 1].bedM) / dx : 0);
  }
  const subReaches: number[] = [];
  // Each cell is either a frozen MC cell (C0..C2 fixed) or an MCT cell carrying the geometry to recompute them each
  // step. A null C0 marks an MCT cell.
  const cells: { C0: number; C1: number; C2: number; mct: boolean; dx: number; slope: number; ta: SectionTable; tb: SectionTable }[] = [];
  let mctReaches = 0;
  for (let j = 0; j + 1 < tables.length; j++) {
    const L = tables[j + 1].x - tables[j].x;
    const pa = propsAtQ(tables[j], qPeakIn, slopes[j]);
    const pb = propsAtQ(tables[j + 1], qPeakIn, slopes[j]);
    const c = Math.max(1e-6, 0.5 * (pa.celerity + pb.celerity));
    const top = Math.max(1, 0.5 * (pa.topWidthM + pb.topWidthM));
    const dxMax = c * dt; // Courant number 1 at the reference peak
    const k = Math.max(1, Math.ceil(L / Math.max(1e-6, dxMax)));
    const dx = L / k; // Courant ≤ 1 after the ceil
    const mct = mctForReach(j, rawSlopes[j]);
    if (mct) mctReaches++;
    // Frozen coefficients at the reference peak (used directly by MC cells, and as the MCT cell's first guess). The
    // original K/X form with the physical clamp X∈[0,0.5]; it equals the Courant/Reynolds form (mcCoeffsCourant) when X
    // is unclamped, but the clamp keeps the frozen scheme stable on flat reaches, so it is preserved exactly.
    const K = dx / c;
    const X = Math.max(0, Math.min(0.5, 0.5 * (1 - qPeakIn / (top * slopes[j] * c * dx))));
    const D = 2 * K * (1 - X) + dt;
    const C0 = (dt - 2 * K * X) / D;
    const C1 = (dt + 2 * K * X) / D;
    const C2 = (2 * K * (1 - X) - dt) / D;
    subReaches.push(k);
    for (let s = 0; s < k; s++) cells.push({ C0, C1, C2, mct, dx, slope: slopes[j], ta: tables[j], tb: tables[j + 1] });
  }
  if (router === "auto")
    notes.push(`Terrain router: ${mctReaches} of ${subReaches.length} reaches confined enough for MCT (bed slope ≥ ${trigger}); the rest route with frozen Muskingum-Cunge.`);
  const nc = cells.length;
  const Q = new Float64Array(nc + 1); // discharge at cell boundaries, current step
  const Qp = new Float64Array(nc + 1); // previous step
  const peak = new Float64Array(tables.length).fill(0);
  const tPeak = new Float64Array(tables.length).fill(0);
  const tArr = new Float64Array(tables.length).fill(NaN);
  // Boundary index of each section: section j starts at cell sum(subReaches[0..j-1]).
  const bIdx: number[] = [0];
  for (const k of subReaches) bIdx.push(bIdx[bIdx.length - 1] + k);
  const keep = opts.keepPoints ?? 0;
  const series: { t: number[]; q: number[] }[] = tables.map(() => ({ t: [], q: [] }));
  // Storage in each stretch (section j to j+1), integrated from continuity with the same rectangle rule as volIn and
  // volOut, so Σ store = volIn − volOut at every step.
  const store = new Float64Array(tables.length);
  const storeMax = new Float64Array(tables.length);
  const tStoreMax = new Float64Array(tables.length);
  let volIn = 0;
  let volOut = 0;
  let step = 0;
  let time = inflow.tS[0];
  let lastPeakSeen = 0;
  for (;;) {
    const qin = inflowAt(inflow.tS, inflow.qCms, time);
    Qp.set(Q);
    Q[0] = qin;
    for (let i = 0; i < nc; i++) {
      const cell = cells[i];
      const I1 = Qp[i];
      const I2 = Q[i];
      const O1 = Qp[i + 1];
      if (!cell.mct) {
        // Frozen-coefficient Muskingum step. The max(0,·) only trims a small numerical undershoot on the toe; because
        // the coefficients sum to 1 the balance is otherwise exact (see the cell-build note).
        Q[i + 1] = Math.max(0, cell.C0 * I2 + cell.C1 * I1 + cell.C2 * O1);
        continue;
      }
      // MCT: recompute the coefficients at the reach-referenced discharge q* = ¼(I1+I2+O1+O2), iterating on O2 because
      // it depends on the coefficients it feeds. The coefficients always sum to 1, so each step conserves mass whatever
      // the local celerity and width are (the Todini mass-conservative property); two passes converge here.
      let O2 = Math.max(0, cell.C0 * I2 + cell.C1 * I1 + cell.C2 * O1); // frozen result as the first guess
      for (let it = 0; it < 2; it++) {
        const qRef = Math.max(1e-6, 0.25 * (I1 + I2 + O1 + O2));
        const pa = propsAtQ(cell.ta, qRef, cell.slope);
        const pb = propsAtQ(cell.tb, qRef, cell.slope);
        const c = Math.max(1e-6, 0.5 * (pa.celerity + pb.celerity));
        const B = Math.max(1, 0.5 * (pa.topWidthM + pb.topWidthM));
        const co = mcCoeffsCourant(c, B, cell.slope, cell.dx, dt, qRef);
        O2 = Math.max(0, co.C0 * I2 + co.C1 * I1 + co.C2 * O1);
      }
      Q[i + 1] = O2;
    }
    for (let j = 0; j < tables.length; j++) {
      const q = Q[bIdx[j]];
      if (q > peak[j]) {
        peak[j] = q;
        tPeak[j] = time;
      }
      if (Number.isNaN(tArr[j]) && q >= arrivalCms) tArr[j] = time;
      if (keep > 0) {
        series[j].t.push(time);
        series[j].q.push(q);
      }
    }
    for (let j = 0; j + 1 < tables.length; j++) {
      store[j] += (Q[bIdx[j]] - Q[bIdx[j + 1]]) * dt;
      if (store[j] > storeMax[j]) {
        storeMax[j] = store[j];
        tStoreMax[j] = time;
      }
    }
    volIn += qin * dt;
    volOut += Q[nc] * dt;
    step++;
    time = inflow.tS[0] + step * dt;
    const last = tables.length - 1;
    if (peak[last] > lastPeakSeen) lastPeakSeen = peak[last];
    const hydrographDone = time >= inflow.tS[inflow.tS.length - 1];
    if (hydrographDone && lastPeakSeen > 0 && Q[nc] <= frac * lastPeakSeen && time > tPeak[last]) break;
    if (time - inflow.tS[0] >= maxT) {
      notes.push(`Routing stopped at ${opts.maxHours ?? 48} h before the last section receded to ${(frac * 100).toFixed(0)} % of its peak.`);
      break;
    }
  }
  const thinTo = (t: number[], q: number[]) => {
    if (!keep || t.length <= keep) return { t, q };
    const stride = Math.ceil(t.length / keep);
    let ip2 = 0;
    for (let i = 1; i < q.length; i++) if (q[i] > q[ip2]) ip2 = i;
    const idx = new Set<number>();
    for (let i = 0; i < t.length; i += stride) idx.add(i);
    idx.add(ip2);
    idx.add(t.length - 1);
    const ks = [...idx].sort((a, b) => a - b);
    return { t: ks.map((i) => t[i]), q: ks.map((i) => q[i]) };
  };
  const sections: RoutedSection[] = tables.map((t, j) => {
    // Stage slope: the mean of the adjacent reach slopes, floored for stage.
    const up = j > 0 ? slopes[j - 1] : null;
    const down = j + 1 < tables.length ? slopes[j] : null;
    const raw = up != null && down != null ? 0.5 * (up + down) : (up ?? down ?? floorS);
    const sStage = Math.max(raw, floorS);
    const nd = normalDepth(t, peak[j], sStage);
    const s = keep ? thinTo(series[j].t, series[j].q) : null;
    return {
      x: t.x,
      bedM: t.bedM,
      slopeStage: sStage,
      slopeRouting: down ?? up ?? floorR,
      peakCms: peak[j],
      tPeakS: tPeak[j],
      tArrivalS: Number.isNaN(tArr[j]) ? null : tArr[j],
      stageM: nd.depthM,
      wseM: t.bedM + nd.depthM,
      areaM2: nd.areaM2,
      topWidthM: nd.topWidthM,
      velocityMps: nd.velocityMps,
      capped: nd.capped,
      storeMaxM3: storeMax[j],
      tStoreMaxS: tStoreMax[j],
      ...(s ? { t: s.t, q: s.q } : {}),
    };
  });
  const capped = sections.filter((s) => s.capped).length;
  if (capped) notes.push(`${capped} section${capped === 1 ? "" : "s"} cannot carry the routed peak within the cut height: cut taller or wider sections there.`);
  return { sections, volumeInM3: volIn, volumeOutM3: volOut, dtS: dt, steps: step, subReaches, notes };
}
