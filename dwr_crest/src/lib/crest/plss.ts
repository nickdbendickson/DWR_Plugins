import { downstreamDirection, fromLocal, ringArea, toLocal, type XY } from "./aoi.ts";
import { damLocation } from "./engine.ts";
import { clipToConvex, DEPTH_BANDS, exportFloodStripGeoJson, exportFloodStripKml, type FloodStrip } from "./floodstrip.ts";
import type { DamRecord } from "./types.ts";

/**
 * PLSS sections the flood strip touches (Nick, 2026-10-30: "show or list the township/range sections the strip
 * touches").
 *
 * Source: BLM National PLSS CadNSDI map service, layer 2 "PLSS Section" (First Division), queried on request by the
 * strip's bounding box (padded), WGS 84 out. The section polygons that come back are cached on the dam record, so the
 * list, the map outlines and the exports keep working offline afterwards. Nothing is bundled: ND alone has about 70k
 * sections (tens of MB as GeoJSON), and a township grid with computed sections would be wrong wherever the real
 * sections are irregular (correction lines, fractional and lotted sections along the rivers and lakes).
 *
 * The intersection runs in the same local metres as the strip itself (aoi.ts toLocal around the breach), so the wet
 * acres per section add up to the strip's own area.
 */

export const PLSS_SERVICE =
  "https://gis.blm.gov/arcgis/rest/services/Cadastral/BLM_Natl_PLSS_CadNSDI/MapServer";
export const PLSS_SECTION_LAYER = `${PLSS_SERVICE}/2`;
export const PLSS_ATTRIBUTION =
  "PLSS sections: BLM National PLSS CadNSDI (Bureau of Land Management), gis.blm.gov. Public data, provided as is; not a legal survey or land record.";
export const PLSS_ATTRIBUTION_SHORT = "PLSS: BLM CadNSDI";
export const PLSS_OFFLINE =
  "PLSS sections need a connection: look them up once while online (gis.blm.gov), and they are saved with this dam for offline use.";
/** Wet parts smaller than this are slivers (edge rounding against the section lines), counted but not listed. */
export const SLIVER_AC = 0.1;
/** The query box is the strip's box grown by this much, so small edits to the strip reuse the cache. */
export const PLSS_PAD_M = 250;
const PAGE = 1000;
const MAX_PAGES = 20;
const ACRE_M2 = 4046.8564224;
const M_PER_DEG_LAT = 111_320;

/** Principal meridian code in the PLSSID (CadNSDI PRINMERCD 5, 5th Meridian: all of ND); others print as their code. */
const MERIDIANS: Record<string, string> = { "05": "5th PM" };
const FRACTIONS: Record<string, string> = { "0": "", "1": "¼", "2": "½", "3": "¾" };

export type Bbox = [west: number, south: number, east: number, north: number];

/** One section polygon as fetched (lon/lat, MultiPolygon rings), with the identifiers the label needs. */
export type PlssSection = {
  /** First division id, e.g. ND051400N0520W0SN300 (T140N R52W Sec 30, 5th PM). */
  id: string;
  /** Township id, e.g. ND051400N0520W0. */
  plssid: string;
  /** Division type code (SN = section) and text; number and duplicate flag as the service gives them. */
  typ: string;
  txt: string;
  no: string;
  dup: string;
  coordinates: number[][][][];
};

export type PlssCache = {
  source: string;
  attribution: string;
  /** ISO time of the lookup. */
  fetchedAt: string;
  /** Box that was queried (lon/lat); the cache serves any strip inside it. */
  bbox: Bbox;
  sections: PlssSection[];
};

export type PlssTownship = {
  meridianCode: string;
  meridian: string;
  township: string;
  range: string;
};

export type PlssRow = {
  order: number;
  label: string;
  /** "T140N R52W (5th PM)" */
  townshipLabel: string;
  section: string;
  meridian: string;
  township: string;
  range: string;
  id: string;
  plssid: string;
  wetAc: number;
  /** Deepest depth band of the strip inside the section. */
  bandId: string;
  bandLabel: string;
  /** Along the valley from the breach: where the strip enters and leaves the section (km). */
  fromKm: number;
  toKm: number;
  /** Label point (lon/lat): the middle of the wet part, for the map. */
  labelAt: [number, number];
  coordinates: number[][][][];
};

