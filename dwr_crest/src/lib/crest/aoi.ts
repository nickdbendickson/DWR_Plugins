import type { DamGeometry, GeoJSONFeature } from "./types.ts";

/**
 * Drawn area of interest (AOI) as seen from the breach (Nick 2026-10-29, round 3).
 *
 * The desk valley is schematic: a straight 1-D valley with distance downstream and no drawn
 * centreline. Its direction is taken from the record's geometry (`downstreamDirection`): the dam
 * line's normal pointing away from the reservoir point, else the bearing from the reservoir point
 * to the breach location. The reach ends where a ray from the breach along that direction leaves the
 * AOI (its nearest downstream exit), the wet area is the schematic strip clipped to the AOI polygon,
 * and receptors past the exit are outside the drawn area.
 *
 * With no usable direction the fallback is conservative: from a breach inside the AOI the reach
 * ends at the nearest AOI boundary in any direction (the shortest possible exit); from a breach
 * outside it, the valley is assumed to run toward the AOI's nearest point. Both are warned.
 *
 * Polygon rules: Polygon holes are excluded; a MultiPolygon is the union of its parts (only the
 * largest part if parts overlap, with a warning); self-intersecting or degenerate rings are
 * rejected (a bad outer ring means the AOI is not applied; a bad hole or part is dropped); anything
 * else (Point, LineString) is not applied. All of these warn: nothing is ignored silently.
 */

const ACRE_M2 = 4046.8564224;
const M_PER_DEG_LAT = 110574;
const M_PER_DEG_LON_EQ = 111320;
const RAY_M = 1_000_000;
/** Smaller than this is reported as a tiny drawn area. */
export const TINY_AOI_AC = 1;
export const TINY_AOI_M = 100;

export type XY = [number, number];
type Part = { outer: XY[]; holes: XY[][] };
type LonLat = { lon: number; lat: number };

export type DirectionBasis = "dam line" | "reservoir to breach" | "none";

export type Downstream = {
  /** Unit vector (east, north) pointing downstream, or null when it cannot be determined. */
  u: XY | null;
  basis: DirectionBasis;
  /** Plain words for the Flood step and Report. */
  note: string;
};

export type AoiExtent = {
  /** Distance along the downstream ray where the AOI starts (0 when the breach is inside it). */
  nearM: number;
  /** Distance along the downstream ray to the nearest downstream exit (or the fallback distance). */
  farM: number;
  /** AOI area (acres): outer rings minus holes, summed over parts used. */
  areaAc: number;
  breachInside: boolean;
  /** False when the downstream ray never enters the AOI (it is upstream or off to the side). */
  coversDownstream: boolean;
  /** "downstream": ray along the known direction; "nearest-boundary" / "toward-aoi": fallbacks. */
  reachBasis: "downstream" | "nearest-boundary" | "toward-aoi";
  /** Share of the AOI area upstream of the breach (null when the direction is unknown). */
  upstreamFrac: number | null;
  /** Unit vector the strip is laid along, or null when the wet area can only be cut by distance. */
  axis: XY | null;
  parts: number;
  warnings: string[];
  /** Fraction of the schematic cross-section at xM (half-width halfM) inside the AOI, or null without an axis. */
  crossFrac: (xM: number, halfM: number) => number | null;
};

/** Result of reading the drawn feature: usable polygon parts, or why it cannot be used. */
export type AoiShape =
  { ok: true; parts: Part[]; warnings: string[] } | { ok: false; reason: string };

function project(pts: number[][], o: LonLat): XY[] {
  const kx = M_PER_DEG_LON_EQ * Math.cos((o.lat * Math.PI) / 180);
  return pts.map(([lon, lat]) => [(lon - o.lon) * kx, (lat - o.lat) * M_PER_DEG_LAT]);
}

function cleanRing(ring: unknown): number[][] {
  if (!Array.isArray(ring)) return [];
  const pts = ring.filter(
    (p): p is number[] => Array.isArray(p) && Number.isFinite(p[0]) && Number.isFinite(p[1]),
  );
  const out: number[][] = [];
  for (const p of pts) {
    const q = out[out.length - 1];
    if (!q || q[0] !== p[0] || q[1] !== p[1]) out.push(p);
  }
  if (
    out.length > 1 &&
    out[0][0] === out[out.length - 1][0] &&
    out[0][1] === out[out.length - 1][1]
  )
    out.pop();
  return out;
}

function signedArea(r: XY[]) {
  let s = 0;
  for (let i = 0; i < r.length; i++) {
    const [x1, y1] = r[i];
    const [x2, y2] = r[(i + 1) % r.length];
    s += x1 * y2 - x2 * y1;
  }
  return s / 2;
}

