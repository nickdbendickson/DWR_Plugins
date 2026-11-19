/**
 * Model result rasters: LISFLOOD-FP ESRI ASCII grids and GeoTIFFs (the ND Dam Breach Runner's lisflood_*.tif, or
 * DSS-WISE Lite's Hmax/Vmax/Arrival_Time rasters). Pure module (no DOM): parsing, file roles, units, CRS, a
 * full-resolution scan for the numbers, a downsampled retained grid for the map and the receptors, and a Web
 * Mercator warp for display. Runs in the results worker (raster.worker.ts) or, as a fallback, on the main thread.
 *
 * Units: everything leaves this module in ft, ft/s and hours. LISFLOOD-FP writes metres and m/s (.max, .mxe,
 * .maxVc/.Vmax) and hours (.maxtm, .inittm); the Runner's and DSS-WISE's rasters carry the unit in the file name.
 */
import proj4 from "proj4";
import { WET_FT } from "./acer.ts";
import { DEPTH_BANDS } from "./floodstrip.ts";
import { CRS_DEFS, DIFF_BINS, type VarId } from "./raster-meta.ts";

export { classifyFile, CRS_DEFS, DEFAULT_ASCII_CRS, DIFF_BINS, type FileRole, M_TO_FT, VAR_LABEL, type VarId, wantFile } from "./raster-meta.ts";

export { WET_FT };
const SQM_PER_AC = 4046.8564224;
export type Projector = { fwd: (lon: number, lat: number) => [number, number]; inv: (x: number, y: number) => [number, number] };

export function projector(crs: string): Projector {
  const d = CRS_DEFS[crs];
  if (!d) throw new Error(`${crs} is not supported. Use one of ${Object.keys(CRS_DEFS).join(", ")}.`);
  const p = proj4("+proj=longlat +datum=WGS84 +no_defs", d.def);
  return {
    fwd: (lon, lat) => p.forward([lon, lat]) as [number, number],
    inv: (x, y) => p.inverse([x, y]) as [number, number],
  };
}

// ---------------------------------------------------------------------------------------------------------------
// Grids

/** A north-up grid. x0/y0 are the outer upper-left corner; rows run south. */
export type GridGeo = { width: number; height: number; x0: number; y0: number; dx: number; dy: number };

export type AsciiGrid = GridGeo & { data: Float32Array; nodata: number | null; georeferenced: boolean };

/** ESRI ASCII grid (LISFLOOD-FP). Header keys are case-insensitive; xllcenter/yllcenter are shifted to corners. */
export function parseAsciiGrid(text: string): AsciiGrid {
  const head: Record<string, number> = {};
  let pos = 0;
  for (let i = 0; i < 8; i++) {
    const end = text.indexOf("\n", pos);
    const line = text.slice(pos, end < 0 ? text.length : end).trim();
    const m = /^([a-zA-Z_]+)\s+(\S+)$/.exec(line);
    if (!m) break;
    head[m[1].toLowerCase()] = Number(m[2]);
    pos = end < 0 ? text.length : end + 1;
  }
  const width = head.ncols;
  const height = head.nrows;
  const cs = head.cellsize;
  if (!(width > 0 && height > 0 && cs > 0)) throw new Error("not an ESRI ASCII grid (ncols, nrows, cellsize missing)");
  let xll = head.xllcorner;
  let yll = head.yllcorner;
  if (xll == null && head.xllcenter != null) xll = head.xllcenter - cs / 2;
  if (yll == null && head.yllcenter != null) yll = head.yllcenter - cs / 2;
  if (xll == null || yll == null || !Number.isFinite(xll) || !Number.isFinite(yll))
    throw new Error("ASCII grid header has no xllcorner/yllcorner");
  const nodata = head.nodata_value ?? null;
  const n = width * height;
  const data = new Float32Array(n);
  let k = 0;
  const len = text.length;
  let i = pos;
  while (i < len && k < n) {
    let c = text.charCodeAt(i);
    while (c === 32 || c === 10 || c === 13 || c === 9) c = text.charCodeAt(++i);
    if (i >= len) break;
    const s = i;
    while (i < len && (c = text.charCodeAt(i)) !== 32 && c !== 10 && c !== 13 && c !== 9) i++;
    data[k++] = Number(text.slice(s, i));
  }
  if (k < n) throw new Error(`ASCII grid is short: ${k} of ${n} values`);
  return { width, height, x0: xll, y0: yll + height * cs, dx: cs, dy: cs, data, nodata, georeferenced: !(xll === 0 && yll === 0) };
}

