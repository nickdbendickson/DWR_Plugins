/**
 * The inundation-layer gate (Nick, 2026-11-02). Step 5 scored the routed terrain flood (routed-flood.ts) against the
 * ND Breach Runner on the four-dam baseline and found it beats the bathtub outline only on the one LARGE dam
 * (DWR-2801, a 6,144-ac flood) and loses on the small ones (38–256 ac), where a 1-D route down a thalweg is too fine
 * a target and, on the smallest, the trace misses the flood entirely.
 *
 * None of the model-independent "confinement" signals tried (the can't-carry-peak fraction, the envelope-to-released
 * volume ratio, the section bank relief) separates the win from the losses. The one thing that does is flood SIZE,
 * and that is knowable before any model run: the released breach volume. So the gate is a size gate — route the big
 * floods, fall back to the existing bathtub outline for the small ones — not an attempt to detect confinement.
 *
 * The threshold is calibrated on thin data (one large dam on the win side), so GATE_VOLUME_ACFT is a tunable constant,
 * not a law; a few more large-dam runs should firm it up. The gate is a-priori (released volume only), so it needs no
 * DEM and no model to decide; buildInundation then builds only the layer the gate picks.
 *
 * Since 2026-11-03 (Nick) the routed layer is the quasi-dynamic 2-D volume spread (spread.ts) rather than the stage
 * fill: on the corrected DEM (crest PR #35) with closed-basin storage (PR #36) it beats the outline on all four
 * baseline dams and lays down 2–6× the release, where the stage fill laid down 31–779×. The stage fill stays available
 * with { method: "stage" }.
 */

import { type ExtentSensitivity, extentSensitivity } from "./extent.ts";
import { floodStrip } from "./floodstrip.ts";
import {
  buildOutline,
  DEM_SERVICE,
  depthCapFt,
  DEM_ATTRIBUTION,
  demRequest,
  fetchDem,
  outlineInputs,
  polygonizeDepth,
  SMALL_RELEASE_LABEL,
  smallRelease,
  VOLUME_TARGET_RATIO,
} from "./outline.ts";
import { buildRoutedFloodReach, type ReachRound, reachStatus } from "./reach.ts";
import { buildRoutedFlood, type RoutedFloodOptions, type RoutedFloodResult } from "./routed-flood.ts";
import type { Analysis, DamRecord } from "./types.ts";

import type { DemGrid, DemRequest, OutlineCache, OutlineStats, ReachSummary, RoutedSummary } from "./outline.ts";

const ACRE_M2 = 4046.8564224;

/** Map-and-export label for the routed flood, distinct from the bathtub outline's OUTLINE_LABEL. */
export const ROUTED_LABEL = "Routed terrain flood from 3DEP terrain and the breach hydrograph (Muskingum-Cunge routing + volume spread), not a full 2-D hydraulic model";

/** How the desk lays the routed flood on the terrain (routed-flood.ts RoutedFloodOptions.method). */
export const ROUTED_METHOD = "spread" as const;

/**
 * Released breach volume (ac-ft) at or above which the routed flood is used instead of the bathtub outline. Between
 * the baseline's small dams (≤167 ac-ft released) and its one large dam (1,612 ac-ft released); provisional until more
 * large dams are run. See [[crest-fast-inundation-method]].
 */
export const GATE_VOLUME_ACFT = 500;

/**
 * Plausible band for water laid down ÷ water released. A moving flood's max-depth envelope holds more than the release
 * (the LISFLOOD envelopes on the baseline run 3.6–4.4×), so the band is set around them; outside it the desk warns.
 */
export const VOLUME_RATIO_BAND = { lo: 2, hi: 6 } as const;

export type VolumeCheck = {
  laidAcFt: number;
  releasedAcFt: number;
  ratio: number;
  warn: boolean;
  text: string;
  /** SMALL_RELEASE_LABEL ("volume-limited, run the Runner") when the release is under SMALL_RELEASE_ACFT, else null. */
  label: string | null;
};

/**
 * Volume laid down vs released, with a warning outside VOLUME_RATIO_BAND. null when either is unknown. `rawLaidAcFt`
 * is what the outline laid before the volume soft limit lowered it (outline.ts VolumeLimit): the text says the layer
 * was limited. Amber (warn) outside the band or on a release under SMALL_RELEASE_ACFT; a limited layer back inside the
 * band is neutral.
 */
