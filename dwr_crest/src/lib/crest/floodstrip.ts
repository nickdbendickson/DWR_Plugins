import { WET_FT } from "./acer.ts";
import { downstreamDirection, fromLocal, readAoi, ringArea, type Part, type XY } from "./aoi.ts";
import { damLocation, ENGINE_VERSION } from "./engine.ts";
import type { Analysis, DamRecord } from "./types.ts";

/**
 * Flood strip: the desk's schematic flood laid on the real map (Nick, 2026-10-30).
 *
 * The engine (engine.ts) works on a straight 1-D valley: stations at distance x downstream of the breach along the
 * downstream direction (aoi.ts downstreamDirection: the dam-line chord's normal, away from the reservoir point), each
 * with a half-width halfM and a depth. This module draws exactly that strip: the wet stations (depth >= WET_FT),
 * offset ±halfM perpendicular to the same straight centreline, in the same local metres around the breach location
 * the engine's AOI clip uses, then back to WGS 84. When a drawn area (AOI) is applied, the strip is clipped to it (and
 * cut where the valley enters it, when the breach is outside), so it never extends past the drawn area limit.
 *
 * It is a screening picture of a schematic valley, not a mapped flood extent: the real valley bends, the strip does not.
 */

export const FLOOD_STRIP_DISCLAIMER = "Approximate screening strip, not a mapped flood extent";

const ACRE_M2 = 4046.8564224;
/** Tolerance on the wet test: the auto reach ends where the desk depth is 0.1 ft, give or take rounding. */
const WET_EPS = 1e-9;

export type DepthBand = { id: string; label: string; minFt: number; maxFt: number; color: string };

/** Depth bands for shading and the legend (ft). Light to dark blue. */
export const DEPTH_BANDS: DepthBand[] = [
  { id: "d0", label: "0.1–0.5 ft", minFt: WET_FT, maxFt: 0.5, color: "#c6e3f2" },
  { id: "d1", label: "0.5–1 ft", minFt: 0.5, maxFt: 1, color: "#93c9e6" },
  { id: "d2", label: "1–2 ft", minFt: 1, maxFt: 2, color: "#5aa8d6" },
  { id: "d3", label: "2–5 ft", minFt: 2, maxFt: 5, color: "#2a7fbf" },
  { id: "d4", label: "5–10 ft", minFt: 5, maxFt: 10, color: "#135c9c" },
  { id: "d5", label: "10 ft and deeper", minFt: 10, maxFt: Infinity, color: "#08306b" },
];

export function bandFor(depthFt: number): DepthBand {
  for (let i = DEPTH_BANDS.length - 1; i >= 0; i--)
    if (depthFt >= DEPTH_BANDS[i].minFt) return DEPTH_BANDS[i];
  return DEPTH_BANDS[0];
}

export type StripCommon = {
  disclaimer: string;
  dam_id: string;
  dam_name: string;
  crest_navd88_ft: number;
  peak_cfs: number;
  engine_version: string;
};

export type StripFeature = {
  type: "Feature";
  properties: StripCommon & {
    band: string;
    band_label: string;
    /** Schematic depth (ft) at the band's upstream and downstream station. */
    depth_max_ft: number;
    depth_min_ft: number;
    /** Distance downstream of the breach (km) where the band starts and ends, before any AOI clip. */
    from_km: number;
    to_km: number;
    /** Station segments (of the engine's 81 stations) in this band. */
    segments: number;
    area_ac: number;
    color: string;
  };
  geometry:
    | { type: "Polygon"; coordinates: number[][][] }
    | { type: "MultiPolygon"; coordinates: number[][][][] };
};

export type FloodStrip = {
  type: "FeatureCollection";
  /** Foreign member (RFC 7946 §6.1): the same facts as every feature carries, for readers that look here. */
  properties: StripCommon & {
    max_depth_ft: number;
    reach_km: number;
    wet_reach_km: number;
    direction: string;
    clipped_to_aoi: boolean;
    strip_area_ac: number;
    engine_wet_area_ac: number;
    breach_lon: number;
    breach_lat: number;
  };
  features: StripFeature[];
};

export type FloodStripResult =
  { strip: FloodStrip; reason: null } | { strip: null; reason: string };

const r7 = (n: number) => Math.round(n * 1e7) / 1e7;
const r3 = (n: number) => Math.round(n * 1e3) / 1e3;