/** Reader over one variable: full-resolution rows on demand, already in ft, ft/s or h; NaN for nodata. */
export type RowSource = GridGeo & {
  name: string;
  variable: VarId;
  /** Rows [r0, r1) into out (length (r1-r0)*width). */
  rows: (r0: number, r1: number) => Promise<Float32Array>;
  nodata: number | null;
  rawMax?: number;
};

export type GeoTiffInfo = GridGeo & { epsg: number | null; nodata: number | null; citation: string | null };

type TiffImage = {
  getWidth(): number;
  getHeight(): number;
  getOrigin(): number[];
  getResolution(): number[];
  getGDALNoData(): number | null;
  getGeoKeys(): Record<string, unknown> | null;
  readRasters(o: { window: number[]; interleave?: boolean; samples?: number[] }): Promise<unknown>;
};

export async function openGeoTiff(buf: ArrayBuffer): Promise<{ info: GeoTiffInfo; image: TiffImage }> {
  const { fromArrayBuffer } = await import("geotiff");
  const tiff = await fromArrayBuffer(buf);
  const image = (await tiff.getImage()) as unknown as TiffImage;
  const [ox, oy] = image.getOrigin();
  const [rx, ry] = image.getResolution();
  const keys = image.getGeoKeys() ?? {};
  const epsg = Number(keys.ProjectedCSTypeGeoKey ?? keys.GeographicTypeGeoKey ?? NaN);
  return {
    image,
    info: {
      width: image.getWidth(),
      height: image.getHeight(),
      x0: ox,
      y0: oy,
      dx: Math.abs(rx),
      dy: Math.abs(ry),
      epsg: Number.isFinite(epsg) && epsg > 0 && epsg !== 32767 ? epsg : null,
      nodata: image.getGDALNoData(),
      citation: typeof keys.GTCitationGeoKey === "string" ? keys.GTCitationGeoKey : typeof keys.PCSCitationGeoKey === "string" ? keys.PCSCitationGeoKey : null,
    },
  };
}

function clean(v: number, nodata: number | null, factor: number) {
  if (!Number.isFinite(v) || (nodata != null && v === nodata) || v <= -9998) return NaN;
  return v * factor;
}

export function tiffRows(name: string, variable: VarId, factor: number, info: GeoTiffInfo, image: TiffImage): RowSource {
  return {
    ...info,
    name,
    variable,
    rows: async (r0, r1) => {
      const raw = (await image.readRasters({ window: [0, r0, info.width, r1], samples: [0], interleave: true })) as ArrayLike<number>;
      const out = new Float32Array(raw.length);
      for (let i = 0; i < raw.length; i++) out[i] = clean(raw[i], info.nodata, factor);
      return out;
    },
  };
}

export function asciiRows(name: string, variable: VarId, factor: number, g: AsciiGrid, geo: GridGeo = g): RowSource {
  return {
    ...geo,
    name,
    variable,
    nodata: g.nodata,
    rows: async (r0, r1) => {
      const out = new Float32Array((r1 - r0) * g.width);
      for (let i = 0, j = r0 * g.width; i < out.length; i++, j++) out[i] = clean(g.data[j], g.nodata, factor);
      return out;
    },
  };
}

