/**
 * Demand-driven downstream reach for the routed terrain flood (crest issue #62).
 *
 * The terrain box is sized from the engine's schematic stations before any water is routed (outline.ts demRequest),
 * and the drainage trace stops at the schematic's last station (lastX) or at the box's edge. On a flat plain the
 * schematic's depth decays to the wet threshold long before the real breach wave does: on DWR-2801 the LISFLOOD-FP
 * flood runs 16.6 km from the dam while the box ends about 9 km down, so 59 % of the Runner's flood lay outside the
 * terrain entirely, a forced miss no spread can score. Widening the corridor or following the water barely help, and
 * any extension applied to every dam (a slower decay, a blanket pad on the box) wrecks a small dam that never reaches
 * its own box (DWR-4031 CSI 0.35 → 0.05: its small fixed volume is routed into the wrong cells).
 *
 * So the reach is extended on demand, dam by dam. After the spread is built, the terrain and the trace are grown
 * downstream only while the routed wave still reaches the end of the traced channel wet, has water above a floor still
 * travelling, and was not stopped in a closed basin: at most REACH_MAX_ROUNDS times, each by a step of a quarter of
 * the channel (REACH_STEP_MIN_M..REACH_STEP_MAX_M) along the channel's own end direction, keeping the cells on the
 * same lattice so the ground already traced is sampled exactly as before. A dam whose flood is contained on the first
 * box is never extended: its layer is the single build it always got, cell for cell. When the loop stops with water
 * still at the edge (the round cap, a terrain that would coarsen, a drawn area that clips it) the layer is flagged
 * incomplete downstream with the ac-ft stranded, so the reader knows the flood runs further than the layer shows.
 */
import type { Part } from "./aoi.ts";
import { ACFT } from "./breach.ts";
import { burnMask, type FlowLine } from "./flowlines.ts";
import {
  corridorHalfM,
  type DemGrid,
  type DemRequest,
  demRequest,
  fetchDem,
  OUTLINE_MAX_PX,
  type OutlineInputs,
  type ReachStop,
  type ReachSummary,
  TRACE_PAD_M,
} from "./outline.ts";
import { buildRoutedFlood, type RoutedFloodFail, type RoutedFloodOptions, type RoutedFloodResult, settleRoutedFlood } from "./routed-flood.ts";
import type { Centerline } from "./sections.ts";
import type { Analysis, DamRecord } from "./types.ts";

/** Same equirectangular frame as outline.ts. */
const M_PER_DEG_LAT = 110574;
const M_PER_DEG_LON_EQ = 111320;
const kxOf = (lat: number) => M_PER_DEG_LON_EQ * Math.cos((lat * Math.PI) / 180);

/**
 * Most extension rounds after the first build. Was 4 rounds of at most 4 km (16 km in all), which left a big dam's
 * flood unfinished with most of its water still moving (Edenville 2020: 39,500 ac-ft at the edge after 16 km). Now the
 * terrain keeps growing while the water still reaches its edge, up to REACH_MAX_EXTEND_M in all, this many rounds, or
 * the cell budget (Nick, 2026-11-18).
 */
export const REACH_MAX_ROUNDS = 12;
/** Most the terrain is grown downstream in all (m), however many rounds that takes. */
export const REACH_MAX_EXTEND_M = 60_000;
/** Each round grows the terrain by this fraction of the traced channel's length, within the min/max step. */
export const REACH_STEP_FRACTION = 0.25;
export const REACH_STEP_MIN_M = 1000;
export const REACH_STEP_MAX_M = 8000;
/** The floor on the water still travelling at the edge: this fraction of the release, or REACH_FLOOR_ACFT, the lesser. */
export const REACH_FLOOR_FRACTION = 0.05;
export const REACH_FLOOR_ACFT = 10;
/** A round that would coarsen the terrain's cell past this multiple of the first request's cell is not built. */
export const REACH_CELL_GROWTH = 1.5;
/** The water counts as reaching the channel's end when its last wet stretch ends within this of the trace's end (m). */
export const REACH_END_TOL_M = 150;
/** The grown strip is at least this wide each side of the channel's end direction (m). */
export const REACH_MIN_HALF_M = 1000;
/**
 * When the traced channel runs off the terrain but no wet cell touches its border (the last stretch's pool stops a
 * cell or two short), the reach still grows if at least this share of the release is still travelling: a big flood
 * cut off by the box, not a small one that has spent itself (Edenville stopped "contained" with 98 % still moving).
 */
