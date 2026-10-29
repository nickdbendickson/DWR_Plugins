import type { GeoJSONFeature } from "./types.ts";

/**
 * Drawn area of interest (AOI) as seen from the breach.
 *
 * The desk valley is schematic: distance downstream along an unnamed valley, with no drawn
 * centreline or direction. So the AOI cannot be intersected with a centreline. It is reduced to
 * the band of downstream distances it covers, measured straight-line from the breach location:
 * `nearM` (0 when the breach is inside the AOI, else the distance to the nearest AOI edge) to
 * `farM` (the AOI's farthest vertex). Straight-line distance is never more than the distance
 * along a real valley, so this cuts a meandering valley short (more Review, less area), never long.
 */
export type AoiExtent = {
  nearM: number;
  farM: number;
  areaAc: number;
  breachInside: boolean;
};

const ACRE_M2 = 4046.8564224;
const M_PER_DEG_LAT = 110574;
const M_PER_DEG_LON_EQ = 111320;

type XY = [number, number];

/** Outer ring of a drawn Polygon, as [lon, lat] pairs, or null if it is not a usable polygon. */
export function aoiRing(f: GeoJSONFeature | undefined): number[][] | null {
  if (!f || f.geometry?.type !== "Polygon") return null;
  const rings = f.geometry.coordinates as number[][][];
  const ring = Array.isArray(rings) ? rings[0] : null;
  if (!Array.isArray(ring)) return null;
  const pts = ring.filter((p) => Array.isArray(p) && Number.isFinite(p[0]) && Number.isFinite(p[1]));
  const open = pts.length > 1 && pts[0][0] === pts[pts.length - 1][0] && pts[0][1] === pts[pts.length - 1][1] ? pts.slice(0, -1) : pts;
  return open.length >= 3 ? open : null;
}

function project(ring: number[][], lon0: number, lat0: number): XY[] {
  const kx = M_PER_DEG_LON_EQ * Math.cos((lat0 * Math.PI) / 180);
  return ring.map(([lon, lat]) => [(lon - lon0) * kx, (lat - lat0) * M_PER_DEG_LAT]);
}

function inside(pts: XY[], x: number, y: number) {
  let c = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const [xi, yi] = pts[i];
    const [xj, yj] = pts[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) c = !c;
  }
  return c;
}

function segDist(px: number, py: number, [ax, ay]: XY, [bx, by]: XY) {
  const dx = bx - ax;
  const dy = by - ay;
  const l2 = dx * dx + dy * dy;
  const t = l2 > 0 ? Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / l2)) : 0;
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

/** AOI distance band from the breach (local equirectangular metres; fine over the desk's ≤100 km). */
export function aoiExtent(f: GeoJSONFeature | undefined, breach: { lon: number; lat: number }): AoiExtent | null {
  const ring = aoiRing(f);
  if (!ring) return null;
  const pts = project(ring, breach.lon, breach.lat);
  let farM = 0;
  for (const [x, y] of pts) farM = Math.max(farM, Math.hypot(x, y));
  const breachInside = inside(pts, 0, 0);
  let nearM = 0;
  if (!breachInside) {
    nearM = Infinity;
    for (let i = 0; i < pts.length; i++) nearM = Math.min(nearM, segDist(0, 0, pts[i], pts[(i + 1) % pts.length]));
  }
  let twice = 0;
  for (let i = 0; i < pts.length; i++) {
    const [x1, y1] = pts[i];
    const [x2, y2] = pts[(i + 1) % pts.length];
    twice += x1 * y2 - x2 * y1;
  }
  return { nearM, farM, areaAc: Math.abs(twice) / 2 / ACRE_M2, breachInside };
}

/** A square AOI polygon `halfKm` either side of a point (tests and samples). */
export function squareAoi(lon: number, lat: number, halfKm: number): GeoJSONFeature {
  const dLat = (halfKm * 1000) / M_PER_DEG_LAT;
  const dLon = (halfKm * 1000) / (M_PER_DEG_LON_EQ * Math.cos((lat * Math.PI) / 180));
  return {
    type: "Feature",
    properties: {},
    geometry: {
      type: "Polygon",
      coordinates: [
        [
          [lon - dLon, lat - dLat],
          [lon + dLon, lat - dLat],
          [lon + dLon, lat + dLat],
          [lon - dLon, lat + dLat],
          [lon - dLon, lat - dLat],
        ],
      ],
    },
  };
}
