/**
 * Results: imported model runs (LISFLOOD-FP, DSS-WISE) next to the desk estimate. Pure module: where the record's
 * receptors sit on the ground, how each source ranks them, where the sources disagree, and the analysis the
 * Screen, PLSS list and Report use when a model run is the source of record.
 */
import { rankReceptor, WET_FT } from "./acer.ts";
import { downstreamDirection, fromLocal, type XY } from "./aoi.ts";
import { damLocation, screenReceptors } from "./engine.ts";
import { bandFor, DEPTH_BANDS, type FloodStrip, type StripCommon, type StripFeature } from "./floodstrip.ts";
import type { Analysis, DamRecord, ReceptorKind, ReceptorSample, ResultSource, ResultsOfRecord, RunStats } from "./types.ts";

export const SOURCE_LABEL: Record<ResultSource | "desk", string> = {
  desk: "Desk estimate",
  lisflood: "LISFLOOD-FP",
  dsswise: "DSS-WISE",
};
export const SOURCE_COLOR: Record<ResultSource | "desk", string> = {
  desk: "#b45309",
  lisflood: "#c2185b",
  dsswise: "#2e7d32",
};

type Ranked = Analysis["flood"]["receptors"][number];
type Zone = Ranked["zone"];
type NdClass = Ranked["ndClass"];

/**
 * A point to sample in every source. Receptors come from the record; 911 address points (swe/nd911-points) can
 * be added with source "911" and no seed, and are listed but not screened.
 */
export type ResultPoint = {
  id: string;
  name: string;
  kind: ReceptorKind | "address";
  lon: number;
  lat: number;
  source: "receptor" | "911";
  /** "mapped": the seed's own lon/lat; "desk valley": where the desk's straight valley puts it. */
  placed: "mapped" | "desk valley";
};

/** Breach, downstream direction (local east/north) and desk reach for the scan, or why there is none. */
export function resultsContext(dam: DamRecord, result: Analysis) {
  const loc = damLocation(dam);
  if (!loc) return { ctx: null, reason: "No breach location: draw the breach point or enter the dam latitude and longitude." };
  const dir = downstreamDirection(dam.geometry, loc);
  return {
    ctx: { breach: { lon: loc.lon, lat: loc.lat }, u: dir.u as [number, number] | null, reachKm: result.flood.lengthM / 1000 },
    reason: null,
    breachSource: loc.source,
    direction: dir.note,
  };
}

function halfAt(stations: Analysis["flood"]["stations"], xKm: number) {
  if (stations.length === 0) return 28;
  if (xKm <= stations[0].xKm) return stations[0].halfM;
  for (let i = 1; i < stations.length; i++)
    if (xKm <= stations[i].xKm) {
      const a = stations[i - 1];
      const b = stations[i];
      const t = (xKm - a.xKm) / Math.max(1e-9, b.xKm - a.xKm);
      return a.halfM + t * (b.halfM - a.halfM);
    }
  return stations[stations.length - 1].halfM;
}

/**
 * Ground position of each record receptor: its own lon/lat when mapped, else the desk valley's place for it
 * (distance along the downstream direction from the breach, `lateral` across the desk half-width; -1 = left bank
 * looking downstream). Receptors with no breach location or direction are left out.
 */
export function receptorPoints(dam: DamRecord, result: Analysis): ResultPoint[] {
  const loc = damLocation(dam);
  const u = loc ? downstreamDirection(dam.geometry, loc).u : null;
  const out: ResultPoint[] = [];
  dam.receptors.forEach((seed, i) => {
    const r = result.flood.receptors[i];
    if (seed.lon != null && seed.lat != null && Number.isFinite(seed.lon) && Number.isFinite(seed.lat)) {
      out.push({ id: seed.id, name: seed.name, kind: seed.kind, lon: seed.lon, lat: seed.lat, source: "receptor", placed: "mapped" });
      return;
    }
    if (!loc || !u || !r) return;
    const x = r.distanceKm * 1000;
    const h = halfAt(result.flood.stations, r.distanceKm);
    const left: XY = [-u[1], u[0]];
    const p: XY = [u[0] * x - seed.lateral * h * left[0], u[1] * x - seed.lateral * h * left[1]];
    const [lon, lat] = fromLocal(p, loc);
    out.push({ id: seed.id, name: seed.name, kind: seed.kind, lon, lat, source: "receptor", placed: "desk valley" });
  });
  return out;
}

export type SourceRank = {
  covered: boolean;
  depthFt: number;
  velocityFps: number | null;
  arrivalHr: number | null;
  flooded: boolean;
  zone: Zone;
  ndClass: NdClass;
  reason: string;
  judgment: boolean;
};

