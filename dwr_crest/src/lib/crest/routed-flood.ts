/**
 * The routed terrain flood (step 5 of the terrain-inundation plan, Nick, 2026-11-01): the four building blocks wired
 * together into one inundation layer.
 *
 *   outline.ts traces the drainage channel from the breach
 *     → sections.ts cuts DEM cross-sections along it
 *       → routing.ts routes the breach outflow hydrograph to a water surface at each section
 *         → fill.ts spreads that surface over the terrain
 *
 * Unlike the terrain outline (which bathtubs the desk's schematic depths on a narrow corridor with no routing), this
 * routes the actual breach hydrograph down the real valley and fills to the routed water surface, so the depth and
 * the extent come from hydraulics on the ground rather than from the schematic valley.
 *
 * Pure module (no DOM, no fetch); it is given the DEM, as buildOutline is. The output depth grid is in feet on the
 * DEM's own bbox, which is exactly the validation harness's `lonlat` candidate, so it can be scored against a Runner
 * run without polygonising first; the app (and the exports) polygonise it the way the outline does.
 */

import { ACFT, CFS, FT } from "./breach.ts";
import { fillFlood } from "./fill.ts";
import { buildOutline } from "./outline.ts";
import { type RoutedSection, routeMuskingumCunge, sectionTable } from "./routing.ts";
import { type Centerline, cutSections } from "./sections.ts";
import type { Analysis, DamRecord } from "./types.ts";

import type { DemGrid } from "./outline.ts";

export type RoutedFloodOptions = {
  /** Station spacing down the channel (m). Default 50. */
  stationM?: number;
  /** Half-width of each cut cross-section (m); the section's hydraulic table reaches this far. Default 500. */
  sectionHalfWidthM?: number;
  /** Longest simulated time for the routing (h). Default 72. */
  maxHours?: number;
  /** A cell is wet at this depth (m). Default 0.03 (~0.1 ft). */
  minDepthM?: number;
  /** Cap the fill at the released breach volume instead of only flagging the over-spread. Default false. */
  volumeLimit?: boolean;
  /** Floor on the lateral decay slope (m/m); higher stops the sideways spread sooner. Default 0.0005. */
  slopeFloor?: number;
  /**
   * Cap a floodplain cell's depth at the channel stage that fed it plus this margin (m). Off the channel the carried
   * water surface decays only slowly, so where the terrain falls away it would make many-metre-deep water the breach
   * never supplies; the footprint stays about right but the depth (and so the volume) blows out. Default 1.0.
   */
  floodplainDepthMarginM?: number;
  /** Called every so many cells during the DEM trace so a long build can let the page breathe. */
  yieldFn?: () => Promise<void>;
};

/**
 * Default margin (m) for the floodplain depth cap. A floodplain cell is allowed to be at most its seeding channel
 * stage plus this much deep. One metre keeps the realistic floodplain depth (the baseline's DWR-2801 depth bias falls
 * from +21.5 ft to +3.4 ft against the LISFLOOD-FP run, matching the model, with no loss of extent or CSI) while
 * removing the spurious deep pours the bare envelope invents where the ground drops away from the channel.
 */
export const FLOODPLAIN_DEPTH_MARGIN_M = 1.0;

export type RoutedFloodResult = {
  ok: true;
  /** The depth grid as the validation harness's `lonlat` candidate: ft, row-major from the NW, 0 where dry. */
  bbox: DemGrid["bbox"];
  width: number;
  height: number;
  depthFt: Float32Array;
  /** Water-surface elevation (m) per cell, NaN where dry. */
  wseM: Float32Array;
  centerline: Centerline;
  sections: RoutedSection[];
  stats: {
    floodedAc: number;
    peakInCfs: number;
    peakOutCfs: number;
    channelKm: number;
    sections: number;
    cappedSections: number;
    volumeFlag: boolean;
    reachedEdge: boolean;
    routedPeakFellBy: number;
    /** Water volume the fill lays down (ac-ft). */
    fillAcFt: number;
    /** Breach volume released (ac-ft), the denominator of the volume ratio. */
    releasedAcFt: number;
    /** fillAcFt / releasedAcFt: how many times the released volume the max-surface envelope lays down (provenance). */
    volumeRatio: number;
  };
  notes: string[];
};

