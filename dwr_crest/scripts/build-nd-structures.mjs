#!/usr/bin/env node
/**
 * Build src/lib/crest/data/nd_structures.json: every structure the batch screen can pick (Nick, 2026-11-05, "select
 * multiple dams, county or watershed, or state ... with DWR dams, dugouts, unknown structures").
 *
 *   npm run data:nd-structures -- --from data-raw/dwr_structures_<stamp> [--pulled <UTC>] [--out <file>]
 *
 * The folder holds Dams.csv and Dugouts.csv: the full DWR Dams and Dugouts layers from the mapservice.dwr.nd.gov
 * download form (whichextent=all, srid=4269, shapefile), converted with `ogr2ogr -f CSV Dams.csv Dams.shp` (the
 * attribute table; latitude / longitude are fields), plus raw/SHA256SUMS.txt or SHA256SUMS.txt for the zips.
 * Optionally Unknown.csv: DWR's Unknown Structures (not on the map service; Nick exported unknown_structures.gpkg
 * from DWR's database, 2026-11-05), only the columns read here:
 *   ogr2ogr -f CSV Unknown.csv unknown_structures.gpkg -sql "SELECT structure_id, national_id, status, county,
 *     latitude, longitude, max_dam_height, max_pool_vol, max_pool_area, dam_length, year_built, fed_hazard_class,
 *     purpose FROM unknown_structures"
 * Keep it in data-raw/ (git-ignored).
 *
 * What goes in (numbers, county, status, watershed names; never owner, name, designer, remark or permit fields):
 *   nidHuc   the HUC8 watershed of each NID main dam in nd_dams.json
 *   rows     every DWR Dams record that is NOT an NID main dam (the NID entry stands for it), and every DWR Dugouts
 *            record, keyed "DWR-<structure>" / "DUG-<structure>", except status Removed / Not Built and records with
 *            no location in North Dakota
 *            Unknown Structures: most (about 3,600 of 3,770 in the 2026-11-05 export) sit on exactly the point of a
 *            DWR Dams record with the same height and storage, under a new DWR number: the structure was moved into
 *            the Dams layer (a few sit on an NID main dam, within 5 m). Those are not listed twice; the UNK- number becomes an alias on that record ("alias",
 *            so a search for UNK-2 finds it; nidHuc "alias" for an NID dam). The rest are listed as kind "unknown", "UNK-<structure_id>", status
 *            as DWR has it (blank = "Unknown").
 * Watersheds are USGS Watershed Boundary Dataset HUC8 units ("Name (code)"), found by point-in-polygon against the
 * WBD HUC8 polygons over North Dakota (fetched once and cached in the folder as wbd_huc8.geojson). DWR's own
 * hu_sub_bas names are NOT used: they mix two WBD vintages ("Maple" and "Maple River" for one basin), so a watershed
 * pick would split.
 * Nothing here converts datums or reads elevations: the batch screen takes the crest from 3DEP (batch.ts).
 */
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { EXCLUDED, FORM_URL, parseCsv } from "./build-nd-dams-dwr.mjs";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
export const DEFAULT_OUT = join(ROOT, "src/lib/crest/data/nd_structures.json");
export const NID_BUNDLE = join(ROOT, "src/lib/crest/data/nd_dams.json");
export const WBD_URL = "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer";
export const WBD_HUC8_QUERY = `${WBD_URL}/4/query?geometry=-104.2,45.9,-96.5,49.1&geometryType=esriGeometryEnvelope&inSR=4326&spatialRel=esriSpatialRelIntersects&outFields=huc8,name&returnGeometry=true&outSR=4326&maxAllowableOffset=0.002&f=geojson`;
export const ATTRIBUTION =
  "Structures: North Dakota Department of Water Resources, Dams and Dugouts layers (mapservice.dwr.nd.gov), downloaded {date}{unknown}. Provisional, provided 'as is'. Watersheds: USGS Watershed Boundary Dataset HUC8.";