export function sameGrid(a: GridGeo, b: GridGeo, tol = 1e-6) {
  return (
    a.width === b.width &&
    a.height === b.height &&
    Math.abs(a.dx - b.dx) <= tol * Math.max(1, a.dx) &&
    Math.abs(a.dy - b.dy) <= tol * Math.max(1, a.dy) &&
    Math.abs(a.x0 - b.x0) <= a.dx * 0.01 &&
    Math.abs(a.y0 - b.y0) <= a.dy * 0.01
  );
}
export function sameShape(a: GridGeo, b: GridGeo) {
  return a.width === b.width && a.height === b.height && Math.abs(a.dx - b.dx) < 1e-6 * a.dx;
}

// ---------------------------------------------------------------------------------------------------------------
// Scan: full-resolution numbers, plus a downsampled retained grid

export type ScanContext = {
  /** Breach location (record). */
  breach: { lon: number; lat: number };
  /** Local east/north unit vector downstream (aoi.ts downstreamDirection), or null when unknown. */
  u: [number, number] | null;
  /** Desk reach end (km downstream), for the coverage check. */
  reachKm: number;
};

/** Cells kept for display and sampling (max-aggregated depth/velocity, min arrival). */
export const RETAIN_CELLS = 3_000_000;

export type Retained = GridGeo & {
  factor: number;
  depth: Float32Array;
  velocity: Float32Array | null;
  arrival: Float32Array | null;
};

export type ScanStats = {
  peakDepthFt: number;
  peakDepthAllFt: number;
  wetAc: number;
  wetAcAll: number;
  farthestKm: number;
  maxVelocityFps: number | null;
  arrivalMaxHr: number | null;
  nearestWetM: number;
  edgeWetCells: number;
  coverKm: number | null;
  cellM: number;
  cells: number;
  breachLon: number;
  breachLat: number;
};

const ROWS_PER_BLOCK = 256;

/** Breach and downstream direction in the raster's CRS. */
export function frame(ctx: ScanContext, proj: Projector) {
  const [bx, by] = proj.fwd(ctx.breach.lon, ctx.breach.lat);
  let ux = 0;
  let uy = 0;
  if (ctx.u) {
    const kx = 111_319.49 * Math.cos((ctx.breach.lat * Math.PI) / 180);
    const [x1, y1] = proj.fwd(ctx.breach.lon + (ctx.u[0] * 1000) / kx, ctx.breach.lat + (ctx.u[1] * 1000) / 110_946.26);
    const l = Math.hypot(x1 - bx, y1 - by) || 1;
    ux = (x1 - bx) / l;
    uy = (y1 - by) / l;
  }
  return { bx, by, ux, uy, known: !!ctx.u };
}

/** Distance (m) along (ux,uy) from (bx,by) to where the ray leaves the grid; null when the start is outside. */
export function rayExit(g: GridGeo, bx: number, by: number, ux: number, uy: number): number | null {
  const xmin = g.x0;
  const xmax = g.x0 + g.width * g.dx;
  const ymax = g.y0;
  const ymin = g.y0 - g.height * g.dy;
  if (bx < xmin || bx > xmax || by < ymin || by > ymax) return null;
  let t = Infinity;
  if (ux > 1e-12) t = Math.min(t, (xmax - bx) / ux);
  if (ux < -1e-12) t = Math.min(t, (xmin - bx) / ux);
  if (uy > 1e-12) t = Math.min(t, (ymax - by) / uy);
  if (uy < -1e-12) t = Math.min(t, (ymin - by) / uy);
  return Number.isFinite(t) ? t : null;
}