function orient(a: XY, b: XY, c: XY) {
  return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
}

function onSeg(a: XY, b: XY, p: XY) {
  return (
    Math.min(a[0], b[0]) - 1e-9 <= p[0] &&
    p[0] <= Math.max(a[0], b[0]) + 1e-9 &&
    Math.min(a[1], b[1]) - 1e-9 <= p[1] &&
    p[1] <= Math.max(a[1], b[1]) + 1e-9
  );
}

function segsIntersect(a: XY, b: XY, c: XY, d: XY) {
  const eps = 1e-6;
  const d1 = orient(c, d, a);
  const d2 = orient(c, d, b);
  const d3 = orient(a, b, c);
  const d4 = orient(a, b, d);
  if (
    ((d1 > eps && d2 < -eps) || (d1 < -eps && d2 > eps)) &&
    ((d3 > eps && d4 < -eps) || (d3 < -eps && d4 > eps))
  )
    return true;
  if (Math.abs(d1) <= eps && onSeg(c, d, a)) return true;
  if (Math.abs(d2) <= eps && onSeg(c, d, b)) return true;
  if (Math.abs(d3) <= eps && onSeg(a, b, c)) return true;
  if (Math.abs(d4) <= eps && onSeg(a, b, d)) return true;
  return false;
}

/** True when two non-adjacent edges of the ring touch or cross (bow-tie, figure-eight, spike). */
export function selfIntersects(r: XY[]) {
  const n = r.length;
  for (let i = 0; i < n; i++) {
    for (let j = i + 1; j < n; j++) {
      if (j === i + 1 || (i === 0 && j === n - 1)) continue;
      if (segsIntersect(r[i], r[(i + 1) % n], r[j], r[(j + 1) % n])) return true;
    }
  }
  return false;
}

function ringProblem(r: XY[]): string | null {
  if (r.length < 3) return "has fewer than 3 distinct corners";
  const flat = Math.abs(signedArea(r)) < 1;
  if (!flat && selfIntersects(r)) return "crosses itself";
  if (flat)
    return selfIntersects(r) && !collinear(r)
      ? "crosses itself"
      : "has no area (its corners are in a line)";
  return null;
}

function collinear(r: XY[]) {
  for (let i = 2; i < r.length; i++) if (Math.abs(orient(r[0], r[1], r[i])) > 1) return false;
  return true;
}

function inRing(r: XY[], x: number, y: number) {
  let c = false;
  for (let i = 0, j = r.length - 1; i < r.length; j = i++) {
    const [xi, yi] = r[i];
    const [xj, yj] = r[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) c = !c;
  }
  return c;
}

function inPart(p: Part, x: number, y: number) {
  return inRing(p.outer, x, y) && !p.holes.some((h) => inRing(h, x, y));
}

function partArea(p: Part) {
  return Math.abs(signedArea(p.outer)) - p.holes.reduce((s, h) => s + Math.abs(signedArea(h)), 0);
}

function edges(parts: Part[]): [XY, XY][] {
  const out: [XY, XY][] = [];
  for (const p of parts) {
    for (const r of [p.outer, ...p.holes])
      for (let i = 0; i < r.length; i++) out.push([r[i], r[(i + 1) % r.length]]);
  }
  return out;
}

/** Inside intervals [t0, t1] (metres from A) of segment A→B in the union of parts. */
function insideIntervals(parts: Part[], es: [XY, XY][], a: XY, b: XY): [number, number][] {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const len = Math.hypot(dx, dy);
  if (len === 0) return [];
  const ts = [0, 1];
  for (const [c, d] of es) {
    const ex = d[0] - c[0];
    const ey = d[1] - c[1];
    const den = dx * ey - dy * ex;
    if (Math.abs(den) < 1e-12) continue;
    const t = ((c[0] - a[0]) * ey - (c[1] - a[1]) * ex) / den;
    const s = ((c[0] - a[0]) * dy - (c[1] - a[1]) * dx) / den;
    if (t > 0 && t < 1 && s >= 0 && s <= 1) ts.push(t);
  }
  ts.sort((p, q) => p - q);
  const out: [number, number][] = [];
  for (let i = 1; i < ts.length; i++) {
    if (ts[i] - ts[i - 1] < 1e-12) continue;
    const m = (ts[i] + ts[i - 1]) / 2;
    if (parts.some((p) => inPart(p, a[0] + m * dx, a[1] + m * dy))) {
      const last = out[out.length - 1];
      if (last && Math.abs(last[1] - ts[i - 1] * len) < 1e-6) last[1] = ts[i] * len;
      else out.push([ts[i - 1] * len, ts[i] * len]);
    }
  }
  return out;
}

