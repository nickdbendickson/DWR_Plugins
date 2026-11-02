/**
 * Validation harness: how well a fast Crest flood layer matches a model run (Nick, 2026-11-01).
 *
 * The reference is an imported model run (the ND Dam Breach Runner's LISFLOOD-FP rasters, or DSS-WISE), read the
 * way the Results step reads it (raster.ts: a retained depth / velocity / arrival grid in the run's CRS). The
 * candidate is whatever Crest produced: the flood strip or terrain-outline polygons (lon/lat GeoJSON), a lon/lat
 * depth grid (the terrain method's working grid), or another retained grid. The candidate is rasterized onto the
 * reference grid, and the two are scored cell by cell:
 *
 *  - extent: acres wet in both, only in the reference, only in the candidate; recall (the share of the model's
 *    flood the candidate covers: the "5–41 % overlap" number), precision, CSI, and the area ratio (candidate /
 *    reference: the "2–9 ×" number);
 *  - depth: mean |candidate − reference| where both are wet;
 *  - receptors: depth, velocity and arrival at each point from both layers, wet/dry agreement, the TM 11 zone from
 *    each when both carry velocity, and summary errors (bias, MAE, RMSE).
 *
 * Only cells downstream of the breach count when a downstream direction is given, as renderCompare does.
 * Pure module (no DOM); it runs in node for the four-dam scoring (scripts/validate-crest.mjs) and in tests.
 */
import { type ChartKind, WET_FT, zone, type Zone } from "./acer.ts";
import type { Bbox } from "./plss.ts";
import { cellAt, frame, type GridGeo, type Projector, type Retained, samplePoint } from "./raster.ts";

const SQM_PER_AC = 4046.8564224;

export type RefLayer = { grid: Retained; proj: Projector };

/** A GeoJSON polygon feature in lon/lat with a depth (ft) for its cells; strip and outline features carry depth_max_ft. */
export type DepthFeature = {
  properties?: Record<string, unknown> | null;
  geometry: { type: "Polygon"; coordinates: number[][][] } | { type: "MultiPolygon"; coordinates: number[][][][] } | null;
};

export type Candidate =
  | { kind: "polygons"; features: DepthFeature[] }
  | {
      kind: "lonlat";
      bbox: Bbox;
      width: number;
      height: number;
      /** Row-major from the north-west corner, ft; <WET_FT, NaN or nodata = dry. */
      depth: Float32Array;
      velocity?: Float32Array | null;
      arrival?: Float32Array | null;
    }
  | { kind: "layer"; grid: Retained; proj: Projector };

/** The candidate on the reference grid (same width × height). Dry cells hold 0 depth; outside the candidate = NaN. */
export type CandRaster = { depth: Float32Array; velocity: Float32Array | null; arrival: Float32Array | null };

export type Downstream = { lon: number; lat: number; u: [number, number] };

/** Depth (ft) a polygon feature stands for: depth_max_ft, else the middle of its depth band, else just wet. */
export function featureDepth(f: DepthFeature): number {
  const p = f.properties ?? {};
  const hi = Number(p.depth_max_ft);
  const lo = Number(p.depth_min_ft);
  if (Number.isFinite(hi) && hi > 0) return Number.isFinite(lo) && lo > 0 && lo <= hi ? (lo + hi) / 2 : hi;
  return WET_FT;
}

/**
 * Scanline even-odd fill of one polygon (outer ring and holes together) onto a grid, in projected coordinates.
 * `set(i)` is called once per inside cell (cell centres). Rings are closed or not; degenerate rings are skipped.
 */