export function volumeCheck(
  laidAcFt: number | null | undefined,
  releasedAcFt: number | null | undefined,
  opts: { rawLaidAcFt?: number | null } = {},
): VolumeCheck | null {
  if (typeof laidAcFt !== "number" || typeof releasedAcFt !== "number") return null;
  if (!Number.isFinite(laidAcFt) || !Number.isFinite(releasedAcFt) || releasedAcFt <= 0) return null;
  const ratio = laidAcFt / releasedAcFt;
  const { lo, hi } = VOLUME_RATIO_BAND;
  const raw = typeof opts.rawLaidAcFt === "number" && Number.isFinite(opts.rawLaidAcFt) ? opts.rawLaidAcFt / releasedAcFt : null;
  const limited = raw != null && raw > hi;
  const outside = ratio < lo || ratio > hi;
  const small = smallRelease(releasedAcFt);
  // Amber outside the band and on releases under SMALL_RELEASE_ACFT; a volume-limited layer that ends up inside the
  // band (about VOLUME_TARGET_RATIO ×) reads neutral (Nick, 2026-11-03 22:43 CST).
  const warn = outside || small;
  const n = (x: number) => (x < 10 ? x.toFixed(1) : Math.round(x).toLocaleString("en-US"));
  const label = small ? SMALL_RELEASE_LABEL : null;
  const base = `Volume: ${n(laidAcFt)} ac-ft laid down for ${n(releasedAcFt)} ac-ft released (${ratio.toFixed(1)}×).`;
  const text = limited
    ? `${base} Volume-limited: as first laid it held ${n(opts.rawLaidAcFt!)} ac-ft (${raw.toFixed(1)}×, above ${hi}×), so its water surface was lowered to about ${VOLUME_TARGET_RATIO}×` +
      (outside ? `, but it still lies outside the ${lo}–${hi}× band. Check against a model run.` : `, inside the ${lo}–${hi}× band.`)
    : !outside
      ? `${base} Inside the ${lo}–${hi}× a moving flood's max-depth envelope usually holds.`
      : ratio > hi
        ? `${base} Above ${hi}×: the layer holds far more water than the breach releases, so its extent and depths are likely too big. Check against a model run.`
        : `${base} Below ${lo}×: the layer holds less water than a moving flood's envelope usually does, so its extent and depths may be too small. Check against a model run.`;
  return { laidAcFt, releasedAcFt, ratio, warn, text, label };
}

export type LayerChoice = {
  /** The layer the gate picks for this dam. */
  layer: "routed" | "outline";
  /** The released breach volume the decision was made on (ac-ft). */
  releasedAcFt: number;
  /** Plain-words reason, for the desk and the logs. */
  reason: string;
};

/**
 * Pick the inundation layer for a dam from its released breach volume alone (no DEM, no model). Large floods route;
 * small floods fall back to the bathtub outline, which scores better on small dams and never worse.
 */
export function chooseInundationLayer(result: Analysis): LayerChoice {
  const releasedAcFt = result.breach.volumeAcFt;
  const big = Number.isFinite(releasedAcFt) && releasedAcFt >= GATE_VOLUME_ACFT;
  return {
    layer: big ? "routed" : "outline",
    releasedAcFt,
    reason: big
      ? `Routed terrain flood: the breach releases ${releasedAcFt.toFixed(0)} ac-ft (≥ ${GATE_VOLUME_ACFT}), a flood large enough for the routed method to track.`
      : `Bathtub outline: the breach releases only ${releasedAcFt.toFixed(0)} ac-ft (< ${GATE_VOLUME_ACFT}); a 1-D route is too fine a target for a flood this small, so the terrain outline is the more reliable layer.`,
  };
}

export type InundationResult =
  | {
      ok: true;
      /** The layer actually built (may be "outline" after a routed build failed and fell back). */
      layer: "outline" | "routed";
      choice: LayerChoice;
      /** Ready to save on the dam (dam.outline) and to draw/export, whichever layer it holds. */
      cache: OutlineCache;
      /** The routed build, when the built layer is the routed flood. */
      routed?: RoutedFloodResult;
      fellBackToOutline: boolean;
    }
  | { ok: false; reason: string };