function closestOnSeg(a: XY, b: XY): XY {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const l2 = dx * dx + dy * dy;
  const t = l2 > 0 ? Math.max(0, Math.min(1, -(a[0] * dx + a[1] * dy) / l2)) : 0;
  return [a[0] + t * dx, a[1] + t * dy];
}

/** Sutherland–Hodgman clip of a ring to the half-plane dot(p, u) < 0 (upstream side). */
function upstreamArea(r: XY[], u: XY) {
  const out: XY[] = [];
  const side = (p: XY) => p[0] * u[0] + p[1] * u[1];
  for (let i = 0; i < r.length; i++) {
    const p = r[i];
    const q = r[(i + 1) % r.length];
    const sp = side(p);
    const sq = side(q);
    if (sp < 0) out.push(p);
    if (sp < 0 !== sq < 0) {
      const t = sp / (sp - sq);
      out.push([p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])]);
    }
  }
  return out.length >= 3 ? Math.abs(signedArea(out)) : 0;
}

function partsOverlap(a: Part, b: Part) {
  const n = a.outer.length;
  const m = b.outer.length;
  for (let i = 0; i < n; i++)
    for (let j = 0; j < m; j++)
      if (segsIntersect(a.outer[i], a.outer[(i + 1) % n], b.outer[j], b.outer[(j + 1) % m]))
        return true;
  return inRing(b.outer, ...a.outer[0]) || inRing(a.outer, ...b.outer[0]);
}

/** Read and check the drawn AOI feature in local metres around `o`. */
export function readAoi(f: GeoJSONFeature | undefined, o: LonLat): AoiShape {
  if (!f || !f.geometry) return { ok: false, reason: "no drawn area" };
  const type = f.geometry.type as string;
  let polys: unknown[];
  if (type === "Polygon") polys = [f.geometry.coordinates];
  else if (type === "MultiPolygon" && Array.isArray(f.geometry.coordinates))
    polys = f.geometry.coordinates as unknown[];
  else
    return {
      ok: false,
      reason: `Drawn area not applied: it is a ${type || "shape"}, not a polygon. Draw the area as a polygon.`,
    };
  const warnings: string[] = [];
  const multi = type === "MultiPolygon";
  let parts: Part[] = [];
  polys.forEach((rings, k) => {
    const rs = Array.isArray(rings) ? rings : [];
    const outer = project(cleanRing(rs[0]), o);
    const what = multi ? `Part ${k + 1} of the drawn area` : "The drawn area";
    const bad = ringProblem(outer);
    if (bad) {
      warnings.push(`${what} ${bad}${multi ? "; that part is left out" : ""}.`);
      return;
    }
    const holes: XY[][] = [];
    rs.slice(1).forEach((h, j) => {
      const hole = project(cleanRing(h), o);
      const hb = ringProblem(hole);
      if (hb) warnings.push(`Hole ${j + 1} in ${what.toLowerCase()} ${hb}; it is not excluded.`);
      else if (!hole.every((p) => inRing(outer, p[0], p[1])))
        warnings.push(
          `Hole ${j + 1} in ${what.toLowerCase()} is not inside its outline; it is not excluded.`,
        );
      else holes.push(hole);
    });
    parts.push({ outer, holes });
  });
  if (parts.length === 0) {
    const reason =
      warnings.find((w) => w.startsWith("The drawn area")) ??
      "every part of the drawn area is unusable";
    return {
      ok: false,
      reason: `Drawn area not applied: ${reason.replace(/^The drawn area /, "it ").replace(/\.$/, "")}. Redraw it as a simple polygon.`,
    };
  }
  if (parts.length > 1) {
    let overlap = false;
    for (let i = 0; i < parts.length && !overlap; i++)
      for (let j = i + 1; j < parts.length && !overlap; j++)
        overlap = partsOverlap(parts[i], parts[j]);
    if (overlap) {
      const largest = parts.reduce((a, b) => (partArea(b) > partArea(a) ? b : a));
      warnings.push(
        `The drawn area has ${parts.length} overlapping parts; only the largest is used. Merge them into one polygon.`,
      );
      parts = [largest];
    }
  }
  return { ok: true, parts, warnings };
}

function unit(x: number, y: number): XY | null {
  const l = Math.hypot(x, y);
  return l > 0 ? [x / l, y / l] : null;
}

