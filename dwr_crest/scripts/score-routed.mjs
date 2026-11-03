#!/usr/bin/env node
/**
 * Score the routed terrain flood (routed-flood.ts) against a model run, the way validate-crest.mjs scores the strip
 * and outline. One-off, for the baseline comparison (Nick, 2026-11-02); not part of the app.
 *
 *   node --experimental-strip-types scripts/score-routed.mjs --dam <record.json> --runner <dam folder> [--json out.json]
 *     [--spread [--breach-path]] [--probes 300]
 *
 * --spread scores the quasi-dynamic volume spread (spread.ts) instead of the stage fill; --breach-path makes it
 * follow the water from the breach instead of the outline's drainage trace. --probes sets how many points on the model's wet
 * cells compare depth, velocity and arrival.
 *
 * It fetches the same corridor DEM the terrain outline uses, builds the routed flood on it, and scores its depth grid
 * (the harness's lonlat candidate) against the Runner rasters.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { analyze } from "../src/lib/crest/engine.ts";
import { demRequest, fetchDem, outlineInputs } from "../src/lib/crest/outline.ts";
import { createEngine } from "../src/lib/crest/raster-engine.ts";
import { buildRoutedFlood } from "../src/lib/crest/routed-flood.ts";
import { formatReport, validate } from "../src/lib/crest/validation.ts";
import { runnerFiles } from "./validate-crest.mjs";

function parseArgs(argv) {
  const o = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith("--")) throw new Error(`unexpected ${a}`);
    const key = a.slice(2);
    if (key === "volume-limit" || key === "spread" || key === "breach-path") o[key] = true;
    else o[key] = argv[++i];
  }
  for (const k of ["dam", "runner"]) if (!o[k]) throw new Error(`--${k} is required`);
  return o;
}

/** Even-stride probe points on the reference run's wet cells (downstream of the breach when known). */
export function probePoints(ref, downstream, want) {
  const g = ref.grid;
  const wet = [];
  for (let r = 0; r < g.height; r++)
    for (let c = 0; c < g.width; c++) {
      const i = r * g.width + c;
      if (!(g.depth[i] >= 0.1)) continue;
      const [lon, lat] = ref.proj.inv(g.x0 + (c + 0.5) * g.dx, g.y0 - (r + 0.5) * g.dy);
      if (downstream) {
        const e = (lon - downstream.lon) * 111320 * Math.cos((downstream.lat * Math.PI) / 180);
        const n = (lat - downstream.lat) * 110574;
        if (e * downstream.u[0] + n * downstream.u[1] < 0) continue;
      }
      wet.push({ lon, lat });
    }
  if (!want || !wet.length) return [];
  const stride = Math.max(1, Math.floor(wet.length / want));
  const pts = [];
  for (let i = 0; i < wet.length; i += stride) pts.push({ id: `p${pts.length}`, ...wet[i] });
  return pts;
}

export async function main(argv, log = console.log) {
  const o = parseArgs(argv);
  const rec = JSON.parse(readFileSync(o.dam, "utf8"));
  const dam = rec.dam ?? rec;
  const result = analyze(dam);

  const io = outlineInputs(dam, result);
  if (!io.ok) throw new Error(io.reason);
  const u = io.inputs.u;
  // The breach is at the dam point (result.breachLon/Lat are not top-level); the dam record carries lon/lat.
  const breach = { lon: Number(dam.lon), lat: Number(dam.lat) };
  const haveBreach = Number.isFinite(breach.lon) && Number.isFinite(breach.lat);
  const downstream = u && haveBreach ? { ...breach, u } : null;

  log(`Fetching the DEM for ${dam.id}…`);
  const req = demRequest(io.inputs);
  const dem = await fetchDem(req);
  log(`  DEM ${dem.width}×${dem.height} @ ${dem.cellM.toFixed(1)} m`);

  log("Building the routed flood…");
  const rf = await buildRoutedFlood(dam, result, dem, {
    volumeLimit: o["volume-limit"] != null,
    slopeFloor: o["slope-floor"] != null ? Number(o["slope-floor"]) : undefined,
    sectionHalfWidthM: o["half-width"] != null ? Number(o["half-width"]) : undefined,
    method: o.spread ? "spread" : "stage",
    breachPath: o["breach-path"] ? true : undefined,
  });
  if (!rf.ok) throw new Error(rf.reason);
  log(`  routed: peak in ${rf.stats.peakInCfs} cfs → out ${rf.stats.peakOutCfs} cfs (fell ${(rf.stats.routedPeakFellBy * 100).toFixed(0)}%), ${rf.stats.sections} sections, flooded ${rf.stats.floodedAc} ac`);
  for (const n of rf.notes) log(`  note: ${n}`);

  const engine = createEngine();
  const files = runnerFiles(o.runner);
  const summary = await engine.handle(
    { type: "import", key: "ref", source: "lisflood", files, crs: undefined, ctx: downstream ? { breach, u: downstream.u, reachKm: 0 } : null },
    (t) => log(`  ${t}`),
  );
  const ref = engine.get("ref");
  log(`Model: ${summary.label}, ${summary.width} × ${summary.height} cells of ${summary.cellM} m (${summary.crs})`);
  if (summary.stats) log(`  wet downstream ${summary.stats.wetAc.toFixed(0)} ac, peak depth ${summary.stats.peakDepthFt.toFixed(1)} ft`);

  const cand = {
    kind: "lonlat",
    bbox: rf.bbox,
    width: rf.width,
    height: rf.height,
    depth: rf.depthFt,
    velocity: rf.velocityFps ?? null,
    arrival: rf.arrivalHr ?? null,
  };
  // Probe receptors: about --probes points on the model's wet cells downstream of the breach (an even stride), so
  // depth, velocity and arrival can be compared where the model says there is water.
  const points = probePoints(ref, downstream, Number(o.probes ?? 300));
  const report = validate(cand, ref, { label: `routed terrain flood (${rf.method})`, reference: summary.label, points, downstream });
  log("\n" + formatReport(report));

  if (o.json) {
    writeFileSync(o.json, JSON.stringify({ dam: dam.id, runner: o.runner, routed: rf.stats, report }, null, 2));
    log(`\nWrote ${o.json}`);
  }
  return report;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main(process.argv.slice(2)).catch((e) => {
    console.error(e instanceof Error ? e.stack : String(e));
    process.exit(1);
  });
}