export async function scanRun(
  vars: { depth: RowSource; velocity?: RowSource; arrival?: RowSource },
  ctx: ScanContext,
  proj: Projector,
  onProgress?: (frac: number) => void,
): Promise<{ stats: ScanStats; retained: Retained }> {
  const g = vars.depth;
  const { width: W, height: H } = g;
  const f = Math.max(1, Math.ceil(Math.sqrt((W * H) / RETAIN_CELLS)));
  const rw = Math.ceil(W / f);
  const rh = Math.ceil(H / f);
  const rDepth = new Float32Array(rw * rh);
  const rVel = vars.velocity ? new Float32Array(rw * rh) : null;
  const rArr = vars.arrival ? new Float32Array(rw * rh).fill(NaN) : null;
  const fr = frame(ctx, proj);
  const cellAc = (g.dx * g.dy) / SQM_PER_AC;
  let peak = 0;
  let peakAll = 0;
  let wet = 0;
  let wetAll = 0;
  let far = 0;
  let vmax: number | null = vars.velocity ? 0 : null;
  let amax: number | null = vars.arrival ? 0 : null;
  let near = Infinity;
  let edge = 0;
  for (let r0 = 0; r0 < H; r0 += ROWS_PER_BLOCK) {
    const r1 = Math.min(H, r0 + ROWS_PER_BLOCK);
    const d = await g.rows(r0, r1);
    const v = vars.velocity ? await vars.velocity.rows(r0, r1) : null;
    const a = vars.arrival ? await vars.arrival.rows(r0, r1) : null;
    for (let r = r0; r < r1; r++) {
      const y = g.y0 - (r + 0.5) * g.dy;
      const rr = Math.floor(r / f) * rw;
      const off = (r - r0) * W;
      for (let c = 0; c < W; c++) {
        const i = off + c;
        let dep = d[i];
        if (!(dep > 0)) dep = 0;
        const ri = rr + Math.floor(c / f);
        if (dep > rDepth[ri]) rDepth[ri] = dep;
        if (dep < WET_FT) continue;
        const x = g.x0 + (c + 0.5) * g.dx;
        const ex = x - fr.bx;
        const ey = y - fr.by;
        const along = fr.known ? ex * fr.ux + ey * fr.uy : Math.hypot(ex, ey);
        wetAll++;
        if (dep > peakAll) peakAll = dep;
        const dist = Math.hypot(ex, ey);
        if (dist < near) near = dist;
        if (r === 0 || c === 0 || r === H - 1 || c === W - 1) edge++;
        const vel = v ? v[i] : NaN;
        if (rVel && vel > rVel[ri]) rVel[ri] = vel;
        const arr = a ? a[i] : NaN;
        if (rArr && arr >= 0 && !(arr >= rArr[ri])) rArr[ri] = arr;
        if (along < 0) continue;
        wet++;
        if (dep > peak) peak = dep;
        if (along > far) far = along;
        if (vmax != null && vel > vmax) vmax = vel;
        if (amax != null && arr > amax) amax = arr;
      }
    }
    onProgress?.(r1 / H);
  }
  const cover = fr.known ? rayExit(g, fr.bx, fr.by, fr.ux, fr.uy) : null;
  return {
    stats: {
      peakDepthFt: peak,
      peakDepthAllFt: peakAll,
      wetAc: wet * cellAc,
      wetAcAll: wetAll * cellAc,
      farthestKm: far / 1000,
      maxVelocityFps: vmax,
      arrivalMaxHr: amax,
      nearestWetM: Number.isFinite(near) ? near : NaN,
      edgeWetCells: edge,
      coverKm: cover == null ? null : cover / 1000,
      cellM: g.dx,
      cells: W * H,
      breachLon: ctx.breach.lon,
      breachLat: ctx.breach.lat,
    },
    retained: { width: rw, height: rh, x0: g.x0, y0: g.y0, dx: g.dx * f, dy: g.dy * f, factor: f, depth: rDepth, velocity: rVel, arrival: rArr },
  };
}

/** Retained-grid value at a point in the raster CRS, or null outside the grid. */
export function cellAt(g: GridGeo, x: number, y: number): number | null {
  const c = Math.floor((x - g.x0) / g.dx);
  const r = Math.floor((g.y0 - y) / g.dy);
  if (c < 0 || r < 0 || c >= g.width || r >= g.height) return null;
  return r * g.width + c;
}

export type PointSample = { covered: boolean; depthFt: number; velocityFps: number | null; arrivalHr: number | null };