/** How one raster ranks one receptor (raster depth as-is: the model terrain stands in for the pad). */
export function rankSample(dam: DamRecord, seedIndex: number, s: ReceptorSample | undefined, label: string): SourceRank {
  const seed = dam.receptors[seedIndex];
  if (!s || !s.covered)
    return {
      covered: false,
      depthFt: 0,
      velocityFps: null,
      arrivalHr: null,
      flooded: false,
      zone: null,
      ndClass: null,
      reason: s ? `unresolved: outside the ${label} raster; not screened` : `unresolved: not sampled from ${label} (re-import the run); not screened`,
      judgment: true,
    };
  const flooded = s.depthFt >= WET_FT;
  const v = s.velocityFps ?? 0;
  const rank = rankReceptor({
    kind: seed.kind,
    depthFt: flooded ? s.depthFt : 0,
    velocityFps: flooded ? v : 0,
    aadt: seed.aadt,
    roadClass: seed.roadClass,
    habitable: seed.habitable,
    judgmentAsProbable: dam.judgmentAsProbable ?? true,
  });
  const noVel = flooded && s.velocityFps == null && rank.zone != null;
  return {
    covered: true,
    depthFt: flooded ? s.depthFt : 0,
    velocityFps: flooded ? s.velocityFps : s.velocityFps == null ? null : 0,
    arrivalHr: flooded ? s.arrivalHr : null,
    flooded,
    zone: rank.zone,
    ndClass: rank.class,
    reason: noVel ? `${rank.reason} (no velocity raster: zone from depth alone)` : rank.reason,
    judgment: rank.judgment || noVel,
  };
}

export function deskRank(r: Ranked): SourceRank {
  return {
    covered: !r.unresolved,
    depthFt: r.depthFt,
    velocityFps: r.flooded ? r.velocityFps : 0,
    arrivalHr: r.arrivalHr,
    flooded: r.flooded,
    zone: r.zone,
    ndClass: r.ndClass,
    reason: r.rankReason,
    judgment: r.needsJudgment,
  };
}

/** Why the sources disagree on a point (empty = they agree). Unresolved sources are left out of the call. */
export function disagreement(ranks: Partial<Record<ResultSource | "desk", SourceRank>>): string[] {
  const got = Object.entries(ranks).filter(([, r]) => r && r.covered) as [string, SourceRank][];
  if (got.length < 2) return [];
  const out: string[] = [];
  const name = (k: string) => SOURCE_LABEL[k as ResultSource | "desk"];
  const wet = got.filter(([, r]) => r.flooded).map(([k]) => k);
  if (wet.length > 0 && wet.length < got.length)
    out.push(`wet in ${wet.map(name).join(", ")}, dry in ${got.filter(([, r]) => !r.flooded).map(([k]) => name(k)).join(", ")}`);
  const zones = new Set(got.filter(([, r]) => r.flooded && r.zone).map(([, r]) => r.zone));
  if (zones.size > 1) out.push(`TM 11 zone differs (${[...zones].join(" vs ")})`);
  const classes = new Set(got.map(([, r]) => r.ndClass ?? "none"));
  if (classes.size > 1) out.push(`ND class differs (${[...classes].join(" vs ")})`);
  const depths = got.map(([, r]) => r.depthFt);
  const hi = Math.max(...depths);
  const lo = Math.min(...depths);
  if (hi - lo > Math.max(1, 0.5 * hi)) out.push(`depth spread ${(hi - lo).toFixed(1)} ft`);
  return out;
}

const DESK_LINES = [
  /^Velocities are desk estimates/,
  /^This is a desk screen on a schematic valley/,
];

/**
 * The analysis as the source of record sees it: the desk analysis unchanged, or the receptors ranked on the
 * imported raster's values and the screen rerun on them. Breach and hydrograph stay the desk's.
 */
