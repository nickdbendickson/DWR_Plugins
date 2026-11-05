import { WET_FT } from "./acer.ts";
import { downstreamDirection, readAoi, type Part, type XY } from "./aoi.ts";
import { damLocation, ENGINE_VERSION } from "./engine.ts";
import type { ExtentSensitivity } from "./extent.ts";
import { bandFor, DEPTH_BANDS, floodStrip, type FloodStrip, type StripFeature } from "./floodstrip.ts";
import type { Bbox } from "./plss.ts";
import type { Centerline } from "./sections.ts";
import type { Analysis, DamRecord } from "./types.ts";

/**
 * Terrain outline: a general inundation outline from USGS 3DEP terrain and the desk depths (Nick, 2026-10-30).
 *
 * The desk engine has a straight schematic valley: stations downstream of the breach, each with a half-width and a
 * depth. This lays that depth on real ground:
 *
 *  1. 3DEP DEM (about 10 m, metres NAVD88) over a box around the strip centreline, each side
 *     min(max(3 × halfM, 300 m), 2000 m) wide plus a 500 m margin for the trace, clipped to the drawn area when one
 *     is applied. Requested with adjustAspectRatio=false, so the grid covers exactly that box.
 *  2. Drainage on that DEM: a priority-flood (with a tiny epsilon) from the domain's edges fills pits and ponds to
 *     their spill level and gives every cell a downhill receiver. The channel is the drainage path followed from
 *     the toe below the dam (the Breach Runner's inflow rule: ground at least half the dam height below the top of
 *     dam, on the downstream side; lowest within max(90 m, 8 × height), else nearest within 400 m) to the last wet
 *     station's distance, so it follows the real creek's bends instead of the engine's straight line.
 *  3. Water surface on each channel cell = its filled bed + the desk depth (ft × 0.3048 m) at its distance
 *     downstream (the farthest distance reached so far along the channel; linear between stations).
 *  4. Wet cells: ground below the surface of the channel cell they connect to (4-neighbour spread from the channel)
 *     by at least 0.1 ft, up to min(max(3 × halfM, 300 m), 2000 m) from the channel, inside the DEM box (the
 *     corridor around the strip's centreline) and the drawn area. Wet patches under 20 cells are dropped and dry
 *     holes under 20 cells filled; one 3 × 3 mode filter on the depth bands.
 *  5. Cell edges traced to polygons per depth band, collinear points removed, rings of 4 cells or fewer dropped, one
 *     Chaikin smoothing pass.
 *
 * Approximate: a bathtub fill of the desk depths on terrain, not a hydraulic model. No conveyance, no storage
 * routing, no culverts or road embankments beyond what the DEM shows.
 */

export const OUTLINE_LABEL = "Approximate inundation outline from 3DEP terrain and desk depths, not a hydraulic model";
export const DEM_SERVICE = "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer";
export const DEM_ATTRIBUTION =
  "Terrain: USGS 3D Elevation Program (3DEP) seamless DEM, The National Map (elevation.nationalmap.gov). Public domain; metres NAVD88.";
export const DEM_ATTRIBUTION_SHORT = "Terrain: USGS 3DEP";
export const OUTLINE_OFFLINE =
  "The terrain outline needs a connection: build it once while online (USGS 3DEP terrain), and it is saved with this dam for offline use.";

export const OUTLINE_CELL_M = 10;
/** Largest DEM side requested (the service allows 8000); the cell grows past this. */
export const OUTLINE_MAX_PX = 3000;
const SKIP_M = 60;
const MIN_CELLS = 20;
const NODATA = -9999;
const FT = 0.3048;
const WET_M = WET_FT * FT;
const ACRE_M2 = 4046.8564224;
/** Same equirectangular frame as aoi.ts toLocal. */
const M_PER_DEG_LAT = 110574;
const ACRE_FT_M3 = 1233.48184;
const M_PER_DEG_LON_EQ = 111320;

export const corridorHalfM = (halfM: number) => Math.min(Math.max(3 * halfM, 300), 2000);
/** Bumped when the terrain request or the trace changes, so layers saved before rebuild. 2 = 2026-11-03: exact DEM
 *  box (adjustAspectRatio=false), toe start, trace margin. 3 = 2026-11-03: the routed flood's demand-driven downstream
 *  reach (reach.ts) and the exhausted-stretch cap (spread.ts). 4 = 2026-11-04: the spread's ground by straight-line
 *  nearest channel cell and the saddle pour's lowest-first tie order (spread.ts). 5 = 2026-11-04: the terrain-triggered
 *  router — MCT on confined reaches, frozen Muskingum-Cunge on flat plains and tiny pools (routing.ts). 6 = 2026-11-04:
 *  the outline leaves out pit, pond and side-basin storage and bands on the capped depth. 7 = 2026-11-04: the outline
 *  volume soft limit (about 5× the release), channel pits counted against the release, and suspected crossings. */
export const TRACE_VERSION = 7;

/**
 * The depth cap is the dam height itself (Nick, 2026-11-03: no margin). A breach cannot stand water deeper than the dam
 * that held it back, so a deeper cell is a pit, pond, quarry or DEM sink under the water surface, not a flood depth.
 * Such cells stay wet (the extent is unchanged) but their depth is clamped to the cap and counted, so one 200 ft sink
 * no longer sets a band's depth or the depth bias against a model run (DWR-2801 +49 ft, ND00199 275.8 ft).
 */
export function depthCapFt(heightFt: number | null | undefined): number {
  return typeof heightFt === "number" && Number.isFinite(heightFt) && heightFt > 0 ? heightFt : Infinity;
}

/** Cells deeper than the cap (pits, ponds, sinks), clamped and flagged. */
export type DeepCells = { capFt: number; cells: number; rawMaxFt: number };

/**
 * Per-band depth stats that stay inside the band: each cell's depth is clamped to the cap and then into the range of
 * the band the 3 × 3 mode pass gave it, so a band's min/max/mean never run past its label (the old 0.1–143 ft in the
 * 2–5 ft band). Returns an accumulator; call add(rawFt) per cell, then done().
 */
export function bandDepthAcc(b: number, capFt: number) {
  // A band can only hold cells at or under the cap (they are banded on the capped depth), so its stats never run past
  // the cap either: on a dam under 10 ft the "10 ft and deeper" band is never formed, rather than reporting 10 ft.
  const lo = DEPTH_BANDS[b].minFt;
  const hi = Math.max(lo, Math.min(DEPTH_BANDS[b].maxFt, capFt));
  let dMax = -Infinity;
  let dMin = Infinity;
  let sum = 0;
  let n = 0;
  return {
    add(rawFt: number) {
      const d = Math.min(hi, Math.max(lo, Math.min(capFt, Math.max(WET_FT, rawFt))));
      dMax = Math.max(dMax, d);
      dMin = Math.min(dMin, d);
      sum += d;
      n++;
    },
    done: () => ({ dMax, dMin, dMean: n ? sum / n : 0 }),
  };
}

/** Count the cells deeper than the cap. */
export function deepCellsOf(depthFt: ArrayLike<number>, capFt: number, scale = 1): DeepCells | undefined {
  if (!Number.isFinite(capFt)) return undefined;
  let cells = 0;
  let rawMaxFt = 0;
  for (let k = 0; k < depthFt.length; k++) {
    const d = depthFt[k] / scale;
    if (Number.isFinite(d) && d > capFt) {
      cells++;
      rawMaxFt = Math.max(rawMaxFt, d);
    }
  }
  return cells ? { capFt: Math.round(capFt * 10) / 10, cells, rawMaxFt: Math.round(rawMaxFt * 10) / 10 } : undefined;
}

/** Cells whose depth the outline re-measured from a pit's spill level or a side basin's own drainage level. */
export type PitsExcluded = { cells: number; acFt: number };

/** One line for the desk when pit and pond water was left out, or null. */
export function pitsExcludedNote(s: Pick<OutlineStats, "pitsExcluded">): string | null {
  const p = s.pitsExcluded;
  if (!p) return null;
  return `At least ${Math.round(p.acFt).toLocaleString()} ac-ft of pit, pond and low-basin storage (${p.cells.toLocaleString()} cells) is left out: off the traced channel, depth counts from a pit's spill level, and ground draining lower than the channel bed takes the flow depth rather than the channel's whole surface.`;
}

/** One line for the desk when cells were clamped, or null. */
export function deepCellsNote(s: Pick<OutlineStats, "deepCells">): string | null {
  const d = s.deepCells;
  if (!d) return null;
  return `${d.cells.toLocaleString()} cell${d.cells === 1 ? "" : "s"} under the water surface sit deeper than the dam height (${d.capFt.toFixed(1)} ft; deepest ${d.rawMaxFt.toFixed(0)} ft), likely pits, ponds or terrain sinks. They stay in the extent, but their depth is capped at the dam height in the bands and exports. Check them on the map.`;
}

/**
 * Volume soft limit (Nick, 2026-11-03). The outline aims for about VOLUME_TARGET_RATIO × the released volume (a moving
 * flood's max-depth envelope holds more than the release; the LISFLOOD envelopes on the baseline run 3.6–4.4×). When the
 * outline as built lays more than VOLUME_SHRINK_ABOVE × the release, the water surface above the channel bed is lowered
 * (the desk depths scaled by one factor) until it lays about VOLUME_TARGET_RATIO ×. The 2–6× warning
 * (inundation.ts VOLUME_RATIO_BAND) stays.
 */
export const VOLUME_TARGET_RATIO = 5;
export const VOLUME_SHRINK_ABOVE = 6;
/** Bisection steps and the accepted shortfall below the target (2 %). */
export const VOLUME_LIMIT_STEPS = 24;
export const VOLUME_LIMIT_TOL = 0.02;
/** Releases under this (ac-ft) are labelled SMALL_RELEASE_LABEL: a terrain layer is a poor guide to so little water. */
export const SMALL_RELEASE_ACFT = 25;
export const SMALL_RELEASE_LABEL = "volume-limited, run the Runner";
export const smallRelease = (releasedAcFt: number | null | undefined) =>
  typeof releasedAcFt === "number" && Number.isFinite(releasedAcFt) && releasedAcFt > 0 && releasedAcFt < SMALL_RELEASE_ACFT;

/** What the volume soft limit did: the outline as first laid, and the scale the desk depths took to reach the target. */
export type VolumeLimit = { targetRatio: number; shrinkAbove: number; rawLaidAcFt: number; rawWetAc: number; depthScale: number };

/** One line for the desk when the volume limit lowered the water surface, or null. */
export function volumeLimitNote(s: Pick<OutlineStats, "volumeLimit" | "laidAcFt">, releasedAcFt: number): string | null {
  const v = s.volumeLimit;
  if (!v || !(releasedAcFt > 0)) return null;
  const n = (x: number) => Math.round(x).toLocaleString("en-US");
  const now = typeof s.laidAcFt === "number" ? ` It now lays ${n(s.laidAcFt)} ac-ft (${(s.laidAcFt / releasedAcFt).toFixed(1)}×).` : "";
  return (
    `Volume-limited: as first laid, the outline held ${n(v.rawLaidAcFt)} ac-ft over ${n(v.rawWetAc)} ac, ` +
    `${(v.rawLaidAcFt / releasedAcFt).toFixed(1)}× the ${releasedAcFt < 10 ? releasedAcFt.toFixed(1) : n(releasedAcFt)} ac-ft released (over ${v.shrinkAbove}×), ` +
    `so the water surface above the channel bed was lowered to ${Math.round(v.depthScale * 100)} % of the desk depths, aiming for about ${v.targetRatio}×; depth and extent shrink together.${now}`
  );
}