/**
 * Values at a point: the max over the retained cell (one retained cell is f×f source cells, so a structure takes
 * the deepest of the cells around it, at most f×cell size away).
 */
export function samplePoint(g: Retained, proj: Projector, lon: number, lat: number): PointSample {
  const [x, y] = proj.fwd(lon, lat);
  const i = cellAt(g, x, y);
  if (i == null) return { covered: false, depthFt: 0, velocityFps: null, arrivalHr: null };
  const d = g.depth[i];
  const wet = d >= WET_FT;
  const v = g.velocity ? g.velocity[i] : NaN;
  const a = g.arrival ? g.arrival[i] : NaN;
  return {
    covered: true,
    depthFt: d,
    velocityFps: g.velocity ? (wet && Number.isFinite(v) ? v : 0) : null,
    arrivalHr: wet && Number.isFinite(a) ? a : null,
  };
}

// ---------------------------------------------------------------------------------------------------------------
// Display: warp retained grids to a Web Mercator image

const R_MERC = 6378137;
export const mercX = (lon: number) => (R_MERC * lon * Math.PI) / 180;
export const mercY = (lat: number) => R_MERC * Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
export const mercLon = (x: number) => (x / R_MERC) * (180 / Math.PI);
export const mercLat = (y: number) => (2 * Math.atan(Math.exp(y / R_MERC)) - Math.PI / 2) * (180 / Math.PI);

export type View = { west: number; south: number; east: number; north: number; width: number; height: number };

/** Grid bounds in lon/lat (corners and edge midpoints projected back). */
export function lonLatBounds(g: GridGeo, proj: Projector) {
  const xs = [g.x0, g.x0 + (g.width * g.dx) / 2, g.x0 + g.width * g.dx];
  const ys = [g.y0, g.y0 - (g.height * g.dy) / 2, g.y0 - g.height * g.dy];
  let w = Infinity;
  let s = Infinity;
  let e = -Infinity;
  let n = -Infinity;
  for (const x of xs)
    for (const y of ys) {
      const [lo, la] = proj.inv(x, y);
      w = Math.min(w, lo);
      e = Math.max(e, lo);
      s = Math.min(s, la);
      n = Math.max(n, la);
    }
  return { west: w, south: s, east: e, north: n };
}

/** A view (output raster) over lon/lat bounds, at most maxPx on its long side, in Web Mercator pixels. */
export function viewFor(b: { west: number; south: number; east: number; north: number }, maxPx = 2048): View {
  const wm = mercX(b.east) - mercX(b.west);
  const hm = mercY(b.north) - mercY(b.south);
  const k = maxPx / Math.max(wm, hm);
  return { ...b, width: Math.max(1, Math.round(wm * k)), height: Math.max(1, Math.round(hm * k)) };
}

const LATTICE = 8;

/** For each output pixel, the retained-grid index under it (-1 outside). Projected on an 8 px lattice, bilinear between. */
export function warpIndex(g: GridGeo, proj: Projector, v: View): Int32Array {
  const out = new Int32Array(v.width * v.height).fill(-1);
  const mx0 = mercX(v.west);
  const my0 = mercY(v.north);
  const sx = (mercX(v.east) - mx0) / v.width;
  const sy = (my0 - mercY(v.south)) / v.height;
  const lw = Math.ceil(v.width / LATTICE) + 1;
  const lh = Math.ceil(v.height / LATTICE) + 1;
  const LX = new Float64Array(lw * lh);
  const LY = new Float64Array(lw * lh);
  for (let j = 0; j < lh; j++)
    for (let i = 0; i < lw; i++) {
      const [x, y] = proj.fwd(mercLon(mx0 + i * LATTICE * sx), mercLat(my0 - j * LATTICE * sy));
      LX[j * lw + i] = x;
      LY[j * lw + i] = y;
    }
  for (let py = 0; py < v.height; py++) {
    const fj = (py + 0.5) / LATTICE;
    const j = Math.min(lh - 2, Math.floor(fj));
    const ty = fj - j;
    for (let px = 0; px < v.width; px++) {
      const fi = (px + 0.5) / LATTICE;
      const i = Math.min(lw - 2, Math.floor(fi));
      const tx = fi - i;
      const a = j * lw + i;
      const x = (LX[a] * (1 - tx) + LX[a + 1] * tx) * (1 - ty) + (LX[a + lw] * (1 - tx) + LX[a + lw + 1] * tx) * ty;
      const y = (LY[a] * (1 - tx) + LY[a + 1] * tx) * (1 - ty) + (LY[a + lw] * (1 - tx) + LY[a + lw + 1] * tx) * ty;
      const k = cellAt(g, x, y);
      if (k != null) out[py * v.width + px] = k;
    }
  }
  return out;
}

