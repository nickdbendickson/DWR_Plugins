import { WET_FT } from "./acer.ts";
import { downstreamDirection, readAoi, type Part, type XY } from "./aoi.ts";
import { damLocation, ENGINE_VERSION } from "./engine.ts";
import { bandFor, DEPTH_BANDS, floodStrip, type FloodStrip, type StripFeature } from "./floodstrip.ts";
import type { Bbox } from "./plss.ts";
import type { Analysis, DamRecord } from "./types.ts";

/**
 * Terrain outline: a general inundation outline from USGS 3DEP terrain and the desk depths (Nick, 2026-10-30).
 *
 * The desk engine has a straight schematic valley: stations downstream of the breach, each with a half-width and a
 * depth. This lays that depth on real ground:
 *
 *  1. 3DEP DEM (about 10 m, metres NAVD88) over a box around the strip centreline, each side
 *     min(max(3 × halfM, 300 m), 2000 m) wide, clipped to the drawn area when one is applied.
 *  2. Drainage on that DEM: a priority-flood (with a tiny epsilon) from the domain's edges fills pits and ponds to
 *     their spill level and gives every cell a downhill receiver. The channel is the drainage path followed from
 *     the breach (60 m below it on the centreline, clear of the dam) to the last wet station's distance, so it
 *     follows the real creek's bends instead of the engine's straight line.
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
const M_PER_DEG_LON_EQ = 111320;

export const corridorHalfM = (halfM: number) => Math.min(Math.max(3 * halfM, 300), 2000);
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
  /** Channel bed (ft NAVD88, pits filled) at the top and the bottom of the traced channel. */
  bedTopFt: number;
  bedEndFt: number;
  /** Length of the traced channel (km), along its bends. */
  channelKm: number;
  /** Distance downstream (km) of the farthest wet cell. */
  reachKm: number;
  /** The water reached the corridor limit somewhere (it would spread further). */
  hitsCorridor: boolean;
  rings: number;
  droppedPatches: number;
  filledHoles: number;
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
  };
};

export type OutlineCache = {
  fingerprint: string;
  builtAt: string;
  source: string;
  attribution: string;
  stats: OutlineStats;
  outline: TerrainOutline;
};

export type OutlineState =
  | { state: "none"; message: string }
  | { state: "ready"; outline: TerrainOutline; stats: OutlineStats; builtAt: string; message: null }
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
      o: [r(loc.lon * 1e4), r(loc.lat * 1e4)],
      u: u.map(r),
      st: st.map((s) => [r(s.x), r(s.h), r(s.d)]),
      a: parts,
      f: r(fromM),
    }),
  );
  return { ok: true, inputs: { loc, u, st, parts, fromM, lastX: st[st.length - 1].x, fingerprint } };
}

const kxOf = (lat: number) => M_PER_DEG_LON_EQ * Math.cos((lat * Math.PI) / 180);

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

/** exportImage URL: raw little-endian float32 (bsq), WGS 84 in and out, bilinear resampling, metres NAVD88. */
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