/**
 * Suspected crossings (Nick, 2026-11-03 22:43 CST: very large and road-crossing channel pits let the flood pass through).
 * A channel pit is flagged when its bowl below the spill level is more than the release still left when the flood
 * reaches it, or more than CROSSING_STORAGE_MULT × the dam's storage (when known), or when a mapped road (a road
 * receptor with a location) lies within ROAD_CROSSING_M of it. Such a "pit" is most often the DEM's picture of a road
 * embankment with its culvert missing, or a stock pond the flood runs through, not storage the breach must fill.
 * CROSSING_STORAGE_MULT is Docs & QA's suggested threshold (Nick ruled only "pass through"): adjustable.
 */
export const CROSSING_STORAGE_MULT = 1.5;
/** A road receptor this close (m) to a channel pit's cells marks the pit as a road crossing. */
export const ROAD_CROSSING_M = 60;
export type CrossingWhy = "over_remaining_release" | "over_storage" | "road";

/** A flagged channel pit: where it is, its bowl, and the modest storage charged against the release for it. */
export type CrossingPit = { atKm: number; lon: number; lat: number; pitAcFt: number; chargedAcFt: number; why: CrossingWhy[] };

/** Pits the channel runs through: how many, their bowls, what the release fills, and where it runs out. */
export type ChannelPits = {
  pits: number;
  /** Bowl storage below the spill levels of every pit the channel runs through (ac-ft). */
  storageAcFt: number;
  /** Of that, what is charged against the release (ac-ft): full bowls, crossings' shallow sheets, the stop pit's share. */
  countedAcFt: number;
  /** The pit the release runs out in: its bowl, what it holds, and where it is (the flood stops there). */
  stop?: { pitAcFt: number; heldAcFt: number; atKm: number; lon?: number; lat?: number; crossing?: boolean; why?: CrossingWhy[] };
  /** Suspected crossings the flood passed through (absent when none, or on layers built before trace version 6). */
  crossings?: CrossingPit[];
};

export type PitBowl = {
  z: ArrayLike<number>;
  spill: ArrayLike<number>;
  /** The desk's flow depth (m) where the channel enters the pit: a crossing is charged its footprint at this depth. */
  flowM?: number;
  /** A mapped road lies at the pit. */
  road?: boolean;
};
export type PitBudget = {
  storageM3: number;
  countedM3: number;
  stop: { index: number; levelM: number; pitM3: number; heldM3: number; crossing: boolean; why: CrossingWhy[] } | null;
  crossings: { index: number; pitM3: number; chargeM3: number; why: CrossingWhy[] }[];
};

/** What a crossing is charged (m³): its wetted footprint at the flow depth, no more than its bowl. */
export function crossingChargeM3(p: PitBowl, cellM2: number): number {
  const d = Math.max(0, p.flowM ?? 0);
  let full = 0;
  for (let i = 0; i < p.z.length; i++) full += Math.max(0, p.spill[i] - p.z[i]);
  return Math.min(full, p.z.length * d) * cellM2;
}

/** Bowl volume (m³) below `level` (no higher than each cell's spill level). */
export function bowlM3(p: PitBowl, level: number, cellM2: number): number {
  let v = 0;
  for (let i = 0; i < p.z.length; i++) v += Math.max(0, Math.min(level, p.spill[i]) - p.z[i]);
  return v * cellM2;
}

/**
 * Channel pits against the release (Nick, 2026-11-03). Pits the channel runs through, in channel order: each takes its
 * bowl below its spill level out of the release before the flood moves on downstream. A pit the remaining release can
 * fill counts in full (as a large release does). A suspected crossing (CROSSING_STORAGE_MULT above: a bowl bigger than
 * the release left, or than 1.5 × the storage, or a mapped road) lets the flood pass through and is charged only its
 * wetted footprint at the flow depth (crossingChargeM3). Only when even that is more than what is left does the
 * release run out there, holding the rest at the level it reaches (bisection), and the flood goes no further. A
 * release of Infinity (unknown) counts every pit in full unless it is over the storage or at a road.
 */
export function channelPitBudget(pits: PitBowl[], releasedM3: number, cellM2: number, opts: { storageM3?: number | null } = {}): PitBudget {
  let left = releasedM3;
  let storageM3 = 0;
  let countedM3 = 0;
  let stop: PitBudget["stop"] = null;
  const crossings: PitBudget["crossings"] = [];
  const stor = typeof opts.storageM3 === "number" && Number.isFinite(opts.storageM3) && opts.storageM3 > 0 ? opts.storageM3 : null;
  for (let i = 0; i < pits.length; i++) {
    const p = pits[i];
    let top = -Infinity;
    let bottom = Infinity;
    for (let c = 0; c < p.z.length; c++) {
      top = Math.max(top, p.spill[c]);
      bottom = Math.min(bottom, p.z[c]);
    }
    const full = bowlM3(p, top, cellM2);
    storageM3 += full;
    if (stop) continue;
    const why: CrossingWhy[] = [];
    if (full > left) why.push("over_remaining_release");
    if (stor != null && full > CROSSING_STORAGE_MULT * stor) why.push("over_storage");
    if (p.road) why.push("road");
    if (why.length === 0) {
      left -= full;
      countedM3 += full;
      continue;
    }
    // A suspected crossing: the flood passes through, charged only its footprint at the flow depth.
    const charge = crossingChargeM3(p, cellM2);
    if (charge <= left) {
      left -= charge;
      countedM3 += charge;
      crossings.push({ index: i, pitM3: full, chargeM3: charge, why });
      continue;
    }
    let lo = bottom;
    let hi = top;
    for (let it = 0; it < 40; it++) {
      const mid = (lo + hi) / 2;
      if (bowlM3(p, mid, cellM2) <= left) lo = mid;
      else hi = mid;
    }
    const held = bowlM3(p, lo, cellM2);
    countedM3 += held;
    stop = { index: i, levelM: lo, pitM3: full, heldM3: held, crossing: true, why };
  }
  return { storageM3, countedM3, stop, crossings };
}

/** One line for the desk on the pits the channel runs through, or null. */
export function channelPitsNote(s: Pick<OutlineStats, "channelPits">): string | null {
  const c = s.channelPits;
  if (!c) return null;
  const n = (x: number) => (x < 10 ? x.toFixed(1) : Math.round(x).toLocaleString("en-US"));
  const head = `The channel runs through ${c.pits} pit${c.pits === 1 ? "" : "s"} holding ${n(c.storageAcFt)} ac-ft below their spill levels; they take their storage out of the release before the flood moves on downstream.`;
  const why: Record<CrossingWhy, string> = {
    over_remaining_release: "bigger than the release left",
    over_storage: `over ${CROSSING_STORAGE_MULT}× the storage`,
    road: "at a mapped road",
  };
  const at = (x: { atKm: number; lon?: number; lat?: number }) =>
    `${x.atKm.toFixed(2)} km` + (typeof x.lon === "number" && typeof x.lat === "number" ? ` (${x.lat.toFixed(5)}, ${x.lon.toFixed(5)})` : "");
  const xs = c.crossings ?? [];
  const cross = xs.length
    ? ` Suspected crossing${xs.length === 1 ? "" : "s"} the flood passes through, charged only ${xs.length === 1 ? "its" : "their"} footprint at the flow depth: ` +
      xs.map((x) => `${at(x)}, ${n(x.pitAcFt)} ac-ft bowl, ${n(x.chargedAcFt)} charged (${x.why.map((w) => why[w]).join(", ")})`).join("; ") +
      "."
    : "";
  return c.stop
    ? `${head}${cross} The release runs out in the pit at ${at(c.stop)} (it fills ${n(c.stop.heldAcFt)} of its ${n(c.stop.pitAcFt)} ac-ft), so the outline stops there` + (c.stop.crossing && c.stop.why?.length ? ` (a suspected crossing: ${c.stop.why.map((w) => why[w]).join(", ")}, but even its footprint at the flow depth is more than the release left).` : ".")
    : `${head}${cross} The release covers them all (${n(c.countedAcFt)} ac-ft charged).`;
}

/**
 * A cell is in a closed depression (pit, pothole, dry pond) when the drainage fill stands this far above its ground.
 * 0.03 m keeps the fill's 1e-5 m epsilon and DEM noise out.
 */
export const PIT_TOL_M = 0.03;

/** Margin (m) added around the corridor box when fetching the DEM, so the drainage trace has room to bend. */
export const TRACE_PAD_M = 500;
export const searchHalfM = (halfM: number) => Math.max(1.5 * halfM, 150);

export type OutlineInputs = {
  loc: { lon: number; lat: number };
  u: XY;
  /** Wet stations: distance downstream (m), half-width (m), desk depth (ft). */
  st: { x: number; h: number; d: number }[];
  parts: Part[] | null;
  fromM: number;
  lastX: number;
  fingerprint: string;
};

export type DemGrid = {
  /** West, south, east, north (degrees, NAD 83 / WGS 84). */
  bbox: Bbox;
  width: number;
  height: number;
  cellM: number;
  /** Row-major from the north-west corner, metres NAVD88; NODATA (-9999) or NaN where missing. */
  z: Float32Array;
};

export type DemRequest = Omit<DemGrid, "z">;

export type OutlineStats = {
  dem: { width: number; height: number; cellM: number; cells: number; bytes: number; bbox: Bbox; missing: number };
  outlineAc: number;
  engineAc: number;
  stripAc: number;
  wetCells: number;
  /** Acres of the wet cells before smoothing (the polygons' outlineAc is a little less: corners cut, tiny rings dropped). */
  wetCellAc: number;
  bands: { band: string; label: string; ac: number }[];
  /** Channel bed (ft NAVD88, pits filled) at the top and the bottom of the traced channel. null when the
   * layer did not trace a channel (the routed/spread flood), so a reader never takes 0 ft as measured. */
  bedTopFt: number | null;
  bedEndFt: number | null;
  /** How the trace's first cell was chosen (absent on outlines built before 2026-11-03: the old axis start). */
  traceStart?: TraceStart;
  /** Length of the traced channel (km), along its bends. */
  channelKm: number;
  /** Distance downstream (km) of the farthest wet cell. null when not traced (routed/spread flood). */
  reachKm: number | null;
  /** The water reached the corridor limit somewhere (it would spread further). */
  hitsCorridor: boolean;
  rings: number;
  /** Connected-component cleanup counts from the terrain trace. null when the layer was not traced that
   * way (the routed/spread flood), so a reader never takes 0 as "none dropped / none filled". */
  droppedPatches: number | null;
  filledHoles: number | null;
  /** Cells deeper than the dam height, clamped (absent when none, or on older layers). */
  deepCells?: DeepCells;
  /** Pit, pond and side-basin water the outline no longer counts (absent when none, or on older layers). */
  pitsExcluded?: PitsExcluded;
  /** Pits the traced channel runs through, counted against the release (absent when none, or on older layers). */
  channelPits?: ChannelPits;
  /** The volume soft limit lowered the water surface (absent when it did not, or on older layers). */
  volumeLimit?: VolumeLimit;
  /** SMALL_RELEASE_LABEL when the release is under SMALL_RELEASE_ACFT (absent otherwise, or on older layers). */
  volumeLabel?: string;
  /** Water laid down by the layer (ac-ft, from the clamped cell depths), when measured. */
  laidAcFt?: number;
  /**
   * How far the routed flood's edge moves with a foot of depth error (extent.ts), the terrain confidence behind the
   * per-dam triage (triage.ts). On a routed layer it is measured on the layer itself; on a terrain outline it is
   * measured on a routed-spread probe built on the same terrain (the outline's bathtub footprint is not what the
   * thresholds were fit on). Absent on layers built before 2026-11-04, or when the probe could not be built.
   */
  extent?: ExtentSensitivity;
};

