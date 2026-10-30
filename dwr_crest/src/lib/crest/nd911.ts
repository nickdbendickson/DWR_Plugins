import { downstreamDirection, toLocal, type XY } from "./aoi.ts";
import { damLocation } from "./engine.ts";
import { DEPTH_BANDS, exportFloodStripGeoJson, exportFloodStripKml, type FloodStrip } from "./floodstrip.ts";
import { bboxContains, padBbox, plssKmlLines, plssProperties, stripBbox, type Bbox, type PlssTable } from "./plss.ts";
import type { Analysis, DamRecord, ReceptorSeed } from "./types.ts";

/**
 * ND NG911 Site/Structure Address Points near the flood strip (Nick, 2026-10-30: "is it possible to show the 911
 * address points").
 *
 * Source: ND GIS Hub (NDGISHUB, ND Department of Emergency Services) "NDGISHUB Site Structure Address Points" feature
 * service, layer 0, queried on request by the strip's bounding box (padded), WGS 84 out, all pages. Only the fields
 * below are requested; the points that come back are saved on the dam record, so the map, the list and the exports
 * keep working offline afterwards. Nothing statewide is bundled.
 *
 * The points only inform: they never change the desk screen by themselves. "Add as structures for review" turns
 * chosen points into receptor seeds that the user then classifies (habitable or not), and they can be removed again.
 */

export const ND911_LAYER =
  "https://services1.arcgis.com/GOcSXpzwBHyk2nog/arcgis/rest/services/NDGISHUBSiteStructureAddressPoints/FeatureServer/0";
export const ND911_ATTRIBUTION =
  "911 address points: ND GIS Hub NG911 Site/Structure Address Points (State of North Dakota, NDGISHUB), services1.arcgis.com. Public data, provided as is; an address point is not a structure survey.";
export const ND911_ATTRIBUTION_SHORT = "911 points: ND GIS Hub NG911";
export const ND911_OFFLINE =
  "911 address points need a connection: look them up once while online (ND GIS Hub), and they are saved with this dam for offline use.";
export const ND911_PAD_M = 250;
/** The service's maxRecordCount. */
const PAGE = 2000;
const MAX_PAGES = 25;
/** Receptor id prefix for seeds made from address points. */
export const ND911_SEED_PREFIX = "nd911-";

/**
 * Published fields the desk keeps (requested by outFields, and nothing else is read). ADDRESS is the service's full
 * street address; the unit fields and the postal community complete it.
 */
export const ND911_FIELDS = [
  "Site_NGUID",
  "ADDRESS",
  "Building",
  "Floor",
  "Unit",
  "Room",
  "Post_Comm",
  "State",
  "Post_Code",
  "County",
  "Inc_Muni",
  "Uninc_Comm",
  "MSAGComm",
  "Place_Type",
  "Placement",
  "Mile_Post",
  "DiscrpAgID",
  "DateUpdate",
  "SOURCE",
  "ACCURACY",
] as const;

/**
 * Published fields not requested, and why. The layer has no owner or occupant field; the free-text ones are left out
 * because they can carry a person's or business's name.
 */
export const ND911_DROPPED_FIELDS: { field: string; why: string }[] = [
  { field: "LandmkName", why: "free text (landmark name, can name a person or business)" },
  { field: "Addtl_Loc", why: "free text (additional location, can name a person or business)" },
  { field: "NOTES", why: "free text (agency notes)" },
  { field: "AddNum_Pre, Add_Number, AddNum_Suf, St_PreMod, St_PreDir, St_PreTyp, St_PreSep, St_Name, St_PosTyp, St_PosDir, St_PosMod", why: "street components; ADDRESS already holds the full street address" },
  { field: "LSt_PreDir, LSt_Name, LSt_Type, LSt_PosDir, MSAG_ADD", why: "legacy / MSAG duplicates of the address" },
  { field: "Post_Code4, Seat, Nbrhd_Comm, Country, AddCode, AddDataURI, ESN", why: "not needed for the list (ESN is a dispatch zone code)" },
  { field: "Effective, Expire, LOCAL_ID, OBJECTID, OBJECTID_1", why: "record keeping" },
  { field: "Long, Lat, Elev", why: "duplicates of the point geometry (Elev is not shown)" },
];