export const REACH_TRACE_EDGE_SHARE = 0.5;
/** A box cropped back to the river keeps at least this much either side of the traced channel (m), plus the trace pad. */
export const REACH_KEEP_HALF_M = 3000;
/** The channel's end direction is taken over its last this many metres. */
export const REACH_END_RUN_M = 1000;

export type ReachDemand = {
  /** Grow the terrain and build again. */
  extend: boolean;
  /** Why not, when not. */
  stop: ReachStop | null;
  floorAcFt: number;
  travellingAcFt: number;
};

/** Read the demand signal off a routed flood: does the wave still reach the end of the traced channel with water to carry? */
export function reachDemand(rf: RoutedFloodResult | RoutedFloodFail): ReachDemand {
  if (!rf.ok || !rf.demand) return { extend: false, stop: "none", floorAcFt: 0, travellingAcFt: 0 };
  const d = rf.demand;
  const floorAcFt = Math.min(REACH_FLOOR_FRACTION * rf.stats.releasedAcFt, REACH_FLOOR_ACFT);
  const travellingAcFt = d.travellingM3 / ACFT;
  const stop: ReachStop | null = d.stoppedInBasin
    ? "basin"
    : !d.reachedEdge && !(d.traceAtEdge && travellingAcFt >= REACH_TRACE_EDGE_SHARE * rf.stats.releasedAcFt)
      ? "contained"
      : !(travellingAcFt > floorAcFt)
        ? "volume-floor"
        : d.wetEndM < d.channelEndM - REACH_END_TOL_M
          ? "contained"
          : null;
  return { extend: stop == null, stop, floorAcFt, travellingAcFt };
}

/** The traced channel's end and its direction over its last REACH_END_RUN_M (the bearing u when the trace is shorter). */
export function channelEnd(line: Centerline, u: readonly [number, number]): { e: number; n: number; x: number; dir: [number, number] } {
  const pts = line.pts;
  const end = pts[pts.length - 1];
  let run = 0;
  let from = end;
  for (let p = pts.length - 1; p > 0; p--) {
    run += Math.hypot(pts[p].e - pts[p - 1].e, pts[p].n - pts[p - 1].n);
    from = pts[p - 1];
    if (run >= REACH_END_RUN_M) break;
  }
  const de = end.e - from.e;
  const dn = end.n - from.n;
  const len = Math.hypot(de, dn);
  const dir: [number, number] = len > 1 ? [de / len, dn / len] : [u[0], u[1]];
  return { e: end.e, n: end.n, x: end.x, dir };
}

export type ReachExtension = {
  /** Where the strip starts (local metres from loc) and which way it runs. */
  e: number;
  n: number;
  dir: [number, number];
  lengthM: number;
  halfM: number;
};

/**
 * Grow a terrain request to take in a strip lengthM long and 2·halfM wide from (e, n) along dir, plus the trace pad,
 * clipped to the drawn area's box the way demRequest clips. The cells stay on the request's lattice (the box grows by
 * whole cells on every side) while the grown box holds no more than maxPx² cells.
 *
 * Past that budget, when `keep` is given (the traced channel so far and how far either side of it the flood needs),
 * the box is laid out again on the same lattice around just the channel and the new strip: the ground the river has
 * turned away from is dropped (cropped: true). A river that bends away from the first bearing then keeps its fine
 * cells down the valley instead of dragging a rectangle of empty upland along (Edenville: the first box ran 40 km
 * south-south-west, the river turns south-east below Sanford). Only when even that is past the budget is the box laid
 * out afresh at the coarser cell demRequest would use (coarsened: true; the caller decides whether that is allowed).
 */