export type TerrainOutline = FloodStrip & {
  properties: FloodStrip["properties"] & {
    kind: "terrain_outline";
    outline_area_ac: number;
    dem_source: string;
    dem_cell_m: number;
    dem_width: number;
    dem_height: number;
    corridor: string;
    /** The volume soft limit's note, when it lowered the water surface. */
    volume_limit?: string;
    /** "volume-limited, run the Runner" on releases under SMALL_RELEASE_ACFT. */
    volume_label?: string;
    /** The trace version the outline was built with (TRACE_VERSION). */
    trace_version?: number;
    /** Channel pits against the release (trace version 6 on): totals, where the release ran out, suspected crossings. */
    channel_pits?: { pits: number; storage_ac_ft: number; counted_ac_ft: number };
    /** Where the release ran out in a channel pit and the outline stops; null when it did not. */
    channel_pit_stop?: { at_km: number; lon: number; lat: number; held_ac_ft: number; pit_ac_ft: number; crossing: boolean; why: CrossingWhy[] } | null;
    /** Suspected crossings the flood passed through ([] when none). */
    suspected_crossings?: { at_km: number; lon: number; lat: number; pit_ac_ft: number; charged_ac_ft: number; why: CrossingWhy[] }[];
  };
};

/** Routed-flood facts for the desk panel, when the saved layer is the routed flood rather than the bathtub outline. */
export type RoutedSummary = {
  floodedAc: number;
  peakInCfs: number;
  peakOutCfs: number;
  /** Fraction the routed peak fell by, end to end (0..1). */
  routedPeakFellBy: number;
  channelKm: number;
  sections: number;
  cappedSections: number;
  volumeFlag: boolean;
  /** Water volume the fill lays down (ac-ft). */
  fillAcFt: number;
  /** Breach volume released (ac-ft). */
  releasedAcFt: number;
  /** fillAcFt / releasedAcFt — the volume the max-surface envelope invents over the release (method provenance). */
  volumeRatio: number;
  /** How the flood was laid on the terrain; absent on layers built before the volume spread became the default. */
  method?: "stage" | "spread";
  reachedEdge: boolean;
  /** Spread only: ac-ft still travelling at the end of the traced reach, and how far down the channel the water got (km). */
  travellingAcFt?: number;
  wetEndKm?: number;
  /** Spread only: the flood ended in a closed basin that holds all the water still travelling. */
  stoppedInBasin?: boolean;
  /** The demand-driven downstream reach (reach.ts); absent on layers built before it. */
  reach?: ReachSummary;
  /**
   * Spread only; absent on layers built before the cap: stretches that ran out of ground, those held at the cap, and
   * the ac-ft of the slices withheld (water that would stand beyond the ground shown).
   */
  exhaustedStretches?: number;
  cappedStretches?: number;
  unpouredAcFt?: number;
  notes: string[];
};

/** Why the demand-driven reach (reach.ts) stopped extending the terrain downstream. */
export type ReachStop =
  /** The flood does not reach the terrain's edge, or the water stops short of the traced channel's end. */
  | "contained"
  /** The flood ends in a closed basin that holds all the water still travelling. */
  | "basin"
  /** Less water is still travelling at the edge than the floor (5 % of the release or 10 ac-ft, whichever is less). */
  | "volume-floor"
  /** The round cap (REACH_MAX_ROUNDS) was reached with water still at the edge: the layer is incomplete downstream. */
  | "round-cap"
  /** Growing the terrain again would coarsen its cells past the limit: incomplete downstream. */
  | "dem-limit"
  /** The terrain could not grow (the drawn area clips it): incomplete downstream. */
  | "no-growth"
  /** The routed flood could not be rebuilt on the grown terrain; the previous round's layer is kept: incomplete. */
  | "trace-failed"
  /** The grown terrain could not be fetched (the 3DEP service failed); the previous round's layer is kept: incomplete. */
  | "fetch-failed"
  /** No demand signal (the stage fill, or no routed flood). */
  | "none";

export type ReachSummary = {
  /** Extension rounds that were built (0 = the single build every layer used to get). */
  rounds: number;
  /** How far the terrain was grown downstream in all (km, along the channel's end direction). */
  extendedKm: number;
  stoppedBecause: ReachStop;
  /** The wave still reached the edge with water above the floor when the loop stopped: the flood runs further. */
  incomplete: boolean;
  /** ac-ft still travelling at the edge when an incomplete loop stopped (0 when complete). */
  strandedAcFt: number;
  /** The terrain the final layer was built on. */
  demCellM: number;
  demCells: number;
};

export type OutlineCache = {
  fingerprint: string;
  builtAt: string;
  source: string;
  attribution: string;
  stats: OutlineStats;
  outline: TerrainOutline;
  /** Which inundation layer this cache holds. Absent means the bathtub outline (back-compatible with saved dams). */
  layer?: "outline" | "routed";
  /** Present when layer is "routed": the routed-flood facts the panel shows in place of the outline's. */
  routed?: RoutedSummary;
};

export type OutlineState =
  | { state: "none"; message: string }
  | {
      state: "ready";
      outline: TerrainOutline;
      stats: OutlineStats;
      builtAt: string;
      message: null;
      layer: "outline" | "routed";
      routed?: RoutedSummary;
    }
  | { state: "build" | "offline"; message: string };

// ---- inputs and the DEM request -------------------------------------------------------------------------------

/** FNV-1a over a string: a short stable fingerprint of the outline's inputs. */
function fnv(s: string) {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h.toString(16).padStart(8, "0");
}

/** The engine facts the outline is built from (the same centreline, stations and AOI as the strip), or why none. */
export function outlineInputs(dam: DamRecord, result: Analysis): { ok: true; inputs: OutlineInputs } | { ok: false; reason: string } {
  const strip = floodStrip(dam, result);
  if (!strip.strip) return { ok: false, reason: strip.reason.replace("No flood strip", "No terrain outline") };
  const loc = damLocation(dam)!;
  const u = downstreamDirection(dam.geometry, loc).u!;
  const { flood } = result;
  const st = flood.stations
    .filter((s) => s.depthFt >= WET_FT - 1e-9)
    .map((s) => ({ x: s.s * flood.lengthM, h: s.halfM, d: s.depthFt }));
  const aoi = flood.aoi?.applied ? flood.aoi : null;
  let parts: Part[] | null = null;
  let fromM = 0;
  if (aoi) {
    const shape = readAoi(dam.geometry?.aoi, loc);
    if (shape.ok) parts = shape.parts;
    fromM = aoi.nearKm * 1000;
  }
  const r = (n: number) => Math.round(n * 1000) / 1000;
  const fingerprint = fnv(
    JSON.stringify({
      v: ENGINE_VERSION,
      t: TRACE_VERSION,
      o: [r(loc.lon * 1e4), r(loc.lat * 1e4)],
      u: u.map(r),
      st: st.map((s) => [r(s.x), r(s.h), r(s.d)]),
      a: parts,
      f: r(fromM),
      s: Number.isFinite(dam.storageAcFt) ? r(dam.storageAcFt) : null,
      rd: roadPoints(dam).map((q) => [r(q.lon * 1e4), r(q.lat * 1e4)]),
    }),
  );
  return { ok: true, inputs: { loc, u, st, parts, fromM, lastX: st[st.length - 1].x, fingerprint } };
}

const kxOf = (lat: number) => M_PER_DEG_LON_EQ * Math.cos((lat * Math.PI) / 180);

/** Mapped roads the desk knows of: road receptors with a location (Crest holds no road network). */
export function roadPoints(dam: DamRecord): { lon: number; lat: number }[] {
  return (dam.receptors ?? [])
    .filter((r) => r.kind === "road" && typeof r.lon === "number" && typeof r.lat === "number" && Number.isFinite(r.lon) && Number.isFinite(r.lat))
    .map((r) => ({ lon: r.lon!, lat: r.lat! }));
}

/** Interpolate a station value by distance downstream (clamped to the ends). */
function interp(st: OutlineInputs["st"], x: number, key: "h" | "d"): number {
  if (x <= st[0].x) return st[0][key];
  for (let i = 1; i < st.length; i++)
    if (st[i].x >= x) {
      const a = st[i - 1];
      const b = st[i];
      const t = b.x > a.x ? (x - a.x) / (b.x - a.x) : 0;
      return a[key] + t * (b[key] - a[key]);
    }
  return st[st.length - 1][key];
}

/** DEM box and size: the corridor's extent (clipped to the drawn area's), at OUTLINE_CELL_M or coarser. */
export function demRequest(inp: OutlineInputs, cellM = OUTLINE_CELL_M, maxPx = OUTLINE_MAX_PX): DemRequest {
  const { loc, u, st } = inp;
  const n: XY = [-u[1], u[0]];
  const kx = kxOf(loc.lat);
  let b: Bbox = [180, 90, -180, -90];
  const add = (x: number, y: number) => {
    const lon = loc.lon + x / kx;
    const lat = loc.lat + y / M_PER_DEG_LAT;
    b = [Math.min(b[0], lon), Math.min(b[1], lat), Math.max(b[2], lon), Math.max(b[3], lat)];
  };
  for (const s of st) {
    const w = corridorHalfM(s.h);
    for (const t of [-w, w]) add(u[0] * s.x + n[0] * t, u[1] * s.x + n[1] * t);
  }
  // Room around the corridor for the drainage trace: a creek that bends out toward the box edge would otherwise leave
  // the DEM there and end the trace (ND00199's creek runs east 200 m inside the north edge of the bare corridor box).
  const pad = TRACE_PAD_M / M_PER_DEG_LAT;
  const padLon = TRACE_PAD_M / kx;
  b = [b[0] - padLon, b[1] - pad, b[2] + padLon, b[3] + pad];
  if (inp.parts) {
    let a: Bbox = [180, 90, -180, -90];
    for (const p of inp.parts)
      for (const [x, y] of p.outer) {
        const lon = loc.lon + x / kx;
        const lat = loc.lat + y / M_PER_DEG_LAT;
        a = [Math.min(a[0], lon), Math.min(a[1], lat), Math.max(a[2], lon), Math.max(a[3], lat)];
      }
    b = [Math.max(b[0], a[0]), Math.max(b[1], a[1]), Math.min(b[2], a[2]), Math.min(b[3], a[3])];
  }
  const wM = (b[2] - b[0]) * kx;
  const hM = (b[3] - b[1]) * M_PER_DEG_LAT;
  const cell = Math.max(cellM, Math.max(wM, hM) / maxPx);
  const width = Math.max(2, Math.ceil(wM / cell));
  const height = Math.max(2, Math.ceil(hM / cell));
  // Grow the box to whole cells so cells stay square in the local frame.
  const east = b[0] + (width * cell) / kx;
  const south = b[3] - (height * cell) / M_PER_DEG_LAT;
  const r8 = (v: number) => Math.round(v * 1e8) / 1e8;
  return { bbox: [r8(b[0]), r8(south), r8(east), r8(b[3])], width, height, cellM: Math.round(cell * 1000) / 1000 };
}