export type Nd911Point = {
  /** NENA NG911 site id (Site_NGUID). */
  nguid: string;
  /** Full address: ADDRESS, unit parts, postal community, state, ZIP. */
  address: string;
  street: string;
  unit: string;
  community: string;
  state: string;
  zip: string;
  county: string;
  municipality: string;
  msagCommunity: string;
  placeType: string;
  placement: string;
  milePost: string;
  agency: string;
  source: string;
  accuracy: string;
  /** DateUpdate as an ISO date (yyyy-mm-dd), or "". */
  updated: string;
  lon: number;
  lat: number;
};

export type Nd911Cache = {
  source: string;
  attribution: string;
  fetchedAt: string;
  bbox: Bbox;
  fields: string[];
  points: Nd911Point[];
};

export type Nd911Row = {
  order: number;
  nguid: string;
  address: string;
  placeType: string;
  placement: string;
  bandId: string;
  bandLabel: string;
  /** Along the valley from the breach, km (projected on the downstream direction). */
  downstreamKm: number;
  /** Signed offset from the strip's centre line, m (+ to the right looking downstream). */
  offsetM: number;
  lon: number;
  lat: number;
  /** Already added as a receptor seed for review. */
  added: boolean;
};

export type Nd911Table = {
  rows: Nd911Row[];
  /** Points fetched in the box but outside the strip (shown on the map, not listed). */
  outside: Nd911Point[];
  total: number;
  /** What the points were tested against ("flood strip" or "terrain outline"). */
  against: string;
  fetchedAt: string;
  source: string;
  attribution: string;
};

export type Nd911State =
  | { state: "ready"; table: Nd911Table; message: string | null }
  | { state: "lookup"; message: string }
  | { state: "offline"; message: string }
  | { state: "none"; message: string };

const r6 = (n: number) => Math.round(n * 1e6) / 1e6;
const r3 = (n: number) => Math.round(n * 1e3) / 1e3;

export function nd911QueryUrl(bbox: Bbox, offset = 0) {
  const q = new URLSearchParams({
    geometry: bbox.join(","),
    geometryType: "esriGeometryEnvelope",
    inSR: "4326",
    spatialRel: "esriSpatialRelIntersects",
    outFields: ND911_FIELDS.join(","),
    returnGeometry: "true",
    outSR: "4326",
    geometryPrecision: "7",
    orderByFields: "OBJECTID_1",
    resultOffset: String(offset),
    resultRecordCount: String(PAGE),
    f: "geojson",
  });
  return `${ND911_LAYER}/query?${q}`;
}

type Fetch = (url: string, init?: { signal?: AbortSignal }) => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>;

type ServiceFeature = {
  properties?: Record<string, unknown> | null;
  geometry?: { type: string; coordinates: unknown } | null;
};

const clean = (v: unknown) => (v == null ? "" : String(v).trim());

function isoDate(v: unknown) {
  if (typeof v === "number" && Number.isFinite(v)) return new Date(v).toISOString().slice(0, 10);
  const s = clean(v);
  return /^\d{4}-\d{2}-\d{2}/.test(s) ? s.slice(0, 10) : "";
}