export type RoutedFloodFail = { ok: false; reason: string };

/** Build the routed flood on a DEM. Reuses buildOutline for the DEM drainage trace (its centreline). */
export async function buildRoutedFlood(
  dam: DamRecord,
  result: Analysis,
  g: DemGrid,
  opts: RoutedFloodOptions = {},
): Promise<RoutedFloodResult | RoutedFloodFail> {
  const series = result.breach.series;
  if (!series || series.length < 2)
    return { ok: false, reason: "No routed flood: the breach has no outflow hydrograph." };

  const built = await buildOutline(dam, result, g, { yieldFn: opts.yieldFn });
  if (!built.ok)
    return { ok: false, reason: built.reason.replace("No terrain outline", "No routed flood") };
  const centerline = built.centerline;
  if (centerline.pts.length < 2)
    return { ok: false, reason: "No routed flood: the traced channel is too short." };

  const sections = cutSections(g, centerline, {
    stationM: opts.stationM ?? 50,
    halfWidthM: opts.sectionHalfWidthM ?? 500,
  });
  if (sections.length < 2)
    return { ok: false, reason: "No routed flood: fewer than two usable cross-sections were cut." };

  const tables = sections.map((s) => sectionTable(s));
  // The breach outflow hydrograph (SI) is the routing inflow at the first section.
  const inflow = {
    tS: series.map((p) => p.tHr * 3600),
    qCms: series.map((p) => p.qCfs * CFS),
  };
  const routed = routeMuskingumCunge(inflow, tables, { maxHours: opts.maxHours ?? 72 });

  const fill = fillFlood(g, centerline, routed.sections, {
    minDepthM: opts.minDepthM ?? 0.03,
    availableVolumeM3: result.breach.volumeAcFt * ACFT,
    volumeLimit: opts.volumeLimit ?? false,
    slopeFloor: opts.slopeFloor,
    maxFloodplainDepthM: opts.floodplainDepthMarginM ?? FLOODPLAIN_DEPTH_MARGIN_M,
  });

  const N = g.width * g.height;
  const depthFt = new Float32Array(N);
  for (let k = 0; k < N; k++) depthFt[k] = fill.depthM[k] > 0 ? fill.depthM[k] / FT : 0;

  const peakInCfs = routed.sections[0].peakCms / CFS;
  const peakOutCfs = routed.sections[routed.sections.length - 1].peakCms / CFS;
  const channelM = centerline.pts.length ? centerline.pts[centerline.pts.length - 1].x : 0;
  const notes = [...routed.notes];
  if (fill.reachedEdge)
    notes.push(
      "The routed flood reached the edge of the DEM; it would spread further than the DEM shows.",
    );
  if (fill.volumeFlag)
    notes.push(
      `The fill lays down ${(fill.volumeM3 / ACFT).toFixed(0)} ac-ft, more than the breach releases (${result.breach.volumeAcFt.toFixed(0)} ac-ft): the water-surface envelope is wider than the released volume supports.`,
    );

  return {
    ok: true,
    bbox: g.bbox,
    width: g.width,
    height: g.height,
    depthFt,
    wseM: fill.wseM,
    centerline,
    sections: routed.sections,
    stats: {
      floodedAc: Math.round(fill.floodedAc * 10) / 10,
      peakInCfs: Math.round(peakInCfs),
      peakOutCfs: Math.round(peakOutCfs),
      channelKm: Math.round(channelM / 100) / 10,
      sections: sections.length,
      cappedSections: routed.sections.filter((s) => s.capped).length,
      volumeFlag: fill.volumeFlag,
      reachedEdge: fill.reachedEdge,
      routedPeakFellBy: peakInCfs > 0 ? Math.round((1 - peakOutCfs / peakInCfs) * 100) / 100 : 0,
      fillAcFt: Math.round(fill.volumeM3 / ACFT),
      releasedAcFt: Math.round(result.breach.volumeAcFt),
      volumeRatio: result.breach.volumeAcFt > 0 ? Math.round((fill.volumeM3 / ACFT / result.breach.volumeAcFt) * 10) / 10 : 0,
    },
    notes,
  };
}
