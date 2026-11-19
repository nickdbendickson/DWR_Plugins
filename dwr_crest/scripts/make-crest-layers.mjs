#!/usr/bin/env node
/**
 * Build a Crest desk flood strip and terrain outline for a dam, straight from its inventory record and 3DEP, so the
 * validation harness can score them against a model run. One-off tool for the four-dam baseline (Nick, 2026-11-01);
 * not part of the app.
 *
 * Elevations and the valley axis are derived from 3DEP where the record has none, and the geometry (dam line,
 * reservoir point) is synthesized from the DEM's local downhill. That is labelled on the output; it is a stand-in
 * for a real surveyed record, good enough to compare where the flood goes and how big it is.
 *
 *   node --experimental-strip-types scripts/make-crest-layers.mjs --out <dir> \
 *     --id DWR-2801 --nid ND00199 --name "..." --lon -97.39 --lat 46.98 \
 *     --height 27.5 --length 1700 --storage 1615.5 --area 154.5 [--crest 1071.09 --streambed 1043.59 --datum NAVD88]
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { analyze, damLocation } from "../src/lib/crest/engine.ts";
import { floodStrip } from "../src/lib/crest/floodstrip.ts";
import { buildOutline, demRequest, fetchDem, outlineInputs } from "../src/lib/crest/outline.ts";

const M_PER_DEG_LAT = 110574;
const kx = (lat) => 111320 * Math.cos((lat * Math.PI) / 180);

export function parseArgs(argv) {
  const o = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith("--")) throw new Error(`unexpected ${a}`);
    o[a.slice(2)] = argv[++i];
  }
  for (const k of ["out", "id", "lon", "lat", "height"]) if (o[k] == null) throw new Error(`--${k} is required`);
  return o;
}

/** A small 3DEP DEM (metres NAVD88) around a point, through Crest's own fetchDem. */
async function demAround(lon, lat, halfKmLat, halfKmLon, cellM = 10) {
  const dLat = (halfKmLat * 1000) / M_PER_DEG_LAT;
  const dLon = (halfKmLon * 1000) / kx(lat);
  const bbox = [lon - dLon, lat - dLat, lon + dLon, lat + dLat];
  const width = Math.max(16, Math.round((2 * dLon * kx(lat)) / cellM));
  const height = Math.max(16, Math.round((2 * dLat * M_PER_DEG_LAT) / cellM));
  return fetchDem({ bbox, width, height, cellM });
}

function demAt(g, lon, lat) {
  const fx = ((lon - g.bbox[0]) / (g.bbox[2] - g.bbox[0])) * g.width;
  const fy = ((g.bbox[3] - lat) / (g.bbox[3] - g.bbox[1])) * g.height;
  const i = Math.max(0, Math.min(g.width - 1, Math.floor(fx)));
  const j = Math.max(0, Math.min(g.height - 1, Math.floor(fy)));
  const z = g.z[j * g.width + i];
  return Number.isFinite(z) && z > -9000 ? z : null;
}

/** Downhill bearing (unit east/north) from the dam point: toward the lowest DEM cell within `rM` metres. */
function downhill(g, lon, lat, rM = 500) {
  let best = Infinity;
  let bx = 0;
  let by = 0;
  const k = kx(lat);
  for (let j = 0; j < g.height; j++) {
    const clat = g.bbox[3] - ((j + 0.5) / g.height) * (g.bbox[3] - g.bbox[1]);
    for (let i = 0; i < g.width; i++) {
      const clon = g.bbox[0] + ((i + 0.5) / g.width) * (g.bbox[2] - g.bbox[0]);
      const e = (clon - lon) * k;
      const n = (clat - lat) * M_PER_DEG_LAT;
      if (e * e + n * n > rM * rM) continue;
      const z = g.z[j * g.width + i];
      if (Number.isFinite(z) && z > -9000 && z < best) {
        best = z;
        bx = e;
        by = n;
      }
    }
  }
  const L = Math.hypot(bx, by) || 1;
  return [bx / L, by / L];
}

