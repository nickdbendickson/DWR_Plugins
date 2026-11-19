/**
 * Display simplification for the map (MapDraw.tsx, DashboardMap.tsx). A 10 m routed layer is six MultiPolygons of
 * ~80,000 vertices in 641 rings; Leaflet's SVG renderer redrew every one on each pan and zoom. The drawn copy is
 * simplified to what a pixel can show at the current zoom (Douglas-Peucker, tolerance ~0.5 px); exports, areas, the
 * 911 and PLSS lookups keep the exact rings. Pure; no Leaflet here.
 */
import type { FloodStrip } from "./floodstrip.ts";

export type LonLat = [number, number];

/** Metres per degree of longitude at a latitude, and of latitude (the equirectangular frame the engine uses). */
const M_PER_DEG_LAT = 110574;
const M_PER_DEG_LON_EQ = 111320;

/**
 * Metres on the ground that one screen pixel covers at a Web Mercator zoom and latitude (256 px tiles, as Leaflet).
 * Zoom 15 at 47° N: about 3.3 m.
 */
export function metresPerPixel(zoom: number, lat: number): number {
  return (156543.03392 * Math.cos((lat * Math.PI) / 180)) / 2 ** zoom;
}

/**
 * Douglas-Peucker on a closed ring (first point = last point, as GeoJSON has it), in lon/lat with the tolerance in
 * metres. Keeps the first point and the farthest point from it as the split, so a ring never collapses to a line; a
 * ring that would be left with fewer than 4 distinct points comes back as the original. Iterative (no recursion depth
 * on a 10,000-point ring).
 */
export function simplifyRing(ring: readonly LonLat[], tolM: number): LonLat[] {
  const n = ring.length;
  if (n < 6 || !(tolM > 0)) return ring.slice();
  const closed = ring[0][0] === ring[n - 1][0] && ring[0][1] === ring[n - 1][1];
  const pts = closed ? ring.slice(0, n - 1) : ring.slice();
  const m = pts.length;
  if (m < 5) return ring.slice();
  const lat0 = pts[0][1];
  const kx = M_PER_DEG_LON_EQ * Math.cos((lat0 * Math.PI) / 180);
  const x = new Float64Array(m);
  const y = new Float64Array(m);
  for (let i = 0; i < m; i++) {
    x[i] = pts[i][0] * kx;
    y[i] = pts[i][1] * M_PER_DEG_LAT;
  }
  // The ring is split at the point farthest from the start, then each half is simplified as an open line.
  let far = 1;
  let fd = -1;
  for (let i = 1; i < m; i++) {
    const d = (x[i] - x[0]) ** 2 + (y[i] - y[0]) ** 2;
    if (d > fd) {
      fd = d;
      far = i;
    }
  }
  const keep = new Uint8Array(m);
  keep[0] = 1;
  keep[far] = 1;
  const tol2 = tolM * tolM;
  const stack: number[] = [0, far, far, m]; // pairs (a, b): the open run a..b (b may be m meaning "back to 0")
  while (stack.length) {
    const b = stack.pop()!;
    const a = stack.pop()!;
    const bi = b === m ? 0 : b;
    const ax = x[a];
    const ay = y[a];
    const dx = x[bi] - ax;
    const dy = y[bi] - ay;
    const L2 = dx * dx + dy * dy;
    let best = -1;
    let bd = tol2;
    for (let i = a + 1; i < b; i++) {
      let d2: number;
      if (L2 > 0) {
        const cross = (x[i] - ax) * dy - (y[i] - ay) * dx;
        d2 = (cross * cross) / L2;
      } else d2 = (x[i] - ax) ** 2 + (y[i] - ay) ** 2;
      if (d2 > bd) {
        bd = d2;
        best = i;
      }
    }
    if (best >= 0) {
      keep[best] = 1;
      stack.push(a, best, best, b);
    }
  }
  const out: LonLat[] = [];
  for (let i = 0; i < m; i++) if (keep[i]) out.push(pts[i]);
  if (out.length < 3) return ring.slice();
  out.push(out[0]);
  return out;
}

type Geometry = FloodStrip["features"][number]["geometry"];

/** Simplify every ring of a Polygon / MultiPolygon geometry (a new geometry; the input is untouched). */
export function simplifyGeometry(geom: Geometry, tolM: number): Geometry {
  if (geom.type === "Polygon") return { type: "Polygon", coordinates: geom.coordinates.map((r) => simplifyRing(r as LonLat[], tolM)) };
  return { type: "MultiPolygon", coordinates: geom.coordinates.map((poly) => poly.map((r) => simplifyRing(r as LonLat[], tolM))) };
}

/** Number of vertices in a Polygon / MultiPolygon geometry (for tests and the status line). */
export function vertexCount(geom: Geometry): number {
  if (geom.type === "Polygon") return geom.coordinates.reduce((s, r) => s + r.length, 0);
  return geom.coordinates.reduce((s, p) => s + p.reduce((t, r) => t + r.length, 0), 0);
}

/**
 * The drawn copy of a FloodStrip-shaped collection (the strip, the terrain outline, a run's strip): every band's rings
 * simplified to `pixels` screen pixels at `zoom`, the properties shared (not copied), the foreign `extent` member (the
 * generalized outline line, outline.ts) left as it is: it is already generalized and is one line. Below SIMPLIFY_FROM_M
 * nothing is simplified (the tolerance is smaller than the cell edges the rings follow).
 */
export function simplifyForDisplay<T extends FloodStrip>(fc: T, zoom: number, pixels = 0.5): T {
  const tolM = metresPerPixel(zoom, firstLat(fc) ?? 47) * pixels;
  if (tolM < SIMPLIFY_FROM_M) return fc;
  return { ...fc, features: fc.features.map((f) => ({ ...f, geometry: simplifyGeometry(f.geometry, tolM) })) };
}

/** Latitude of the first vertex of the first band, for the pixel scale; undefined on an empty collection. */
function firstLat(fc: FloodStrip): number | undefined {
  const g = fc.features[0]?.geometry;
  if (!g) return undefined;
  const ring: number[][] | undefined = g.type === "Polygon" ? g.coordinates[0] : g.coordinates[0]?.[0];
  return ring?.[0]?.[1];
}

/** Tolerances under this (m) are not worth a pass: the rings follow 10 m cell edges smoothed by half a cell. */
export const SIMPLIFY_FROM_M = 0.75;

/**
 * Zoom levels are quantised for the cache key so a pan (same zoom) and the fractional zooms of a pinch reuse one
 * simplified copy: whole zoom levels, so each level has its own copy and a half-level pinch takes the nearer one.
 */
export function displayZoomKey(zoom: number): number {
  return Math.round(zoom);
}