export function extendRequest(
  prev: DemRequest,
  loc: { lon: number; lat: number },
  ext: ReachExtension,
  parts: Part[] | null,
  maxPx = OUTLINE_MAX_PX,
  keep?: { pts: { e: number; n: number }[]; halfM: number },
): { req: DemRequest; grew: boolean; grewCells: number; coarsened: boolean; cropped: boolean } {
  const kx = kxOf(loc.lat);
  const n: [number, number] = [-ext.dir[1], ext.dir[0]];
  let b: [number, number, number, number] = [prev.bbox[0], prev.bbox[1], prev.bbox[2], prev.bbox[3]];
  const toLL = (x: number, y: number): [number, number] => [loc.lon + x / kx, loc.lat + y / M_PER_DEG_LAT];
  const grow = (box: [number, number, number, number], x: number, y: number): [number, number, number, number] => {
    const [lon, lat] = toLL(x, y);
    return [Math.min(box[0], lon), Math.min(box[1], lat), Math.max(box[2], lon), Math.max(box[3], lat)];
  };
  const strip: [number, number][] = [];
  for (const s of [-TRACE_PAD_M, ext.lengthM + TRACE_PAD_M])
    for (const t of [-(ext.halfM + TRACE_PAD_M), ext.halfM + TRACE_PAD_M]) strip.push([ext.e + ext.dir[0] * s + n[0] * t, ext.n + ext.dir[1] * s + n[1] * t]);
  for (const [x, y] of strip) b = grow(b, x, y);
  let a: [number, number, number, number] | null = null;
  if (parts) {
    a = [180, 90, -180, -90];
    for (const p of parts)
      for (const [x, y] of p.outer) {
        const [lon, lat] = toLL(x, y);
        a = [Math.min(a[0], lon), Math.min(a[1], lat), Math.max(a[2], lon), Math.max(a[3], lat)];
      }
    // Never shrink below what was already fetched: the first request was clipped the same way.
    b = [Math.min(prev.bbox[0], Math.max(b[0], a[0])), Math.min(prev.bbox[1], Math.max(b[1], a[1])), Math.max(prev.bbox[2], Math.min(b[2], a[2])), Math.max(prev.bbox[3], Math.min(b[3], a[3]))];
  }
  const r8 = (v: number) => Math.round(v * 1e8) / 1e8;
  const cellLon = (prev.bbox[2] - prev.bbox[0]) / prev.width;
  const cellLat = (prev.bbox[3] - prev.bbox[1]) / prev.height;
  const budget = maxPx * maxPx;
  /** A box on the previous lattice: whole cells from the previous west/north edges, out (or in) to cover `box`. */
  const onLattice = (box: [number, number, number, number]) => {
    const i0 = Math.floor((box[0] - prev.bbox[0]) / cellLon + 1e-6);
    const i1 = Math.ceil((box[2] - prev.bbox[0]) / cellLon - 1e-6);
    const j0 = Math.floor((prev.bbox[3] - box[3]) / cellLat + 1e-6);
    const j1 = Math.ceil((prev.bbox[3] - box[1]) / cellLat - 1e-6);
    const width = i1 - i0;
    const height = j1 - j0;
    const west = prev.bbox[0] + i0 * cellLon;
    const north = prev.bbox[3] - j0 * cellLat;
    const req: DemRequest = { bbox: [r8(west), r8(north - height * cellLat), r8(west + width * cellLon), r8(north)], width, height, cellM: prev.cellM };
    return { req, grew: i0 < 0 || j0 < 0 || i1 > prev.width || j1 > prev.height };
  };
  // Whole cells on the previous lattice, out from each side.
  const kW = Math.max(0, Math.ceil((prev.bbox[0] - b[0]) / cellLon - 1e-6));
  const kE = Math.max(0, Math.ceil((b[2] - prev.bbox[2]) / cellLon - 1e-6));
  const kS = Math.max(0, Math.ceil((prev.bbox[1] - b[1]) / cellLat - 1e-6));
  const kN = Math.max(0, Math.ceil((b[3] - prev.bbox[3]) / cellLat - 1e-6));
  const width = prev.width + kW + kE;
  const height = prev.height + kN + kS;
  const grew = kW + kE + kN + kS > 0;
  if (width * height <= budget) {
    const west = prev.bbox[0] - kW * cellLon;
    const north = prev.bbox[3] + kN * cellLat;
    const req: DemRequest = {
      bbox: [r8(west), r8(north - height * cellLat), r8(west + width * cellLon), r8(north)],
      width,
      height,
      cellM: prev.cellM,
    };
    return { req, grew, grewCells: width * height - prev.width * prev.height, coarsened: false, cropped: false };
  }
  if (keep && keep.pts.length) {
    // Just the channel so far (each side by keep.halfM) and the new strip, on the same lattice.
    let c: [number, number, number, number] = [180, 90, -180, -90];
    for (const [x, y] of strip) c = grow(c, x, y);
    const h = keep.halfM + TRACE_PAD_M;
    for (const p of keep.pts) for (const [dx, dy] of [[-h, -h], [h, h]]) c = grow(c, p.e + dx, p.n + dy);
    if (a) c = [Math.max(c[0], a[0]), Math.max(c[1], a[1]), Math.min(c[2], a[2]), Math.min(c[3], a[3])];
    const lc = onLattice(c);
    if (lc.req.width * lc.req.height <= budget && lc.req.width > 1 && lc.req.height > 1)
      return { req: lc.req, grew: lc.grew, grewCells: lc.req.width * lc.req.height - prev.width * prev.height, coarsened: false, cropped: true };
  }
  // Past the cell budget: lay the box out afresh at the coarser cell, as demRequest does.
  const wM = (b[2] - b[0]) * kx;
  const hM = (b[3] - b[1]) * M_PER_DEG_LAT;
  const cell = Math.sqrt((wM * hM) / budget);
  const w2 = Math.max(2, Math.ceil(wM / cell));
  const h2 = Math.max(2, Math.ceil(hM / cell));
  const req: DemRequest = {
    bbox: [r8(b[0]), r8(b[3] - (h2 * cell) / M_PER_DEG_LAT), r8(b[0] + (w2 * cell) / kx), r8(b[3])],
    width: w2,
    height: h2,
    cellM: Math.round(cell * 1000) / 1000,
  };
  // Coarser cells can mean fewer of them over a bigger box: growth is the box's, not the cell count's.
  return { req, grew, grewCells: w2 * h2 - prev.width * prev.height, coarsened: true, cropped: false };
}