export function analysisOfRecord(dam: DamRecord, result: Analysis): { result: Analysis; ref: ResultsOfRecord | null; label: string } {
  const ref = dam.resultsOfRecord ?? null;
  if (!ref) return { result, ref: null, label: SOURCE_LABEL.desk };
  const byId = new Map(ref.samples.map((s) => [s.id, s]));
  const receptors: Ranked[] = result.flood.receptors.map((r, i) => {
    const k = rankSample(dam, i, byId.get(r.id), ref.label);
    return {
      ...r,
      depthFt: k.depthFt,
      velocityFps: k.velocityFps ?? 0,
      arrivalHr: k.arrivalHr,
      flooded: k.flooded,
      unresolved: !k.covered,
      outsideAoi: false,
      wouldBe: null,
      zone: k.zone,
      ndClass: k.ndClass,
      rankReason: k.reason,
      needsJudgment: k.judgment,
    };
  });
  const screen = screenReceptors(receptors, dam.receptors, { judgmentAsProbable: dam.judgmentAsProbable ?? true });
  screen.reasons = screen.reasons.filter((l) => !DESK_LINES.some((re) => re.test(l)));
  const st = ref.stats;
  screen.reasons.push(
    `Source of record: ${ref.label}, imported ${ref.importedAt.slice(0, 10)} (${ref.files.length} file${ref.files.length === 1 ? "" : "s"}). Receptor depths are the raster's maximum depth at each receptor (no pad subtracted)${st.maxVelocityFps == null ? "; no velocity raster, so TM 11 zones use depth alone" : ""}.`,
  );
  if (receptors.some((r, i) => dam.receptors[i] && dam.receptors[i].lon == null))
    screen.reasons.push("Receptors without a mapped location are sampled where the desk's straight valley puts them, so a miss can be placement, not the model.");
  screen.reasons.push("This screen uses an imported model run. It is still not an ND Dam Safety classification of record.");
  if (dam.classOfRecord) {
    screen.classOfRecord = dam.classOfRecord;
    screen.reasons.unshift(`Class of record: ${dam.classOfRecord}. This screen (${screen.label}) is not the class of record.`);
  }
  return {
    result: {
      ...result,
      flood: {
        ...result.flood,
        engineLabel: ref.label,
        maxDepthFt: st.peakDepthFt,
        acres: st.wetAc,
        maxVelocityFps: st.maxVelocityFps ?? receptors.reduce((m, r) => Math.max(m, r.velocityFps), 0),
        velocityBasis: st.maxVelocityFps == null ? `${ref.label}: no velocity raster` : `${ref.label} max velocity raster`,
        receptors,
      },
      screen,
    },
    ref,
    label: ref.label,
  };
}

/** Desk column of the comparison table, from the desk analysis. */
export function deskStats(result: Analysis) {
  const wet = result.flood.stations.filter((s) => s.depthFt >= WET_FT - 1e-9);
  const arr = result.flood.receptors.filter((r) => r.flooded && r.arrivalHr != null).map((r) => r.arrivalHr as number);
  return {
    peakDepthFt: result.flood.maxDepthFt,
    wetAc: result.flood.acres,
    farthestKm: wet.length ? wet[wet.length - 1].xKm : 0,
    maxVelocityFps: result.flood.maxVelocityFps,
    arrivalMaxHr: arr.length ? Math.max(...arr) : null,
  };
}

// ---------------------------------------------------------------------------------------------------------------
// Import checks that need the record (the worker does the raster-only ones)

export type Check = { level: "ok" | "warn" | "error"; text: string };

/** Record-dependent checks on a scanned run: dam match, coverage of the desk reach, flood leaving the grid. */
export function recordChecks(
  dam: DamRecord,
  result: Analysis,
  stats: RunStats,
  runDam: { lon: number; lat: number; id?: string; nid?: string } | null,
): Check[] {
  const out: Check[] = [];
  const loc = damLocation(dam);
  if (runDam && loc) {
    const kx = 111_319.49 * Math.cos((loc.lat * Math.PI) / 180);
    const d = Math.hypot((runDam.lon - loc.lon) * kx, (runDam.lat - loc.lat) * 110_946.26);
    const ids = [runDam.id, runDam.nid].filter(Boolean).join(" / ");
    const idMatch = !runDam.nid || !dam.nid || runDam.nid.toUpperCase() === dam.nid.toUpperCase() || runDam.id === dam.id;
    out.push({
      level: d > 1000 || !idMatch ? "error" : d > 150 ? "warn" : "ok",
      text: `Run's dam${ids ? ` ${ids}` : ""} is ${Math.round(d)} m from this record's dam location (${loc.source === "record" ? "NID lat/lon" : loc.source})${idMatch ? "" : ` and its ID does not match ${dam.nid}`}.`,
    });
  }
  if (Number.isFinite(stats.nearestWetM))
    out.push({
      level: stats.nearestWetM > 1500 ? "error" : stats.nearestWetM > 300 ? "warn" : "ok",
      text: `Nearest wet cell is ${Math.round(stats.nearestWetM)} m from the breach location${stats.nearestWetM > 1500 ? ": the run may be for another dam or misplaced (CRS?)" : ""}.`,
    });
  else out.push({ level: "error", text: "No cell is 0.1 ft or deeper." });
  if (stats.coverKm == null) out.push({ level: "error", text: "The breach location is outside the raster." });
  else if (stats.coverKm < result.flood.lengthM / 1000)
    out.push({
      level: "warn",
      text: `Coverage stops ${stats.coverKm.toFixed(1)} km downstream, short of the desk reach end (${(result.flood.lengthM / 1000).toFixed(1)} km); receptors past it are unresolved.`,
    });
  else out.push({ level: "ok", text: `Coverage reaches ${stats.coverKm.toFixed(1)} km downstream (desk reach ${(result.flood.lengthM / 1000).toFixed(1)} km).` });
  if (stats.edgeWetCells > 0)
    out.push({ level: "warn", text: `${stats.edgeWetCells.toLocaleString("en-US")} wet cells touch the raster edge: the flood runs off the grid there.` });
  return out;
}