/**
 * Downstream direction from the record's geometry, in local metres around the breach location.
 * 1. Dam line: its normal (from the chord, first to last vertex), on the side away from the reservoir point.
 * 2. No dam line: the bearing from the reservoir point to the breach location.
 * Otherwise none (the caller falls back and warns).
 */
export function downstreamDirection(
  g: DamGeometry | undefined,
  breach: LonLat & { source?: string },
): Downstream {
  const res =
    g?.reservoirPoint?.geometry?.type === "Point"
      ? (g.reservoirPoint.geometry.coordinates as number[])
      : null;
  const R =
    res && Number.isFinite(res[0]) && Number.isFinite(res[1]) ? project([res], breach)[0] : null;
  const line =
    g?.damLine?.geometry?.type === "LineString" ? cleanRing(g.damLine.geometry.coordinates) : [];
  let why = "";
  if (line.length >= 2) {
    const pts = project([line[0], line[line.length - 1]], breach);
    const n = unit(-(pts[1][1] - pts[0][1]), pts[1][0] - pts[0][0]);
    if (!n || Math.hypot(pts[1][0] - pts[0][0], pts[1][1] - pts[0][1]) < 5)
      why = "the dam line is too short to give a direction";
    else if (!R)
      why = "the dam line has two sides and there is no reservoir point to say which is upstream";
    else {
      const mx = (pts[0][0] + pts[1][0]) / 2;
      const my = (pts[0][1] + pts[1][1]) / 2;
      const s = n[0] * (R[0] - mx) + n[1] * (R[1] - my);
      if (Math.abs(s) < 5) why = "the reservoir point sits on the dam line";
      else {
        const u: XY = s > 0 ? [-n[0], -n[1]] : n;
        return {
          u,
          basis: "dam line",
          note: `Downstream direction: the dam line's normal, on the side away from the reservoir point (bearing ${bearing(u)}°).`,
        };
      }
    }
  }
  if (line.length < 2) {
    if (R && breach.source !== "reservoir point" && Math.hypot(R[0], R[1]) >= 20) {
      const u = unit(-R[0], -R[1])!;
      return {
        u,
        basis: "reservoir to breach",
        note: `Downstream direction: from the reservoir point to the breach location (bearing ${bearing(u)}°); no dam line drawn.`,
      };
    }
    if (!why)
      why = R
        ? "the reservoir point is at the breach location"
        : "there is no dam line or reservoir point";
  }
  return {
    u: null,
    basis: "none",
    note: `Downstream direction unknown: ${why}. Draw the dam line and the reservoir point.`,
  };
}

function bearing(u: XY) {
  return Math.round(((Math.atan2(u[0], u[1]) * 180) / Math.PI + 360) % 360);
}

export function fmtKm(m: number) {
  return Math.round(m) < 1000 ? `${Math.round(m)} m` : `${(m / 1000).toFixed(1)} km`;
}

export function fmtAc(ac: number) {
  return ac >= 10
    ? Math.round(ac).toLocaleString("en-US")
    : ac >= 1
      ? ac.toFixed(1)
      : ac.toFixed(2);
}