/** exportImage URL: raw little-endian float32 (bsq), WGS 84 in and out, exactly the requested bbox, bilinear, metres NAVD88. */
export function demUrl(req: DemRequest) {
  const q = new URLSearchParams({
    bbox: req.bbox.join(","),
    bboxSR: "4326",
    imageSR: "4326",
    size: `${req.width},${req.height}`,
    format: "bsq",
    pixelType: "F32",
    noData: String(NODATA),
    interpolation: "RSP_BilinearInterpolation",
    // Without this the server keeps square pixels in degrees and silently widens the latitude extent by about
    // 1/cos(lat) (1.5× in ND), so every cell sits hundreds of metres from where the grid says it is.
    adjustAspectRatio: "false",
    f: "image",
  });
  return `${DEM_SERVICE}/exportImage?${q.toString()}`;
}

/** The float32 raster from an exportImage bsq response (a trailing validity mask, when present, is ignored). */
export function parseBsq(buf: ArrayBuffer, width: number, height: number): Float32Array {
  const need = width * height * 4;
  if (buf.byteLength < need)
    throw new Error(`The 3DEP terrain service returned ${buf.byteLength} bytes; expected ${need} for ${width} × ${height}.`);
  const view = new DataView(buf);
  const z = new Float32Array(width * height);
  for (let i = 0; i < z.length; i++) z[i] = view.getFloat32(i * 4, true);
  return z;
}

type Fetch = (url: string, init?: { signal?: AbortSignal }) => Promise<{ ok: boolean; status: number; arrayBuffer: () => Promise<ArrayBuffer> }>;

/** Waits before retrying a 3DEP answer of 5xx (the service often answers 502 a few times, then serves the image). */
export const DEM_RETRY_MS = [1500, 4000];

