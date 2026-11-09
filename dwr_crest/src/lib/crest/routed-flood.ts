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
import { breachPath, SETTLE_SWEEPS, settlePools, spreadVolume, spreadVolumeAsync } from "./spread.ts";
import { type OutlineInputs, outlineInputs, traceChannel } from "./outline.ts";
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
  /**
   * Which Muskingum-Cunge variant routes each reach: "mc" (frozen coefficients everywhere), "mct" (variable-parameter
   * Todini everywhere), or "auto" (the default: MCT on confined reaches, MC on flat plains and tiny pools). See
   * RoutingOptions.router.
   */
  router?: "mc" | "mct" | "auto";
  /** A cell is wet at this depth (m). Default 0.03 (~0.1 ft). */
  minDepthM?: number;
  /**
   * Spread only: sweeps of the water-surface settling that joins neighbouring stretches' pools into one sheet
   * (spread.ts settlePools). Default SETTLE_SWEEPS; 0 leaves each stretch's pool as poured (the layer before 2026-11-05).
   */
  settleSweeps?: number;
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
  /**
   * How the routed flood is laid on the terrain. "spread" (default, the shipped layer of record): pour each stretch's
   * peak storage from the routing as a level pool on its own ground (spread.ts), the quasi-dynamic 2-D volume spread.
   * "stage" (the method as first merged, retired as the default): seed the channel with the Manning stage and carry
   * that surface across the floodplain (fill.ts); its surface decays only slowly, so it over-pours where the terrain
   * falls away. The desk always passes "spread" (inundation.ts ROUTED_METHOD); "stage" is kept for comparison runs.
   */
  method?: "stage" | "spread";
  /** Spread only: follow the breach's own flow path rather than the outline's drainage trace (default false). */
  breachPath?: boolean;
  /**
   * Spread only: closed basins on the channel hold water the wave must fill before it passes, and one that holds all of
   * it ends the flood (default true). See spread.ts step 3c.
   */
  basins?: boolean;
  /** Spread only: bumps lower than this (m) don't end a section's hydraulic table. Default SPREAD_RIDGE_TOL_M. */
  ridgeTolM?: number;
  /** Called every so many cells during the DEM trace so a long build can let the page breathe. */
  yieldFn?: () => Promise<void>;
  /**
   * The outline inputs to trace with instead of outlineInputs(dam, result): the demand-driven reach (reach.ts) passes
   * them with lastX pushed downstream so the trace may run past the schematic's last station.
   */
  inputs?: OutlineInputs;
};

/**
 * Default margin (m) for the floodplain depth cap. A floodplain cell is allowed to be at most its seeding channel
 * stage plus this much deep. One metre keeps the realistic floodplain depth (the baseline's DWR-2801 depth bias falls
 * from +21.5 ft to +3.4 ft against the LISFLOOD-FP run, matching the model, with no loss of extent or CSI) while
 * removing the spurious deep pours the bare envelope invents where the ground drops away from the channel.
 */
export const FLOODPLAIN_DEPTH_MARGIN_M = 1.0;

/** The spread's volume flag: envelope-to-release ratio above which the layer is flagged (LISFLOOD envelope ~3–5×). */
export const SPREAD_RATIO_FLAG = 10;

/** Ridge tolerance (m) for the spread's section tables (see buildRoutedFlood). */
export const SPREAD_RIDGE_TOL_M = 0.5;

/** How far (m) from the traced channel the spread's section tables look for the thalweg. */
export const SPREAD_THALWEG_WINDOW_M = 30;