/** Build the OutlineCache for a routed flood: the polygonised bands, a synthesized OutlineStats, and the routed facts. */
function routedCache(
  dam: DamRecord,
  result: Analysis,
  g: DemGrid,
  routed: Extract<RoutedFloodResult, { ok: true }>,
  fingerprint: string,
  reach?: ReachSummary,
): OutlineCache {
  const common = floodStrip(dam, result).strip!.properties;
  const poly = polygonizeDepth(g, routed.depthFt, { common, label: ROUTED_LABEL, title: `${common.dam_id} routed terrain flood`, capFt: depthCapFt(dam.heightFt) });
  const rings = poly.outline.features.reduce(
    (s, f) => s + (f.geometry.type === "Polygon" ? f.geometry.coordinates.length : f.geometry.coordinates.reduce((t, p) => t + p.length, 0)),
    0,
  );
  const stats: OutlineStats = {
    dem: { width: g.width, height: g.height, cellM: g.cellM, cells: g.width * g.height, bytes: g.width * g.height * 4, bbox: g.bbox, missing: 0 },
    outlineAc: poly.areaAc,
    engineAc: common.engine_wet_area_ac,
    stripAc: common.strip_area_ac,
    wetCells: poly.wetCells,
    wetCellAc: Math.round(((poly.wetCells * g.cellM * g.cellM) / ACRE_M2) * 1000) / 1000,
    bands: poly.bands,
    // The routed/spread flood does not trace a channel the way the terrain outline does, so these are
    // not measured here. null (not 0) keeps any reader from taking a bed elevation, reach, or cleanup
    // count as real. channelKm and hitsCorridor come from the routing and are genuine.
    bedTopFt: null,
    bedEndFt: null,
    channelKm: routed.stats.channelKm,
    reachKm: null,
    hitsCorridor: routed.stats.reachedEdge,
    rings,
    droppedPatches: null,
    filledHoles: null,
    ...(poly.deepCells ? { deepCells: poly.deepCells } : {}),
    extent: { ...extentSensitivity(g, routed.depthFt), basis: "routed-layer", stoppedInBasin: routed.stats.stoppedInBasin, volumeRatio: routed.stats.volumeRatio },
  };
  const r: RoutedSummary = {
    floodedAc: routed.stats.floodedAc,
    peakInCfs: routed.stats.peakInCfs,
    peakOutCfs: routed.stats.peakOutCfs,
    routedPeakFellBy: routed.stats.routedPeakFellBy,
    channelKm: routed.stats.channelKm,
    sections: routed.stats.sections,
    cappedSections: routed.stats.cappedSections,
    volumeFlag: routed.stats.volumeFlag,
    fillAcFt: routed.stats.fillAcFt,
    releasedAcFt: routed.stats.releasedAcFt,
    volumeRatio: routed.stats.volumeRatio,
    method: routed.method,
    reachedEdge: routed.stats.reachedEdge,
    exhaustedStretches: routed.stats.exhaustedStretches,
    cappedStretches: routed.stats.cappedStretches,
    unpouredAcFt: routed.stats.unpouredAcFt,
    travellingAcFt: routed.stats.travellingAcFt,
    wetEndKm: routed.stats.wetEndKm,
    stoppedInBasin: routed.stats.stoppedInBasin,
    ...(reach ? { reach } : {}),
    notes: routed.notes,
  };
  return { fingerprint, builtAt: new Date().toISOString(), source: DEM_SERVICE, attribution: DEM_ATTRIBUTION, stats, outline: poly.outline, layer: "routed", routed: r };
}

/**
 * Build the inundation layer the gate picks. When the gate picks routed but the routed build fails (e.g. too short a
 * channel), it falls back to the outline rather than returning nothing, and flags that it did. Either way it returns a
 * single OutlineCache to save on the dam and draw/export.
 */
export async function buildInundation(
  dam: DamRecord,
  result: Analysis,
  g: DemGrid,
  opts: RoutedFloodOptions = {},
): Promise<InundationResult> {
  const choice = chooseInundationLayer(result);
  const io = outlineInputs(dam, result);
  if (!io.ok) return { ok: false, reason: io.reason };

  if (choice.layer === "routed") {
    const routed = await buildRoutedFlood(dam, result, g, { method: ROUTED_METHOD, ...opts });
    if (routed.ok) {
      return { ok: true, layer: "routed", choice, cache: routedCache(dam, result, g, routed, io.inputs.fingerprint), routed, fellBackToOutline: false };
    }
    // Routed was picked but could not be built; fall back to the outline so the desk still gets a layer.
    const built = await buildOutline(dam, result, g, { yieldFn: opts.yieldFn });
    if (!built.ok) return { ok: false, reason: `${routed.reason} Outline fallback also failed: ${built.reason}` };
    return {
      ok: true,
      layer: "outline",
      choice: { ...choice, reason: `${choice.reason} (Routed could not be built: ${routed.reason} Fell back to the outline.)` },
      cache: built.cache,
      fellBackToOutline: true,
    };
  }

  const built = await buildOutline(dam, result, g, { yieldFn: opts.yieldFn });
  if (!built.ok) return { ok: false, reason: built.reason };
  const extent = await probeExtent(dam, result, g, opts);
  const cache = extent ? { ...built.cache, stats: { ...built.cache.stats, extent } } : built.cache;
  return { ok: true, layer: "outline", choice, cache, fellBackToOutline: false };
}