/** Service features to cached points: only the kept fields are read (anything else the service sends is ignored). */
export function readPoints(features: ServiceFeature[]): Nd911Point[] {
  const out: Nd911Point[] = [];
  for (const f of features) {
    const g = f.geometry;
    if (!g || g.type !== "Point" || !Array.isArray(g.coordinates)) continue;
    const [lon, lat] = g.coordinates as number[];
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) continue;
    const p = f.properties ?? {};
    const s = (k: (typeof ND911_FIELDS)[number]) => clean(p[k]);
    const unit = [
      s("Building") && `Bldg ${s("Building")}`,
      s("Floor") && `Fl ${s("Floor")}`,
      s("Unit") && `Unit ${s("Unit")}`,
      s("Room") && `Rm ${s("Room")}`,
    ]
      .filter(Boolean)
      .join(" ");
    const street = s("ADDRESS");
    const tail = [s("Post_Comm"), [s("State"), s("Post_Code")].filter(Boolean).join(" ")].filter(Boolean).join(", ");
    const address = [[street, unit].filter(Boolean).join(" "), tail].filter(Boolean).join(", ");
    out.push({
      nguid: s("Site_NGUID"),
      address: address || "(no address)",
      street,
      unit,
      community: s("Post_Comm"),
      state: s("State"),
      zip: s("Post_Code"),
      county: s("County"),
      municipality: s("Inc_Muni"),
      msagCommunity: s("MSAGComm"),
      placeType: s("Place_Type"),
      placement: s("Placement"),
      milePost: s("Mile_Post"),
      agency: s("DiscrpAgID"),
      source: s("SOURCE"),
      accuracy: s("ACCURACY"),
      updated: isoDate(p.DateUpdate),
      lon: r6(lon),
      lat: r6(lat),
    });
  }
  return out;
}

/** Box a lookup covers for `area` (strip or outline): its box grown by ND911_PAD_M. */
export const nd911Bbox = (area: FloodStrip): Bbox => padBbox(stripBbox(area), ND911_PAD_M);

/** One on-request lookup: every address point in the box, all pages. Throws with a readable message. */
export async function fetchNd911(
  area: FloodStrip | Bbox,
  fetchFn: Fetch = fetch as unknown as Fetch,
  now = new Date(),
): Promise<Nd911Cache> {
  const bbox = Array.isArray(area) ? area : nd911Bbox(area);
  const points: Nd911Point[] = [];
  for (let page = 0; page < MAX_PAGES; page++) {
    const ctl = typeof AbortController === "undefined" ? null : new AbortController();
    const timer = ctl ? setTimeout(() => ctl.abort(), 30_000) : null;
    let body: { features?: ServiceFeature[]; exceededTransferLimit?: boolean; properties?: { exceededTransferLimit?: boolean }; error?: { message?: string } };
    try {
      const res = await fetchFn(nd911QueryUrl(bbox, page * PAGE), ctl ? { signal: ctl.signal } : undefined);
      if (!res.ok) throw new Error(`the ND GIS Hub service answered HTTP ${res.status}`);
      body = (await res.json()) as typeof body;
    } catch (e) {
      const msg = (e as Error).name === "AbortError" ? "the ND GIS Hub service did not answer in 30 s" : (e as Error).message;
      throw new Error(`911 address point lookup failed: ${msg}. ${ND911_OFFLINE}`);
    } finally {
      if (timer) clearTimeout(timer);
    }
    if (body.error) throw new Error(`911 address point lookup failed: ${body.error.message ?? "service error"}.`);
    const feats = body.features ?? [];
    points.push(...readPoints(feats));
    const more = body.exceededTransferLimit ?? body.properties?.exceededTransferLimit ?? false;
    if (!more || feats.length === 0) break;
    if (page === MAX_PAGES - 1) throw new Error(`911 address point lookup stopped at ${MAX_PAGES * PAGE} points; draw a smaller area.`);
  }
  const seen = new Set<string>();
  return {
    source: ND911_LAYER,
    attribution: ND911_ATTRIBUTION,
    fetchedAt: now.toISOString(),
    bbox,
    fields: [...ND911_FIELDS],
    points: points.filter((p) => {
      const k = p.nguid || `${p.lon},${p.lat},${p.address}`;
      return seen.has(k) ? false : (seen.add(k), true);
    }),
  };
}

// ---- geometry ------------------------------------------------------------------------------------------------