export type RoutedFloodResult = {
  ok: true;
  /** The depth grid as the validation harness's `lonlat` candidate: ft, row-major from the NW, 0 where dry. */
  bbox: DemGrid["bbox"];
  width: number;
  height: number;
  depthFt: Float32Array;
  /** Water-surface elevation (m) per cell, NaN where dry. */
  wseM: Float32Array;
  /** Velocity (ft/s) and arrival (h) per cell, NaN where dry; only the "spread" method carries them. */
  velocityFps?: Float32Array;
  arrivalHr?: Float32Array;
  /** Which way the routed surface was laid on the terrain. */
  method: "stage" | "spread";
  /** Spread only: the cells the pools may settle over (spread.ts SpreadResult.ground), kept for settleRoutedFlood. */
  ground?: Uint8Array;
  /** Spread only: sweeps of settlePools applied to the layer (0 or absent: the pools as poured). */
  settled?: number;
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
    /** Spread only (0 for the stage fill): stretches that ran out of ground, those held at the cap, and the ac-ft withheld. */
    exhaustedStretches: number;
    cappedStretches: number;
    unpouredAcFt: number;
    /** Spread only (0 / false for the stage fill): the demand signal the reach loop reads, rounded for display. */
    travellingAcFt: number;
    wetEndKm: number;
    stoppedInBasin: boolean;
    /** Water volume the fill lays down (ac-ft). */
    fillAcFt: number;
    /** Breach volume released (ac-ft), the denominator of the volume ratio. */
    releasedAcFt: number;
    /** fillAcFt / releasedAcFt: how many times the released volume the max-surface envelope lays down (provenance). */
    volumeRatio: number;
  };
  /**
   * Spread only: the unrounded demand signal for the downstream reach (reach.ts). The wave still reaches the end of the
   * traced channel (wetEndM against channelEndM), has this much water still travelling, and was not stopped in a basin.
   */
  demand?: { reachedEdge: boolean; travellingM3: number; wetEndM: number; channelEndM: number; stoppedInBasin: boolean };
  notes: string[];
};

export type RoutedFloodFail = { ok: false; reason: string };

