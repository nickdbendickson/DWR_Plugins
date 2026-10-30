import { damLocation } from "./engine.ts";
import type { Analysis, DamRecord, SeriesPoint } from "./types.ts";

const CFS_PER_CMS = 35.3146667;

/** Hydrograph CSV in FIMsim / Breach Runner format: time_hours,discharge_cms */
export function exportHydrographCsv(series: SeriesPoint[]): string {
  const lines = ["time_hours,discharge_cms"];
  for (const p of series) {
    const cms = p.qCfs / CFS_PER_CMS;
    lines.push(`${p.tHr.toFixed(4)},${cms.toFixed(4)}`);
  }
  return lines.join("\n") + "\n";
}

/** Minimal dam.json matching fields the Runner / Screening read. */
export function exportDamJson(dam: DamRecord, analysis: Analysis): string {
  const loc = damLocation(dam);
  const obj = {
    dam_id: dam.nid || dam.id,
    name: dam.name,
    height_ft: dam.heightFt,
    storage_acft: dam.storageAcFt,
    surface_area_top_ac: dam.surfaceAreaAc && dam.surfaceAreaAc > 0 ? dam.surfaceAreaAc : null,
    length_ft: dam.lengthFt,
    crest_ft: analysis.datum.crestNavdFt,
    crest_datum: "NAVD88",
    datum_rule: analysis.datum.rule,
    datum_note: dam.datumNote ?? null,
    lat: loc?.lat ?? null,
    lon: loc?.lon ?? null,
    method: dam.method,
    failure_mode: dam.failureMode,
    screened: {
      level: analysis.screen.level,
      level_label: analysis.screen.label,
      judgment_as_probable: analysis.screen.judgmentAsProbable,
      acer_level: analysis.screen.acerLevel,
      judgment: analysis.screen.judgment,
      notes: analysis.screen.reasons,
    },
    hydrograph_peak_cfs: analysis.breach.peakCfs,
    froehlich_1995_peak_cfs: analysis.breach.empiricalPeakCfs,
    volume_acft: analysis.breach.volumeAcFt,
    // Only when an AOI is drawn, so dam.json is unchanged without one.
    ...(analysis.flood.aoi
      ? {
          aoi_limit: {
            applied: analysis.flood.aoi.applied,
            label: analysis.flood.aoi.label,
            near_km: analysis.flood.aoi.nearKm,
            far_km: analysis.flood.aoi.farKm,
            direction_basis: analysis.flood.aoi.directionBasis,
            direction: analysis.flood.aoi.direction,
            reach_basis: analysis.flood.aoi.reachBasis,
            area_basis: analysis.flood.aoi.areaBasis,
            upstream_frac: analysis.flood.aoi.upstreamFrac,
            max_depth_ft: analysis.flood.maxDepthFt,
            max_depth_inside_ft: analysis.flood.aoi.maxDepthInsideFt,
            edge_depth_ft: analysis.flood.aoi.edgeDepthFt,
            area_ac: analysis.flood.aoi.areaAc,
            limits_reach: analysis.flood.aoi.limitsReach,
            wet_at_edge: analysis.flood.aoi.wetAtEdge,
            reach_km: analysis.flood.lengthM / 1000,
            wet_area_ac: analysis.flood.acres,
          },
        }
      : {}),
    // Only for records filled from the NID (ND dam picker), so other dam.json files are unchanged.
    ...(dam.nidId
      ? {
          nid_id: dam.nidId,
          nid_hazard: dam.nidHazard ?? null,
          nid_hazard_note: "As reported to the NID (FEMA class). Not used by the desk screen; not the class of record.",
          provenance: dam.provenance ?? {},
          attribution: [dam.nidRef?.attribution, dam.nidRef?.dwrAttribution].filter(Boolean),
        }
      : {}),
  };
  return JSON.stringify(obj, null, 2) + "\n";
}

/** WGS 84 / NAD 83 geographic to UTM (metres), zone from the longitude (ND: 13N or 14N). */
export function toUtm(lon: number, lat: number, zone = Math.floor((lon + 180) / 6) + 1) {
  const a = 6378137;
  const f = 1 / 298.257222101; // GRS 80 (NAD 83); WGS 84 differs by < 1 mm here
  const k0 = 0.9996;
  const e2 = f * (2 - f);
  const ep2 = e2 / (1 - e2);
  const phi = (lat * Math.PI) / 180;
  const lam = (lon * Math.PI) / 180;
  const lam0 = (((zone - 1) * 6 - 180 + 3) * Math.PI) / 180;
  const sin = Math.sin(phi);
  const cos = Math.cos(phi);
  const tan = Math.tan(phi);
  const N = a / Math.sqrt(1 - e2 * sin * sin);
  const T = tan * tan;
  const C = ep2 * cos * cos;
  const A = cos * (lam - lam0);
  const e4 = e2 * e2;
  const e6 = e4 * e2;
  const M =
    a *
    ((1 - e2 / 4 - (3 * e4) / 64 - (5 * e6) / 256) * phi -
      ((3 * e2) / 8 + (3 * e4) / 32 + (45 * e6) / 1024) * Math.sin(2 * phi) +
      ((15 * e4) / 256 + (45 * e6) / 1024) * Math.sin(4 * phi) -
      ((35 * e6) / 3072) * Math.sin(6 * phi));
  const x =
    k0 * N * (A + ((1 - T + C) * A ** 3) / 6 + ((5 - 18 * T + T * T + 72 * C - 58 * ep2) * A ** 5) / 120) + 500000;
  const y =
    k0 *
    (M +
      N *
        tan *
        ((A * A) / 2 +
          ((5 - T + 9 * C + 4 * C * C) * A ** 4) / 24 +
          ((61 - 58 * T + T * T + 600 * C - 330 * ep2) * A ** 6) / 720));
  return { x, y, zone, epsg: 26900 + zone };
}