export function fillPolygon(g: GridGeo, proj: Projector, rings: number[][][], set: (i: number) => void) {
  const edges: { x0: number; y0: number; x1: number; y1: number }[] = [];
  let rmin = Infinity;
  let rmax = -Infinity;
  for (const ring of rings) {
    const pts: [number, number][] = [];
    for (const c of ring) {
      const [x, y] = proj.fwd(c[0], c[1]);
      const col = (x - g.x0) / g.dx;
      const row = (g.y0 - y) / g.dy;
      if (!Number.isFinite(col) || !Number.isFinite(row)) continue;
      pts.push([col, row]);
    }
    if (pts.length < 3) continue;
    for (let i = 0; i < pts.length; i++) {
      const a = pts[i];
      const b = pts[(i + 1) % pts.length];
      if (a[1] === b[1]) continue;
      edges.push({ x0: a[0], y0: a[1], x1: b[0], y1: b[1] });
      rmin = Math.min(rmin, a[1], b[1]);
      rmax = Math.max(rmax, a[1], b[1]);
    }
  }
  if (!edges.length) return;
  const r0 = Math.max(0, Math.floor(rmin));
  const r1 = Math.min(g.height - 1, Math.ceil(rmax));
  const xs: number[] = [];
  for (let r = r0; r <= r1; r++) {
    const y = r + 0.5;
    xs.length = 0;
    for (const e of edges) {
      const ylo = Math.min(e.y0, e.y1);
      const yhi = Math.max(e.y0, e.y1);
      // Half-open on the top end so a vertex on the scanline is counted once.
      if (y < ylo || y >= yhi) continue;
      xs.push(e.x0 + ((y - e.y0) * (e.x1 - e.x0)) / (e.y1 - e.y0));
    }
    if (xs.length < 2) continue;
    xs.sort((a, b) => a - b);
    for (let k = 0; k + 1 < xs.length; k += 2) {
      const c0 = Math.max(0, Math.ceil(xs[k] - 0.5));
      const c1 = Math.min(g.width - 1, Math.floor(xs[k + 1] - 0.5));
      for (let c = c0; c <= c1; c++) set(r * g.width + c);
    }
  }
}

/** Put the candidate on the reference grid. */
export function rasterizeCandidate(cand: Candidate, ref: RefLayer): CandRaster {
  const g = ref.grid;
  const n = g.width * g.height;
  const depth = new Float32Array(n);
  let velocity: Float32Array | null = null;
  let arrival: Float32Array | null = null;
  if (cand.kind === "polygons") {
    for (const f of cand.features) {
      if (!f.geometry) continue;
      const d = featureDepth(f);
      const polys = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates;
      for (const rings of polys)
        fillPolygon(g, ref.proj, rings, (i) => {
          if (d > depth[i]) depth[i] = d;
        });
    }
    return { depth, velocity, arrival };
  }
  // Grids: nearest candidate cell under each reference cell centre; NaN where the candidate has no cell.
  depth.fill(NaN);
  if (cand.kind === "lonlat" ? cand.velocity : cand.grid.velocity) velocity = new Float32Array(n).fill(NaN);
  if (cand.kind === "lonlat" ? cand.arrival : cand.grid.arrival) arrival = new Float32Array(n).fill(NaN);
  for (let r = 0; r < g.height; r++) {
    const y = g.y0 - (r + 0.5) * g.dy;
    for (let c = 0; c < g.width; c++) {
      const x = g.x0 + (c + 0.5) * g.dx;
      const i = r * g.width + c;
      const [lon, lat] = ref.proj.inv(x, y);
      let j: number | null = null;
      let src: { depth: Float32Array; velocity?: Float32Array | null; arrival?: Float32Array | null };
      if (cand.kind === "lonlat") {
        const cc = Math.floor(((lon - cand.bbox[0]) / (cand.bbox[2] - cand.bbox[0])) * cand.width);
        const rr = Math.floor(((cand.bbox[3] - lat) / (cand.bbox[3] - cand.bbox[1])) * cand.height);
        j = cc < 0 || rr < 0 || cc >= cand.width || rr >= cand.height ? null : rr * cand.width + cc;
        src = cand;
      } else {
        const [cx, cy] = cand.proj.fwd(lon, lat);
        j = cellAt(cand.grid, cx, cy);
        src = cand.grid;
      }
      if (j == null) continue;
      const d = src.depth[j];
      depth[i] = Number.isFinite(d) && d > -9000 ? Math.max(0, d) : 0;
      if (velocity && src.velocity) velocity[i] = src.velocity[j];
      if (arrival && src.arrival) arrival[i] = src.arrival[j];
    }
  }
  return { depth, velocity, arrival };
}