function polyArea(rings: XY[][]) {
  if (rings.length === 0) return 0;
  return (
    Math.abs(ringArea(rings[0])) - rings.slice(1).reduce((s, h) => s + Math.abs(ringArea(h)), 0)
  );
}

function dedupe(r: XY[]): XY[] {
  const out: XY[] = [];
  for (const p of r) {
    const q = out[out.length - 1];
    if (!q || Math.hypot(p[0] - q[0], p[1] - q[1]) > 1e-6) out.push(p);
  }
  while (
    out.length > 1 &&
    Math.hypot(out[0][0] - out[out.length - 1][0], out[0][1] - out[out.length - 1][1]) <= 1e-6
  )
    out.pop();
  return out;
}

/** Sutherland–Hodgman: the part of ring `subject` (any simple ring) inside the convex counter-clockwise ring `clip`. */
export function clipToConvex(subject: XY[], clip: XY[]): XY[] {
  let out = subject;
  for (let i = 0; i < clip.length && out.length > 0; i++) {
    const a = clip[i];
    const b = clip[(i + 1) % clip.length];
    const side = (p: XY) => (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]);
    const input = out;
    out = [];
    for (let j = 0; j < input.length; j++) {
      const p = input[j];
      const q = input[(j + 1) % input.length];
      const sp = side(p);
      const sq = side(q);
      if (sp >= 0) out.push(p);
      if (sp >= 0 !== sq >= 0) {
        const t = sp / (sp - sq);
        out.push([p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])]);
      }
    }
  }
  const d = dedupe(out);
  return d.length >= 3 && Math.abs(ringArea(d)) > 1e-6 ? d : [];
}

/** Counter-clockwise outer ring, clockwise holes (RFC 7946 §3.1.6). */
function orient(rings: XY[][]): XY[][] {
  return rings.map((r, i) => {
    const ccw = ringArea(r) > 0;
    return (i === 0) === ccw ? r : [...r].reverse();
  });
}

/** Strip of the stations i0..i1 as a convex ring, local metres (counter-clockwise). */
function bandRing(st: { x: number; h: number }[], i0: number, i1: number, u: XY): XY[] {
  const n: XY = [-u[1], u[0]];
  const left: XY[] = [];
  const right: XY[] = [];
  for (let i = i0; i <= i1; i++) {
    const { x, h } = st[i];
    left.push([u[0] * x + n[0] * h, u[1] * x + n[1] * h]);
    right.push([u[0] * x - n[0] * h, u[1] * x - n[1] * h]);
  }
  const ring = dedupe([...left, ...right.reverse()]);
  return ringArea(ring) > 0 ? ring : ring.reverse();
}

/** Half-plane x >= fromM along u, as a large convex ring (for the "valley enters the AOI here" cut). */
function halfPlane(u: XY, fromM: number, farM: number): XY[] {
  const n: XY = [-u[1], u[0]];
  const W = 1e7;
  const at = (x: number, y: number): XY => [u[0] * x + n[0] * y, u[1] * x + n[1] * y];
  const ring = [at(fromM, -W), at(farM + 1e6, -W), at(farM + 1e6, W), at(fromM, W)];
  return ringArea(ring) > 0 ? ring : ring.reverse();
}

/**
 * The wet strip as WGS 84 polygons, banded by depth, or why there is none. Uses the engine's own stations, reach
 * length, centreline (straight, from the breach location along the downstream direction) and AOI clip.
 */