/** Statuses never listed: there is nothing standing to breach. */
export const DROP_STATUS = new Set(["Removed", "Not Built"]);
export const COLS = ["id", "kind", "nid", "status", "county", "lat", "lon", "heightFt", "storageAcFt", "areaAc", "lengthFt", "year", "hazard", "purpose", "huc8", "alias"];
export const NID_HUC_COLS = ["nid", "huc8", "alias"];

const numv = (v) => {
  if (v == null || String(v).trim() === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};
const pos = (v) => {
  const n = numv(v);
  return n != null && n > 0 ? n : null;
};
const txt = (v) => (v == null ? null : String(v).trim() || null);
const r5 = (v) => Math.round(v * 1e5) / 1e5;
const r1 = (v) => (v == null ? null : Math.round(v * 10) / 10);
const inNd = (lat, lon) => lat != null && lon != null && lat >= 45.9 && lat <= 49.1 && lon >= -104.2 && lon <= -96.5;
const year = (v) => {
  const y = Number(String(v ?? "").slice(0, 4));
  return y >= 1800 && y <= 2100 ? y : null;
};

/** Ray-casting point-in-ring. */
function inRing(x, y, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** A HUC8 lookup from WBD HUC8 GeoJSON: (lon, lat) => "Name (code)" or null. Holes respected; bounding boxes first. */
export function huc8Lookup(geojson) {
  const units = geojson.features.map((f) => {
    const polys = f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates;
    let b = [Infinity, Infinity, -Infinity, -Infinity];
    for (const p of polys) for (const [x, y] of p[0]) b = [Math.min(b[0], x), Math.min(b[1], y), Math.max(b[2], x), Math.max(b[3], y)];
    return { label: `${f.properties.name} (${f.properties.huc8})`, polys, b };
  });
  return (lon, lat) => {
    for (const u of units) {
      if (lon < u.b[0] || lon > u.b[2] || lat < u.b[1] || lat > u.b[3]) continue;
      if (u.polys.some((p) => inRing(lon, lat, p[0]) && !p.slice(1).some((h) => inRing(lon, lat, h)))) return u.label;
    }
    return null;
  };
}

/**
 * The bundle from the DWR tables (arrays of CSV row objects), the NID main dams ([{ nid, lat, lon }]) and a HUC8 lookup
 * (opts.huc8: (lon, lat) => label or null; huc8Lookup).
 */
export async function buildStructures({ dams, dugouts, unknown = [] }, nid, opts = {}) {
  const mainIds = new Set(nid.map((d) => d.nid));
  const huc = opts.huc8 ?? (() => null);
  let noHuc = 0;
  const hucOf = (lon, lat) => {
    const h = lon != null && lat != null ? huc(lon, lat) : null;
    if (!h) noHuc++;
    return h;
  };
  const nidHuc = nid.map((d) => [d.nid, hucOf(d.lon, d.lat), null]);
  /** An Unknown Structure within this many metres of an NID main dam is that dam. */
  const NID_SAME_M = 5;

  const rows = [];
  const counts = { dams: 0, dugouts: 0, unknown: 0, unknownAliased: 0, droppedStatus: 0, noLocation: 0, nidMain: 0 };
  const status = {};
  for (const r of dams) {
    const sid = Number(r.structure_);
    if (!sid) continue;
    const id = txt(r.national_i);
    if (id && mainIds.has(id)) {
      counts.nidMain++;
      continue;
    }
    const st = txt(r.status) ?? "Unknown";
    if (DROP_STATUS.has(st)) {
      counts.droppedStatus++;
      continue;
    }
    const lat = numv(r.latitude);
    const lon = numv(r.longitude);
    if (!inNd(lat, lon)) {
      counts.noLocation++;
      continue;
    }
    status[st] = (status[st] ?? 0) + 1;
    counts.dams++;
    rows.push({
      id: `DWR-${sid}`,
      kind: "dam",
      nid: id,
      status: st,
      county: txt(r.county),
      lat: r5(lat),
      lon: r5(lon),
      heightFt: r1(pos(r.max_dam_he)),
      storageAcFt: r1(pos(r.max_pool_v)),
      areaAc: r1(pos(r.max_pool_a)),
      lengthFt: r1(pos(r.dam_length)),
      year: year(r.year_built),
      hazard: txt(r.hazard_cla),
      purpose: txt(r.purpose),
      huc8: hucOf(lon, lat),
    });
  }
  for (const r of dugouts) {
    const sid = Number(r.structure_);
    if (!sid) continue;
    const lat = numv(r.latitude);
    const lon = numv(r.longitude);
    if (!inNd(lat, lon)) {
      counts.noLocation++;
      continue;
    }
    counts.dugouts++;
    rows.push({
      id: `DUG-${sid}`,
      kind: "dugout",
      nid: txt(r.national_i),
      status: "Dugout",
      county: txt(r.county),
      lat: r5(lat),
      lon: r5(lon),
      heightFt: r1(pos(r.max_height)),
      storageAcFt: r1(pos(r.max_storag)),
      areaAc: r1(pos(r.max_surfac)),
      lengthFt: r1(pos(r.total_leng)),
      year: null,
      hazard: txt(r.fed_hazard),
      purpose: txt(r.purpose),
      huc8: hucOf(lon, lat),
    });
  }
  // Unknown Structures: an alias on the DWR record at the same point (same structure, new number), else a row.
  const at = new Map();
  for (const r of rows) {
    const k = `${r.lat},${r.lon}`;
    at.set(k, [...(at.get(k) ?? []), r]);
  }
  for (const r of unknown) {
    const sid = Number(r.structure_id);
    if (!sid) continue;
    const st = txt(r.status) ?? "Unknown";
    if (DROP_STATUS.has(st)) {
      counts.droppedStatus++;
      continue;
    }
    const lat = numv(r.latitude);
    const lon = numv(r.longitude);
    if (!inNd(lat, lon)) {
      counts.noLocation++;
      continue;
    }
    const h = r1(pos(r.max_dam_height));
    const v = r1(pos(r.max_pool_vol));
    const here = at.get(`${r5(lat)},${r5(lon)}`);
    if (here?.length) {
      // Several DWR records can share a point: take the one with the same height and storage, else the first.
      const same = here.find((x) => x.heightFt === h && x.storageAcFt === v) ?? here.find((x) => x.storageAcFt === v) ?? here[0];
      same.alias = same.alias ? `${same.alias} UNK-${sid}` : `UNK-${sid}`;
      counts.unknownAliased++;
      continue;
    }
    const kx = 111320 * Math.cos((lat * Math.PI) / 180);
    const main = nid.findIndex((d) => d.lat != null && d.lon != null && Math.hypot((d.lat - lat) * 110574, (d.lon - lon) * kx) <= NID_SAME_M);
    if (main >= 0) {
      nidHuc[main][2] = nidHuc[main][2] ? `${nidHuc[main][2]} UNK-${sid}` : `UNK-${sid}`;
      counts.unknownAliased++;
      continue;
    }
    status[st] = (status[st] ?? 0) + 1;
    counts.unknown++;
    rows.push({
      id: `UNK-${sid}`,
      kind: "unknown",
      nid: txt(r.national_id),
      status: st,
      county: txt(r.county),
      lat: r5(lat),
      lon: r5(lon),
      heightFt: h,
      storageAcFt: v,
      areaAc: r1(pos(r.max_pool_area)),
      lengthFt: r1(pos(r.dam_length)),
      year: year(r.year_built),
      hazard: txt(r.fed_hazard_class),
      purpose: txt(r.purpose),
      huc8: hucOf(lon, lat),
    });
  }
  rows.sort((a, b) => a.id.localeCompare(b.id, "en", { numeric: true }));
  const pulledUtc = opts.pulledUtc ?? new Date().toISOString().replace(/\.\d+Z$/, "Z");
  return {
    v: 1,
    source: {
      formUrl: FORM_URL,
      layers: `Dams (layer 254, qlayer Dams) and Dugouts (layer 277, qlayer Dugouts), full data set${unknown.length ? "; Unknown Structures from DWR's database (export, not on the map service)" : ""}`,
      wbd: WBD_URL,
      pulledUtc,
      sha256: opts.sha256 ?? {},
      records: { dams: dams.length, dugouts: dugouts.length, unknown: unknown.length },
      counts: { ...counts, noHuc8: noHuc },
      status: Object.fromEntries(Object.entries(status).sort(([a], [b]) => a.localeCompare(b))),
      excludedFields: EXCLUDED,
      attribution: ATTRIBUTION.replace("{date}", pulledUtc.slice(0, 10)).replace("{unknown}", unknown.length ? ", and Unknown Structures (DWR export)" : ""),
    },
    nidHuc: { cols: NID_HUC_COLS, rows: nidHuc },
    cols: COLS,
    rows: rows.map((r) => COLS.map((c) => r[c] ?? null)),
  };
}

function readSums(file) {
  if (!existsSync(file)) return undefined;
  return Object.fromEntries(
    readFileSync(file, "utf8")
      .split(/\r?\n/)
      .map((l) => l.trim().split(/\s+/))
      .filter((p) => p.length === 2 && /^[0-9a-f]{64}$/.test(p[0]))
      .map(([h, n]) => [n, h]),
  );
}

async function main(argv) {
  const arg = (k) => {
    const i = argv.indexOf(k);
    return i >= 0 ? argv[i + 1] : undefined;
  };
  const from = arg("--from");
  if (!from) throw new Error("--from <folder with Dams.csv and Dugouts.csv> is required");
  const damsFile = join(from, "Dams.csv");
  const damsCsv = readFileSync(damsFile, "utf8");
  const dugCsv = readFileSync(join(from, "Dugouts.csv"), "utf8");
  const unkFile = join(from, "Unknown.csv");
  const unkCsv = existsSync(unkFile) ? readFileSync(unkFile, "utf8") : null;
  const nb = JSON.parse(readFileSync(NID_BUNDLE, "utf8"));
  const ix = (c) => nb.cols.indexOf(c);
  const nid = nb.rows.map((r) => ({ nid: r[ix("nid")], lat: r[ix("lat")], lon: r[ix("lon")] }));
  const wbdFile = join(from, "wbd_huc8.geojson");
  if (!existsSync(wbdFile)) {
    const res = await fetch(WBD_HUC8_QUERY);
    if (!res.ok) throw new Error(`WBD HUC8 download failed: HTTP ${res.status}`);
    writeFileSync(wbdFile, await res.text());
  }
  const wbdText = readFileSync(wbdFile, "utf8");
  const huc8 = huc8Lookup(JSON.parse(wbdText));
  const sha = (t) => createHash("sha256").update(t).digest("hex");
  const bundle = await buildStructures({ dams: parseCsv(damsCsv).rows, dugouts: parseCsv(dugCsv).rows, unknown: unkCsv ? parseCsv(unkCsv).rows : [] }, nid, {
    huc8,
    pulledUtc: arg("--pulled") ?? statSync(damsFile).mtime.toISOString().replace(/\.\d+Z$/, "Z"),
    sha256: { ...(readSums(join(from, "raw", "SHA256SUMS.txt")) ?? readSums(join(from, "SHA256SUMS.txt")) ?? {}), "Dams.csv": sha(damsCsv), "Dugouts.csv": sha(dugCsv), ...(unkCsv ? { "Unknown.csv": sha(unkCsv) } : {}), "wbd_huc8.geojson": sha(wbdText) },
  });
  const out = arg("--out") ?? DEFAULT_OUT;
  mkdirSync(dirname(out), { recursive: true });
  writeFileSync(out, JSON.stringify(bundle));
  const s = bundle.source;
  console.log(
    `[nd-structures] ${s.counts.dams} DWR dams + ${s.counts.dugouts} dugouts + ${s.counts.unknown} unknown structures (${s.counts.unknownAliased} more are DWR or NID dams already, kept as aliases) (skipped: ${s.counts.nidMain} NID main dams, ${s.counts.droppedStatus} removed/not built, ${s.counts.noLocation} no location); no HUC8 ${s.counts.noHuc8}; status ${JSON.stringify(s.status)} -> ${out}`,
  );
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main(process.argv.slice(2)).catch((e) => {
    console.error(e instanceof Error ? e.stack : String(e));
    process.exit(1);
  });
}