/**
 * The grown terrain on the same lattice: the cells already fetched are kept and only the new bands around them (north,
 * south, west, east) are fetched, each in tiles. A round used to fetch the whole grown box again (Edenville: 7 million
 * cells a round for a 4 km step). Falls back to one whole fetch when the new box is not on the old lattice.
 */
export async function growGrid(prev: DemGrid, req: DemRequest, fetchFn: (r: DemRequest) => Promise<DemGrid>): Promise<DemGrid> {
  const dlon = (prev.bbox[2] - prev.bbox[0]) / prev.width;
  const dlat = (prev.bbox[3] - prev.bbox[1]) / prev.height;
  // Where the old grid's north-west cell sits in the new one (negative: the new box was cropped on that side).
  const kW = Math.round((prev.bbox[0] - req.bbox[0]) / dlon);
  const kN = Math.round((req.bbox[3] - prev.bbox[3]) / dlat);
  const W = req.width;
  const H = req.height;
  const oi0 = Math.max(0, kW);
  const oi1 = Math.min(W, kW + prev.width);
  const oj0 = Math.max(0, kN);
  const oj1 = Math.min(H, kN + prev.height);
  const onLattice =
    Math.abs(req.cellM - prev.cellM) < 1e-6 &&
    Math.abs((req.bbox[2] - req.bbox[0]) / req.width - dlon) < 1e-9 &&
    Math.abs((req.bbox[3] - req.bbox[1]) / req.height - dlat) < 1e-9 &&
    oi1 > oi0 &&
    oj1 > oj0;
  if (!onLattice) return fetchFn(req);
  const z = new Float32Array(W * H);
  const r8 = (v: number) => Math.round(v * 1e8) / 1e8;
  const band = async (i0: number, j0: number, w: number, h: number) => {
    if (w <= 0 || h <= 0) return;
    const west = req.bbox[0] + i0 * dlon;
    const north = req.bbox[3] - j0 * dlat;
    const g = await fetchFn({ bbox: [r8(west), r8(north - h * dlat), r8(west + w * dlon), r8(north)], width: w, height: h, cellM: req.cellM });
    for (let r = 0; r < h; r++) z.set(g.z.subarray(r * w, r * w + w), (j0 + r) * W + i0);
  };
  await band(0, 0, W, oj0);
  await band(0, oj1, W, H - oj1);
  await band(0, oj0, oi0, oj1 - oj0);
  await band(oi1, oj0, W - oi1, oj1 - oj0);
  for (let r = oj0; r < oj1; r++) {
    const src = (r - kN) * prev.width + (oi0 - kW);
    z.set(prev.z.subarray(src, src + (oi1 - oi0)), r * W + oi0);
  }
  return { ...req, z };
}