export function floodStrip(dam: DamRecord, result: Analysis): FloodStripResult {
  const { flood } = result;
  const wet = flood.stations.filter((s) => s.depthFt >= WET_FT - WET_EPS);
  if (wet.length < 2)
    return {
      strip: null,
      reason: "No flood strip: the schematic depth is under 0.1 ft along the whole reach.",
    };
  const loc = damLocation(dam);
  if (!loc)
    return {
      strip: null,
      reason:
        "No flood strip: no breach location. Draw the breach point or enter the dam latitude and longitude.",
    };
  const dir = downstreamDirection(dam.geometry, loc);
  if (!dir.u) return { strip: null, reason: `No flood strip on the map. ${dir.note}` };
  const u = dir.u;

  const aoi = flood.aoi?.applied ? flood.aoi : null;
  let parts: Part[] | null = null;
  let fromM = 0;
  if (aoi) {
    const shape = readAoi(dam.geometry?.aoi, loc);
    if (!shape.ok)
      return { strip: null, reason: "No flood strip: the drawn area could not be read." };
    if (aoi.reachBasis !== "downstream" || aoi.areaBasis !== "clipped") {
      return {
        strip: null,
        reason:
          "No flood strip: the downstream direction is unknown, so the strip cannot be placed in the drawn area.",
      };
    }
    if (flood.acres <= 0 || aoi.farKm <= aoi.nearKm) {
      return {
        strip: null,
        reason:
          "No flood strip: the drawn area does not reach the wet valley downstream of the breach.",
      };
    }
    parts = shape.parts;
    fromM = aoi.nearKm * 1000;
  }

  const st = wet.map((s) => ({ x: s.s * flood.lengthM, h: s.halfM, d: s.depthFt }));
  // Group station segments by the band of their mid-segment depth (the depth falls downstream, so bands are contiguous).
  const groups: { band: DepthBand; i0: number; i1: number }[] = [];
  for (let i = 0; i + 1 < st.length; i++) {
    const band = bandFor((st[i].d + st[i + 1].d) / 2);
    const g = groups[groups.length - 1];
    if (g && g.band.id === band.id && g.i1 === i) g.i1 = i + 1;
    else groups.push({ band, i0: i, i1: i + 1 });
  }

  const common: StripCommon = {
    disclaimer: FLOOD_STRIP_DISCLAIMER,
    dam_id: dam.nid || dam.id,
    dam_name: dam.name,
    crest_navd88_ft: r3(result.datum.crestNavdFt),
    peak_cfs: Math.round(result.breach.peakCfs),
    engine_version: ENGINE_VERSION,
  };
  const toLonLat = (r: XY[]) => {
    const ring = r.map((p) => fromLocal(p, loc).map(r7) as [number, number]);
    return [...ring, ring[0]];
  };

  const features: StripFeature[] = [];
  let totalM2 = 0;
  const lastX = st[st.length - 1].x;
  for (const g of groups) {
    let band = bandRing(st, g.i0, g.i1, u);
    if (fromM > 0) {
      band = clipToConvex(band, halfPlane(u, fromM, lastX));
      if (band.length === 0) continue;
    }
    let polys: XY[][][];
    if (!parts) polys = [[band]];
    else {
      polys = [];
      for (const p of parts) {
        const outer = clipToConvex(p.outer, band);
        if (outer.length === 0) continue;
        const holes = p.holes.map((h) => clipToConvex(h, band)).filter((h) => h.length > 0);
        polys.push([outer, ...holes]);
      }
    }
    polys = polys.map(orient);
    const areaM2 = polys.reduce((s, rings) => s + polyArea(rings), 0);
    if (polys.length === 0 || areaM2 <= 1e-6) continue;
    totalM2 += areaM2;
    const coords = polys.map((rings) => rings.map(toLonLat));
    features.push({
      type: "Feature",
      properties: {
        ...common,
        band: g.band.id,
        band_label: g.band.label,
        depth_max_ft: r3(st[g.i0].d),
        depth_min_ft: r3(st[g.i1].d),
        from_km: r3(st[g.i0].x / 1000),
        to_km: r3(st[g.i1].x / 1000),
        segments: g.i1 - g.i0,
        area_ac: r3(areaM2 / ACRE_M2),
        color: g.band.color,
      },
      geometry:
        coords.length === 1
          ? { type: "Polygon", coordinates: coords[0] }
          : { type: "MultiPolygon", coordinates: coords },
    });
  }
  if (features.length === 0)
    return {
      strip: null,
      reason: "No flood strip: none of the wet strip lies inside the drawn area.",
    };

  return {
    strip: {
      type: "FeatureCollection",
      properties: {
        ...common,
        max_depth_ft: r3(flood.maxDepthFt),
        reach_km: r3(flood.lengthM / 1000),
        wet_reach_km: r3(lastX / 1000),
        direction: dir.note,
        clipped_to_aoi: parts != null,
        strip_area_ac: r3(totalM2 / ACRE_M2),
        engine_wet_area_ac: r3(flood.acres),
        breach_lon: r7(loc.lon),
        breach_lat: r7(loc.lat),
      },
      features,
    },
    reason: null,
  };
}