export async function fetchDem(req: DemRequest, fetchFn: Fetch = fetch as unknown as Fetch, retryMs: number[] = DEM_RETRY_MS): Promise<DemGrid> {
  const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
  const timer = ctl ? setTimeout(() => ctl.abort(), 90_000) : null;
  try {
    let res = await fetchFn(demUrl(req), ctl ? { signal: ctl.signal } : undefined);
    for (const ms of retryMs) {
      if (res.ok || res.status < 500) break;
      await new Promise((r) => setTimeout(r, ms));
      res = await fetchFn(demUrl(req), ctl ? { signal: ctl.signal } : undefined);
    }
    if (!res.ok) throw new Error(`The 3DEP terrain service answered ${res.status}.`);
    return { ...req, z: parseBsq(await res.arrayBuffer(), req.width, req.height) };
  } catch (e) {
    const m = (e as Error).message || String(e);
    if (/^The 3DEP/.test(m)) throw e;
    throw new Error(`Could not reach the 3DEP terrain service (${m}). ${OUTLINE_OFFLINE}`);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

// ---- the outline ----------------------------------------------------------------------------------------------

export type BuildOptions = {
  /** Called every so many cells so a long build can let the page breathe (the desk passes a setTimeout(0)). */
  yieldFn?: () => Promise<void>;
  builtAt?: Date;
  /**
   * Use these inputs instead of outlineInputs(dam, result): the demand-driven reach (reach.ts) passes the schematic's
   * inputs with lastX (where the trace may stop) pushed downstream, round by round.
   */
  inputs?: OutlineInputs;
};

/** Even-odd scanline mask of the AOI parts (local metres) on the grid. */
function aoiMask(g: DemGrid, inp: OutlineInputs, cellX: (i: number) => number, cellY: (j: number) => number): Uint8Array {
  const m = new Uint8Array(g.width * g.height);
  if (!inp.parts) return m.fill(1);
  const rings = inp.parts.flatMap((p) => [p.outer, ...p.holes]);
  const x0 = cellX(0);
  const dx = cellX(1) - x0;
  for (let j = 0; j < g.height; j++) {
    const y = cellY(j);
    const xs: number[] = [];
    for (const r of rings)
      for (let k = 0; k < r.length; k++) {
        const a = r[k];
        const b = r[(k + 1) % r.length];
        if (a[1] > y !== b[1] > y) xs.push(a[0] + ((y - a[1]) / (b[1] - a[1])) * (b[0] - a[0]));
      }
    xs.sort((p, q) => p - q);
    for (let k = 0; k + 1 < xs.length; k += 2) {
      const i0 = Math.max(0, Math.ceil((xs[k] - x0) / dx));
      const i1 = Math.min(g.width - 1, Math.floor((xs[k + 1] - x0) / dx));
      for (let i = i0; i <= i1; i++) m[j * g.width + i] = 1;
    }
  }
  return m;
}

/** 4-neighbour components of cells where `on(k)`; returns labels (0 = off) and sizes, and which touch the border. */
function components(W: number, H: number, on: (k: number) => boolean) {
  const lab = new Int32Array(W * H);
  const sizes: number[] = [0];
  const border: boolean[] = [false];
  const q = new Int32Array(W * H);
  let id = 0;
  for (let s = 0; s < W * H; s++) {
    if (lab[s] || !on(s)) continue;
    id++;
    let head = 0;
    let tail = 0;
    q[tail++] = s;
    lab[s] = id;
    let n = 0;
    let edge = false;
    while (head < tail) {
      const k = q[head++];
      n++;
      const i = k % W;
      const j = (k - i) / W;
      if (i === 0 || j === 0 || i === W - 1 || j === H - 1) edge = true;
      const nb = [i > 0 ? k - 1 : -1, i < W - 1 ? k + 1 : -1, j > 0 ? k - W : -1, j < H - 1 ? k + W : -1];
      for (const t of nb)
        if (t >= 0 && !lab[t] && on(t)) {
          lab[t] = id;
          q[tail++] = t;
        }
    }
    sizes.push(n);
    border.push(edge);
  }
  return { lab, sizes, border };
}

type Ring = { pts: XY[]; area: number };

/** Directed boundary edges of the cells with mask[k] (inside on the left, north up), linked into rings. */
export function traceRings(mask: Uint8Array, W: number, H: number): Ring[] {
  const VW = W + 1;
  // Outgoing edge bits per vertex: E=1, N=2, W=4, S=8. Vertex (vi, vj): vj counts grid lines from the north.
  const out = new Uint8Array(VW * (H + 1));
  const at = (i: number, j: number) => i >= 0 && j >= 0 && i < W && j < H && mask[j * W + i] === 1;
  for (let j = 0; j < H; j++)
    for (let i = 0; i < W; i++) {
      if (!at(i, j)) continue;
      if (!at(i, j + 1)) out[(j + 1) * VW + i] |= 1; // bottom: BL -> BR (east)
      if (!at(i + 1, j)) out[(j + 1) * VW + i + 1] |= 2; // right: BR -> TR (north)
      if (!at(i, j - 1)) out[j * VW + i + 1] |= 4; // top: TR -> TL (west)
      if (!at(i - 1, j)) out[j * VW + i] |= 8; // left: TL -> BL (south)
    }
  const DI = [1, 0, -1, 0];
  const DJ = [0, -1, 0, 1];
  const rings: Ring[] = [];
  for (let v0 = 0; v0 < out.length; v0++) {
    while (out[v0]) {
      let d = [0, 1, 2, 3].find((k) => out[v0] & (1 << k))!;
      const d0 = d;
      let v = v0;
      const pts: XY[] = [];
      let prev = -1;
      for (;;) {
        out[v] &= ~(1 << d);
        const vi = v % VW;
        const vj = (v - vi) / VW;
        if (d !== prev) pts.push([vi, -vj]);
        prev = d;
        v = (vj + DJ[d]) * VW + vi + DI[d];
        if (v === v0) break;
        // Prefer a left turn, then straight, then right: diagonal pinches stay apart (4-neighbour cells).
        const opts = [(d + 1) % 4, d, (d + 3) % 4];
        const nd = opts.find((k) => out[v] & (1 << k));
        if (nd === undefined) break;
        d = nd;
      }
      // Drop the start point when the closing edge runs straight through it.
      if (pts.length > 2 && prev === d0) pts.shift();
      let a = 0;
      for (let k = 0; k < pts.length; k++) {
        const p = pts[k];
        const q = pts[(k + 1) % pts.length];
        a += p[0] * q[1] - q[0] * p[1];
      }
      rings.push({ pts, area: a / 2 });
    }
  }
  return rings;
}

function pip(p: XY, r: XY[]) {
  let inside = false;
  for (let i = 0, j = r.length - 1; i < r.length; j = i++) {
    const a = r[i];
    const b = r[j];
    if (a[1] > p[1] !== b[1] > p[1] && p[0] < ((b[0] - a[0]) * (p[1] - a[1])) / (b[1] - a[1]) + a[0]) inside = !inside;
  }
  return inside;
}

function shoelace(r: XY[]) {
  let s = 0;
  for (let i = 0; i < r.length; i++) {
    const a = r[i];
    const b = r[(i + 1) % r.length];
    s += a[0] * b[1] - b[0] * a[1];
  }
  return s / 2;
}

function chaikin(r: XY[]): XY[] {
  const o: XY[] = [];
  for (let k = 0; k < r.length; k++) {
    const p = r[k];
    const q = r[(k + 1) % r.length];
    o.push([0.75 * p[0] + 0.25 * q[0], 0.75 * p[1] + 0.25 * q[1]]);
    o.push([0.25 * p[0] + 0.75 * q[0], 0.25 * p[1] + 0.75 * q[1]]);
  }
  return o;
}

/** Polygons (outer + holes, vertex units) of one band mask; rings of 4 cells or fewer dropped. */
function polygonsOf(mask: Uint8Array, W: number, H: number): XY[][][] {
  const rings = traceRings(mask, W, H).filter((r) => Math.abs(r.area) > 4);
  const outers = rings.filter((r) => r.area > 0).map((r) => {
    const bb = [Infinity, Infinity, -Infinity, -Infinity];
    for (const [x, y] of r.pts) {
      bb[0] = Math.min(bb[0], x);
      bb[1] = Math.min(bb[1], y);
      bb[2] = Math.max(bb[2], x);
      bb[3] = Math.max(bb[3], y);
    }
    return { r, bb, holes: [] as XY[][] };
  });
  for (const h of rings.filter((r) => r.area < 0)) {
    // A point half a cell to the left of the hole's first edge: a cell of the band, inside its own outer ring.
    const [a, b] = [h.pts[0], h.pts[1]];
    const len = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1;
    const p: XY = [(a[0] + b[0]) / 2 - (0.5 * (b[1] - a[1])) / len, (a[1] + b[1]) / 2 + (0.5 * (b[0] - a[0])) / len];
    let best: (typeof outers)[number] | null = null;
    for (const o of outers) {
      if (p[0] < o.bb[0] || p[0] > o.bb[2] || p[1] < o.bb[1] || p[1] > o.bb[3]) continue;
      if (pip(p, o.r.pts) && (!best || o.r.area < best.r.area)) best = o;
    }
    if (best) best.holes.push(h.pts);
  }
  return outers.map((o) => [o.r.pts, ...o.holes]);
}

/** Binary min-heap of cell indices keyed by `key[k]`. */
class MinHeap {
  private a: Int32Array;
  private key: Float64Array;
  size = 0;
  constructor(key: Float64Array) {
    this.key = key;
    this.a = new Int32Array(Math.max(16, key.length));
  }
  push(k: number) {
    const a = this.a;
    const key = this.key;
    let i = this.size++;
    a[i] = k;
    while (i > 0) {
      const p = (i - 1) >> 1;
      if (key[a[p]] <= key[k]) break;
      a[i] = a[p];
      i = p;
    }
    a[i] = k;
  }
  pop(): number {
    const a = this.a;
    const key = this.key;
    const top = a[0];
    const last = a[--this.size];
    let i = 0;
    for (;;) {
      let c = 2 * i + 1;
      if (c >= this.size) break;
      if (c + 1 < this.size && key[a[c + 1]] < key[a[c]]) c++;
      if (key[a[c]] >= key[last]) break;
      a[i] = a[c];
      i = c;
    }
    a[i] = last;
    return top;
  }
}

const r7 = (n: number) => Math.round(n * 1e7) / 1e7;
const r3 = (n: number) => Math.round(n * 1e3) / 1e3;

export type PolygonizedDepth = {
  /** FloodStrip-shaped band polygons (lon/lat), so the map, legend, PLSS/911 lists and exports read it like the strip. */
  outline: TerrainOutline;
  bands: OutlineStats["bands"];
  /** Drawn (smoothed) polygon area, acres. */
  areaAc: number;
  wetCells: number;
  deepCells?: DeepCells;
};

/**
 * Turn a depth grid (feet, row-major from the NW, 0 or NaN where dry) on a DEM frame into FloodStrip-shaped band
 * polygons — the same depth-band → 3×3 mode filter → cell-edge trace → Chaikin pipeline the terrain outline draws,
 * so a routed flood (routed-flood.ts) draws and exports exactly like the bathtub outline. Pure; no `along`, so the
 * per-band from_km/to_km are left at 0. Shares the band tracing with buildOutline but does not run its hydrology.
 *
 * NOTE: the band → polygon tail here mirrors buildOutline's; if that pipeline changes, change both (or unify them).
 */
export function polygonizeDepth(
  g: DemGrid,
  depthFt: Float32Array,
  opts: { common: FloodStrip["properties"]; label: string; title: string; capFt?: number },
): PolygonizedDepth {
  const capFt = opts.capFt ?? Infinity;
  const W = g.width;
  const H = g.height;
  const N = W * H;
  const dlon = (g.bbox[2] - g.bbox[0]) / W;
  const dlat = (g.bbox[3] - g.bbox[1]) / H;
  const midLat = (g.bbox[1] + g.bbox[3]) / 2;
  const kx = M_PER_DEG_LON_EQ * Math.cos((midLat * Math.PI) / 180);
  const cellM2 = dlon * kx * dlat * M_PER_DEG_LAT;
  const toLonLat = (p: XY): [number, number] => [r7(g.bbox[0] + p[0] * dlon), r7(g.bbox[3] + p[1] * dlat)];
  const wet = (k: number) => Number.isFinite(depthFt[k]) && depthFt[k] >= WET_FT - 1e-9;

  // Depth bands + one 3×3 mode pass, as the outline does.
  const idx = new Map(DEPTH_BANDS.map((b, i) => [b.id, i]));
  const band0 = new Int8Array(N).fill(-1);
  for (let k = 0; k < N; k++) if (wet(k)) band0[k] = idx.get(bandFor(Math.max(WET_FT, Math.min(capFt, depthFt[k]))).id)!;
  const band = new Int8Array(band0);
  const cnt = new Int32Array(DEPTH_BANDS.length);
  for (let j = 0; j < H; j++)
    for (let i = 0; i < W; i++) {
      const k = j * W + i;
      if (band0[k] < 0) continue;
      cnt.fill(0);
      for (let dj = -1; dj <= 1; dj++)
        for (let di = -1; di <= 1; di++) {
          const ii = i + di;
          const jj = j + dj;
          if (ii < 0 || jj < 0 || ii >= W || jj >= H) continue;
          const b = band0[jj * W + ii];
          if (b >= 0) cnt[b]++;
        }
      let best = band0[k];
      for (let b = 0; b < cnt.length; b++) if (cnt[b] > cnt[best]) best = b;
      band[k] = best;
    }

  const features: StripFeature[] = [];
  const bands: OutlineStats["bands"] = [];
  let wetCells = 0;
  let polyAc = 0;
  for (let b = 0; b < DEPTH_BANDS.length; b++) {
    const mask = new Uint8Array(N);
    let cells = 0;
    const acc = bandDepthAcc(b, capFt);
    for (let k = 0; k < N; k++)
      if (band[k] === b) {
        mask[k] = 1;
        cells++;
        acc.add(depthFt[k]);
      }
    if (cells === 0) continue;
    const { dMax, dMin, dMean } = acc.done();
    wetCells += cells;
    const polys = polygonsOf(mask, W, H);
    if (polys.length === 0) continue;
    let cellArea = 0;
    const coords = polys.map((rings) =>
      rings.map((r, ri) => {
        const smooth = chaikin(r);
        cellArea += (ri === 0 ? 1 : -1) * Math.abs(shoelace(smooth));
        const sm = smooth.map(toLonLat);
        return [...sm, sm[0]];
      }),
    );
    const ac = (cellArea * cellM2) / ACRE_M2;
    polyAc += ac;
    bands.push({ band: DEPTH_BANDS[b].id, label: DEPTH_BANDS[b].label, ac: r3(ac) });
    features.push({
      type: "Feature",
      properties: {
        ...opts.common,
        disclaimer: opts.label,
        band: DEPTH_BANDS[b].id,
        band_label: DEPTH_BANDS[b].label,
        depth_max_ft: r3(dMax),
        depth_min_ft: r3(dMin),
        depth_mean_ft: r3(dMean),
        from_km: 0,
        to_km: 0,
        segments: 0,
        area_ac: r3(ac),
        color: DEPTH_BANDS[b].color,
      },
      geometry: coords.length === 1 ? { type: "Polygon", coordinates: coords[0] } : { type: "MultiPolygon", coordinates: coords },
    });
  }

  const areaAc = r3(polyAc);
  const outline: TerrainOutline = {
    type: "FeatureCollection",
    properties: {
      ...opts.common,
      disclaimer: opts.label,
      title: opts.title,
      kind: "terrain_outline",
      strip_area_ac: areaAc,
      outline_area_ac: areaAc,
      dem_source: DEM_SERVICE,
      trace_version: TRACE_VERSION,
      dem_cell_m: r3(g.cellM),
      dem_width: W,
      dem_height: H,
      corridor: "none (routed flood on terrain)",
    },
    features,
  };
  const deepCells = deepCellsOf(depthFt, capFt);
  return { outline, bands, areaAc, wetCells, ...(deepCells ? { deepCells } : {}) };
}

/**
 * Build the outline on a DEM. Returns FloodStrip-shaped band features (so the PLSS and 911 lists, the map legend and
 * the exports read it like the strip) and the numbers behind it.
 */
/** How the traced channel's first cell was chosen. */
export type TraceStart = {
  /** "toe": lowest ground in the usual radius; "toe-wide": nearest low-enough ground out to TOE_WIDE_M; "axis": none
   *  qualified, so the old start on the downstream axis SKIP_M below the breach. */
  how: "toe" | "toe-wide" | "axis";
  /** Distance from the dam point (m) and ground below the top of dam (ft) at the start cell; absent for "axis". */
  fromDamM?: number;
  belowTopFt?: number;
};

/** Second search radius for the toe, as the Runner's (terrain.py): a wide embankment can put the toe out there. */
export const TOE_WIDE_M = 400;

type ToeGrid = {
  W: number;
  H: number;
  cellX: (i: number) => number;
  cellY: (j: number) => number;
  along: Float32Array;
  dom: Uint8Array;
  a0: number;
  cellM: number;
  valid: (k: number) => boolean;
};

/**
 * The toe below the dam, where the channel trace starts. Mirrors the Breach Runner's find_inflow_point: among cells on
 * the downstream side of the dam (inside the drainage domain), take those at least half the dam height below the top
 * of dam; in the usual radius max(90 m, 8 × height) the LOWEST (ties to the nearest), else out to TOE_WIDE_M the
 * NEAREST (ties to the lower; the lowest of a wide circle can be a far valley). k = -1 when none qualifies.
 */
export function findToe(
  g: DemGrid,
  dam: Pick<DamRecord, "heightFt">,
  crestNavdFt: number,
  t: ToeGrid,
): { k: number; start: TraceStart } {
  const none = { k: -1, start: { how: "axis" as const } };
  const hM = dam.heightFt * FT;
  if (!(hM > 0)) return none;
  // Top of dam: the tied crest, or the highest ground within a cell or two of the dam point when there is none.
  let topM = crestNavdFt * FT;
  const r1 = Math.max(90, 8 * hM);
  const rr = Math.max(r1, TOE_WIDE_M);
  const cand: { k: number; d: number; z: number }[] = [];
  let groundTop = -Infinity;
  for (let j = 0; j < t.H; j++) {
    const y = t.cellY(j);
    if (Math.abs(y) > rr) continue;
    for (let i = 0; i < t.W; i++) {
      const x = t.cellX(i);
      const d = Math.hypot(x, y);
      if (d > rr) continue;
      const k = j * t.W + i;
      if (!t.valid(k)) continue;
      if (d <= 2 * t.cellM) groundTop = Math.max(groundTop, g.z[k]);
      if (t.dom[k] && t.along[k] > t.a0) cand.push({ k, d, z: g.z[k] });
    }
  }
  if (!Number.isFinite(topM)) topM = groundTop;
  if (!Number.isFinite(topM)) return none;
  const need = topM - 0.5 * hM;
  const ok = cand.filter((c) => c.z <= need);
  const pick = (c: { k: number; d: number; z: number }, how: "toe" | "toe-wide") => ({
    k: c.k,
    start: { how, fromDamM: r3(c.d), belowTopFt: r3((topM - c.z) / FT) },
  });
  const inner = ok.filter((c) => c.d <= r1);
  if (inner.length) {
    inner.sort((a, b) => Math.round(a.z * 1000) - Math.round(b.z * 1000) || a.d - b.d);
    return pick(inner[0], "toe");
  }
  if (ok.length) {
    ok.sort((a, b) => a.d - b.d || a.z - b.z);
    return pick(ok[0], "toe-wide");
  }
  return none;
}

/**
 * Build the terrain outline, or (traceOnly) only the drainage trace. The trace is the first half of the work: the
 * drainage priority-flood, the toe and the channel path. The bathtub flood, the speck/hole cleanup, the band mode pass
 * and the polygonisation are the expensive second half, and the routed flood (routed-flood.ts) needs only the trace
 * (its section cutter works from the centreline). traceOnly returns right after the centreline so that build skips the
 * second half; the full build runs the whole body unchanged. (M12)
 */
async function buildOutlineImpl(
  dam: DamRecord,
  result: Analysis,
  g: DemGrid,
  opts: BuildOptions,
  traceOnly: boolean,
): Promise<{ ok: true; cache: OutlineCache | null; centerline: Centerline } | { ok: false; reason: string }> {
  const io = outlineInputs(dam, result);
  if (!io.ok) return io;
  const inp = opts.inputs ?? io.inputs;
  const strip = floodStrip(dam, result).strip!;
  const { loc, u, st } = inp;
  const W = g.width;
  const H = g.height;
  const N = W * H;
  const kx = kxOf(loc.lat);
  const dlon = (g.bbox[2] - g.bbox[0]) / W;
  const dlat = (g.bbox[3] - g.bbox[1]) / H;
  const cellX = (i: number) => (g.bbox[0] + (i + 0.5) * dlon - loc.lon) * kx;
  const cellY = (j: number) => (g.bbox[3] - (j + 0.5) * dlat - loc.lat) * M_PER_DEG_LAT;
  const cellM2 = dlon * kx * dlat * M_PER_DEG_LAT;
  const cellM = Math.sqrt(cellM2);
  let tick = 0;
  const breathe = async (n: number) => {
    tick += n;
    if (opts.yieldFn && tick > 150_000) {
      tick = 0;
      await opts.yieldFn();
    }
  };
  const valid = (k: number) => {
    const z = g.z[k];
    return Number.isFinite(z) && z > NODATA + 1 && z < 9000;
  };
  let missing = 0;
  for (let k = 0; k < N; k++) if (!valid(k)) missing++;

  // Along (a) the centreline per cell, and the domain: valid ground, inside the drawn area, below the breach.
  const inAoi = aoiMask(g, inp, cellX, cellY);
  const along = new Float32Array(N);
  const dom = new Uint8Array(N);
  const a0 = Math.max(0, inp.fromM);
  for (let j = 0; j < H; j++) {
    const y = cellY(j);
    for (let i = 0; i < W; i++) {
      const k = j * W + i;
      const a = cellX(i) * u[0] + y * u[1];
      along[k] = a;
      if (a >= a0 && inAoi[k] && valid(k)) dom[k] = 1;
    }
    await breathe(W);
  }
  const cellAt = (x: number, y: number) => {
    const i = Math.floor((loc.lon + x / kx - g.bbox[0]) / dlon);
    const j = Math.floor((g.bbox[3] - (loc.lat + y / M_PER_DEG_LAT)) / dlat);
    return i < 0 || j < 0 || i >= W || j >= H ? -1 : j * W + i;
  };
  const nbrs = (k: number, out: Int32Array) => {
    const i = k % W;
    const j = (k - i) / W;
    out[0] = i > 0 ? k - 1 : -1;
    out[1] = i < W - 1 ? k + 1 : -1;
    out[2] = j > 0 ? k - W : -1;
    out[3] = j < H - 1 ? k + W : -1;
    out[4] = i > 0 && j > 0 ? k - W - 1 : -1;
    out[5] = i < W - 1 && j > 0 ? k - W + 1 : -1;
    out[6] = i > 0 && j < H - 1 ? k + W - 1 : -1;
    out[7] = i < W - 1 && j < H - 1 ? k + W + 1 : -1;
  };
  const nb = new Int32Array(8);

  // Drainage: priority-flood with a small epsilon from the domain's edge (not the cut just below the breach), so pits
  // and ponds fill to their spill level and every cell has a downhill receiver toward an edge.
  const fill = new Float64Array(N).fill(NaN);
  const recv = new Int32Array(N).fill(-1);
  const heap = new MinHeap(fill);
  const upstreamCut = a0 + 2 * cellM;
  const isEdge = (k: number) => {
    nbrs(k, nb);
    for (let t = 0; t < 4; t++) if (nb[t] < 0 || !dom[nb[t]]) return true;
    return false;
  };
  for (let k = 0; k < N; k++)
    if (dom[k] && along[k] > upstreamCut && isEdge(k)) {
      fill[k] = g.z[k];
      heap.push(k);
    }
  if (heap.size === 0) return { ok: false, reason: "No terrain outline: the terrain has no downstream edge to drain to." };
  let popped = 0;
  while (heap.size > 0) {
    const c = heap.pop();
    nbrs(c, nb);
    for (let t = 0; t < 8; t++) {
      const m = nb[t];
      if (m < 0 || !dom[m] || !Number.isNaN(fill[m])) continue;
      fill[m] = Math.max(g.z[m], fill[c] + 1e-5);
      recv[m] = c;
      heap.push(m);
    }
    if ((++popped & 0x3ffff) === 0) await breathe(0x40000);
  }
  await breathe(N);

  // The old start, kept as the fallback when no ground below the dam qualifies as the toe: on the centreline,
  // SKIP_M below the breach, or the nearest domain cell on the cross-line there.
  const fallbackStart = () => {
    const ax = Math.max(SKIP_M, a0 + cellM);
    let k = cellAt(u[0] * ax, u[1] * ax);
    if (k >= 0 && dom[k]) return k;
    let best = Infinity;
    const n: XY = [-u[1], u[0]];
    for (let t = -searchHalfM(st[0].h); t <= searchHalfM(st[0].h); t += cellM / 2) {
      const c = cellAt(u[0] * ax + n[0] * t, u[1] * ax + n[1] * t);
      if (c >= 0 && dom[c] && Math.abs(t) < best) {
        best = Math.abs(t);
        k = c;
      }
    }
    return k;
  };

  // Channel start: the toe below the dam, found the way the Breach Runner finds its inflow point (terrain.py
  // find_inflow_point), then the drainage followed from there. A fixed step down the bearing can land on a valley
  // side or a knoll when the dam point sits off the creek or the bearing is a few degrees out, and the trace then
  // runs down a different drainage, with a bed above the crest (DWR-4031, DWR-6141).
  const toe = findToe(g, dam, result.datum.crestNavdFt, { W, H, cellX, cellY, along, dom, a0, cellM, valid });
  let k0 = toe.k;
  if (k0 < 0) k0 = fallbackStart();
  if (k0 < 0 || !dom[k0]) return { ok: false, reason: "No terrain outline: no terrain found just below the breach." };
  const path: number[] = [];
  const pathA: number[] = [];
  let amax = along[k0];
  for (let k = k0; k >= 0 && path.length < N; k = recv[k]) {
    amax = Math.max(amax, along[k]);
    if (amax > inp.lastX) break;
    path.push(k);
    pathA.push(amax);
  }
  let channelM = 0;
  for (let p = 1; p < path.length; p++) {
    const di = (path[p] % W) - (path[p - 1] % W);
    channelM += di !== 0 && Math.abs(path[p] - path[p - 1]) !== 1 ? cellM * Math.SQRT2 : cellM;
  }

  // The traced channel as a local-frame polyline, for the section cutter (sections.ts) and the routed flood. Sibling
  // to the cache, so it is not part of what is saved on the dam. Built here so a traceOnly caller can return before the
  // bathtub flood and polygonisation.
  const centerline: Centerline = {
    loc: { lon: loc.lon, lat: loc.lat },
    pts: path.map((k, p) => {
      const i = k % W;
      return { e: cellX(i), n: cellY((k - i) / W), x: pathA[p] };
    }),
  };
  if (traceOnly) return { ok: true, centerline, cache: null };

  // Flood (height above the nearest channel cell): each cell takes the water surface of its nearest channel cell
  // (filled bed + the desk depth at that cell's distance downstream) and is wet when its ground is under that surface
  // by 0.1 ft, within the corridor width of that channel cell, and connected (4-neighbour) to the channel.
  const deskD = new Float32Array(N);
  const roomC = new Float32Array(N);
  const src = new Int32Array(N).fill(-1);
  /** Index along the path of each channel cell (its first visit). */
  const pathIdx = new Int32Array(N).fill(-1);
  const q = new Int32Array(N);
  let tail = 0;
  for (let p = 0; p < path.length; p++) {
    const k = path[p];
    if (src[k] >= 0) continue;
    deskD[k] = interp(st, pathA[p], "d") * FT;
    roomC[k] = corridorHalfM(interp(st, pathA[p], "h"));
    src[k] = k;
    pathIdx[k] = p;
    q[tail++] = k;
  }
  const dist2 = (m: number, s0: number) => {
    const di = (m % W) - (s0 % W);
    const dj = (m - (m % W)) / W - (s0 - (s0 % W)) / W;
    return di * di + dj * dj;
  };
  // Nearest channel cell by an 8-neighbour sweep that keeps the closer source (an approximate distance transform).
  for (let head = 0; head < tail; head++) {
    const k = q[head];
    const s0 = src[k];
    nbrs(k, nb);
    for (let t = 0; t < 8; t++) {
      const m = nb[t];
      if (m < 0 || !dom[m]) continue;
      if (src[m] < 0) {
        src[m] = s0;
        q[tail++] = m;
      } else if (src[m] !== s0 && dist2(m, s0) < dist2(m, src[m])) src[m] = s0;
    }
    if ((head & 0x3ffff) === 0) await breathe(0x40000);
  }
  // Pits, ponds and side basins (Docs & QA on #57, 2026-11-03). The channel's surface is fill + d at the channel, and a
  // cell used to take it whole: ground that drains to a lower level than that channel bed (a pond or low basin beside a
  // steep reach, DWR-4031's 1567 ft pond under a 1584 ft bed) stood 17–21 ft deep, and a closed pothole off the channel
  // counted its whole bowl. Neither is water the breach can lay there: water reaching a lower basin runs on down it at
  // about the flow depth, and a pothole off the channel is water already there (or storage a small release never fills).
  // So each cell's surface is the lower of the channel's and its own drainage level (fill) plus the channel's flow
  // depth, and an off-channel pit counts only the water above its spill level. Cells left with less than 0.1 ft fall out
  // of the extent, and anything reached only through them falls out with it (the 4-neighbour flood below).
  // Closed depressions (filled above the ground by more than PIT_TOL_M) that the traced channel passes through: the
  // breach flow runs through these and fills them, so their bowl counts, but only up to the water the breach releases
  // (channelPitBudget below). Others are off the channel.
  const pitC = components(W, H, (k) => !!dom[k] && fill[k] - g.z[k] > PIT_TOL_M);
  const pitOnChannel = new Uint8Array(pitC.sizes.length);
  for (const k of path) if (pitC.lab[k] > 0) pitOnChannel[pitC.lab[k]] = 1;
  await breathe(N);
  const offPit = (m: number) => pitC.lab[m] > 0 && !pitOnChannel[pitC.lab[m]];
  // Channel pits against the release (Nick, 2026-11-03): in channel order, each pit the channel runs through takes its
  // bowl (below its spill level) out of the released volume before the flood moves on downstream. A pit the remaining
  // release cannot fill holds what is left at the level that volume reaches, and the flood stops there.
  const releasedM3 = Number.isFinite(result.breach.volumeAcFt) && result.breach.volumeAcFt > 0 ? result.breach.volumeAcFt * ACRE_FT_M3 : Infinity;
  const pitOrder: number[] = [];
  const pitFirstP = new Map<number, number>();
  for (let p = 0; p < path.length; p++) {
    const c = pitC.lab[path[p]];
    if (c > 0 && !pitFirstP.has(c)) {
      pitFirstP.set(c, p);
      pitOrder.push(c);
    }
  }
  const pitCells = new Map<number, number[]>();
  for (const c of pitOrder) pitCells.set(c, []);
  for (let k = 0; k < N; k++) {
    const c = pitC.lab[k];
    if (c > 0 && pitOnChannel[c]) pitCells.get(c)!.push(k);
  }
  // Mapped roads (road receptors with a location): the channel pits within ROAD_CROSSING_M of one.
  const roadPit = new Set<number>();
  {
    const rc = Math.ceil(ROAD_CROSSING_M / cellM);
    for (const rp of roadPoints(dam)) {
      const i0 = Math.floor((rp.lon - g.bbox[0]) / dlon);
      const j0 = Math.floor((g.bbox[3] - rp.lat) / dlat);
      for (let j = j0 - rc; j <= j0 + rc; j++)
        for (let i = i0 - rc; i <= i0 + rc; i++) {
          if (i < 0 || j < 0 || i >= W || j >= H) continue;
          const k = j * W + i;
          const c = pitC.lab[k];
          if (c > 0 && pitOnChannel[c] && Math.hypot(cellX(i) - (rp.lon - loc.lon) * kx, cellY(j) - (rp.lat - loc.lat) * M_PER_DEG_LAT) <= ROAD_CROSSING_M + cellM / 2)
            roadPit.add(c);
        }
    }
  }
  const storageM3 = Number.isFinite(dam.storageAcFt) && dam.storageAcFt > 0 ? dam.storageAcFt * ACRE_FT_M3 : null;
  const budget = channelPitBudget(
    pitOrder.map((c) => {
      const cells = pitCells.get(c)!;
      return {
        z: cells.map((k) => g.z[k]),
        spill: cells.map((k) => fill[k]),
        flowM: deskD[path[pitFirstP.get(c)!]],
        road: roadPit.has(c),
      };
    }),
    releasedM3,
    cellM2,
    { storageM3 },
  );
  /** Suspected crossings (labels): the flood passes through, its depth there no more than the flow depth. */
  const crossingPit = new Set(budget.crossings.map((x) => pitOrder[x.index]));
  const inCrossing = (m: number) => crossingPit.size > 0 && crossingPit.has(pitC.lab[m]);
  /** The pit the release runs out in (label), its water level (m), and the path index the flood stops at. */
  const stopPit = budget.stop ? pitOrder[budget.stop.index] : -1;
  const stopLevel = budget.stop ? budget.stop.levelM : 0;
  const cutP = budget.stop ? pitFirstP.get(stopPit)! : Infinity;
  const inStopPit = (m: number) => stopPit > 0 && pitC.lab[m] === stopPit;
  await breathe(N);

  const capFt = depthCapFt(dam.heightFt);
  /**
   * The bathtub flood with the desk depths scaled by `scale` (1 = the desk's own; the volume limit lowers it), then
   * the speck and pinhole cleanup, then the volume laid down from the capped depths.
   */
  const lay = async (scale: number) => {
    /** Water surface at a cell: the channel's, no higher than the cell's own drainage level plus the flow depth there. */
    const wseAt = (m: number) => {
      if (inStopPit(m)) return Math.min(stopLevel, fill[m]);
      const s0 = src[m];
      const d0 = deskD[s0] * scale;
      const w = Math.min(fill[s0] + d0, fill[m] + d0);
      // A suspected crossing: a sheet at the flow depth over its ground, not its bowl filled to the spill level.
      return inCrossing(m) ? Math.min(w, g.z[m] + d0) : w;
    };
    /** The floor water is measured from: the ground, or an off-channel pit's spill level. */
    const floorAt = (m: number) => (offPit(m) ? fill[m] : g.z[m]);
    const depth = new Float32Array(N).fill(-1);
    const under = (m: number) => {
      const s0 = src[m];
      if (s0 < 0 || along[m] > inp.lastX) return false;
      if (!inStopPit(m) && pathIdx[s0] >= cutP) return false;
      return wseAt(m) - floorAt(m) >= WET_M - 1e-9;
    };
    const inRoom = (m: number) => dist2(m, src[m]) * cellM2 <= roomC[src[m]] * roomC[src[m]];
    let n = 0;
    for (const k of path)
      if (depth[k] < 0 && under(k)) {
        depth[k] = wseAt(k) - floorAt(k);
        q[n++] = k;
      }
    let hitsCorridor = false;
    for (let head = 0; head < n; head++) {
      const k = q[head];
      nbrs(k, nb);
      for (let t = 0; t < 4; t++) {
        const m = nb[t];
        if (m < 0 || depth[m] >= 0 || !dom[m] || !under(m)) continue;
        if (!inRoom(m)) {
          hitsCorridor = true;
          continue;
        }
        depth[m] = wseAt(m) - floorAt(m);
        q[n++] = m;
      }
      if ((head & 0x3ffff) === 0) await breathe(0x40000);
    }
    if (n === 0) return null;
    // What the rebasing took out of the cells still in the extent, against the channel's surface taken whole (cells
    // that fell out of the extent are not counted, so this is a floor): for the desk note.
    let pitCellsOut = 0;
    let pitM3 = 0;
    for (let k = 0; k < N; k++) {
      if (depth[k] < 0) continue;
      const old = fill[src[k]] + deskD[src[k]] * scale - g.z[k];
      const now = depth[k];
      if (old - now > PIT_TOL_M) {
        pitCellsOut++;
        pitM3 += (old - now) * cellM2;
      }
    }

    // Specks and pinholes.
    const wetC = components(W, H, (k) => depth[k] >= 0);
    let droppedPatches = 0;
    for (let s = 1; s < wetC.sizes.length; s++) if (wetC.sizes[s] < MIN_CELLS) droppedPatches++;
    for (let k = 0; k < N; k++) if (depth[k] >= 0 && wetC.sizes[wetC.lab[k]] < MIN_CELLS) depth[k] = -1;
    await breathe(N);
    const dryC = components(W, H, (k) => depth[k] < 0);
    let filledHoles = 0;
    for (let s = 1; s < dryC.sizes.length; s++) if (dryC.sizes[s] < MIN_CELLS && !dryC.border[s]) filledHoles++;
    // Fill a small interior dry pocket only where the cell is in the domain and has valid terrain. Without the
    // dom/valid guard the cleanup would wet a tiny NODATA hole or an out-of-AOI pocket, pushing the outline past
    // the drawn area and over missing terrain — contradicting its own clipped_to_aoi provenance.
    for (let k = 0; k < N; k++)
      if (depth[k] < 0 && dom[k] && valid(k) && dryC.sizes[dryC.lab[k]] < MIN_CELLS && !dryC.border[dryC.lab[k]]) depth[k] = WET_M;
    await breathe(N);
    // Depth cap (dam height, no margin): deeper cells are pits/sinks under the surface, clamped and flagged.
    let laidM3 = 0;
    let wet = 0;
    for (let k = 0; k < N; k++)
      if (depth[k] >= 0) {
        laidM3 += Math.min(depth[k], capFt * FT) * cellM2;
        wet++;
      }
    return { depth, hitsCorridor, pitCells: pitCellsOut, pitM3, droppedPatches, filledHoles, laidM3, wet };
  };

  const first = await lay(1);
  if (!first || first.wet === 0) return { ok: false, reason: "No terrain outline: no ground connected to the channel lies under the desk water surface." };
  // Volume soft limit (Nick, 2026-11-03): aim for about VOLUME_TARGET_RATIO × the release. Over VOLUME_SHRINK_ABOVE ×,
  // lower the water surface above the channel bed (the desk depths scaled by one factor, found by bisection) until the
  // outline lays about the target; depth and extent shrink together, as a smaller flood's would.
  let laid = first;
  let volumeLimit: VolumeLimit | undefined;
  if (Number.isFinite(releasedM3) && laid.laidM3 > VOLUME_SHRINK_ABOVE * releasedM3) {
    const target = VOLUME_TARGET_RATIO * releasedM3;
    const raw = laid;
    let lo = 0;
    let hi = 1;
    let best: Awaited<ReturnType<typeof lay>> = null;
    let bestScale = 0;
    for (let it = 0; it < VOLUME_LIMIT_STEPS; it++) {
      const mid = (lo + hi) / 2;
      const r = await lay(mid);
      if (r && r.laidM3 <= target) {
        lo = mid;
        if (r.wet > 0) {
          best = r;
          bestScale = mid;
        }
        if (r.laidM3 >= target * (1 - VOLUME_LIMIT_TOL)) break;
      } else hi = mid;
    }
    if (!best) {
      best = await lay(lo);
      bestScale = lo;
    }
    if (best && best.wet > 0) {
      laid = best;
      volumeLimit = {
        targetRatio: VOLUME_TARGET_RATIO,
        shrinkAbove: VOLUME_SHRINK_ABOVE,
        rawLaidAcFt: r3(raw.laidM3 / ACRE_FT_M3),
        rawWetAc: r3((raw.wet * cellM2) / ACRE_M2),
        depthScale: r3(bestScale),
      };
    }
  }
  const { depth, hitsCorridor, droppedPatches, filledHoles, laidM3 } = laid;
  const pitCellsN = laid.pitCells;
  const pitM3 = laid.pitM3;
  const deepCells = deepCellsOf(depth, capFt, FT);
  const lonLatOfCell = (k: number) => ({ lon: r7(g.bbox[0] + ((k % W) + 0.5) * dlon), lat: r7(g.bbox[3] - (Math.floor(k / W) + 0.5) * dlat) });
  const channelPits: ChannelPits | undefined = pitOrder.length
    ? {
        pits: pitOrder.length,
        storageAcFt: r3(budget.storageM3 / ACRE_FT_M3),
        countedAcFt: r3(budget.countedM3 / ACRE_FT_M3),
        ...(budget.stop
          ? {
              stop: {
                pitAcFt: r3(budget.stop.pitM3 / ACRE_FT_M3),
                heldAcFt: r3(budget.stop.heldM3 / ACRE_FT_M3),
                atKm: r3(Math.max(0, pathA[cutP]) / 1000),
                ...lonLatOfCell(path[cutP]),
                crossing: budget.stop.crossing,
                why: budget.stop.why,
              },
            }
          : {}),
        ...(budget.crossings.length
          ? {
              crossings: budget.crossings.map((x) => {
                const p0 = pitFirstP.get(pitOrder[x.index])!;
                return {
                  atKm: r3(Math.max(0, pathA[p0]) / 1000),
                  ...lonLatOfCell(path[p0]),
                  pitAcFt: r3(x.pitM3 / ACRE_FT_M3),
                  chargedAcFt: r3(x.chargeM3 / ACRE_FT_M3),
                  why: x.why,
                };
              }),
            }
          : {}),
      }
    : undefined;

  // Bands, one 3 × 3 mode pass.
  const band0 = new Int8Array(N).fill(-1);
  const idx = new Map(DEPTH_BANDS.map((b, i) => [b.id, i]));
  for (let k = 0; k < N; k++) if (depth[k] >= 0) band0[k] = idx.get(bandFor(Math.max(WET_FT, Math.min(capFt, depth[k] / FT))).id)!;
  const band = new Int8Array(band0);
  const cnt = new Int32Array(DEPTH_BANDS.length);
  for (let j = 0; j < H; j++) {
    for (let i = 0; i < W; i++) {
      const k = j * W + i;
      if (band0[k] < 0) continue;
      cnt.fill(0);
      for (let dj = -1; dj <= 1; dj++)
        for (let di = -1; di <= 1; di++) {
          const ii = i + di;
          const jj = j + dj;
          if (ii < 0 || jj < 0 || ii >= W || jj >= H) continue;
          const b = band0[jj * W + ii];
          if (b >= 0) cnt[b]++;
        }
      let best = band0[k];
      for (let b = 0; b < cnt.length; b++) if (cnt[b] > cnt[best]) best = b;
      band[k] = best;
    }
    await breathe(W * 9);
  }

  // Per band: cells, depth range, distance range, polygons.
  const common = { ...strip.properties };
  const features: StripFeature[] = [];
  const bandsAc: OutlineStats["bands"] = [];
  let wetCells = 0;
  let polyAc = 0;
  let reach = 0;
  const toLonLat = (p: XY): [number, number] => [r7(g.bbox[0] + p[0] * dlon), r7(g.bbox[3] + p[1] * dlat)];
  for (let b = 0; b < DEPTH_BANDS.length; b++) {
    const mask = new Uint8Array(N);
    let cells = 0;
    const acc = bandDepthAcc(b, capFt);
    let lo = Infinity;
    let hi = -Infinity;
    for (let k = 0; k < N; k++)
      if (band[k] === b) {
        mask[k] = 1;
        cells++;
        acc.add(depth[k] / FT);
        lo = Math.min(lo, along[k]);
        hi = Math.max(hi, along[k]);
      }
    if (cells === 0) continue;
    const { dMax, dMin, dMean } = acc.done();
    wetCells += cells;
    reach = Math.max(reach, hi);
    const polys = polygonsOf(mask, W, H);
    await breathe(N);
    if (polys.length === 0) continue;
    // Acres of the drawn (smoothed) polygons, so the lists and exports add up to what the map shows.
    let cellArea = 0;
    const coords = polys.map((rings) =>
      rings.map((r, ri) => {
        const smooth = chaikin(r);
        cellArea += (ri === 0 ? 1 : -1) * Math.abs(shoelace(smooth));
        const sm = smooth.map(toLonLat);
        return [...sm, sm[0]];
      }),
    );
    const ac = (cellArea * cellM2) / ACRE_M2;
    polyAc += ac;
    bandsAc.push({ band: DEPTH_BANDS[b].id, label: DEPTH_BANDS[b].label, ac: r3(ac) });
    features.push({
      type: "Feature",
      properties: {
        ...common,
        disclaimer: OUTLINE_LABEL,
        band: DEPTH_BANDS[b].id,
        band_label: DEPTH_BANDS[b].label,
        depth_max_ft: r3(dMax),
        depth_min_ft: r3(dMin),
        depth_mean_ft: r3(dMean),
        from_km: r3(Math.max(0, lo) / 1000),
        to_km: r3(hi / 1000),
        segments: 0,
        area_ac: r3(ac),
        color: DEPTH_BANDS[b].color,
      },
      geometry: coords.length === 1 ? { type: "Polygon", coordinates: coords[0] } : { type: "MultiPolygon", coordinates: coords },
    });
  }
  if (features.length === 0)
    return { ok: false, reason: "No terrain outline: no ground connected to the channel lies under the desk water surface." };
  const outlineAc = r3(polyAc);
  const stats: OutlineStats = {
    dem: { width: W, height: H, cellM: r3(cellM), cells: N, bytes: N * 4, bbox: g.bbox, missing },
    outlineAc,
    engineAc: strip.properties.engine_wet_area_ac,
    stripAc: strip.properties.strip_area_ac,
    wetCells,
    wetCellAc: r3((wetCells * cellM2) / ACRE_M2),
    bands: bandsAc,
    bedTopFt: r3(fill[path[0]] / FT),
    bedEndFt: r3(fill[path[path.length - 1]] / FT),
    traceStart: toe.k >= 0 ? toe.start : { how: "axis" },
    channelKm: r3(channelM / 1000),
    reachKm: r3(reach / 1000),
    hitsCorridor,
    rings: features.reduce((s, f) => s + (f.geometry.type === "Polygon" ? f.geometry.coordinates.length : f.geometry.coordinates.reduce((t, p) => t + p.length, 0)), 0),
    droppedPatches,
    filledHoles,
    ...(deepCells ? { deepCells } : {}),
    ...(pitCellsN ? { pitsExcluded: { cells: pitCellsN, acFt: r3(pitM3 / ACRE_FT_M3) } } : {}),
    ...(channelPits ? { channelPits } : {}),
    ...(volumeLimit ? { volumeLimit } : {}),
    ...(smallRelease(result.breach.volumeAcFt) ? { volumeLabel: SMALL_RELEASE_LABEL } : {}),
    laidAcFt: r3(laidM3 / ACRE_FT_M3),
  };
  const outline: TerrainOutline = {
    type: "FeatureCollection",
    properties: {
      ...common,
      disclaimer: OUTLINE_LABEL,
      title: `${common.dam_id} terrain outline (approximate)`,
      kind: "terrain_outline",
      strip_area_ac: outlineAc,
      outline_area_ac: outlineAc,
      dem_source: DEM_SERVICE,
      trace_version: TRACE_VERSION,
      dem_cell_m: r3(cellM),
      dem_width: W,
      dem_height: H,
      corridor: "up to min(max(3 × half-width, 300 m), 2000 m) from the traced channel",
      ...(volumeLimit ? { volume_limit: volumeLimitNote({ volumeLimit, laidAcFt: stats.laidAcFt }, result.breach.volumeAcFt) ?? "" } : {}),
      ...(stats.volumeLabel ? { volume_label: stats.volumeLabel } : {}),
      ...(channelPits
        ? {
            channel_pits: { pits: channelPits.pits, storage_ac_ft: channelPits.storageAcFt, counted_ac_ft: channelPits.countedAcFt },
            channel_pit_stop: channelPits.stop
              ? {
                  at_km: channelPits.stop.atKm,
                  lon: channelPits.stop.lon!,
                  lat: channelPits.stop.lat!,
                  held_ac_ft: channelPits.stop.heldAcFt,
                  pit_ac_ft: channelPits.stop.pitAcFt,
                  crossing: !!channelPits.stop.crossing,
                  why: channelPits.stop.why ?? [],
                }
              : null,
            suspected_crossings: (channelPits.crossings ?? []).map((x) => ({
              at_km: x.atKm,
              lon: x.lon,
              lat: x.lat,
              pit_ac_ft: x.pitAcFt,
              charged_ac_ft: x.chargedAcFt,
              why: x.why,
            })),
          }
        : {}),
    },
    features,
  };
  return {
    ok: true,
    centerline,
    cache: {
      fingerprint: inp.fingerprint,
      builtAt: (opts.builtAt ?? new Date()).toISOString(),
      source: DEM_SERVICE,
      attribution: DEM_ATTRIBUTION,
      stats,
      outline,
      layer: "outline",
    },
  };
}

/** Build the terrain outline on a DEM (the full flood: trace, bathtub, bands, polygons) plus its centreline. */
export async function buildOutline(
  dam: DamRecord,
  result: Analysis,
  g: DemGrid,
  opts: BuildOptions = {},
): Promise<{ ok: true; cache: OutlineCache; centerline: Centerline } | { ok: false; reason: string }> {
  const r = await buildOutlineImpl(dam, result, g, opts, false);
  if (!r.ok) return r;
  return { ok: true, cache: r.cache!, centerline: r.centerline };
}

/**
 * Just the drainage trace (the outline's centreline), without the bathtub flood, cleanup, bands or polygonisation.
 * The routed flood needs only this; building the full outline just to read its centreline would throw the expensive
 * half away.
 */
export async function traceChannel(
  dam: DamRecord,
  result: Analysis,
  g: DemGrid,
  opts: BuildOptions = {},
): Promise<{ ok: true; centerline: Centerline } | { ok: false; reason: string }> {
  const r = await buildOutlineImpl(dam, result, g, opts, true);
  if (!r.ok) return r;
  return { ok: true, centerline: r.centerline };
}

/** What the desk can show for the outline now: the saved one (if built from the same inputs), or what to do. */
export function outlineState(dam: DamRecord, result: Analysis, online: boolean): OutlineState {
  const io = outlineInputs(dam, result);
  if (!io.ok) return { state: "none", message: io.reason };
  const c = dam.outline;
  // A routed layer saved before the volume spread became the desk's method (2026-11-03) is the old stage fill.
  const oldStage = c?.layer === "routed" && c.routed?.method !== "spread";
  if (c && c.fingerprint === io.inputs.fingerprint && !oldStage)
    return { state: "ready", outline: c.outline, stats: c.stats, builtAt: c.builtAt, message: null, layer: c.layer ?? "outline", routed: c.routed };
  if (c) {
    const msg = oldStage
      ? "This routed flood was built with the old stage fill, which spread up to hundreds of times the released water; the desk now pours the routed volume instead."
      : c.stats.traceStart
      ? "The desk inputs changed since the terrain outline was built."
      : "This terrain layer was built before the 2026-11-03 terrain fix (its terrain grid sat up to hundreds of metres off, so the traced channel could miss the flood).";
    return online ? { state: "build", message: `${msg} Build it again.` } : { state: "offline", message: `${msg} ${OUTLINE_OFFLINE}` };
  }
  return online
    ? { state: "build", message: "The terrain outline is not built yet (one 3DEP terrain request)." }
    : { state: "offline", message: OUTLINE_OFFLINE };
}

/** Plain words for the outline acres against the engine's wet acres. */
/** One line on where the traced channel starts, for the desk panel. */
export function traceStartNote(s: OutlineStats): string | null {
  const t = s.traceStart;
  if (!t) return null;
  if (t.how === "axis")
    return "No ground at least half the dam height below the top of dam within 400 m downstream, so the channel starts 60 m down the downstream axis. Check the dam point, the dam line and the crest.";
  const at = `Channel traced from the toe, ${Math.round(t.fromDamM ?? 0)} m from the dam point and ${Math.round(t.belowTopFt ?? 0)} ft below the top of dam.`;
  return t.how === "toe-wide" ? `${at} That is outside the usual search radius, so check the dam point.` : at;
}

export function outlineAcresNote(s: OutlineStats): string {
  const pct = s.engineAc > 0 ? Math.round((100 * (s.outlineAc - s.engineAc)) / s.engineAc) : 0;
  const more = s.outlineAc >= s.engineAc;
  return [
    `Outline ${s.outlineAc.toFixed(1)} ac against the engine's ${s.engineAc.toFixed(1)} wet ac (${more ? "+" : ""}${pct}%).`,
    "The engine's acres are its straight schematic valley (twice the half-width along the reach). The outline puts the same desk depths on the ground along the creek the terrain drains to",
    `(${s.channelKm.toFixed(1)} km of channel along its bends), so it is`,
    more
      ? "wider where the land next to the channel is flat and low, and narrower where the channel is cut in."
      : "narrower where the channel is cut in and the banks rise faster than the schematic valley, and wider where the land is flat.",
    "It counts only ground under the water surface of its nearest channel point that is connected to the channel, within the corridor",
    s.hitsCorridor ? "(the water reaches the corridor limit somewhere, so the outline is cut there)." : "(it stays inside the corridor).",
    "No routing or storage: depths come from the desk, not from the terrain.",
  ].join(" ");
}

/**
 * The area the PLSS and 911 lookups should cover: the strip and (when built) the outline together, so one cached
 * lookup serves the lists against either.
 */
export function lookupArea(strip: FloodStrip, outline: FloodStrip | null | undefined): FloodStrip {
  return outline ? { ...strip, features: [...strip.features, ...outline.features] } : strip;
}