/** The inputs with the trace's stop (lastX) pushed stepM further along the bearing; the last station repeats there. */
export function extendInputs(inp: OutlineInputs, stepM: number): OutlineInputs {
  const last = inp.st[inp.st.length - 1];
  const lastX = inp.lastX + stepM;
  return { ...inp, lastX, st: [...inp.st, { x: lastX, h: last.h, d: last.d }] };
}

/** The step a round grows the terrain by: a quarter of the traced channel, within REACH_STEP_MIN_M..REACH_STEP_MAX_M. */
export const reachStepM = (channelEndM: number) => Math.min(REACH_STEP_MAX_M, Math.max(REACH_STEP_MIN_M, REACH_STEP_FRACTION * channelEndM));

export type ReachRound = {
  /** 0 is the first build; 1.. are extension rounds. */
  round: number;
  stage: "fetch" | "build";
  req: DemRequest;
  /** Extension rounds: how far the terrain is being grown and how much water still reached the edge. */
  stepKm: number;
  travellingAcFt: number;
};

export type ReachBuildOptions = RoutedFloodOptions & {
  /** The schematic's inputs (outlineInputs). */
  inputs: OutlineInputs;
  /** The terrain fetch (fetchDem by default; tests and the scorer pass their own). */
  fetch?: (req: DemRequest) => Promise<DemGrid>;
  /** Extension rounds allowed (REACH_MAX_ROUNDS by default; 0 builds once, the way every layer was built before). */
  maxRounds?: number;
  /** Progress, round by round. */
  onRound?: (r: ReachRound) => void;
  /** The routed-flood builder (buildRoutedFlood by default; tests pass a stand-in). */
  build?: typeof buildRoutedFlood;
  /** Most cells on a side of the terrain (OUTLINE_MAX_PX by default; tests shrink it). */
  maxPx?: number;
  /** Terrain cell asked for (m): OUTLINE_CELL_M by default. The cell still grows when the box passes maxPx cells. */
  cellM?: number;
  /**
   * Mapped flowlines through lakes for a terrain box (flowlines.ts fetchFlowlines), burned into flat water so the
   * trace crosses lakes where the river runs. Absent: the trace follows the terrain alone. A failed read is a note.
   */
  flowlines?: (bbox: DemRequest["bbox"]) => Promise<FlowLine[]>;
};

export type ReachBuild = {
  /** The terrain the final layer was built on, its request, and the inputs it was traced with. */
  grid: DemGrid;
  request: DemRequest;
  inputs: OutlineInputs;
  routed: RoutedFloodResult | RoutedFloodFail;
  reach: ReachSummary;
};

/**
 * Fetch the terrain, build the routed flood, and grow the terrain downstream while the flood asks for it (see the
 * module note). The routed result carries a note on how the reach ended; the summary says the same in fields.
 */