/** The strip, or null when there is no geometry or nothing wet (floodStrip says why). */
export function floodFootprint(dam: DamRecord, result: Analysis): FloodStrip | null {
  return floodStrip(dam, result).strip;
}

/** <root>_flood_strip.geojson: WGS 84 (RFC 7946, lon/lat). */
export function exportFloodStripGeoJson(strip: FloodStrip): string {
  return JSON.stringify(strip, null, 2) + "\n";
}

const esc = (s: string) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

/** KML colour: aabbggrr. */
function kmlColor(hex: string, alpha = 0x73) {
  const h = hex.replace("#", "");
  return `${alpha.toString(16).padStart(2, "0")}${h.slice(4, 6)}${h.slice(2, 4)}${h.slice(0, 2)}`;
}

/**
 * <root>_flood_strip.kml: the same polygons and facts, for Google Earth and the like. `extra` adds lines to the
 * document description (the PLSS section list, plss.ts);
 * `placemarks` adds KML after the bands (the 911 address points, nd911.ts).
 */
export function exportFloodStripKml(strip: FloodStrip, extra: string[] = [], placemarks: string[] = []): string {
  const p = strip.properties;
  const desc = [
    `${p.disclaimer}.`,
    `Dam ${p.dam_id} (${p.dam_name}). Crest ${p.crest_navd88_ft.toFixed(2)} ft NAVD88. Peak ${p.peak_cfs.toLocaleString("en-US")} cfs.`,
    `Schematic desk valley: max depth ${p.max_depth_ft.toFixed(2)} ft, wet strip ${p.wet_reach_km.toFixed(2)} km, ${p.strip_area_ac.toFixed(1)} ac${p.clipped_to_aoi ? ", clipped to the drawn area" : ""}.`,
    p.direction,
    `Engine: ${p.engine_version}.`,
    ...extra,
  ].join("\n");
  const coords = (ring: number[][]) => ring.map(([lon, lat]) => `${lon},${lat}`).join(" ");
  const polygon = (rings: number[][][]) =>
    [
      "<Polygon><tessellate>1</tessellate>",
      `<outerBoundaryIs><LinearRing><coordinates>${coords(rings[0])}</coordinates></LinearRing></outerBoundaryIs>`,
      ...rings
        .slice(1)
        .map(
          (h) =>
            `<innerBoundaryIs><LinearRing><coordinates>${coords(h)}</coordinates></LinearRing></innerBoundaryIs>`,
        ),
      "</Polygon>",
    ].join("");
  const styles = DEPTH_BANDS.map(
    (b) =>
      `<Style id="${b.id}"><LineStyle><width>0</width></LineStyle><PolyStyle><color>${kmlColor(b.color)}</color><outline>0</outline></PolyStyle></Style>`,
  );
  const marks = strip.features.map((f) => {
    const q = f.properties;
    const geom =
      f.geometry.type === "Polygon"
        ? polygon(f.geometry.coordinates)
        : `<MultiGeometry>${f.geometry.coordinates.map(polygon).join("")}</MultiGeometry>`;
    const data = Object.entries(q)
      .map(([k, v]) => `<Data name="${esc(k)}"><value>${esc(String(v))}</value></Data>`)
      .join("");
    return [
      "<Placemark>",
      `<name>${esc(`Depth ${q.band_label}`)}</name>`,
      `<description>${esc(`${q.disclaimer}. Schematic depth ${q.depth_max_ft.toFixed(2)} to ${q.depth_min_ft.toFixed(2)} ft, ${q.from_km.toFixed(2)}–${q.to_km.toFixed(2)} km downstream of the breach.`)}</description>`,
      `<styleUrl>#${q.band}</styleUrl>`,
      `<ExtendedData>${data}</ExtendedData>`,
      geom,
      "</Placemark>",
    ].join("\n");
  });
  return (
    [
      '<?xml version="1.0" encoding="UTF-8"?>',
      '<kml xmlns="http://www.opengis.net/kml/2.2">',
      "<Document>",
      `<name>${esc(`${p.dam_id} flood strip (approximate)`)}</name>`,
      `<description>${esc(desc)}</description>`,
      ...styles,
      ...marks,
      ...placemarks,
      "</Document>",
      "</kml>",
    ].join("\n") + "\n"
  );
}
