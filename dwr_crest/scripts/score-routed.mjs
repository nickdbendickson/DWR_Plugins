#!/usr/bin/env node
/**
 * Score the routed terrain flood (routed-flood.ts) against a model run, the way validate-crest.mjs scores the strip
 * and outline. One-off, for the baseline comparison (Nick, 2026-11-02); not part of the app.
 *
 *   node --experimental-strip-types scripts/score-routed.mjs --dam <record.json> --runner <dam folder> [--json out.json]
 *     [--spread [--breach-path] [--no-basins] [--no-reach] [--settle N]] [--probes 300] [--wet-ft 0.1]
 *     [--cell 5 [--max-px 6000]]
 *
 * --spread scores the quasi-dynamic volume spread (spread.ts) instead of the stage fill; --breach-path makes it
 * follow the water from the breach instead of the outline's drainage trace; --no-basins turns off closed-basin storage;
 * --no-reach builds once on the corridor DEM instead of growing the terrain downstream on demand (reach.ts, what the
 * desk does). --settle sets the sweeps that let the spread's pools settle into one sheet (0 = as poured). --probes sets how many points on the model's wet cells compare depth, velocity and arrival. --wet-ft
 * scores both layers as wet at that depth instead of 0.1 ft (1 ft is where the 2-D models agree with each other).
 * --cell builds on a terrain cell other than the desk's 10 m (with --spread and the reach; --max-px lifts the 3,000
 * cells a side the desk allows). It is here to repeat the cell-size test in docs/accuracy-envelope.md; the desk
 * always builds at 10 m.
 *
 * It fetches the same corridor DEM the terrain outline uses (grown downstream on demand for the spread), builds the
 * routed flood on it, and scores its depth grid (the harness's lonlat candidate) against the Runner rasters.
 */
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { analyze } from "../src/lib/crest/engine.ts";
import { demRequest, fetchDem, outlineInputs } from "../src/lib/crest/outline.ts";
import { createEngine } from "../src/lib/crest/raster-engine.ts";
import { buildRoutedFloodReach } from "../src/lib/crest/reach.ts";
import { buildRoutedFlood } from "../src/lib/crest/routed-flood.ts";
import { formatReport, validate } from "../src/lib/crest/validation.ts";
import { runnerFiles } from "./validate-crest.mjs";

function parseArgs(argv) {
  const o = {};
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (!a.startsWith("--")) throw new Error(`unexpected ${a}`);
    const key = a.slice(2);
    if (key === "volume-limit" || key === "spread" || key === "breach-path" || key === "no-basins" || key === "no-reach") o[key] = true;
    else o[key] = argv[++i];
  }
  for (const k of ["dam", "runner"]) if (!o[k]) throw new Error(`--${k} is required`);
  return o;
}

/** Even-stride probe points on the reference run's wet cells (downstream of the breach when known). */
export function probePoints(ref, downstream, want, wetFt = 0.1) {
  const g = ref.grid;
  const wet = [];
  for (let r = 0; r < g.height; r++)
    for (let c = 0; c < g.width; c++) {
      const i = r * g.width + c;
      if (!(g.depth[i] >= wetFt)) continue;
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

  const rfOpts = {
    volumeLimit: o["volume-limit"] != null,
    slopeFloor: o["slope-floor"] != null ? Number(o["slope-floor"]) : undefined,
    sectionHalfWidthM: o["half-width"] != null ? Number(o["half-width"]) : undefined,
    method: o.spread ? "spread" : "stage",
    breachPath: o["breach-path"] ? true : undefined,
    basins: o["no-basins"] ? false : undefined,
    settleSweeps: o.settle != null ? Number(o.settle) : undefined,
  };
  let rf;
  let dem;
  let reach = null;
  if (o.spread && !o["no-reach"]) {
    // What the desk does: the corridor DEM, grown downstream while the routed wave still reaches its edge with water.
    const rb = await buildRoutedFloodReach(dam, result, {
      ...rfOpts,
      inputs: io.inputs,
      cellM: o.cell != null ? Number(o.cell) : undefined,
      maxPx: o["max-px"] != null ? Number(o["max-px"]) : undefined,
      onRound: (r) => log(r.round === 0 ? (r.stage === "fetch" ? `Fetching the DEM for ${dam.id} (${r.req.width}×${r.req.height} @ ${r.req.cellM} m)…` : "Building the routed flood…") : r.stage === "fetch" ? `  reach round ${r.round}: ${r.travellingAcFt.toFixed(0)} ac-ft still at the edge, growing the DEM ${r.stepKm.toFixed(1)} km downstream (${r.req.width}×${r.req.height} @ ${r.req.cellM} m)…` : `  reach round ${r.round}: rebuilding the routed flood…`),
    });
    if (!rb.routed.ok) throw new Error(rb.routed.reason);
    rf = rb.routed;
    dem = rb.grid;
    reach = rb.reach;
    log(`  reach: ${reach.rounds} round${reach.rounds === 1 ? "" : "s"} (+${reach.extendedKm} km), stopped: ${reach.stoppedBecause}${reach.incomplete ? `, INCOMPLETE (${reach.strandedAcFt} ac-ft stranded)` : ""}; DEM ${dem.width}×${dem.height} @ ${dem.cellM.toFixed(1)} m`);
  } else {
    log(`Fetching the DEM for ${dam.id}…`);
    const req = demRequest(io.inputs);
    dem = await fetchDem(req);
    log(`  DEM ${dem.width}×${dem.height} @ ${dem.cellM.toFixed(1)} m`);
    log("Building the routed flood…");
    rf = await buildRoutedFlood(dam, result, dem, rfOpts);
    if (!rf.ok) throw new Error(rf.reason);
  }
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
  const wetFt = o["wet-ft"] != null ? Number(o["wet-ft"]) : undefined;
  if (wetFt != null && !(wetFt > 0)) throw new Error("--wet-ft needs a depth in feet");
  const points = probePoints(ref, downstream, Number(o.probes ?? 300), wetFt ?? 0.1);
  const report = validate(cand, ref, { label: `routed terrain flood (${rf.method})`, reference: summary.label, points, downstream, wetFt });
  log("\n" + formatReport(report));

  if (o.json) {
    writeFileSync(o.json, JSON.stringify({ dam: dam.id, runner: o.runner, wetFt: wetFt ?? 0.1, routed: rf.stats, reach, dem: { width: dem.width, height: dem.height, cellM: dem.cellM, bbox: dem.bbox }, report }, null, 2));
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