function hex(c: string): [number, number, number] {
  const m = /^#?([0-9a-f]{6})$/i.exec(c);
  const n = m ? parseInt(m[1], 16) : 0x3b6ea5;
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}
const BAND_RGB = DEPTH_BANDS.map((b) => hex(b.color));
function bandIndex(d: number) {
  for (let i = DEPTH_BANDS.length - 1; i >= 0; i--) if (d >= DEPTH_BANDS[i].minFt) return i;
  return -1;
}

const DIFF_RGB = DIFF_BINS.map((b) => hex(b.color));

export type Layer = { grid: Retained; proj: Projector };

/** Depth image (RGBA) for one run. */
export function renderDepth(l: Layer, v: View, alpha = 200): Uint8ClampedArray {
  const idx = warpIndex(l.grid, l.proj, v);
  const px = new Uint8ClampedArray(v.width * v.height * 4);
  for (let p = 0; p < idx.length; p++) {
    const k = idx[p];
    if (k < 0) continue;
    const b = bandIndex(l.grid.depth[k]);
    if (b < 0) continue;
    const c = BAND_RGB[b];
    px[p * 4] = c[0];
    px[p * 4 + 1] = c[1];
    px[p * 4 + 2] = c[2];
    px[p * 4 + 3] = alpha;
  }
  return px;
}

export type CompareStats = {
  bothAc: number;
  aOnlyAc: number;
  bOnlyAc: number;
  /** Mean |A−B| over pixels wet in either (ft) and the extremes. */
  meanAbsFt: number;
  maxAOverFt: number;
  maxBOverFt: number;
  /** Pixel size of the common grid (m, at the view's centre). */
  pixelM: number;
};

/**
 * Compare two runs on a common Web Mercator grid: A−B depth image (dry = 0 ft inside a run's extent; outside
 * either extent = not compared), outline image (edge pixels of each wet area), and overlap acres. Only pixels
 * downstream of the dam line count when `downstream` is given.
 */