/** Where the AOI sits along the downstream valley from the breach. */
export function aoiExtent(shape: Extract<AoiShape, { ok: true }>, dir: Downstream): AoiExtent {
  const { parts } = shape;
  const es = edges(parts);
  const warnings = [...shape.warnings];
  const areaM2 = parts.reduce((s, p) => s + partArea(p), 0);
  const breachInside = parts.some((p) => inPart(p, 0, 0));
  let nearestM = Infinity;
  let nearestPt: XY = [0, 0];
  for (const [a, b] of es) {
    const c = closestOnSeg(a, b);
    const d = Math.hypot(c[0], c[1]);
    if (d < nearestM) {
      nearestM = d;
      nearestPt = c;
    }
  }
  let axis = dir.u;
  let reachBasis: AoiExtent["reachBasis"] = "downstream";
  let nearM = 0;
  let farM = 0;
  let coversDownstream = true;
  if (!axis && breachInside) {
    reachBasis = "nearest-boundary";
    farM = nearestM;
    warnings.push(
      `${dir.note} The reach is cut at the drawn area's nearest edge in any direction (${fmtKm(nearestM)}), the shortest it could be, and the wet area is cut by distance only.`,
    );
  } else {
    if (!axis) {
      reachBasis = "toward-aoi";
      axis = unit(nearestPt[0], nearestPt[1]);
      warnings.push(
        `${dir.note} The valley is assumed to run from the breach toward the drawn area's nearest point (${fmtKm(nearestM)} away).`,
      );
    }
    const iv = axis ? insideIntervals(parts, es, [0, 0], [axis[0] * RAY_M, axis[1] * RAY_M]) : [];
    const first = breachInside ? iv.find((v) => v[0] < 1e-6) : iv.find((v) => v[0] > 0);
    if (first) {
      nearM = breachInside ? 0 : first[0];
      farM = first[1];
    } else {
      coversDownstream = false;
      warnings.push(
        "The drawn area does not reach the valley downstream of the breach: it is upstream or off to the side. Nothing is counted inside it, and every receptor is outside the drawn area.",
      );
    }
  }
  let upstreamFrac: number | null = null;
  if (dir.u && areaM2 > 0) {
    const up = parts.reduce(
      (s, p) =>
        s +
        upstreamArea(p.outer, dir.u!) -
        p.holes.reduce((h, r) => h + upstreamArea(r, dir.u!), 0),
      0,
    );
    upstreamFrac = Math.max(0, Math.min(1, up / areaM2));
    if (upstreamFrac > 0.5) {
      warnings.push(
        `Most of the drawn area (${Math.round(upstreamFrac * 100)}%) is upstream of the breach. The flood runs downstream; check the drawn area covers the valley below the dam.`,
      );
    }
  }
  const areaAc = areaM2 / ACRE_M2;
  if (areaAc < TINY_AOI_AC || (coversDownstream && farM - nearM < TINY_AOI_M)) {
    warnings.push(
      `The drawn area is tiny (${fmtKm(Math.max(0, farM - nearM))} along the valley, ${fmtAc(areaAc)} ac). It probably does not cover the flood; redraw it.`,
    );
  }
  const strip = reachBasis === "nearest-boundary" ? null : axis;
  const crossFrac = (xM: number, halfM: number) => {
    if (!strip) return null;
    if (halfM <= 0) return 0;
    const cx = strip[0] * xM;
    const cy = strip[1] * xM;
    const nx = -strip[1] * halfM;
    const ny = strip[0] * halfM;
    const iv = insideIntervals(parts, es, [cx - nx, cy - ny], [cx + nx, cy + ny]);
    return iv.reduce((s, [a, b]) => s + b - a, 0) / (2 * halfM);
  };
  return {
    nearM,
    farM,
    areaAc,
    breachInside,
    coversDownstream,
    reachBasis,
    upstreamFrac,
    axis: strip,
    parts: parts.length,
    warnings,
    crossFrac,
  };
}

/** True when the feature is something the user drew as an AOI (any geometry), so it is never ignored silently. */
export function hasAoi(f: GeoJSONFeature | undefined) {
  return !!f && !!f.geometry;
}

const offset = (o: LonLat, eastKm: number, northKm: number): [number, number] => [
  o.lon + (eastKm * 1000) / (M_PER_DEG_LON_EQ * Math.cos((o.lat * Math.PI) / 180)),
  o.lat + (northKm * 1000) / M_PER_DEG_LAT,
];

/** Polygon from [eastKm, northKm] corners around a point (tests and samples). */
export function polygonKm(
  o: LonLat,
  corners: [number, number][],
  holes: [number, number][][] = [],
): GeoJSONFeature {
  const ring = (c: [number, number][]) => [...c, c[0]].map(([e, n]) => offset(o, e, n));
  return {
    type: "Feature",
    properties: {},
    geometry: { type: "Polygon", coordinates: [ring(corners), ...holes.map(ring)] },
  };
}

/** A square AOI polygon `halfKm` either side of a point (tests and samples). */
export function squareAoi(lon: number, lat: number, halfKm: number): GeoJSONFeature {
  const h = halfKm;
  return polygonKm({ lon, lat }, [
    [-h, -h],
    [h, -h],
    [h, h],
    [-h, h],
  ]);
}

/** A dam line `halfKm` either side of a point, east–west, with the reservoir point `resKm` north (tests). */
export function damGeometryKm(
  o: LonLat,
  opts: { halfKm?: number; resNorthKm?: number; resEastKm?: number } = {},
): DamGeometry {
  const h = opts.halfKm ?? 0.2;
  return {
    damLine: {
      type: "Feature",
      properties: {},
      geometry: { type: "LineString", coordinates: [offset(o, -h, 0), offset(o, h, 0)] },
    },
    reservoirPoint: {
      type: "Feature",
      properties: {},
      geometry: {
        type: "Point",
        coordinates: offset(o, opts.resEastKm ?? 0, opts.resNorthKm ?? 0.5),
      },
    },
  };
}