export async function buildRoutedFloodReach(dam: DamRecord, result: Analysis, opts: ReachBuildOptions): Promise<ReachBuild> {
  const { inputs: inputs0, fetch: fetchOpt, maxRounds: maxRoundsOpt, onRound, build: buildOpt, maxPx: maxPxOpt, cellM: cellOpt, flowlines, ...rfOpts } = opts;
  // Flowlines for each terrain box, burned into its flat water. Read once per box; a failure turns them off for the run.
  let flowNote: string | null = null;
  let burned = 0;
  const burnFor = async (g: DemGrid): Promise<Uint8Array | null> => {
    if (!flowlines || flowNote) return null;
    try {
      const m = burnMask(g, await flowlines(g.bbox));
      burned = m ? m.reduce((a, v) => a + v, 0) : 0;
      return m;
    } catch (e) {
      flowNote = `The 3DHP flowlines could not be read (${(e as Error).message || String(e)}); the trace crosses lakes by the terrain alone.`;
      return null;
    }
  };
  const fetchFn = fetchOpt ?? fetchDem;
  const build = buildOpt ?? buildRoutedFlood;
  const maxRounds = maxRoundsOpt ?? REACH_MAX_ROUNDS;
  const maxPx = maxPxOpt ?? OUTLINE_MAX_PX;
  let inputs = inputs0;
  // A corridor past the cell budget is cut short rather than coarsened: the rounds below grow it along the channel.
  let req = demRequest(inputs, cellOpt, maxPx, { shorten: true });
  const baseCellM = req.cellM;
  onRound?.({ round: 0, stage: "fetch", req, stepKm: 0, travellingAcFt: 0 });
  let grid = await fetchFn(req);
  onRound?.({ round: 0, stage: "build", req, stepKm: 0, travellingAcFt: 0 });
  // Rounds are built unsettled (the demand signal doesn't depend on it) and only the round kept is settled, below.
  const settleSweeps = rfOpts.settleSweeps;
  let burn = await burnFor(grid);
  let routed = await build(dam, result, grid, { ...rfOpts, inputs, settleSweeps: 0, burn });
  let rounds = 0;
  let extendedM = 0;
  let stop: ReachStop;
  let incomplete = false;
  for (;;) {
    const d = reachDemand(routed);
    if (!d.extend) {
      stop = d.stop ?? "none";
      break;
    }
    if (!routed.ok) {
      stop = "none";
      break;
    }
    if (rounds >= maxRounds || extendedM >= REACH_MAX_EXTEND_M - 1) {
      stop = "round-cap";
      incomplete = true;
      break;
    }
    const end = channelEnd(routed.centerline, inputs.u);
    const stepM = Math.min(reachStepM(end.x), REACH_MAX_EXTEND_M - extendedM);
    const halfM = Math.max(corridorHalfM(inputs.st[inputs.st.length - 1].h), REACH_MIN_HALF_M);
    // The channel so far, and how far either side of it the flood is let reach, for a box cropped back to the river.
    const keepHalfM = Math.max(REACH_KEEP_HALF_M, Math.max(...inputs.st.map((q) => corridorHalfM(q.h))));
    const ext = extendRequest(req, inputs.loc, { e: end.e, n: end.n, dir: end.dir, lengthM: stepM, halfM }, inputs.parts, maxPx, {
      pts: routed.centerline.pts,
      halfM: keepHalfM,
    });
    if (ext.coarsened && ext.req.cellM > REACH_CELL_GROWTH * baseCellM + 1e-9) {
      stop = "dem-limit";
      incomplete = true;
      break;
    }
    if (!ext.grew) {
      stop = "no-growth";
      incomplete = true;
      break;
    }
    const next = extendInputs(inputs, stepM);
    onRound?.({ round: rounds + 1, stage: "fetch", req: ext.req, stepKm: stepM / 1000, travellingAcFt: d.travellingAcFt });
    let grid2: DemGrid | null = null;
    // One retry: the terrain service drops a request now and then, and a whole run of rounds should not end on one.
    for (let attempt = 0; attempt < 2 && !grid2; attempt++) {
      try {
        grid2 = ext.coarsened ? await fetchFn(ext.req) : await growGrid(grid, ext.req, fetchFn);
      } catch {
        grid2 = null;
      }
    }
    if (!grid2) {
      // The terrain service failed on the grown box: keep the round that built, say the flood runs further.
      stop = "fetch-failed";
      incomplete = true;
      break;
    }
    onRound?.({ round: rounds + 1, stage: "build", req: ext.req, stepKm: stepM / 1000, travellingAcFt: d.travellingAcFt });
    const burn2 = await burnFor(grid2);
    const routed2 = await build(dam, result, grid2, { ...rfOpts, inputs: next, settleSweeps: 0, burn: burn2 });
    if (!routed2.ok) {
      // Keep the round that built; say the flood runs further.
      stop = "trace-failed";
      incomplete = true;
      break;
    }
    routed = routed2;
    grid = grid2;
    burn = burn2;
    req = ext.req;
    inputs = next;
    rounds++;
    extendedM += stepM;
  }
  const stranded = incomplete ? reachDemand(routed).travellingAcFt : 0;
  const reach: ReachSummary = {
    rounds,
    extendedKm: Math.round(extendedM / 100) / 10,
    stoppedBecause: stop,
    incomplete,
    strandedAcFt: Math.round(stranded),
    demCellM: req.cellM,
    demCells: req.width * req.height,
  };
  if (routed.ok) await settleRoutedFlood(grid, routed, { sweeps: settleSweeps, minDepthM: rfOpts.minDepthM, yieldFn: rfOpts.yieldFn });
  if (routed.ok) {
    const note = reachNote(reach);
    if (note) routed.notes.push(note);
    if (flowNote) routed.notes.push(flowNote);
    else if (burn && burned) routed.notes.push(`Across lakes the trace follows the USGS 3DHP mapped flowline (${burned.toLocaleString("en-US")} flat water cells steered), not the shortest way over the flat.`);
  }
  return { grid, request: req, inputs, routed, reach };
}