export type LisfloodOptions = {
  cellSizeM?: number;
  /** Number of inflow cells the point source is spread over (the Runner uses 2). */
  inflowCells?: number;
  /** Point source in the DEM's coordinates. Default: the breach location projected to UTM (NAD 83). */
  pointX?: number;
  pointY?: number;
  simHours?: number;
  fpfric?: number;
  /** DEM extent (DEM CRS, metres) for the FREE edge lines. Default: a range that covers any edge. */
  demExtent?: { xmin: number; xmax: number; ymin: number; ymax: number };
};

export type LisfloodPackage = { par: string; bci: string; bdy: string; notes: string };

/**
 * LISFLOOD-FP package: .par with the keywords LISFLOOD needs, .bci point source(s) at the breach,
 * .bdy in LISFLOOD's format (comment line, boundary name, "N seconds", then value/time pairs) in
 * m3/s per metre of cell width spread over `inflowCells` cells: Q / (cell size × cells).
 */
export function generateLisfloodPackage(dam: DamRecord, series: SeriesPoint[], opts: LisfloodOptions = {}): LisfloodPackage {
  const cell = opts.cellSizeM != null && Number.isFinite(opts.cellSizeM) && opts.cellSizeM > 0 ? opts.cellSizeM : 10;
  const cells = Math.max(1, Math.round(opts.inflowCells ?? 2));
  const simHours = opts.simHours ?? 72;
  const fpfric = opts.fpfric ?? 0.06;
  const resroot = (dam.nid || "dam").toLowerCase().replace(/[^a-z0-9]/g, "_");

  let x = opts.pointX;
  let y = opts.pointY;
  let crsNote: string;
  if (x != null && y != null && Number.isFinite(x) && Number.isFinite(y)) {
    crsNote = "Point source at the coordinates given (DEM CRS).";
  } else {
    const loc = damLocation(dam);
    if (!loc) {
      throw new Error("No breach location: draw the breach point or enter the dam latitude and longitude first.");
    }
    const u = toUtm(loc.lon, loc.lat);
    x = u.x;
    y = u.y;
    crsNote = `Point source at the ${loc.source} (${loc.lat.toFixed(5)}, ${loc.lon.toFixed(5)}) projected to NAD83 / UTM zone ${u.zone}N (EPSG:${u.epsg}). The DEM must be in that CRS, metres, with ${cell} m cells.`;
  }

  const simS = Math.round(simHours * 3600);
  const par = [
    `# Crest desk export for ${dam.name} (${dam.nid || dam.id}). ${simHours} h run so the recession drains.`,
    `DEMfile ${resroot}.dem.asc`,
    `resroot ${resroot}`,
    "dirroot results",
    `sim_time ${simS}`,
    "initial_tstep 1.000",
    "massint 300",
    "saveint 3600",
    `bcifile ${resroot}.bci`,
    `bdyfile ${resroot}.bdy`,
    "acceleration",
    "hazard",
    `fpfric ${fpfric.toFixed(4)}`,
    "",
  ].join("\n");

  // Point sources across the breach: the breach cell and the next (cells-1) cells east. Then FREE
  // (normal-depth) N/S/E/W edges, as the Runner's run.bci: LISFLOOD's default edges are closed, so a
  // 72 h run would pond against the DEM edge. N/S lines span x, E/W lines span y.
  const ext = opts.demExtent;
  const [x0, x1, y0, y1] = ext ? [ext.xmin, ext.xmax, ext.ymin, ext.ymax] : [-1e8, 1e8, -1e8, 1e8];
  const bci =
    [
      ...Array.from({ length: cells }, (_, i) => `P ${(x! + i * cell).toFixed(3)} ${y!.toFixed(3)} QVAR breach`),
      `N ${x0.toFixed(3)} ${x1.toFixed(3)} FREE`,
      `S ${x0.toFixed(3)} ${x1.toFixed(3)} FREE`,
      `E ${y0.toFixed(3)} ${y1.toFixed(3)} FREE`,
      `W ${y0.toFixed(3)} ${y1.toFixed(3)} FREE`,
    ].join("\n") + "\n";

  const pts = [...series];
  const last = pts[pts.length - 1];
  if (!last || last.tHr * 3600 < simS) pts.push({ tHr: simHours, qCfs: 0 });
  const bdyLines = [`breach hydrograph: m3/s / (cell size x ${cells} inflow cells)`, "breach", `${pts.length} seconds`];
  for (const p of pts) {
    const unit = p.qCfs / CFS_PER_CMS / (cell * cells);
    bdyLines.push(`${unit.toFixed(6)} ${(p.tHr * 3600).toFixed(4)}`);
  }
  const bdy = bdyLines.join("\n") + "\n";
  const notes = [
    crsNote,
    `Grid: ${cell} m cells; inflow over ${cells} cells. Default grid is NAD83 / UTM 14N at 10 m (Nick, 2026-10-29); the Runner's EPSG:5070 30 m grid needs the point and cell size entered.`,
    ext
      ? "Edges: FREE (normal depth) on N/S/E/W over the DEM extent given."
      : "Edges: FREE (normal depth) on N/S/E/W; the ranges cover any DEM, so every edge cell is open.",
    `Supply ${resroot}.dem.asc (ASCII grid, NAVD88 metres) beside the .par.`,
    `fpfric ${fpfric} is a single floodplain Manning n; replace it with land-cover roughness for a study run.`,
  ].join("\n");
  return { par, bci, bdy, notes };
}