export async function fetchDem(req: DemRequest, fetchFn: Fetch = fetch as unknown as Fetch): Promise<DemGrid> {
  const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
  const timer = ctl ? setTimeout(() => ctl.abort(), 90_000) : null;
  try {
    const res = await fetchFn(demUrl(req), ctl ? { signal: ctl.signal } : undefined);
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

/**
 * Build the outline on a DEM. Returns FloodStrip-shaped band features (so the PLSS and 911 lists, the map legend and
 * the exports read it like the strip) and the numbers behind it.
 */
export async function buildOutline(
  dam: DamRecord,
  result: Analysis,
  g: DemGrid,
  opts: BuildOptions = {},
): Promise<{ ok: true; cache: OutlineCache } | { ok: false; reason: string }> {
  const io = outlineInputs(dam, result);
  if (!io.ok) return io;
  const inp = io.inputs;
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

  // Channel: follow the drainage from the breach (on the centreline, SKIP_M below it) until past the last wet station.
  let k0 = cellAt(u[0] * Math.max(SKIP_M, a0 + cellM), u[1] * Math.max(SKIP_M, a0 + cellM));
  if (k0 < 0 || !dom[k0]) {
    // Nearest domain cell on the cross-line there.
    let best = Infinity;
    const n: XY = [-u[1], u[0]];
    const ax = Math.max(SKIP_M, a0 + cellM);
    for (let t = -searchHalfM(st[0].h); t <= searchHalfM(st[0].h); t += cellM / 2) {
      const k = cellAt(u[0] * ax + n[0] * t, u[1] * ax + n[1] * t);
      if (k >= 0 && dom[k] && Math.abs(t) < best) {
        best = Math.abs(t);
        k0 = k;
      }
    }
  }
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

  // Flood (height above the nearest channel cell): each cell takes the water surface of its nearest channel cell
  // (filled bed + the desk depth at that cell's distance downstream) and is wet when its ground is under that surface
  // by 0.1 ft, within the corridor width of that channel cell, and connected (4-neighbour) to the channel.
  const wseC = new Float32Array(N);
  const roomC = new Float32Array(N);
  const src = new Int32Array(N).fill(-1);
  const q = new Int32Array(N);
  let tail = 0;
  for (let p = 0; p < path.length; p++) {
    const k = path[p];
    if (src[k] >= 0) continue;
    wseC[k] = fill[k] + interp(st, pathA[p], "d") * FT;
    roomC[k] = corridorHalfM(interp(st, pathA[p], "h"));
    src[k] = k;
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
  const depth = new Float32Array(N).fill(-1);
  const under = (m: number) => {
    const s0 = src[m];
    return s0 >= 0 && along[m] <= inp.lastX && wseC[s0] - g.z[m] >= WET_M - 1e-9;
  };
  const inRoom = (m: number) => dist2(m, src[m]) * cellM2 <= roomC[src[m]] * roomC[src[m]];
  tail = 0;
  for (const k of path)
    if (depth[k] < 0 && under(k)) {
      depth[k] = wseC[k] - g.z[k];
      q[tail++] = k;
    }
  let hitsCorridor = false;
  for (let head = 0; head < tail; head++) {
    const k = q[head];
    nbrs(k, nb);
    for (let t = 0; t < 4; t++) {
      const m = nb[t];
      if (m < 0 || depth[m] >= 0 || !dom[m] || !under(m)) continue;
      if (!inRoom(m)) {
        hitsCorridor = true;
        continue;
      }
      depth[m] = wseC[src[m]] - g.z[m];
      q[tail++] = m;
    }
    if ((head & 0x3ffff) === 0) await breathe(0x40000);
  }
  if (tail === 0) return { ok: false, reason: "No terrain outline: no ground connected to the channel lies under the desk water surface." };

  // Specks and pinholes.
  const wetC = components(W, H, (k) => depth[k] >= 0);
  let droppedPatches = 0;
  for (let s = 1; s < wetC.sizes.length; s++) if (wetC.sizes[s] < MIN_CELLS) droppedPatches++;
  for (let k = 0; k < N; k++) if (depth[k] >= 0 && wetC.sizes[wetC.lab[k]] < MIN_CELLS) depth[k] = -1;
  await breathe(N);
  const dryC = components(W, H, (k) => depth[k] < 0);
  let filledHoles = 0;
  for (let s = 1; s < dryC.sizes.length; s++) if (dryC.sizes[s] < MIN_CELLS && !dryC.border[s]) filledHoles++;
  for (let k = 0; k < N; k++)
    if (depth[k] < 0 && dryC.sizes[dryC.lab[k]] < MIN_CELLS && !dryC.border[dryC.lab[k]]) depth[k] = WET_M;
  await breathe(N);

  // Bands, one 3 × 3 mode pass.
  const band0 = new Int8Array(N).fill(-1);
  const idx = new Map(DEPTH_BANDS.map((b, i) => [b.id, i]));
  for (let k = 0; k < N; k++) if (depth[k] >= 0) band0[k] = idx.get(bandFor(Math.max(WET_FT, depth[k] / FT)).id)!;
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
    let dMax = -Infinity;
    let dMin = Infinity;
    let lo = Infinity;
    let hi = -Infinity;
    for (let k = 0; k < N; k++)
      if (band[k] === b) {
        mask[k] = 1;
        cells++;
        const dft = Math.max(WET_FT, depth[k] / FT);
        dMax = Math.max(dMax, dft);
        dMin = Math.min(dMin, dft);
        lo = Math.min(lo, along[k]);
        hi = Math.max(hi, along[k]);
      }
    if (cells === 0) continue;
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
    channelKm: r3(channelM / 1000),
    reachKm: r3(reach / 1000),
    hitsCorridor,
    rings: features.reduce((s, f) => s + (f.geometry.type === "Polygon" ? f.geometry.coordinates.length : f.geometry.coordinates.reduce((t, p) => t + p.length, 0)), 0),
    droppedPatches,
    filledHoles,
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
      dem_cell_m: r3(cellM),
      dem_width: W,
      dem_height: H,
      corridor: "up to min(max(3 × half-width, 300 m), 2000 m) from the traced channel",
    },
    features,
  };
  return {
    ok: true,
    cache: {
      fingerprint: inp.fingerprint,
      builtAt: (opts.builtAt ?? new Date()).toISOString(),
      source: DEM_SERVICE,
      attribution: DEM_ATTRIBUTION,
      stats,
      outline,
    },
  };
}

/** What the desk can show for the outline now: the saved one (if built from the same inputs), or what to do. */
export function outlineState(dam: DamRecord, result: Analysis, online: boolean): OutlineState {
  const io = outlineInputs(dam, result);
  if (!io.ok) return { state: "none", message: io.reason };
  const c = dam.outline;
  if (c && c.fingerprint === io.inputs.fingerprint)
    return { state: "ready", outline: c.outline, stats: c.stats, builtAt: c.builtAt, message: null };
  if (c) {
    const msg = "The desk inputs changed since the terrain outline was built.";
    return online ? { state: "build", message: `${msg} Build it again.` } : { state: "offline", message: `${msg} ${OUTLINE_OFFLINE}` };
  }
  return online
    ? { state: "build", message: "The terrain outline is not built yet (one 3DEP terrain request)." }
    : { state: "offline", message: OUTLINE_OFFLINE };
}

/** Plain words for the outline acres against the engine's wet acres. */
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