export function renderCompare(
  a: Layer,
  b: Layer,
  v: View,
  colors: [string, string],
  downstream?: { lon: number; lat: number; u: [number, number] } | null,
): { diff: Uint8ClampedArray; outline: Uint8ClampedArray; stats: CompareStats } {
  const ia = warpIndex(a.grid, a.proj, v);
  const ib = warpIndex(b.grid, b.proj, v);
  const n = v.width * v.height;
  const wa = new Uint8Array(n);
  const wb = new Uint8Array(n);
  const diff = new Uint8ClampedArray(n * 4);
  const mx0 = mercX(v.west);
  const my0 = mercY(v.north);
  const sx = (mercX(v.east) - mx0) / v.width;
  const sy = (my0 - mercY(v.south)) / v.height;
  const latC = (v.north + v.south) / 2;
  const cosC = Math.cos((latC * Math.PI) / 180);
  let both = 0;
  let aOnly = 0;
  let bOnly = 0;
  let absSum = 0;
  let absN = 0;
  let maxA = 0;
  let maxB = 0;
  const kx = downstream ? 111_319.49 * Math.cos((downstream.lat * Math.PI) / 180) : 0;
  for (let py = 0; py < v.height; py++) {
    const lat = mercLat(my0 - (py + 0.5) * sy);
    const cos = Math.cos((lat * Math.PI) / 180);
    const pixAc = (sx * cos * (sy * cos)) / SQM_PER_AC;
    for (let px = 0; px < v.width; px++) {
      const p = py * v.width + px;
      const ka = ia[p];
      const kb = ib[p];
      const da = ka >= 0 ? a.grid.depth[ka] : NaN;
      const db = kb >= 0 ? b.grid.depth[kb] : NaN;
      const A = da >= WET_FT;
      const B = db >= WET_FT;
      if (A) wa[p] = 1;
      if (B) wb[p] = 1;
      if (!(A || B) || ka < 0 || kb < 0) continue;
      if (downstream) {
        const lon = mercLon(mx0 + (px + 0.5) * sx);
        const e = (lon - downstream.lon) * kx;
        const nn = (lat - downstream.lat) * 110_946.26;
        if (e * downstream.u[0] + nn * downstream.u[1] < 0) continue;
      }
      if (A && B) both += pixAc;
      else if (A) aOnly += pixAc;
      else bOnly += pixAc;
      const dd = (A ? da : 0) - (B ? db : 0);
      absSum += Math.abs(dd);
      absN++;
      if (dd > maxA) maxA = dd;
      if (-dd > maxB) maxB = -dd;
      let bi = 0;
      while (bi < DIFF_BINS.length - 1 && dd >= DIFF_BINS[bi].max) bi++;
      const c = DIFF_RGB[bi];
      diff[p * 4] = c[0];
      diff[p * 4 + 1] = c[1];
      diff[p * 4 + 2] = c[2];
      diff[p * 4 + 3] = 210;
    }
  }
  const outline = new Uint8ClampedArray(n * 4);
  const ca = hex(colors[0]);
  const cb = hex(colors[1]);
  const edgeOf = (w: Uint8Array, p: number, x: number, y: number) =>
    w[p] === 1 && (x === 0 || y === 0 || x === v.width - 1 || y === v.height - 1 || !w[p - 1] || !w[p + 1] || !w[p - v.width] || !w[p + v.width]);
  for (let y = 0; y < v.height; y++)
    for (let x = 0; x < v.width; x++) {
      const p = y * v.width + x;
      const c = edgeOf(wa, p, x, y) ? ca : edgeOf(wb, p, x, y) ? cb : null;
      if (!c) continue;
      outline[p * 4] = c[0];
      outline[p * 4 + 1] = c[1];
      outline[p * 4 + 2] = c[2];
      outline[p * 4 + 3] = 255;
    }
  return {
    diff,
    outline,
    stats: {
      bothAc: both,
      aOnlyAc: aOnly,
      bOnlyAc: bOnly,
      meanAbsFt: absN ? absSum / absN : 0,
      maxAOverFt: maxA,
      maxBOverFt: maxB,
      pixelM: sx * cosC,
    },
  };
}

/** Outline image (edge pixels of the wet area) for one run. */
export function renderOutline(l: Layer, v: View, color: string): Uint8ClampedArray {
  const idx = warpIndex(l.grid, l.proj, v);
  const w = new Uint8Array(idx.length);
  for (let p = 0; p < idx.length; p++) if (idx[p] >= 0 && l.grid.depth[idx[p]] >= WET_FT) w[p] = 1;
  const c = hex(color);
  const out = new Uint8ClampedArray(idx.length * 4);
  for (let y = 0; y < v.height; y++)
    for (let x = 0; x < v.width; x++) {
      const p = y * v.width + x;
      if (!w[p]) continue;
      if (!(x === 0 || y === 0 || x === v.width - 1 || y === v.height - 1 || !w[p - 1] || !w[p + 1] || !w[p - v.width] || !w[p + v.width])) continue;
      out[p * 4] = c[0];
      out[p * 4 + 1] = c[1];
      out[p * 4 + 2] = c[2];
      out[p * 4 + 3] = 255;
    }
  return out;
}