export type PlssTable = {
  rows: PlssRow[];
  sliversIgnored: number;
  sliverAc: number;
  listedAc: number;
  stripAc: number;
  /** Strip area that no fetched section covers (outside the data, e.g. water or unsurveyed), ac. */
  uncoveredAc: number;
  fetchedAt: string;
  source: string;
  attribution: string;
};

export type PlssState =
  | { state: "ready"; table: PlssTable; message: string | null }
  | { state: "lookup"; message: string }
  | { state: "offline"; message: string }
  | { state: "none"; message: string };

/** Township, range and meridian from the CadNSDI PLSSID: SS MM TTT F D RRR F D U (e.g. ND 05 140 0 N 052 0 W 0). */
export function parsePlssid(plssid: string): PlssTownship | null {
  const m = /^([A-Z]{2})(\d{2})(\d{3})(\d)([NS])(\d{3})(\d)([EW])(\d)$/.exec(plssid);
  if (!m) return null;
  const [, , mc, tn, tf, td, rn, rf, rd] = m;
  return {
    meridianCode: mc,
    meridian: MERIDIANS[mc] ?? `PM code ${mc}`,
    township: `${Number(tn)}${FRACTIONS[tf] ?? ""}${td}`,
    range: `${Number(rn)}${FRACTIONS[rf] ?? ""}${rd}`,
  };
}

/** "T139N R52W Sec 12 (5th PM)"; a non-section first division prints its type ("Lot", "Tract" …). */
export function sectionLabel(s: Pick<PlssSection, "plssid" | "typ" | "txt" | "no" | "dup">) {
  const t = parsePlssid(s.plssid);
  const num = /^\d+$/.test(s.no) ? String(Number(s.no)) : s.no;
  const kind = s.typ === "SN" ? "Sec" : s.txt || s.typ;
  const dup = s.dup && s.dup !== "0" ? ` (dup ${s.dup})` : "";
  const tr = t ? `T${t.township} R${t.range}` : s.plssid;
  return { tr, label: `${tr} ${kind} ${num}${dup}${t ? ` (${t.meridian})` : ""}`, section: `${num}${dup}`, t };
}

export function stripBbox(strip: FloodStrip): Bbox {
  const b: Bbox = [180, 90, -180, -90];
  for (const f of strip.features) {
    const polys = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates;
    for (const p of polys)
      for (const [x, y] of p[0]) {
        b[0] = Math.min(b[0], x);
        b[1] = Math.min(b[1], y);
        b[2] = Math.max(b[2], x);
        b[3] = Math.max(b[3], y);
      }
  }
  return b;
}

const r6 = (n: number) => Math.round(n * 1e6) / 1e6;
const r3 = (n: number) => Math.round(n * 1e3) / 1e3;

export function padBbox(b: Bbox, m = PLSS_PAD_M): Bbox {
  const dLat = m / M_PER_DEG_LAT;
  const dLon = m / (M_PER_DEG_LAT * Math.cos((((b[1] + b[3]) / 2) * Math.PI) / 180));
  return [r6(b[0] - dLon), r6(b[1] - dLat), r6(b[2] + dLon), r6(b[3] + dLat)];
}

export const bboxContains = (outer: Bbox, inner: Bbox) =>
  outer[0] <= inner[0] && outer[1] <= inner[1] && outer[2] >= inner[2] && outer[3] >= inner[3];

/** Query URL for one page of sections in `bbox` (GeoJSON, WGS 84). */
export function plssQueryUrl(bbox: Bbox, offset = 0) {
  const q = new URLSearchParams({
    geometry: bbox.join(","),
    geometryType: "esriGeometryEnvelope",
    inSR: "4326",
    spatialRel: "esriSpatialRelIntersects",
    outFields: "PLSSID,FRSTDIVID,FRSTDIVTYP,FRSTDIVTXT,FRSTDIVNO,FRSTDIVDUP",
    returnGeometry: "true",
    outSR: "4326",
    geometryPrecision: "6",
    orderByFields: "OBJECTID",
    resultOffset: String(offset),
    resultRecordCount: String(PAGE),
    f: "geojson",
  });
  return `${PLSS_SECTION_LAYER}/query?${q}`;
}