export type ExtentScore = {
  /** Acres wet (≥ 0.1 ft) in the reference, in the candidate, and the overlap split. */
  refAc: number;
  candAc: number;
  bothAc: number;
  refOnlyAc: number;
  candOnlyAc: number;
  /** bothAc / refAc: the share of the model's flood the candidate covers. */
  recall: number;
  /** bothAc / candAc. */
  precision: number;
  /** bothAc / (bothAc + refOnlyAc + candOnlyAc). */
  csi: number;
  /** candAc / refAc. */
  areaRatio: number;
  /** Mean and 90th-percentile |candidate − reference| depth (ft) over cells wet in both. */
  meanAbsDepthFt: number;
  p90AbsDepthFt: number;
  /** Mean candidate − reference depth (ft) over cells wet in both (+ = candidate deeper). */
  depthBiasFt: number;
  /** Reference cells outside the candidate grid (grid candidates only): the candidate's domain was too small. */
  refOutsideCandAc: number;
  cellM: number;
  /** Cells compared (downstream of the breach when a direction is given). */
  cells: number;
};

/** Which reference cells count: all, or only those downstream of the breach along u. */
function downstreamMask(g: GridGeo, proj: Projector, downstream: Downstream | null | undefined): Uint8Array | null {
  if (!downstream) return null;
  const fr = frame({ breach: downstream, u: downstream.u, reachKm: 0 }, proj);
  if (!fr.known) return null;
  const m = new Uint8Array(g.width * g.height);
  for (let r = 0; r < g.height; r++) {
    const y = g.y0 - (r + 0.5) * g.dy;
    for (let c = 0; c < g.width; c++) {
      const x = g.x0 + (c + 0.5) * g.dx;
      if ((x - fr.bx) * fr.ux + (y - fr.by) * fr.uy >= 0) m[r * g.width + c] = 1;
    }
  }
  return m;
}

export function scoreExtent(ref: RefLayer, cand: CandRaster, downstream?: Downstream | null): ExtentScore {
  const g = ref.grid;
  const mask = downstreamMask(g, ref.proj, downstream);
  const cellAc = (g.dx * g.dy) / SQM_PER_AC;
  let both = 0;
  let refOnly = 0;
  let candOnly = 0;
  let outside = 0;
  let cells = 0;
  const diffs: number[] = [];
  let bias = 0;
  for (let i = 0; i < g.width * g.height; i++) {
    if (mask && !mask[i]) continue;
    cells++;
    const dr = g.depth[i];
    const dc = cand.depth[i];
    const R = dr >= WET_FT;
    const C = dc >= WET_FT;
    if (R && Number.isNaN(dc)) outside++;
    if (R && C) {
      both++;
      diffs.push(Math.abs(dc - dr));
      bias += dc - dr;
    } else if (R) refOnly++;
    else if (C) candOnly++;
  }
  diffs.sort((a, b) => a - b);
  const refAc = (both + refOnly) * cellAc;
  const candAc = (both + candOnly) * cellAc;
  const bothAc = both * cellAc;
  const union = both + refOnly + candOnly;
  return {
    refAc,
    candAc,
    bothAc,
    refOnlyAc: refOnly * cellAc,
    candOnlyAc: candOnly * cellAc,
    recall: refAc > 0 ? bothAc / refAc : 0,
    precision: candAc > 0 ? bothAc / candAc : 0,
    csi: union > 0 ? both / union : 0,
    areaRatio: refAc > 0 ? candAc / refAc : candAc > 0 ? Infinity : 1,
    meanAbsDepthFt: diffs.length ? diffs.reduce((s, d) => s + d, 0) / diffs.length : 0,
    p90AbsDepthFt: diffs.length ? diffs[Math.min(diffs.length - 1, Math.floor(0.9 * diffs.length))] : 0,
    depthBiasFt: diffs.length ? bias / diffs.length : 0,
    refOutsideCandAc: outside * cellAc,
    cellM: g.dx,
    cells,
  };
}

export type ReceptorPoint = { id: string; lon: number; lat: number; kind?: ChartKind };

export type ReceptorScore = {
  id: string;
  lon: number;
  lat: number;
  /** Inside the reference grid. */
  covered: boolean;
  ref: { depthFt: number; velocityFps: number | null; arrivalHr: number | null; wet: boolean; zone: Zone | null };
  cand: { depthFt: number; velocityFps: number | null; arrivalHr: number | null; wet: boolean; zone: Zone | null };
  /** "both wet", "both dry", "missed" (model wet, candidate dry) or "extra" (candidate wet, model dry). */
  verdict: "both wet" | "both dry" | "missed" | "extra" | "not covered";
  depthDiffFt: number | null;
  velocityDiffFps: number | null;
  arrivalDiffHr: number | null;
};

