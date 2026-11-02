#!/usr/bin/env node
/**
 * Score a Crest flood layer against a model run (the validation harness, src/lib/crest/validation.ts), from the
 * terminal. Run by hand; never chained into build, dev or test.
 *
 *   node --experimental-strip-types scripts/validate-crest.mjs \
 *     --runner <dam folder | zip>          the ND Dam Breach Runner's results (lisflood_*.tif, <run>.max/.Vmax/.inittm, dam.json)
 *     --crest <flood.geojson> [...]        one or more Crest layers: the flood strip or terrain outline export
 *     [--receptors <points.csv|.geojson>]  911 points or receptors (CSV with lon,lat columns, or GeoJSON Points)
 *     [--breach lon,lat] [--bearing deg]   breach location and downstream bearing (else from the GeoJSON's properties)
 *     [--crs EPSG:xxxxx]                   CRS of ASCII grids without one (default: dam.json's, else EPSG:32614)
 *     [--kind house|mobile]                TM 11 chart for the receptor zones (default house)
 *     [--json <out.json>]                  also write the full report (every receptor row)
 *
 * The flood strip and terrain outline GeoJSON carry breach_lon / breach_lat and the direction note ("bearing 130°"),
 * so --breach and --bearing are only needed for other layers. With a bearing, only cells downstream of the breach
 * count, as the Results step's compare does.
 */
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { basename, extname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { createEngine } from "../src/lib/crest/raster-engine.ts";
import { formatReport, validate } from "../src/lib/crest/validation.ts";

export function parseArgs(argv) {
  const o = { crest: [], crs: "auto", kind: "house" };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const v = () => {
      if (i + 1 >= argv.length) throw new Error(`${a} needs a value`);
      return argv[++i];
    };
    if (a === "--runner") o.runner = v();
    else if (a === "--crest") o.crest.push(v());
    else if (a === "--receptors") o.receptors = v();
    else if (a === "--breach") {
      const [lon, lat] = v().split(",").map(Number);
      if (!Number.isFinite(lon) || !Number.isFinite(lat)) throw new Error("--breach needs lon,lat");
      o.breach = { lon, lat };
    } else if (a === "--bearing") o.bearing = Number(v());
    else if (a === "--crs") o.crs = v();
    else if (a === "--kind") o.kind = v();
    else if (a === "--json") o.json = v();
    else if (a === "--label") (o.labels ??= []).push(v());
    else throw new Error(`unknown option ${a}`);
  }
  if (!o.runner) throw new Error("--runner is required");
  if (!o.crest.length) throw new Error("at least one --crest is required");
  return o;
}

/** Every file under a folder (recursively), or the zip itself, as the engine's InFile[]. */
export function runnerFiles(path) {
  const p = resolve(path);
  if (!existsSync(p)) throw new Error(`${path} does not exist`);
  const toBuf = (f) => {
    const b = readFileSync(f);
    return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength);
  };
  if (statSync(p).isFile()) return [{ name: basename(p), buf: toBuf(p) }];
  const out = [];
  const walk = (dir, rel) => {
    for (const e of readdirSync(dir, { withFileTypes: true })) {
      const full = join(dir, e.name);
      const r = rel ? `${rel}/${e.name}` : e.name;
      if (e.isDirectory()) walk(full, r);
      else if (e.isFile() && statSync(full).size > 0) out.push({ name: r, buf: toBuf(full) });
    }
  };
  walk(p, "");
  return out;
}

/** The unit vector (east, north) of a compass bearing in degrees. */
export const bearingVector = (deg) => [Math.sin((deg * Math.PI) / 180), Math.cos((deg * Math.PI) / 180)];

/** Breach and bearing from a flood-strip / terrain-outline GeoJSON's properties, where present. */
export function layerFrame(fc) {
  const p = fc?.properties ?? {};
  const lon = Number(p.breach_lon);
  const lat = Number(p.breach_lat);
  const m = /bearing\s+(-?\d+(?:\.\d+)?)\s*°/.exec(String(p.direction ?? ""));
  return {
    breach: Number.isFinite(lon) && Number.isFinite(lat) ? { lon, lat } : null,
    bearing: m ? Number(m[1]) : null,
    label: typeof p.title === "string" && p.title ? p.title : fc?.features?.[0]?.properties?.kind === "terrain_outline" ? "terrain outline" : "flood strip",
  };
}