/** Build the routed flood on a DEM. Uses traceChannel for the DEM drainage trace (its centreline) only. */
export async function buildRoutedFlood(
  dam: DamRecord,
  result: Analysis,
  g: DemGrid,
  opts: RoutedFloodOptions = {},
): Promise<RoutedFloodResult | RoutedFloodFail> {
  const series = result.breach.series;
  if (!series || series.length < 2)
    return { ok: false, reason: "No routed flood: the breach has no outflow hydrograph." };

  const built = await traceChannel(dam, result, g, { yieldFn: opts.yieldFn, inputs: opts.inputs });
  if (!built.ok)
    return { ok: false, reason: built.reason.replace("No terrain outline", "No routed flood") };
  let centerline = built.centerline;
  let pathNote: string | null = null;
  // Default to the spread, the shipped layer of record: a caller that omits method gets what the desk ships, not the
  // retired stage fill. The desk passes it explicitly (inundation.ts), so this only guards a direct caller.
  const method = opts.method ?? "spread";
  // Optionally the spread follows the breach's own flow path (spread.ts breachPath) instead of the outline's drainage
  // trace. Off by default: once the DEM registration and trace start were fixed (crest PR #35) the trace scored better
  // on all four baseline dams and kept the envelope volume near the LISFLOOD band; the path detours through potholes.
  if (method === "spread" && (opts.breachPath ?? false) && centerline.pts.length) {
    const io = opts.inputs ? ({ ok: true, inputs: opts.inputs } as const) : outlineInputs(dam, result);
    const p = io.ok ? breachPath(g, centerline.loc, io.inputs.u as [number, number], centerline.pts[0]) : null;
    if (p && p.pts.length >= 2) {
      centerline = p;
      pathNote = `Flow path: followed the water from the breach (${(p.pts[p.pts.length - 1].x / 1000).toFixed(1)} km to the DEM edge), not the drainage trace.`;
    }
  }
  if (centerline.pts.length < 2)
    return { ok: false, reason: "No routed flood: the traced channel is too short." };

  const sections = cutSections(g, centerline, {
    stationM: opts.stationM ?? 50,
    halfWidthM: opts.sectionHalfWidthM ?? 500,
  });
  if (sections.length < 2)
    return { ok: false, reason: "No routed flood: fewer than two usable cross-sections were cut." };

  // The spread reads each stretch's storage from the routing, so a section whose table stops at a few centimetres of
  // DEM noise (near-zero area, so near-zero celerity and a huge K·Q) would pour a spurious slice; it cuts its tables
  // past bumps lower than SPREAD_RIDGE_TOL_M. The stage method keeps the tables as merged.
  // It also looks for each section's thalweg near the traced channel rather than anywhere on the 1 km cut.
  const tableOpts =
    method === "spread"
      ? { ridgeTolM: opts.ridgeTolM ?? SPREAD_RIDGE_TOL_M, thalwegWindowM: SPREAD_THALWEG_WINDOW_M }
      : {};
  const tables = sections.map((s) => sectionTable(s, undefined, undefined, undefined, tableOpts));
  // The breach outflow hydrograph (SI) is the routing inflow at the first section.
  const inflow = {
    tS: series.map((p) => p.tHr * 3600),
    qCms: series.map((p) => p.qCfs * CFS),
  };
  // Terrain-triggered routing: MCT in defined valleys, frozen MC on flat plains and tiny pools (routing.ts router).
  // "auto" holds the MC footprint where it is right (2801 plain, 4031 pool) and takes MCT's attenuation where it helps
  // (2865 incised, 3034 confined). The confinement trigger (0.0015) is insensitive from 0.0005 to 0.002 on the four
  // baseline dams; see the sweep in /mnt/project-files/crest-inundation/mct-terrain-router-2026-11-04/.
  const routed = routeMuskingumCunge(inflow, tables, {
    maxHours: opts.maxHours ?? 72,
    router: opts.router ?? "auto",
  });

  const releasedAcFt = result.breach.volumeAcFt;
  const N = g.width * g.height;
  const depthFt = new Float32Array(N);
  const notes = [...routed.notes];
  if (pathNote) notes.unshift(pathNote);
  let wseM: Float32Array;
  let floodedAc: number;
  let volumeM3: number;
  let volumeFlag: boolean;
  let reachedEdge: boolean;
  let velocityFps: Float32Array | undefined;
  let arrivalHr: Float32Array | undefined;
  let exhaustedStretches = 0;
  let cappedStretches = 0;
  let unpouredM3 = 0;
  let demand: RoutedFloodResult["demand"];
  let ground: Uint8Array | undefined;
  if (method === "spread") {
    const spOpts = {
      minDepthM: opts.minDepthM ?? 0.03,
      releasedM3: releasedAcFt * ACFT,
      basins: opts.basins ?? true,
      // A stretch that runs out of ground is held at the routed stage + 1 ft and never above 2× the dam's height.
      maxLevelM: Number.isFinite(dam.heightFt) && dam.heightFt > 0 ? 2 * dam.heightFt * FT : undefined,
    };
    // With a yield hook (the in-thread build on the desk) the pour pauses between its passes; without one (a worker,
    // Node) it runs straight through. Same result either way.
    const sp = opts.yieldFn ? await spreadVolumeAsync(g, centerline, routed.sections, spOpts, opts.yieldFn) : spreadVolume(g, centerline, routed.sections, spOpts);
    ground = sp.ground;
    exhaustedStretches = sp.exhaustedStretches;
    cappedStretches = sp.cappedStretches;
    unpouredM3 = sp.unpouredM3;
    demand = {
      reachedEdge: sp.reachedEdge,
      travellingM3: sp.travellingM3,
      wetEndM: sp.wetEndM,
      channelEndM: centerline.pts[centerline.pts.length - 1].x,
      stoppedInBasin: sp.stoppedIn != null,
    };
    for (let k = 0; k < N; k++) depthFt[k] = sp.depthM[k] > 0 ? sp.depthM[k] / FT : 0;
    velocityFps = new Float32Array(N).fill(NaN);
    arrivalHr = new Float32Array(N).fill(NaN);
    for (let k = 0; k < N; k++)
      if (sp.depthM[k] > 0) {
        velocityFps[k] = sp.velocityMps[k] / FT;
        arrivalHr[k] = sp.arrivalS[k] / 3600;
      }
    wseM = sp.wseM;
    floodedAc = sp.floodedAc;
    volumeM3 = sp.volumeM3;
    reachedEdge = sp.reachedEdge;
    // The spread is volume-conserving by construction; the flag here is the envelope outside the band a real
    // max-depth envelope holds (LISFLOOD ~3–5× the release on the baseline dams), checked loosely at 10×.
    volumeFlag = sp.volumeRatio != null && sp.volumeRatio > SPREAD_RATIO_FLAG;
    notes.push(
      `Volume spread: ${sp.stretches} stretches poured at their own peak storage, ${(sp.volumeM3 / ACFT).toFixed(0)} ac-ft laid down for ${releasedAcFt.toFixed(0)} ac-ft released (${sp.volumeRatio == null ? "—" : sp.volumeRatio.toFixed(1) + "×"}, the max-depth envelope of a moving flood).`,
    );
    if (sp.retainedM3 > 0)
      notes.push(
        `${(sp.retainedM3 / ACFT).toFixed(0)} ac-ft is left behind in depressions on the way down; ${sp.travellingM3 > 0 ? `${(sp.travellingM3 / ACFT).toFixed(0)} ac-ft is still travelling at the end of the traced reach` : "the flood is spent before the end of the traced reach"}.`,
      );
    if (sp.stoppedIn)
      notes.push(
        `The flood ends ${(sp.stoppedIn.atM / 1000).toFixed(1)} km down in a closed basin that holds ${(sp.stoppedIn.capacityM3 / ACFT).toFixed(0)} ac-ft below its spill level (${(sp.stoppedIn.spillM / FT).toFixed(0)} ft), more than the ${(sp.stoppedIn.heldM3 / ACFT).toFixed(0)} ac-ft that reaches it: it stands there as a ${sp.stoppedIn.areaAc.toFixed(0)} ac pond. If the basin is closed by a road or rail fill, check for a culvert or bridge the DEM doesn't show.`,
      );
    if (sp.basinsFilled)
      notes.push(`${sp.basinsFilled} closed basin${sp.basinsFilled === 1 ? "" : "s"} on the channel filled to the spill level before the wave passed.`);
    if (sp.exhaustedStretches)
      notes.push(
        `${sp.exhaustedStretches} stretch${sp.exhaustedStretches === 1 ? "" : "es"} ran out of ground before the slice was poured (the DEM is too small there)` +
          (sp.cappedStretches
            ? `; on ${sp.cappedStretches} the pool is held at the routed stage + 1 ft (never above 2× the dam height) and ${(sp.unpouredM3 / ACFT).toFixed(0)} ac-ft of the slices is not laid down: it would stand beyond the ground shown, not deeper on it.`
            : "."),
      );
    notes.push(
      "A level pool reaches only ground below its level: a breach front that surges over a low divide can wet ground this layer leaves dry (a known under-prediction).",
    );
  } else {
    const fill = fillFlood(g, centerline, routed.sections, {
      minDepthM: opts.minDepthM ?? 0.03,
      availableVolumeM3: releasedAcFt * ACFT,
      volumeLimit: opts.volumeLimit ?? false,
      slopeFloor: opts.slopeFloor,
      maxFloodplainDepthM: opts.floodplainDepthMarginM ?? FLOODPLAIN_DEPTH_MARGIN_M,
    });
    for (let k = 0; k < N; k++) depthFt[k] = fill.depthM[k] > 0 ? fill.depthM[k] / FT : 0;
    wseM = fill.wseM;
    floodedAc = fill.floodedAc;
    volumeM3 = fill.volumeM3;
    volumeFlag = fill.volumeFlag;
    reachedEdge = fill.reachedEdge;
    if (fill.volumeFlag)
      notes.push(
        `The fill lays down ${(fill.volumeM3 / ACFT).toFixed(0)} ac-ft, more than the breach releases (${releasedAcFt.toFixed(0)} ac-ft): the water-surface envelope is wider than the released volume supports.`,
      );
  }

  const peakInCfs = routed.sections[0].peakCms / CFS;
  const peakOutCfs = routed.sections[routed.sections.length - 1].peakCms / CFS;
  const channelM = centerline.pts.length ? centerline.pts[centerline.pts.length - 1].x : 0;
  if (reachedEdge)
    // After the routing notes, which a flow-path note (unshifted above) pushes down by one.
    notes.splice(
      routed.notes.length + (pathNote ? 1 : 0),
      0,
      "The routed flood reached the edge of the DEM; it would spread further than the DEM shows.",
    );
  const fillAcFt = volumeM3 / ACFT;

  const rf: RoutedFloodResult = {
    ok: true,
    bbox: g.bbox,
    width: g.width,
    height: g.height,
    depthFt,
    wseM,
    ...(velocityFps ? { velocityFps, arrivalHr } : {}),
    method,
    ...(ground ? { ground } : {}),
    centerline,
    sections: routed.sections,
    stats: {
      floodedAc: Math.round(floodedAc * 10) / 10,
      peakInCfs: Math.round(peakInCfs),
      peakOutCfs: Math.round(peakOutCfs),
      channelKm: Math.round(channelM / 100) / 10,
      sections: sections.length,
      cappedSections: routed.sections.filter((s) => s.capped).length,
      volumeFlag,
      reachedEdge,
      routedPeakFellBy: peakInCfs > 0 ? Math.round((1 - peakOutCfs / peakInCfs) * 100) / 100 : 0,
      fillAcFt: Math.round(fillAcFt),
      releasedAcFt: Math.round(releasedAcFt),
      volumeRatio: releasedAcFt > 0 ? Math.round((fillAcFt / releasedAcFt) * 10) / 10 : 0,
      exhaustedStretches,
      cappedStretches,
      unpouredAcFt: Math.round(unpouredM3 / ACFT),
      travellingAcFt: demand ? Math.round(demand.travellingM3 / ACFT) : 0,
      wetEndKm: demand ? Math.round(demand.wetEndM / 100) / 10 : 0,
      stoppedInBasin: demand?.stoppedInBasin ?? false,
    },
    ...(demand ? { demand } : {}),
    notes,
  };
  const sweeps = opts.settleSweeps ?? SETTLE_SWEEPS;
  if (method === "spread" && sweeps > 0) await settleRoutedFlood(g, rf, { sweeps, minDepthM: opts.minDepthM, yieldFn: opts.yieldFn });
  return rf;
}