export type ReceptorSummary = {
  n: number;
  covered: number;
  bothWet: number;
  bothDry: number;
  missed: number;
  extra: number;
  /** (bothWet + bothDry) / covered. */
  wetAgreement: number;
  /** Over points wet in both: candidate − reference depth (ft). */
  depthBiasFt: number | null;
  depthMaeFt: number | null;
  depthRmseFt: number | null;
  velocityMaeFps: number | null;
  arrivalMaeHr: number | null;
  /** Points with a TM 11 zone from both layers, and how many agree. */
  zoned: number;
  zoneAgree: number;
};

export function scoreReceptors(points: ReceptorPoint[], ref: RefLayer, cand: CandRaster, defaultKind: ChartKind = "house"): ReceptorScore[] {
  const g = ref.grid;
  return points.map((p) => {
    const s = samplePoint(g, ref.proj, p.lon, p.lat);
    const [x, y] = ref.proj.fwd(p.lon, p.lat);
    const i = cellAt(g, x, y);
    const kind = p.kind ?? defaultKind;
    const zoneOf = (d: number, v: number | null, wet: boolean) => (v == null ? null : zone(kind, wet ? d : 0, wet ? v : 0));
    if (!s.covered || i == null)
      return {
        id: p.id,
        lon: p.lon,
        lat: p.lat,
        covered: false,
        ref: { depthFt: 0, velocityFps: null, arrivalHr: null, wet: false, zone: null },
        cand: { depthFt: 0, velocityFps: null, arrivalHr: null, wet: false, zone: null },
        verdict: "not covered",
        depthDiffFt: null,
        velocityDiffFps: null,
        arrivalDiffHr: null,
      };
    const cd = cand.depth[i];
    const cWet = cd >= WET_FT;
    const cv = cand.velocity ? cand.velocity[i] : NaN;
    const ca = cand.arrival ? cand.arrival[i] : NaN;
    const candS = {
      depthFt: Number.isFinite(cd) ? cd : 0,
      velocityFps: cand.velocity ? (cWet && Number.isFinite(cv) ? cv : 0) : null,
      arrivalHr: cWet && Number.isFinite(ca) ? ca : null,
      wet: cWet,
      zone: null as Zone | null,
    };
    const rWet = s.depthFt >= WET_FT;
    const refS = { depthFt: s.depthFt, velocityFps: s.velocityFps, arrivalHr: s.arrivalHr, wet: rWet, zone: zoneOf(s.depthFt, s.velocityFps, rWet) };
    candS.zone = zoneOf(candS.depthFt, candS.velocityFps, cWet);
    const verdict = rWet && cWet ? "both wet" : !rWet && !cWet ? "both dry" : rWet ? "missed" : "extra";
    const bothWet = rWet && cWet;
    return {
      id: p.id,
      lon: p.lon,
      lat: p.lat,
      covered: true,
      ref: refS,
      cand: candS,
      verdict,
      depthDiffFt: bothWet ? candS.depthFt - refS.depthFt : null,
      velocityDiffFps: bothWet && candS.velocityFps != null && refS.velocityFps != null ? candS.velocityFps - refS.velocityFps : null,
      arrivalDiffHr: bothWet && candS.arrivalHr != null && refS.arrivalHr != null ? candS.arrivalHr - refS.arrivalHr : null,
    };
  });
}