/**
 * The triage's terrain confidence for a dam whose layer is the terrain outline (triage.ts). The extent check was fit
 * on the routed volume spread, which follows the water and holds the released volume; the outline's bathtub footprint
 * does not, so its own fringe says little about the terrain. So a spread is built on the same terrain as a probe, its
 * edge sensitivity measured, and the spread itself thrown away: the outline stays the layer. null when the probe
 * cannot be built (the triage then reads the dam as uncertain).
 */
export async function probeExtent(dam: DamRecord, result: Analysis, g: DemGrid, opts: RoutedFloodOptions = {}): Promise<ExtentSensitivity | null> {
  try {
    const probe = await buildRoutedFlood(dam, result, g, { ...opts, method: ROUTED_METHOD });
    if (!probe.ok) return null;
    return { ...extentSensitivity(g, probe.depthFt), basis: "routed-probe",
      stoppedInBasin: probe.stats.stoppedInBasin,
      volumeRatio: probe.stats.volumeRatio,
      reachedEdge: probe.stats.reachedEdge && probe.stats.travellingAcFt > 0,
    };
  } catch {
    return null;
  }
}

export type InundationReachOptions = {
  yieldFn?: () => Promise<void>;
  /** Progress lines for the desk ("Fetching the 3DEP terrain…", "growing the terrain 2.2 km downstream…"). */
  onStatus?: (text: string) => void;
  /** The terrain fetch (fetchDem by default; tests pass their own). */
  fetch?: (req: DemRequest) => Promise<DemGrid>;
  /** Extension rounds allowed for the routed flood (reach.ts REACH_MAX_ROUNDS by default; 0 builds once). */
  maxRounds?: number;
};

/**
 * Fetch the terrain and build the inundation layer the gate picks, growing the terrain downstream on demand when the
 * layer is the routed flood (reach.ts). This is what the desk's build button runs: the terrain outline gets the one
 * corridor box it always got; the routed flood gets the box grown while the routed wave still reaches its edge with
 * water to carry, and its summary says how the reach ended (RoutedSummary.reach).
 */
export async function buildInundationReach(dam: DamRecord, result: Analysis, opts: InundationReachOptions = {}): Promise<InundationResult> {
  const choice = chooseInundationLayer(result);
  const io = outlineInputs(dam, result);
  if (!io.ok) return { ok: false, reason: io.reason };
  const fetchFn = opts.fetch ?? fetchDem;
  if (choice.layer === "routed") {
    const rb = await buildRoutedFloodReach(dam, result, {
      inputs: io.inputs,
      method: ROUTED_METHOD,
      yieldFn: opts.yieldFn,
      fetch: fetchFn,
      maxRounds: opts.maxRounds,
      onRound: (r: ReachRound) => opts.onStatus?.(reachStatus(r)),
    });
    if (rb.routed.ok)
      return {
        ok: true,
        layer: "routed",
        choice,
        cache: routedCache(dam, result, rb.grid, rb.routed, io.inputs.fingerprint, rb.reach),
        routed: rb.routed,
        fellBackToOutline: false,
      };
    // Routed was picked but could not be built; fall back to the outline on the terrain fetched so the desk still gets a layer.
    opts.onStatus?.("Building the terrain outline instead…");
    const built = await buildOutline(dam, result, rb.grid, { yieldFn: opts.yieldFn });
    if (!built.ok) return { ok: false, reason: `${rb.routed.reason} Outline fallback also failed: ${built.reason}` };
    return {
      ok: true,
      layer: "outline",
      choice: { ...choice, reason: `${choice.reason} (Routed could not be built: ${rb.routed.reason} Fell back to the outline.)` },
      cache: built.cache,
      fellBackToOutline: true,
    };
  }
  const req = demRequest(io.inputs);
  opts.onStatus?.(`Fetching the 3DEP terrain (${req.width} × ${req.height} cells at ${Math.round(req.cellM)} m)…`);
  const grid = await fetchFn(req);
  opts.onStatus?.("Building the inundation layer on the terrain…");
  return buildInundation(dam, result, grid, { yieldFn: opts.yieldFn });
}