export async function main(argv, log = console.log) {
  const o = parseArgs(argv);
  const lon = Number(o.lon);
  const lat = Number(o.lat);
  const heightFt = Number(o.height);
  mkdirSync(o.out, { recursive: true });

  log(`Fetching 3DEP around ${o.id}…`);
  const near = await demAround(lon, lat, 0.6, 0.6, 10);
  const groundM = demAt(near, lon, lat);
  const FT = 0.3048;
  const crestFt = o.crest != null ? Number(o.crest) : groundM != null ? groundM / FT : null;
  if (crestFt == null) throw new Error("could not read a crest elevation from 3DEP");
  const streambedFt = o.streambed != null ? Number(o.streambed) : crestFt - heightFt;
  const u = downhill(near, lon, lat, 500);
  const bearing = ((Math.atan2(u[0], u[1]) * 180) / Math.PI + 360) % 360;
  // Dam line perpendicular to the downhill axis, reservoir point stepped uphill.
  const lenFt = Number(o.length ?? 1200);
  const halfM = (lenFt * FT) / 2;
  const perp = [-u[1], u[0]];
  const toLL = (e, n) => [lon + e / kx(lat), lat + n / M_PER_DEG_LAT];
  const damLine = [toLL(perp[0] * halfM, perp[1] * halfM), toLL(-perp[0] * halfM, -perp[1] * halfM)];
  const reservoir = toLL(-u[0] * 150, -u[1] * 150);

  const dam = {
    id: o.id,
    name: o.name ?? o.id,
    nid: o.nid ?? "",
    county: "",
    year: 0,
    heightFt,
    lengthFt: lenFt,
    crestFt,
    streambedFt,
    spillwayFt: crestFt,
    normalPoolFt: crestFt - Math.min(heightFt * 0.3, 5),
    storageAcFt: Number(o.storage ?? 0),
    drainageMi2: 0,
    statedDatum: o.datum ?? "NAVD88",
    demCrestFt: crestFt,
    failureMode: "piping",
    method: "froehlich",
    engine: "lisflood",
    lat,
    lon,
    surfaceAreaAc: o.area != null ? Number(o.area) : undefined,
    receptors: [],
    geometry: {
      reservoirPoint: { type: "Feature", properties: {}, geometry: { type: "Point", coordinates: reservoir } },
      damLine: { type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: damLine } },
    },
  };

  const result = analyze(dam);
  if (!damLocation(dam)) throw new Error("no dam location");
  const fs = floodStrip(dam, result);
  if (!fs.strip) throw new Error(`no flood strip: ${fs.reason}`);
  const note = `Auto-derived record: crest ${o.crest != null ? "(given)" : "from 3DEP at the dam point"} ${crestFt.toFixed(1)} ft, streambed ${streambedFt.toFixed(1)} ft, downstream bearing ${bearing.toFixed(0)}° from the DEM downhill, dam line synthesized. A stand-in for a surveyed record.`;
  fs.strip.properties = { ...fs.strip.properties, auto_derived: note };
  writeFileSync(join(o.out, `${o.id}_strip.geojson`), JSON.stringify(fs.strip));
  log(`strip: peak ${result.breach.peakCfs.toFixed(0)} cfs, reach ${fs.strip.properties.reach_km?.toFixed?.(1)} km, ${fs.strip.features.length} bands`);

  const io = outlineInputs(dam, result);
  let outlineOk = false;
  if (io.ok) {
    const req = demRequest(io.inputs);
    log(`Fetching the outline DEM (${req.width}×${req.height})…`);
    const g = await fetchDem(req);
    const built = await buildOutline(dam, result, g);
    if (built.ok) {
      const ol = built.cache.outline;
      ol.properties = { ...ol.properties, auto_derived: note };
      writeFileSync(join(o.out, `${o.id}_outline.geojson`), JSON.stringify(ol));
      log(`outline: ${built.cache.stats.outlineAc.toFixed(0)} ac, channel ${built.cache.stats.channelKm.toFixed(1)} km`);
      outlineOk = true;
    } else log(`outline not built: ${built.reason}`);
  } else log(`outline inputs: ${io.reason}`);

  writeFileSync(join(o.out, `${o.id}_record.json`), JSON.stringify({ dam, note }, null, 2));
  return { strip: true, outline: outlineOk, bearing, crestFt, streambedFt };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main(process.argv.slice(2)).catch((e) => {
    console.error(e instanceof Error ? e.stack : String(e));
    process.exit(1);
  });
}