function inRing(r: number[][], x: number, y: number) {
  let inside = false;
  for (let i = 0, j = r.length - 1; i < r.length; j = i++) {
    const [xi, yi] = r[i];
    const [xj, yj] = r[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** The band of `area` (strip or outline) the point falls in, or null (lon/lat rings, holes respected). */
export function bandAt(area: FloodStrip, lon: number, lat: number): { id: string; label: string } | null {
  let best: { id: string; label: string; rank: number } | null = null;
  const rank = new Map(DEPTH_BANDS.map((b, i) => [b.id, i]));
  for (const f of area.features) {
    const polys = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates;
    for (const p of polys) {
      if (!inRing(p[0], lon, lat)) continue;
      if (p.slice(1).some((h) => inRing(h, lon, lat))) continue;
      const k = rank.get(f.properties.band) ?? 0;
      if (!best || k > best.rank) best = { id: f.properties.band, label: f.properties.band_label, rank: k };
    }
  }
  return best ? { id: best.id, label: best.label } : null;
}

function frame(dam: DamRecord, area: FloodStrip) {
  const loc = damLocation(dam) ?? { lon: area.properties.breach_lon, lat: area.properties.breach_lat };
  const o = { lon: area.properties.breach_lon, lat: area.properties.breach_lat };
  const u = downstreamDirection(dam.geometry, loc).u ?? ([0, -1] as XY);
  // Right of downstream: u rotated clockwise.
  const n: XY = [u[1], -u[0]];
  return { o, u, n };
}

/** Points inside `area`, downstream order, with the band and distance; the rest are kept as `outside`. */
export function nd911Table(dam: DamRecord, area: FloodStrip, cache: Nd911Cache, against = "flood strip"): Nd911Table {
  const { o, u, n } = frame(dam, area);
  const added = new Set(dam.receptors.filter((r) => r.id.startsWith(ND911_SEED_PREFIX)).map((r) => r.id));
  const rows: Omit<Nd911Row, "order">[] = [];
  const outside: Nd911Point[] = [];
  for (const p of cache.points) {
    const band = bandAt(area, p.lon, p.lat);
    if (!band) {
      outside.push(p);
      continue;
    }
    const [x, y] = toLocal(p.lon, p.lat, o);
    rows.push({
      nguid: p.nguid,
      address: p.address,
      placeType: p.placeType,
      placement: p.placement,
      bandId: band.id,
      bandLabel: band.label,
      downstreamKm: r3((x * u[0] + y * u[1]) / 1000),
      offsetM: Math.round(x * n[0] + y * n[1]),
      lon: p.lon,
      lat: p.lat,
      added: added.has(seedId(p.nguid, p.lon, p.lat)),
    });
  }
  rows.sort((a, b) => a.downstreamKm - b.downstreamKm || a.address.localeCompare(b.address));
  return {
    rows: rows.map((r, i) => ({ ...r, order: i + 1 })),
    outside,
    total: cache.points.length,
    against,
    fetchedAt: cache.fetchedAt,
    source: cache.source,
    attribution: cache.attribution,
  };
}

/** What the desk can show for the 911 list right now (same rules as plssState). */
export function nd911State(dam: DamRecord, area: FloodStrip | null, online: boolean, against = "flood strip"): Nd911State {
  if (!area) return { state: "none", message: "No flood strip, so no 911 address points." };
  const cache = dam.nd911;
  if (cache && bboxContains(cache.bbox, stripBbox(area))) {
    return { state: "ready", table: nd911Table(dam, area, cache, against), message: null };
  }
  if (cache) {
    const msg = `The ${against} now reaches past the 911 address points saved with this dam.`;
    return online ? { state: "lookup", message: `${msg} Look them up again.` } : { state: "offline", message: `${msg} ${ND911_OFFLINE}` };
  }
  return online
    ? { state: "lookup", message: "911 address points are not looked up yet for this strip (one query to the ND GIS Hub)." }
    : { state: "offline", message: ND911_OFFLINE };
}

// ---- receptors for review -------------------------------------------------------------------------------------

export function seedId(nguid: string, lon: number, lat: number) {
  const key = nguid.replace(/[{}]/g, "") || `${lon.toFixed(6)}_${lat.toFixed(6)}`;
  return `${ND911_SEED_PREFIX}${key}`;
}

/**
 * A receptor seed for review from one listed point: at its distance downstream, its lateral place across the strip,
 * on the channel bed (pad 0 ft, the desk's deepest case; set the pad once the floor height is known). Kind "house"
 * with `habitable` unset: the screen counts it as habitable and lists it as needing a call until the user classifies
 * it.
 */
export function nd911Seed(row: Nd911Row, result: Pick<Analysis, "flood">): ReceptorSeed {
  const st = result.flood.stations;
  const xKm = Math.max(0, row.downstreamKm);
  let half = st.length ? st[st.length - 1].halfM : 100;
  for (let i = 1; i < st.length; i++) {
    if (st[i].xKm >= xKm) {
      const a = st[i - 1];
      const b = st[i];
      const t = b.xKm > a.xKm ? (xKm - a.xKm) / (b.xKm - a.xKm) : 0;
      half = a.halfM + t * (b.halfM - a.halfM);
      break;
    }
  }
  const lateral = Math.max(-1, Math.min(1, row.offsetM / Math.max(half, 1)));
  return {
    id: seedId(row.nguid, row.lon, row.lat),
    name: `911: ${row.address}`,
    kind: "house",
    station: 0,
    distanceKm: xKm,
    lateral: Math.round(lateral * 100) / 100,
    padFt: 0,
    source: "nd911",
    sourceRef: row.nguid,
  };
}

/** Receptors with the chosen rows added (rows already added are left as they are). */
export function addNd911Seeds(receptors: ReceptorSeed[], rows: Nd911Row[], result: Pick<Analysis, "flood">): ReceptorSeed[] {
  const have = new Set(receptors.map((r) => r.id));
  const add = rows.map((r) => nd911Seed(r, result)).filter((s) => !have.has(s.id) && (have.add(s.id), true));
  return [...receptors, ...add];
}

/** Receptors without the 911-made seeds (all, or just `ids`). Hand-made seeds are never touched. */
export function removeNd911Seeds(receptors: ReceptorSeed[], ids?: string[]): ReceptorSeed[] {
  const only = ids ? new Set(ids) : null;
  return receptors.filter((r) => !(r.source === "nd911" && (!only || only.has(r.id))));
}

// ---- exports ------------------------------------------------------------------------------------------------

const csvCell = (v: string | number) => {
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

/** <root>_911_address_points.csv: the points inside, downstream order, with the source on every row. */
export function exportNd911Csv(t: Nd911Table, cache: Nd911Cache): string {
  const byId = new Map(cache.points.map((p) => [p.nguid, p]));
  const head = [
    "order",
    "address",
    "depth_band",
    "downstream_km",
    "offset_m",
    "place_type",
    "placement",
    "county",
    "municipality",
    "msag_community",
    "site_nguid",
    "agency",
    "updated",
    "lon",
    "lat",
    "added_for_review",
    "tested_against",
    "source",
  ];
  const src = `${t.attribution} Fetched ${t.fetchedAt}.`;
  const lines = [head.join(",")];
  for (const r of t.rows) {
    const p = byId.get(r.nguid);
    lines.push(
      [
        r.order,
        r.address,
        r.bandLabel,
        r.downstreamKm.toFixed(3),
        r.offsetM,
        r.placeType,
        r.placement,
        p?.county ?? "",
        p?.municipality ?? "",
        p?.msagCommunity ?? "",
        r.nguid,
        p?.agency ?? "",
        p?.updated ?? "",
        r.lon.toFixed(6),
        r.lat.toFixed(6),
        r.added ? "yes" : "no",
        t.against,
        src,
      ]
        .map(csvCell)
        .join(","),
    );
  }
  return lines.join("\n") + "\n";
}

/** Collection properties (foreign members) for the strip GeoJSON. */
export function nd911Properties(t: Nd911Table) {
  return {
    nd911_attribution: t.attribution,
    nd911_source: t.source,
    nd911_fetched_at: t.fetchedAt,
    nd911_tested_against: t.against,
    nd911_points_inside: t.rows.length,
    nd911_points_in_box: t.total,
  };
}

/** The listed points as GeoJSON Point features (kind "nd911_address_point"), appended after the band polygons. */
export function nd911Features(t: Nd911Table) {
  return t.rows.map((r) => ({
    type: "Feature" as const,
    properties: {
      kind: "nd911_address_point",
      order: r.order,
      address: r.address,
      depth_band: r.bandLabel,
      downstream_km: r.downstreamKm,
      offset_m: r.offsetM,
      place_type: r.placeType,
      placement: r.placement,
      site_nguid: r.nguid,
      added_for_review: r.added,
      source: ND911_ATTRIBUTION_SHORT,
    },
    geometry: { type: "Point" as const, coordinates: [r.lon, r.lat] },
  }));
}

export function nd911KmlLines(t: Nd911Table): string[] {
  return [
    "",
    `911 address points inside the ${t.against} (${t.rows.length} of ${t.total} in the box, downstream order):`,
    ...t.rows.map((r) => `${r.order}. ${r.address}: ${r.bandLabel}, ${r.downstreamKm.toFixed(2)} km downstream`),
    `${t.attribution} Fetched ${t.fetchedAt}.`,
  ];
}

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

/** KML placemarks for the listed points (a folder), for exportFloodStripKml's `placemarks`. */
export function nd911KmlPlacemarks(t: Nd911Table): string[] {
  if (t.rows.length === 0) return [];
  return [
    `<Folder><name>${esc(`911 address points inside the ${t.against} (${t.rows.length})`)}</name>`,
    `<description>${esc(t.attribution)}</description>`,
    ...t.rows.map((r) =>
      [
        "<Placemark>",
        `<name>${esc(r.address)}</name>`,
        `<description>${esc(`${r.bandLabel}; ${r.downstreamKm.toFixed(2)} km downstream of the breach; place type ${r.placeType || "not given"}. ${ND911_ATTRIBUTION_SHORT}.`)}</description>`,
        `<ExtendedData><Data name="site_nguid"><value>${esc(r.nguid)}</value></Data><Data name="depth_band"><value>${esc(r.bandLabel)}</value></Data><Data name="downstream_km"><value>${r.downstreamKm}</value></Data></ExtendedData>`,
        `<Point><coordinates>${r.lon},${r.lat}</coordinates></Point>`,
        "</Placemark>",
      ].join(""),
    ),
    "</Folder>",
  ];
}

/**
 * The flood-strip (or outline) GeoJSON with the PLSS list and the 911 summary as collection properties, and the
 * listed 911 points as Point features after the band polygons. With neither, the plain strip export.
 */
export function exportFloodGeoJson(area: FloodStrip, plss: PlssTable | null, nd911: Nd911Table | null): string {
  if (!plss && !nd911) return exportFloodStripGeoJson(area);
  return exportFloodStripGeoJson({
    ...area,
    properties: { ...area.properties, ...(plss ? plssProperties(plss) : {}), ...(nd911 ? nd911Properties(nd911) : {}) },
    features: [...area.features, ...(nd911 ? nd911Features(nd911) : [])],
  } as unknown as FloodStrip);
}

export function exportFloodKml(area: FloodStrip, plss: PlssTable | null, nd911: Nd911Table | null): string {
  return exportFloodStripKml(
    area,
    [...(plss ? plssKmlLines(plss) : []), ...(nd911 ? nd911KmlLines(nd911) : [])],
    nd911 ? nd911KmlPlacemarks(nd911) : [],
  );
}
