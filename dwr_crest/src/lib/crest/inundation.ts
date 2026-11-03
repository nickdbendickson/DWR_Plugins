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

import { floodStrip } from "./floodstrip.ts";
import { buildOutline, DEM_SERVICE, DEM_ATTRIBUTION, outlineInputs, polygonizeDepth } from "./outline.ts";
import { buildRoutedFlood, type RoutedFloodOptions, type RoutedFloodResult } from "./routed-flood.ts";
import type { Analysis, DamRecord } from "./types.ts";

import type { DemGrid, OutlineCache, OutlineStats, RoutedSummary } from "./outline.ts";

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
): OutlineCache {
  const common = floodStrip(dam, result).strip!.properties;
  const poly = polygonizeDepth(g, routed.depthFt, { common, label: ROUTED_LABEL, title: `${common.dam_id} routed terrain flood` });
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
  return { ok: true, layer: "outline", choice, cache: built.cache, fellBackToOutline: false };
}