export function summarizeReceptors(rows: ReceptorScore[]): ReceptorSummary {
  const covered = rows.filter((r) => r.covered);
  const count = (v: ReceptorScore["verdict"]) => covered.filter((r) => r.verdict === v).length;
  const dd = covered.map((r) => r.depthDiffFt).filter((d): d is number => d != null);
  const vd = covered.map((r) => r.velocityDiffFps).filter((d): d is number => d != null);
  const ad = covered.map((r) => r.arrivalDiffHr).filter((d): d is number => d != null);
  const mean = (a: number[]) => (a.length ? a.reduce((s, x) => s + x, 0) / a.length : null);
  const mae = (a: number[]) => mean(a.map(Math.abs));
  const zoned = covered.filter((r) => r.ref.zone && r.cand.zone);
  const bothWet = count("both wet");
  const bothDry = count("both dry");
  return {
    n: rows.length,
    covered: covered.length,
    bothWet,
    bothDry,
    missed: count("missed"),
    extra: count("extra"),
    wetAgreement: covered.length ? (bothWet + bothDry) / covered.length : 0,
    depthBiasFt: mean(dd),
    depthMaeFt: mae(dd),
    depthRmseFt: dd.length ? Math.sqrt(dd.reduce((s, x) => s + x * x, 0) / dd.length) : null,
    velocityMaeFps: mae(vd),
    arrivalMaeHr: mae(ad),
    zoned: zoned.length,
    zoneAgree: zoned.filter((r) => r.ref.zone === r.cand.zone).length,
  };
}

export type ValidationReport = {
  label: string;
  reference: string;
  extent: ExtentScore;
  receptors: { rows: ReceptorScore[]; summary: ReceptorSummary };
  downstreamOnly: boolean;
};

export function validate(
  cand: Candidate,
  ref: RefLayer,
  opts: { label: string; reference: string; points?: ReceptorPoint[]; downstream?: Downstream | null; kind?: ChartKind },
): ValidationReport {
  const raster = rasterizeCandidate(cand, ref);
  const extent = scoreExtent(ref, raster, opts.downstream);
  const rows = scoreReceptors(opts.points ?? [], ref, raster, opts.kind);
  return { label: opts.label, reference: opts.reference, extent, receptors: { rows, summary: summarizeReceptors(rows) }, downstreamOnly: !!opts.downstream };
}

const pct = (x: number) => `${(100 * x).toFixed(0)} %`;
const ac = (x: number) => `${x.toFixed(x < 10 ? 1 : 0)} ac`;
const ft = (x: number | null) => (x == null ? "—" : `${x >= 0 ? "+" : ""}${x.toFixed(2)} ft`);

/** A plain-text report for the terminal or a notes file. */
export function formatReport(r: ValidationReport): string {
  const e = r.extent;
  const s = r.receptors.summary;
  const lines = [
    `${r.label} vs ${r.reference}${r.downstreamOnly ? " (downstream of the breach only)" : ""}`,
    `  extent    model ${ac(e.refAc)}, candidate ${ac(e.candAc)} (${e.areaRatio.toFixed(2)} × the model), both ${ac(e.bothAc)}`,
    `            recall ${pct(e.recall)}  precision ${pct(e.precision)}  CSI ${e.csi.toFixed(2)}  cell ${e.cellM.toFixed(0)} m`,
    `  depth     where both wet: bias ${ft(e.depthBiasFt)}, mean |diff| ${e.meanAbsDepthFt.toFixed(2)} ft, p90 ${e.p90AbsDepthFt.toFixed(2)} ft`,
  ];
  if (e.refOutsideCandAc > 0) lines.push(`  domain    ${ac(e.refOutsideCandAc)} of the model's flood lies outside the candidate grid`);
  if (s.n) {
    lines.push(
      `  receptors ${s.covered}/${s.n} inside the model grid: both wet ${s.bothWet}, both dry ${s.bothDry}, missed ${s.missed}, extra ${s.extra} (agreement ${pct(s.wetAgreement)})`,
    );
    lines.push(
      `            depth bias ${ft(s.depthBiasFt)}, MAE ${s.depthMaeFt == null ? "—" : s.depthMaeFt.toFixed(2) + " ft"}, RMSE ${s.depthRmseFt == null ? "—" : s.depthRmseFt.toFixed(2) + " ft"}` +
        `; velocity MAE ${s.velocityMaeFps == null ? "—" : s.velocityMaeFps.toFixed(2) + " ft/s"}; arrival MAE ${s.arrivalMaeHr == null ? "—" : s.arrivalMaeHr.toFixed(2) + " h"}`,
    );
    if (s.zoned) lines.push(`            TM 11 zone agrees at ${s.zoneAgree}/${s.zoned} points with a zone from both`);
  }
  return lines.join("\n");
}