type Fetch = (url: string, init?: { signal?: AbortSignal }) => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>;

type ServiceFeature = {
  properties?: Record<string, unknown> | null;
  geometry?: { type: string; coordinates: unknown } | null;
};

/** Turn the service's GeoJSON features into cached sections (drops features with no usable polygon). */
export function readSections(features: ServiceFeature[]): PlssSection[] {
  const out: PlssSection[] = [];
  for (const f of features) {
    const p = f.properties ?? {};
    const g = f.geometry;
    if (!g || (g.type !== "Polygon" && g.type !== "MultiPolygon")) continue;
    const coords = (g.type === "Polygon" ? [g.coordinates] : g.coordinates) as number[][][][];
    const str = (k: string) => (p[k] == null ? "" : String(p[k]));
    out.push({
      id: str("FRSTDIVID"),
      plssid: str("PLSSID"),
      typ: str("FRSTDIVTYP"),
      txt: str("FRSTDIVTXT"),
      no: str("FRSTDIVNO"),
      dup: str("FRSTDIVDUP"),
      coordinates: coords.map((poly) => poly.map((ring) => ring.map(([x, y]) => [r6(x), r6(y)]))),
    });
  }
  return out;
}

/** One on-request lookup: every section in the strip's padded box, all pages. Throws with a readable message. */
export async function fetchPlss(strip: FloodStrip, fetchFn: Fetch = fetch as unknown as Fetch, now = new Date()): Promise<PlssCache> {
  const bbox = padBbox(stripBbox(strip));
  const sections: PlssSection[] = [];
  for (let page = 0; page < MAX_PAGES; page++) {
    const ctl = typeof AbortController === "undefined" ? null : new AbortController();
    const timer = ctl ? setTimeout(() => ctl.abort(), 30_000) : null;
    let body: { features?: ServiceFeature[]; exceededTransferLimit?: boolean; properties?: { exceededTransferLimit?: boolean }; error?: { message?: string } };
    try {
      const res = await fetchFn(plssQueryUrl(bbox, page * PAGE), ctl ? { signal: ctl.signal } : undefined);
      if (!res.ok) throw new Error(`the BLM PLSS service answered HTTP ${res.status}`);
      body = (await res.json()) as typeof body;
    } catch (e) {
      const msg = (e as Error).name === "AbortError" ? "the BLM PLSS service did not answer in 30 s" : (e as Error).message;
      throw new Error(`PLSS lookup failed: ${msg}. ${PLSS_OFFLINE}`);
    } finally {
      if (timer) clearTimeout(timer);
    }
    if (body.error) throw new Error(`PLSS lookup failed: ${body.error.message ?? "service error"}.`);
    const feats = body.features ?? [];
    sections.push(...readSections(feats));
    const more = body.exceededTransferLimit ?? body.properties?.exceededTransferLimit ?? false;
    if (!more || feats.length === 0) break;
  }
  const seen = new Set<string>();
  return {
    source: PLSS_SECTION_LAYER,
    attribution: PLSS_ATTRIBUTION,
    fetchedAt: now.toISOString(),
    bbox,
    sections: sections.filter((s) => (seen.has(s.id) ? false : (seen.add(s.id), true))),
  };
}

// ---- geometry ------------------------------------------------------------------------------------------------

function ccw(r: XY[]): XY[] {
  return ringArea(r) >= 0 ? r : [...r].reverse();
}

const openRing = (ring: number[][], o: { lon: number; lat: number }): XY[] => {
  const pts = ring.map(([lon, lat]) => toLocal(lon, lat, o));
  const a = pts[0];
  const b = pts[pts.length - 1];
  if (pts.length > 1 && Math.hypot(a[0] - b[0], a[1] - b[1]) < 1e-6) pts.pop();
  // drop repeated points
  return pts.filter((p, i) => i === 0 || Math.hypot(p[0] - pts[i - 1][0], p[1] - pts[i - 1][1]) > 1e-6);
};