/**
 * Let a spread's pools settle into one sheet (spread.ts settlePools), in place: depth, velocity, arrival and WSE are
 * rewritten and the flooded area and laid-down volume re-counted. The demand signal (water still travelling, the
 * basin it stopped in, how far down it got) is the pour's and is left alone, so the reach loop (reach.ts) behaves the
 * same with or without it; reach.ts builds its rounds unsettled and settles only the round it keeps.
 */
export async function settleRoutedFlood(
  g: DemGrid,
  rf: RoutedFloodResult,
  opts: { sweeps?: number; minDepthM?: number; yieldFn?: () => Promise<void> } = {},
): Promise<RoutedFloodResult> {
  const sweeps = opts.sweeps ?? SETTLE_SWEEPS;
  if (rf.method !== "spread" || !rf.ground || !(sweeps > 0) || rf.settled) return rf;
  const N = rf.width * rf.height;
  const before = rf.stats.floodedAc;
  const { volume } = await settlePools(
    g,
    rf.ground,
    { depth: rf.depthFt, velocity: rf.velocityFps, arrival: rf.arrivalHr },
    { sweeps, minDepth: (opts.minDepthM ?? 0.03) / FT, zScale: 1 / FT, gravity: 9.80665 / FT, yieldFn: opts.yieldFn },
  );
  const midLat = (g.bbox[1] + g.bbox[3]) / 2;
  const cellM2 = ((g.bbox[2] - g.bbox[0]) / g.width) * 111320 * Math.cos((midLat * Math.PI) / 180) * (((g.bbox[3] - g.bbox[1]) / g.height) * 110574);
  let wet = 0;
  for (let k = 0; k < N; k++) {
    if (rf.depthFt[k] > 0) {
      wet++;
      rf.wseM[k] = g.z[k] + rf.depthFt[k] * FT;
    } else rf.wseM[k] = NaN;
  }
  const floodedAc = (wet * cellM2) / 4046.8564224;
  const fillAcFt = (volume * FT * cellM2) / ACFT;
  rf.settled = sweeps;
  rf.stats.floodedAc = Math.round(floodedAc * 10) / 10;
  rf.stats.fillAcFt = Math.round(fillAcFt);
  rf.stats.volumeRatio = rf.stats.releasedAcFt > 0 ? Math.round((fillAcFt / rf.stats.releasedAcFt) * 10) / 10 : 0;
  rf.notes.push(
    `The pools were let settle into one sheet (${sweeps} sweeps, water only moved, never added): ${before.toFixed(0)} ac as poured, ${floodedAc.toFixed(0)} ac settled.`,
  );
  return rf;
}
