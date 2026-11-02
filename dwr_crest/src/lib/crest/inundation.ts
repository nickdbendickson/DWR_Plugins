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
 */

import { floodStrip } from "./floodstrip.ts";
import { buildOutline, polygonizeDepth } from "./outline.ts";
import { buildRoutedFlood, type RoutedFloodOptions, type RoutedFloodResult } from "./routed-flood.ts";
import type { Analysis, DamRecord } from "./types.ts";

import type { DemGrid, OutlineCache, TerrainOutline } from "./outline.ts";
import type { Centerline } from "./sections.ts";

/** Map-and-export label for the routed flood, distinct from the bathtub outline's OUTLINE_LABEL. */
export const ROUTED_LABEL = "Routed terrain flood from 3DEP terrain and the breach hydrograph (Muskingum-Cunge routing + terrain fill), not a full 2-D hydraulic model";

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
      layer: "routed";
      choice: LayerChoice;
      routed: RoutedFloodResult;
      /** The routed depth grid polygonised into FloodStrip-shaped band features, so it draws and exports like the outline. */
      outline: TerrainOutline;
      areaAc: number;
      fellBackToOutline: false;
    }
  | { ok: true; layer: "outline"; choice: LayerChoice; cache: OutlineCache; centerline: Centerline; fellBackToOutline: boolean }
  | { ok: false; reason: string };

/**
 * Build the inundation layer the gate picks. When the gate picks routed but the routed build fails (e.g. too short a
 * channel), it falls back to the outline rather than returning nothing, and flags that it did.
 */
export async function buildInundation(
  dam: DamRecord,
  result: Analysis,
  g: DemGrid,
  opts: RoutedFloodOptions = {},
): Promise<InundationResult> {
  const choice = chooseInundationLayer(result);

  if (choice.layer === "routed") {
    const routed = await buildRoutedFlood(dam, result, g, opts);
    if (routed.ok) {
      const common = floodStrip(dam, result).strip!.properties;
      const poly = polygonizeDepth(g, routed.depthFt, {
        common,
        label: ROUTED_LABEL,
        title: `${common.dam_id} routed terrain flood`,
      });
      return { ok: true, layer: "routed", choice, routed, outline: poly.outline, areaAc: poly.areaAc, fellBackToOutline: false };
    }
    // Routed was picked but could not be built; fall back to the outline so the desk still gets a layer.
    const built = await buildOutline(dam, result, g);
    if (!built.ok) return { ok: false, reason: `${routed.reason} Outline fallback also failed: ${built.reason}` };
    return {
      ok: true,
      layer: "outline",
      choice: { ...choice, reason: `${choice.reason} (Routed could not be built: ${routed.reason} Fell back to the outline.)` },
      cache: built.cache,
      centerline: built.centerline,
      fellBackToOutline: true,
    };
  }

  const built = await buildOutline(dam, result, g);
  if (!built.ok) return { ok: false, reason: built.reason };
  return { ok: true, layer: "outline", choice, cache: built.cache, centerline: built.centerline, fellBackToOutline: false };
}