// ---------------------------------------------------------------------------------------------------------------
// Raster → FloodStrip, so plss.ts lists the sections a model run touches

const r7 = (n: number) => Math.round(n * 1e7) / 1e7;
const r3 = (n: number) => Math.round(n * 1e3) / 1e3;

export type StripGrid = {
  width: number;
  height: number;
  x0: number;
  y0: number;
  dx: number;
  dy: number;
  depth: Float32Array;
};

/**
 * Depth-band rectangles from a (coarsened) grid: row runs of one band, merged down the columns, downstream of the
 * dam line only (cell centre along ≥ 0). `inv` maps raster x/y to lon/lat; `along` gives the downstream distance (m).
 */
export function stripFromGrid(
  g: StripGrid,
  inv: (x: number, y: number) => [number, number],
  along: (x: number, y: number) => number,
  common: StripCommon,
  meta: { direction: string; breach: { lon: number; lat: number } },
): FloodStrip | null {
  const band = new Int8Array(g.width * g.height).fill(-1);
  let maxD = 0;
  let farM = 0;
  for (let r = 0; r < g.height; r++)
    for (let c = 0; c < g.width; c++) {
      const d = g.depth[r * g.width + c];
      if (!(d >= WET_FT)) continue;
      const a = along(g.x0 + (c + 0.5) * g.dx, g.y0 - (r + 0.5) * g.dy);
      if (a < 0) continue;
      band[r * g.width + c] = DEPTH_BANDS.indexOf(bandFor(d));
      maxD = Math.max(maxD, d);
      farM = Math.max(farM, a);
    }
  type Rect = { b: number; c0: number; c1: number; r0: number; r1: number };
  const open = new Map<string, Rect>();
  const rects: Rect[] = [];
  for (let r = 0; r < g.height; r++) {
    const seen = new Set<string>();
    let c = 0;
    while (c < g.width) {
      const b = band[r * g.width + c];
      if (b < 0) {
        c++;
        continue;
      }
      let e = c + 1;
      while (e < g.width && band[r * g.width + e] === b) e++;
      const key = `${b}:${c}:${e}`;
      const o = open.get(key);
      if (o && o.r1 === r) o.r1 = r + 1;
      else {
        const n = { b, c0: c, c1: e, r0: r, r1: r + 1 };
        open.set(key, n);
        rects.push(n);
      }
      seen.add(key);
      c = e;
    }
    for (const k of [...open.keys()]) if (!seen.has(k)) open.delete(k);
  }
  if (rects.length === 0) return null;
  const cellAc = (g.dx * g.dy) / 4046.8564224;
  const features: StripFeature[] = [];
  let total = 0;
  DEPTH_BANDS.forEach((bd, bi) => {
    const mine = rects.filter((q) => q.b === bi);
    if (!mine.length) return;
    let ac = 0;
    let lo = Infinity;
    let hi = 0;
    const coords = mine.map((q) => {
      ac += (q.c1 - q.c0) * (q.r1 - q.r0) * cellAc;
      const xa = g.x0 + q.c0 * g.dx;
      const xb = g.x0 + q.c1 * g.dx;
      const ya = g.y0 - q.r0 * g.dy;
      const yb = g.y0 - q.r1 * g.dy;
      for (const [x, y] of [[xa, ya], [xb, yb]]) {
        const a = Math.max(0, along(x, y));
        lo = Math.min(lo, a);
        hi = Math.max(hi, a);
      }
      const ring = [inv(xa, yb), inv(xb, yb), inv(xb, ya), inv(xa, ya)].map(([lon, lat]) => [r7(lon), r7(lat)]);
      return [[...ring, ring[0]]];
    });
    total += ac;
    features.push({
      type: "Feature",
      properties: {
        ...common,
        band: bd.id,
        band_label: bd.label,
        depth_max_ft: Number.isFinite(bd.maxFt) ? bd.maxFt : r3(maxD),
        depth_min_ft: bd.minFt,
        from_km: r3(lo / 1000),
        to_km: r3(hi / 1000),
        segments: mine.length,
        area_ac: r3(ac),
        color: bd.color,
      },
      geometry: { type: "MultiPolygon", coordinates: coords },
    });
  });
  return {
    type: "FeatureCollection",
    properties: {
      ...common,
      max_depth_ft: r3(maxD),
      reach_km: r3(farM / 1000),
      wet_reach_km: r3(farM / 1000),
      direction: meta.direction,
      clipped_to_aoi: false,
      strip_area_ac: r3(total),
      engine_wet_area_ac: r3(total),
      breach_lon: r7(meta.breach.lon),
      breach_lat: r7(meta.breach.lat),
    },
    features,
  };
}