/** The reach in a sentence, for the routed flood's notes ("" when there was no demand signal). */
export function reachNote(r: ReachSummary): string {
  const grown = r.rounds
    ? `The terrain was grown ${r.rounds} time${r.rounds === 1 ? "" : "s"} downstream (+${r.extendedKm.toFixed(1)} km) while the routed wave still reached its edge with water to carry`
    : "";
  switch (r.stoppedBecause) {
    case "none":
      return "";
    case "contained":
      return grown ? `${grown}; it then stayed inside the terrain.` : "Downstream reach: the flood stays inside the terrain fetched, which was not grown.";
    case "basin":
      return grown ? `${grown}; it then ended in a closed basin.` : "Downstream reach: the flood ends in a closed basin inside the terrain, which was not grown.";
    case "volume-floor":
      return grown
        ? `${grown}; what then reached the edge was below the floor.`
        : "Downstream reach: the water reaching the terrain's edge is below the floor, so the terrain was not grown.";
    default: {
      const why =
        r.stoppedBecause === "round-cap"
          ? `the limit of ${REACH_MAX_ROUNDS} rounds or ${REACH_MAX_EXTEND_M / 1000} km of growth`
          : r.stoppedBecause === "dem-limit"
            ? "growing it again would coarsen the terrain past the limit"
            : r.stoppedBecause === "no-growth"
              ? "the drawn area does not let it grow"
              : r.stoppedBecause === "fetch-failed"
                ? "the grown terrain could not be fetched"
                : "the routed flood could not be rebuilt on the grown terrain";
      const head = grown || "Downstream reach: the routed wave reaches the terrain's edge with water to carry";
      return `${head}, and ${r.strandedAcFt} ac-ft still reaches the edge (${why}): the flood runs further downstream than this layer shows. Treat the layer as incomplete there.`;
    }
  }
}

/** A progress line for the desk's build button, per round. */
export function reachStatus(r: ReachRound): string {
  if (r.round === 0)
    return r.stage === "fetch"
      ? `Fetching the 3DEP terrain (${r.req.width} × ${r.req.height} cells at ${Math.round(r.req.cellM)} m)…`
      : "Building the inundation layer on the terrain…";
  return r.stage === "fetch"
    ? `The flood still reaches the terrain's edge with ${Math.round(r.travellingAcFt)} ac-ft to carry: growing the terrain ${r.stepKm.toFixed(1)} km downstream (round ${r.round} of ${REACH_MAX_ROUNDS}, ${r.req.width} × ${r.req.height} cells)…`
    : `Rebuilding the routed flood on the grown terrain (round ${r.round} of ${REACH_MAX_ROUNDS})…`;
}