/** Points from a CSV (lon/lat columns; id from order, site_nguid, address or the row number) or GeoJSON Points. */
export function readPoints(path) {
  const text = readFileSync(path, "utf8");
  if (extname(path).toLowerCase() === ".geojson" || extname(path).toLowerCase() === ".json") {
    const fc = JSON.parse(text);
    return (fc.features ?? [])
      .filter((f) => f.geometry?.type === "Point")
      .map((f, i) => ({ id: String(f.properties?.id ?? f.properties?.order ?? f.properties?.site_nguid ?? i + 1), lon: f.geometry.coordinates[0], lat: f.geometry.coordinates[1] }));
  }
  const rows = parseCsv(text);
  if (rows.length < 2) return [];
  const head = rows[0].map((h) => h.trim().toLowerCase());
  const col = (...names) => names.map((n) => head.indexOf(n)).find((i) => i >= 0) ?? -1;
  const iLon = col("lon", "longitude", "x");
  const iLat = col("lat", "latitude", "y");
  if (iLon < 0 || iLat < 0) throw new Error(`${path}: no lon/lat columns (have ${head.join(", ")})`);
  const iId = col("id", "order", "site_nguid", "address");
  const out = [];
  rows.slice(1).forEach((r, k) => {
    const lon = Number(r[iLon]);
    const lat = Number(r[iLat]);
    if (!Number.isFinite(lon) || !Number.isFinite(lat)) return;
    out.push({ id: iId >= 0 && r[iId] ? r[iId] : String(k + 1), lon, lat });
  });
  return out;
}

/** RFC 4180 CSV: quoted fields, doubled quotes, CR/LF line ends. */
export function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = "";
  let q = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (q) {
      if (ch === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i++;
        } else q = false;
      } else field += ch;
    } else if (ch === '"') q = true;
    else if (ch === ",") {
      row.push(field);
      field = "";
    } else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else field += ch;
  }
  if (field.length || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows.filter((r) => r.length > 1 || (r.length === 1 && r[0] !== ""));
}

export async function main(argv, log = console.log) {
  const o = parseArgs(argv);
  const layers = o.crest.map((f, i) => {
    const fc = JSON.parse(readFileSync(f, "utf8"));
    if (fc?.type !== "FeatureCollection") throw new Error(`${f} is not a GeoJSON FeatureCollection`);
    const fr = layerFrame(fc);
    return { file: f, fc, ...fr, label: o.labels?.[i] ?? fr.label };
  });
  const breach = o.breach ?? layers.map((l) => l.breach).find(Boolean) ?? null;
  const bearing = o.bearing ?? layers.map((l) => l.bearing).find((b) => b != null) ?? null;
  const u = bearing == null ? null : bearingVector(bearing);
  const downstream = breach && u ? { ...breach, u } : null;
  const points = o.receptors ? readPoints(o.receptors) : [];

  const engine = createEngine();
  const files = runnerFiles(o.runner);
  const summary = await engine.handle(
    { type: "import", key: "ref", source: "lisflood", files, crs: o.crs, ctx: breach ? { breach, u, reachKm: 0 } : null },
    (t) => log(`  ${t}`),
  );
  const ref = engine.get("ref");
  log(`Model: ${summary.label}, ${summary.width} × ${summary.height} cells of ${summary.cellM} m (${summary.crs}), read ${summary.used.map((n) => basename(n)).join(", ")}`);
  for (const c of summary.checks) if (c.level === "warn") log(`  warning: ${c.text}`);
  if (summary.stats) log(`  wet downstream ${summary.stats.wetAc.toFixed(0)} ac, peak depth ${summary.stats.peakDepthFt.toFixed(1)} ft, farthest ${summary.stats.farthestKm.toFixed(1)} km`);
  if (!downstream) log("  No downstream bearing: every cell counts, the reservoir included. Pass --breach and --bearing to count only downstream cells.");

  const reports = layers.map((l) =>
    validate({ kind: "polygons", features: l.fc.features ?? [] }, ref, {
      label: l.label,
      reference: summary.label,
      points,
      downstream,
      kind: o.kind,
    }),
  );
  for (const r of reports) log("\n" + formatReport(r));
  if (o.json) {
    writeFileSync(o.json, JSON.stringify({ runner: o.runner, model: summary, breach, bearing, reports }, null, 2));
    log(`\nWrote ${o.json}`);
  }
  return reports;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).catch((e) => {
    console.error(e instanceof Error ? e.message : String(e));
    process.exit(1);
  });
}