/** Ear-clipping triangulation of a simple ring (counter-clockwise triangles). Section rings are small, so O(n³) is fine. */
export function triangulate(ring: XY[]): XY[][] {
  const pts = ccw(ring);
  const idx = pts.map((_, i) => i);
  const tris: XY[][] = [];
  const cross = (a: XY, b: XY, c: XY) => (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
  const same = (p: XY, q: XY) => p[0] === q[0] && p[1] === q[1];
  // Inside or on the edge: a reflex vertex lying on the would-be diagonal blocks the ear too.
  const inside = (p: XY, a: XY, b: XY, c: XY) =>
    !same(p, a) && !same(p, b) && !same(p, c) && cross(a, b, p) >= -1e-9 && cross(b, c, p) >= -1e-9 && cross(c, a, p) >= -1e-9;
  let guard = 0;
  while (idx.length > 3 && guard++ < 10_000) {
    let cut = false;
    for (let k = 0; k < idx.length; k++) {
      const a = pts[idx[(k + idx.length - 1) % idx.length]];
      const b = pts[idx[k]];
      const c = pts[idx[(k + 1) % idx.length]];
      const turn = cross(a, b, c);
      if (Math.abs(turn) <= 1e-9) {
        idx.splice(k, 1); // collinear vertex: no area, drop it
        cut = true;
        break;
      }
      if (turn < 0) continue;
      if (idx.some((j) => inside(pts[j], a, b, c))) continue;
      tris.push([a, b, c]);
      idx.splice(k, 1);
      cut = true;
      break;
    }
    if (!cut) break;
  }
  if (idx.length === 3) {
    const t = idx.map((i) => pts[i]);
    if (Math.abs(cross(t[0], t[1], t[2])) > 1e-9) tris.push(t);
  } else if (idx.length > 3) {
    // Not simple after all: fall back to a fan (exact for convex rings, close for near-convex ones).
    for (let k = 1; k + 1 < idx.length; k++) tris.push(ccw([pts[idx[0]], pts[idx[k]], pts[idx[k + 1]]]));
  }
  return tris;
}

type LocalPoly = { outer: XY[]; holes: XY[][] };

/** Area (m²) of `poly` inside the convex ring `tri`, with the extent of that part along `u`. */
function clipPoly(poly: LocalPoly, tri: XY[], u: XY) {
  const outer = clipToConvex(poly.outer, tri);
  if (outer.length === 0) return null;
  let area = Math.abs(ringArea(outer));
  for (const h of poly.holes) {
    const c = clipToConvex(h, tri);
    if (c.length) area -= Math.abs(ringArea(c));
  }
  let lo = Infinity;
  let hi = -Infinity;
  let cx = 0;
  let cy = 0;
  for (const p of outer) {
    const a = p[0] * u[0] + p[1] * u[1];
    lo = Math.min(lo, a);
    hi = Math.max(hi, a);
    cx += p[0];
    cy += p[1];
  }
  return { area: Math.max(0, area), lo, hi, c: [cx / outer.length, cy / outer.length] as XY };
}

/**
 * Sections the strip touches, with the wet acres and deepest band inside each, sorted downstream. Parts under
 * SLIVER_AC are counted in `sliversIgnored`, not listed.
 */
export function plssTable(dam: DamRecord, strip: FloodStrip, cache: PlssCache): PlssTable {
  const loc = damLocation(dam) ?? { lon: strip.properties.breach_lon, lat: strip.properties.breach_lat };
  const o = { lon: strip.properties.breach_lon, lat: strip.properties.breach_lat };
  const u = downstreamDirection(dam.geometry, loc).u ?? ([0, -1] as XY);
  const rank = new Map(DEPTH_BANDS.map((b, i) => [b.id, i]));

  const bands = strip.features.map((f) => {
    const polys = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates;
    const local: LocalPoly[] = polys.map((p) => ({ outer: ccw(openRing(p[0], o)), holes: p.slice(1).map((h) => openRing(h, o)) }));
    const bb = [Infinity, Infinity, -Infinity, -Infinity];
    for (const lp of local)
      for (const [x, y] of lp.outer) {
        bb[0] = Math.min(bb[0], x);
        bb[1] = Math.min(bb[1], y);
        bb[2] = Math.max(bb[2], x);
        bb[3] = Math.max(bb[3], y);
      }
    return { band: f.properties.band, label: f.properties.band_label, local, bb };
  });

  const rows: Omit<PlssRow, "order">[] = [];
  let sliversIgnored = 0;
  let sliverM2 = 0;
  let coveredM2 = 0;
  for (const s of cache.sections) {
    // Triangles of the section (holes subtracted below).
    const parts = s.coordinates.map((p) => ({ outer: openRing(p[0], o), holes: p.slice(1).map((h) => openRing(h, o)) }));
    const sb = [Infinity, Infinity, -Infinity, -Infinity];
    for (const p of parts)
      for (const [x, y] of p.outer) {
        sb[0] = Math.min(sb[0], x);
        sb[1] = Math.min(sb[1], y);
        sb[2] = Math.max(sb[2], x);
        sb[3] = Math.max(sb[3], y);
      }
    let m2 = 0;
    let lo = Infinity;
    let hi = -Infinity;
    let deepest = -1;
    let bandId = "";
    let bandLabel = "";
    let wx = 0;
    let wy = 0;
    let wa = 0;
    for (const b of bands) {
      if (b.bb[0] > sb[2] || b.bb[2] < sb[0] || b.bb[1] > sb[3] || b.bb[3] < sb[1]) continue;
      let bm2 = 0;
      for (const sp of parts) {
        const sign = [
          ...triangulate(sp.outer).map((t) => [t, 1] as const),
          ...sp.holes.flatMap((h) => triangulate(h).map((t) => [t, -1] as const)),
        ];
        for (const [tri, sg] of sign)
          for (const lp of b.local) {
            const c = clipPoly(lp, tri, u);
            if (!c || c.area <= 0) continue;
            bm2 += sg * c.area;
            if (sg > 0) {
              lo = Math.min(lo, c.lo);
              hi = Math.max(hi, c.hi);
              wx += c.c[0] * c.area;
              wy += c.c[1] * c.area;
              wa += c.area;
            }
          }
      }
      if (bm2 > 1e-3) {
        m2 += bm2;
        const r = rank.get(b.band) ?? 0;
        if (r > deepest) {
          deepest = r;
          bandId = b.band;
          bandLabel = b.label;
        }
      }
    }
    if (m2 <= 1e-3) continue;
    coveredM2 += m2;
    if (m2 / ACRE_M2 < SLIVER_AC) {
      sliversIgnored++;
      sliverM2 += m2;
      continue;
    }
    const { tr, label, section, t } = sectionLabel(s);
    const wsum = Math.max(1e-9, wa);
    const at = fromLocal([wx / wsum, wy / wsum], o);
    rows.push({
      label,
      townshipLabel: `${tr}${t ? ` (${t.meridian})` : ""}`,
      section,
      meridian: t?.meridian ?? "",
      township: t ? `T${t.township}` : "",
      range: t ? `R${t.range}` : "",
      id: s.id,
      plssid: s.plssid,
      wetAc: m2 / ACRE_M2,
      bandId,
      bandLabel,
      fromKm: Math.max(0, lo) / 1000,
      toKm: Math.max(0, hi) / 1000,
      labelAt: [r6(at[0]), r6(at[1])],
      coordinates: s.coordinates,
    });
  }
  rows.sort((a, b) => a.fromKm - b.fromKm || a.toKm - b.toKm || a.label.localeCompare(b.label));
  const listedM2 = rows.reduce((s, r) => s + r.wetAc * ACRE_M2, 0);
  return {
    rows: rows.map((r, i) => ({ ...r, order: i + 1, wetAc: r3(r.wetAc), fromKm: r3(r.fromKm), toKm: r3(r.toKm) })),
    sliversIgnored,
    sliverAc: r3(sliverM2 / ACRE_M2),
    listedAc: r3(listedM2 / ACRE_M2),
    stripAc: strip.properties.strip_area_ac,
    uncoveredAc: r3(Math.max(0, strip.properties.strip_area_ac - coveredM2 / ACRE_M2)),
    fetchedAt: cache.fetchedAt,
    source: cache.source,
    attribution: cache.attribution,
  };
}

/** What the desk can show for the PLSS list right now: the table from the cache, or why not (and what to do). */
export function plssState(dam: DamRecord, strip: FloodStrip | null, online: boolean): PlssState {
  if (!strip) return { state: "none", message: "No flood strip, so no PLSS sections." };
  const cache = dam.plss;
  const need = stripBbox(strip);
  if (cache && bboxContains(cache.bbox, need)) {
    return { state: "ready", table: plssTable(dam, strip, cache), message: null };
  }
  if (cache) {
    const msg = "The flood strip now reaches past the PLSS sections saved with this dam.";
    return online
      ? { state: "lookup", message: `${msg} Look them up again.` }
      : { state: "offline", message: `${msg} ${PLSS_OFFLINE}` };
  }
  return online
    ? { state: "lookup", message: "PLSS sections are not looked up yet for this strip (one query to the BLM PLSS service)." }
    : { state: "offline", message: PLSS_OFFLINE };
}

// ---- exports ------------------------------------------------------------------------------------------------

/** The list as plain properties for the flood-strip GeoJSON / KML. */
export function plssProperties(t: PlssTable) {
  return {
    plss_attribution: t.attribution,
    plss_source: t.source,
    plss_fetched_at: t.fetchedAt,
    plss_sliver_ac: SLIVER_AC,
    plss_slivers_ignored: t.sliversIgnored,
    plss_uncovered_ac: t.uncoveredAc,
    plss_sections: t.rows.map((r) => ({
      order: r.order,
      label: r.label,
      first_division_id: r.id,
      township_id: r.plssid,
      wet_ac: r.wetAc,
      max_depth_band: r.bandLabel,
      from_km: r.fromKm,
      to_km: r.toKm,
    })),
  };
}

const csvCell = (v: string | number) => {
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

/** <root>_plss_sections.csv: one row per touched section, downstream order, with the source on every row. */
export function exportPlssCsv(t: PlssTable): string {
  const head = [
    "order",
    "label",
    "meridian",
    "township",
    "range",
    "section",
    "first_division_id",
    "township_id",
    "wet_ac",
    "max_depth_band",
    "from_km",
    "to_km",
    "source",
  ];
  const src = `${PLSS_ATTRIBUTION} Fetched ${t.fetchedAt}. Slivers under ${SLIVER_AC} ac not listed: ${t.sliversIgnored}.`;
  const lines = [head.join(",")];
  for (const r of t.rows)
    lines.push(
      [r.order, r.label, r.meridian, r.township, r.range, r.section, r.id, r.plssid, r.wetAc.toFixed(3), r.bandLabel, r.fromKm.toFixed(3), r.toKm.toFixed(3), src]
        .map(csvCell)
        .join(","),
    );
  return lines.join("\n") + "\n";
}

/** The flood-strip GeoJSON with the PLSS list in the collection's properties (foreign members, RFC 7946 §6.1). */
export function exportFloodStripGeoJsonWithPlss(strip: FloodStrip, t: PlssTable | null): string {
  if (!t) return exportFloodStripGeoJson(strip);
  return exportFloodStripGeoJson({ ...strip, properties: { ...strip.properties, ...plssProperties(t) } } as FloodStrip);
}

/** Description lines for the flood-strip KML. */
export function plssKmlLines(t: PlssTable): string[] {
  return [
    "",
    `PLSS sections the strip touches (${t.rows.length}, downstream order; wet ac, deepest depth band):`,
    ...t.rows.map((r) => `${r.order}. ${r.label}: ${r.wetAc.toFixed(1)} ac, ${r.bandLabel}, ${r.fromKm.toFixed(2)}–${r.toKm.toFixed(2)} km`),
    `Slivers under ${SLIVER_AC} ac not listed: ${t.sliversIgnored}${t.sliversIgnored ? ` (${t.sliverAc.toFixed(2)} ac)` : ""}.`,
    ...(t.uncoveredAc >= SLIVER_AC ? [`Strip area outside every fetched section: ${t.uncoveredAc.toFixed(1)} ac.`] : []),
    `${t.attribution} Fetched ${t.fetchedAt}.`,
  ];
}

export function exportFloodStripKmlWithPlss(strip: FloodStrip, t: PlssTable | null): string {
  return exportFloodStripKml(strip, t ? plssKmlLines(t) : []);
}
