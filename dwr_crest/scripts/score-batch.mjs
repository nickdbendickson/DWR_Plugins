#!/usr/bin/env node
/**
 * Score Crest's routed terrain flood against a folder of Breach Runner runs, dam by dam, optionally over a sweep of
 * settings, and summarise. The tuning harness for Crest (Nick, 2026-11-18): once a batch of Runner runs is in (the
 * 44-dam study), this says how close the desk's layer comes on each dam and which roughness / wet threshold brings it
 * closest across all of them. It scores; it never changes Crest's built-in values.
 *
 *   node --experimental-strip-types scripts/score-batch.mjs --runs <folder> --out <dir>
 *        [--ids DWR-2801,DWR-4031] [--records <folder>] [--dem-cache <dir>]
 *        [--sweep nChannel=0.03,0.035,0.04] [--sweep nFloodplain=0.04,0.06,0.08] [--wet-ft 0.1,1] [--no-flowlines]
 *
 * --runs     one sub-folder per dam, named by its id (DWR-2801, ND00199, ...), holding that dam's Runner rasters
 *            (Depth (Max).tif and friends, at any depth below it: the Runner's own output folder is fine).
 * --records  where to find each dam's Crest record: <records>/<id>_record.json (a desk save or a make-crest-layers
 *            record, { dam } or the bare record). Also looked for inside the dam's own run folder. A dam with no
 *            record is derived from Crest's own structures (the batch screen's record: inventory + 3DEP), when the id
 *            is one Crest knows.
 * --dem-cache  keep every 3DEP terrain request on disk, so a sweep (or a second run) fetches each box once.
 * --sweep    a record setting and the values to try; several --sweep flags make the full grid of combinations.
 *            Settings: nChannel, nFloodplain (Manning's n), startPool (top|normal).
 * --no-flowlines  trace across lakes by the terrain alone (the desk steers it along the USGS 3DHP flowline).
 * --wet-ft   the wet threshold(s) the extent is scored at (default 0.1; 1 ft is where 2-D models agree with each other).
 *
 * Writes <out>/scores.jsonl (one row per dam × combination × threshold, appended as each finishes, so a stopped run
 * resumes), <out>/scores.csv and <out>/SUMMARY.md (per combination: dams scored, median and mean CSI, median area
 * ratio and depth bias; the combination with the best median CSI first).
 */
import { createHash } from "node:crypto";
import { appendFileSync, existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { analyze } from "../src/lib/crest/engine.ts";
import { fetchFlowlines } from "../src/lib/crest/flowlines.ts";
import { ROUTED_METHOD } from "../src/lib/crest/inundation.ts";
import { fetchDem, outlineInputs } from "../src/lib/crest/outline.ts";
import { createEngine } from "../src/lib/crest/raster-engine.ts";
import { buildRoutedFloodReach } from "../src/lib/crest/reach.ts";
import { validate } from "../src/lib/crest/validation.ts";
import { probePoints } from "./score-routed.mjs";
import { runnerFiles } from "./validate-crest.mjs";

export const SWEEPABLE = {
  nChannel: (dam, v) => ({ ...dam, roughness: { ...dam.roughness, nChannel: Number(v) } }),
  nFloodplain: (dam, v) => ({ ...dam, roughness: { ...dam.roughness, nFloodplain: Number(v) } }),
  startPool: (dam, v) => ({ ...dam, startPool: String(v) }),
};

export function parseArgs(argv) {
  const o = { sweep: [], wetFt: [0.1] };
  const list = (v) => v.split(",").map((x) => x.trim()).filter(Boolean);
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const v = () => {
      const x = argv[++i];
      if (x == null) throw new Error(`${a} needs a value`);
      return x;
    };
    if (a === "--runs") o.runs = v();
    else if (a === "--out") o.out = v();
    else if (a === "--ids") o.ids = list(v());
    else if (a === "--records") o.records = v();
    else if (a === "--dem-cache") o.demCache = v();
    else if (a === "--no-flowlines") o.noFlowlines = true;
    else if (a === "--wet-ft") o.wetFt = list(v()).map(Number);
    else if (a === "--sweep") {
      const m = /^(\w+)=(.+)$/.exec(v());
      if (!m || !SWEEPABLE[m[1]]) throw new Error(`--sweep takes one of ${Object.keys(SWEEPABLE).join(", ")}=v1,v2,...`);
      o.sweep.push({ key: m[1], values: list(m[2]) });
    } else throw new Error(`unexpected ${a}`);
  }
  if (!o.runs || !o.out) throw new Error("--runs <folder> and --out <dir> are required");
  if (o.wetFt.some((w) => !(w > 0))) throw new Error("--wet-ft needs depths in feet");
  return o;
}

/** Every combination of the sweep values, as [{ key: value }], with the empty combination ("as recorded") when none. */
export function combinations(sweep) {
  let out = [{}];
  for (const s of sweep) out = out.flatMap((c) => s.values.map((v) => ({ ...c, [s.key]: v })));
  return out;
}

export const comboLabel = (c) => (Object.keys(c).length ? Object.entries(c).map(([k, v]) => `${k}=${v}`).join(" ") : "as recorded");

/** The dam folders under --runs: sub-folders that hold a Runner depth raster somewhere below them. */
export function damFolders(runs, ids) {
  const want = ids ? new Set(ids) : null;
  const hasDepth = (dir) => {
    for (const e of readdirSync(dir, { withFileTypes: true })) {
      if (e.isFile() && /^depth.*\.tif$/i.test(e.name)) return true;
      if (e.isDirectory() && hasDepth(join(dir, e.name))) return true;
    }
    return false;
  };
  return readdirSync(runs, { withFileTypes: true })
    .filter((e) => e.isDirectory() && (!want || want.has(e.name)))
    .map((e) => ({ id: e.name, dir: join(runs, e.name) }))
    .filter((d) => hasDepth(d.dir))
    .sort((a, b) => a.id.localeCompare(b.id));
}

/** A dam's Crest record from --records, its own run folder, or (last) Crest's structures. Null when none. */
export async function findRecord(id, dir, recordsDir, derive) {
  for (const p of [recordsDir && join(recordsDir, `${id}_record.json`), join(dir, `${id}_record.json`), join(dir, "crest_record.json")]) {
    if (p && existsSync(p)) {
      const j = JSON.parse(readFileSync(p, "utf8"));
      return { dam: j.dam ?? j, from: p };
    }
  }
  if (derive) {
    const dam = await derive(id);
    if (dam) return { dam, from: "Crest structures (batch record)" };
  }
  return null;
}

/** fetchDem with an on-disk cache keyed by the request (bbox, size, cell), when a cache folder is given. */
export function cachedFetch(dir) {
  if (!dir) return (req) => fetchDem(req);
  mkdirSync(dir, { recursive: true });
  return async (req) => {
    const key = createHash("sha1").update(JSON.stringify([req.bbox, req.width, req.height, req.cellM])).digest("hex");
    const p = join(dir, `${key}.f32`);
    if (existsSync(p)) {
      const b = readFileSync(p);
      return { ...req, z: new Float32Array(b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength)) };
    }
    const g = await fetchDem(req);
    writeFileSync(p, Buffer.from(g.z.buffer, g.z.byteOffset, g.z.byteLength));
    return g;
  };
}

const median = (xs) => {
  const v = xs.filter(Number.isFinite).sort((a, b) => a - b);
  if (!v.length) return null;
  const m = v.length >> 1;
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
};
const mean = (xs) => {
  const v = xs.filter(Number.isFinite);
  return v.length ? v.reduce((s, x) => s + x, 0) / v.length : null;
};

export const CSV_COLS = ["id", "combo", "wet_ft", "status", "model_ac", "crest_ac", "recall", "precision", "csi", "area_ratio", "depth_bias_ft", "mean_abs_depth_ft", "peak_in_cfs", "peak_out_cfs", "reach_stop", "dem_cell_m", "seconds", "reason"];

export function toCsv(rows) {
  const esc = (v) => (v == null ? "" : /[",\n]/.test(String(v)) ? `"${String(v).replace(/"/g, '""')}"` : String(v));
  return [CSV_COLS.join(","), ...rows.map((r) => CSV_COLS.map((c) => esc(r[c])).join(","))].join("\n") + "\n";
}

/** Per combination and threshold: dams scored, median/mean CSI, median area ratio and depth bias; best median CSI first. */
export function summarize(rows) {
  const groups = new Map();
  for (const r of rows) {
    const k = `${r.combo}\u0000${r.wet_ft}`;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(r);
  }
  const out = [...groups.entries()].map(([k, rs]) => {
    const [combo, wet] = k.split("\u0000");
    const ok = rs.filter((r) => r.status === "done");
    return {
      combo,
      wetFt: Number(wet),
      scored: ok.length,
      failed: rs.length - ok.length,
      medianCsi: median(ok.map((r) => r.csi)),
      meanCsi: mean(ok.map((r) => r.csi)),
      medianAreaRatio: median(ok.map((r) => r.area_ratio)),
      medianDepthBiasFt: median(ok.map((r) => r.depth_bias_ft)),
    };
  });
  return out.sort((a, b) => a.wetFt - b.wetFt || (b.medianCsi ?? -1) - (a.medianCsi ?? -1));
}

export function summaryMd(sum, meta) {
  const f = (x, d = 2) => (x == null ? "—" : x.toFixed(d));
  const lines = [
    "# Crest vs the Breach Runner: batch score",
    "",
    `Runs: ${meta.runs}. ${meta.dams} dam folders, ${meta.combos} combination${meta.combos === 1 ? "" : "s"}. Crest's routed terrain flood (the desk's layer) scored downstream of the breach.`,
    "",
    "| Combination | Wet ≥ (ft) | Dams scored | Failed | Median CSI | Mean CSI | Median area ratio | Median depth bias (ft) |",
    "|---|---|---|---|---|---|---|---|",
    ...sum.map((s) => `| ${s.combo} | ${s.wetFt} | ${s.scored} | ${s.failed} | ${f(s.medianCsi)} | ${f(s.meanCsi)} | ${f(s.medianAreaRatio)} | ${f(s.medianDepthBiasFt)} |`),
    "",
    "Per-dam rows are in scores.csv. A combination is a record setting applied to every dam; Crest's built-in values are unchanged by this run.",
    "",
  ];
  return lines.join("\n");
}

/** Build and score one dam under one combination; returns one row per wet threshold. */
export async function scoreOne(dam0, combo, ref, refSummary, opts) {
  const t0 = Date.now();
  let dam = dam0;
  for (const [k, v] of Object.entries(combo)) dam = SWEEPABLE[k](dam, v);
  const label = comboLabel(combo);
  const base = { id: dam0.id, combo: label };
  const fail = (reason) => opts.wetFt.map((w) => ({ ...base, wet_ft: w, status: "failed", reason, seconds: Math.round((Date.now() - t0) / 1000) }));
  const result = analyze(dam);
  const io = outlineInputs(dam, result);
  if (!io.ok) return fail(io.reason);
  const rb = await buildRoutedFloodReach(dam, result, { inputs: io.inputs, method: ROUTED_METHOD, fetch: opts.fetch, ...(opts.flowlines ? { flowlines: opts.flowlines } : {}) });
  if (!rb.routed.ok) return fail(rb.routed.reason);
  const rf = rb.routed;
  const breach = { lon: Number(dam.lon), lat: Number(dam.lat) };
  const downstream = Number.isFinite(breach.lon) && Number.isFinite(breach.lat) ? { ...breach, u: io.inputs.u } : null;
  const cand = { kind: "lonlat", bbox: rf.bbox, width: rf.width, height: rf.height, depth: rf.depthFt, velocity: rf.velocityFps ?? null, arrival: rf.arrivalHr ?? null };
  return opts.wetFt.map((w) => {
    const points = probePoints(ref, downstream, 300, w);
    const rep = validate(cand, ref, { label: "routed terrain flood", reference: refSummary.label, points, downstream, wetFt: w });
    const e = rep.extent;
    return {
      ...base,
      wet_ft: w,
      status: "done",
      model_ac: Math.round(e.refAc * 10) / 10,
      crest_ac: Math.round(e.candAc * 10) / 10,
      recall: Math.round(e.recall * 1000) / 1000,
      precision: Math.round(e.precision * 1000) / 1000,
      csi: Math.round(e.csi * 1000) / 1000,
      area_ratio: Math.round(e.areaRatio * 100) / 100,
      depth_bias_ft: e.depthBiasFt == null ? null : Math.round(e.depthBiasFt * 100) / 100,
      mean_abs_depth_ft: Math.round(e.meanAbsDepthFt * 100) / 100,
      peak_in_cfs: rf.stats.peakInCfs,
      peak_out_cfs: rf.stats.peakOutCfs,
      reach_stop: rb.reach.stoppedBecause + (rb.reach.incomplete ? " (incomplete)" : ""),
      dem_cell_m: Math.round(rb.grid.cellM * 10) / 10,
      seconds: Math.round((Date.now() - t0) / 1000),
      reason: null,
    };
  });
}

/** Crest's batch record for an id it knows (inventory + 3DEP), or null. Loaded lazily: the structures bundle is large. */
export function structureDeriver(fetch) {
  let bundles = null;
  return async (id) => {
    const b = await import("../src/lib/crest/batch.ts");
    bundles ??= await b.loadStructures();
    const s = bundles.structures.find((x) => x.id === id || x.nid === id);
    if (!s || b.missingData(s)) return null;
    const near = await fetch(b.nearRequest(s.lon, s.lat, 0.6, 10));
    const rec = b.deriveRecord(s, near, bundles.nd);
    if (!rec.ok) return null;
    const aimed = await b.aimAlongChannel(rec.dam, rec.derived.u, fetch).catch(() => ({ dam: rec.dam }));
    return { ...aimed.dam, id };
  };
}

export async function main(argv, log = console.log) {
  const o = parseArgs(argv);
  const dams = damFolders(resolve(o.runs), o.ids);
  if (!dams.length) throw new Error(`No dam folders with a Runner depth raster under ${o.runs}`);
  const combos = combinations(o.sweep);
  mkdirSync(o.out, { recursive: true });
  const jsonl = join(o.out, "scores.jsonl");
  const rows = existsSync(jsonl) ? readFileSync(jsonl, "utf8").split("\n").filter((l) => l.trim()).map((l) => JSON.parse(l)) : [];
  const done = new Set(rows.filter((r) => r.status === "done").map((r) => `${r.id}\u0000${r.combo}\u0000${r.wet_ft}`));
  const fetch = cachedFetch(o.demCache);
  const derive = structureDeriver(fetch);
  log(`${dams.length} dams × ${combos.length} combination${combos.length === 1 ? "" : "s"} × ${o.wetFt.length} threshold${o.wetFt.length === 1 ? "" : "s"}${done.size ? ` (${done.size} rows already done)` : ""}`);
  for (const d of dams) {
    const todo = combos.filter((c) => o.wetFt.some((w) => !done.has(`${d.id}\u0000${comboLabel(c)}\u0000${w}`)));
    if (!todo.length) continue;
    const rec = await findRecord(d.id, d.dir, o.records, derive).catch((e) => ({ error: e.message }));
    if (!rec || rec.error) {
      const reason = rec?.error ?? "no Crest record (none in --records or the run folder, and not a structure Crest knows)";
      for (const c of todo) for (const w of o.wetFt) {
        const r = { id: d.id, combo: comboLabel(c), wet_ft: w, status: "failed", reason };
        rows.push(r);
        appendFileSync(jsonl, JSON.stringify(r) + "\n");
      }
      log(`${d.id}: skipped, ${reason}`);
      continue;
    }
    const dam = { ...rec.dam, id: d.id };
    const engine = createEngine();
    const result = analyze(dam);
    const io = outlineInputs(dam, result);
    const breach = { lon: Number(dam.lon), lat: Number(dam.lat) };
    const ctx = io.ok && Number.isFinite(breach.lon) ? { breach, u: io.inputs.u, reachKm: 0 } : null;
    const refSummary = await engine.handle({ type: "import", key: "ref", source: "lisflood", files: runnerFiles(d.dir), crs: undefined, ctx }, () => {});
    const ref = engine.get("ref");
    for (const c of todo) {
      const out = await scoreOne(dam, c, ref, refSummary, { fetch, wetFt: o.wetFt, flowlines: o.noFlowlines ? null : (b) => fetchFlowlines(b) }).catch((e) =>
        o.wetFt.map((w) => ({ id: d.id, combo: comboLabel(c), wet_ft: w, status: "failed", reason: e.message })),
      );
      for (const r of out) {
        if (done.has(`${r.id}\u0000${r.combo}\u0000${r.wet_ft}`)) continue;
        rows.push(r);
        appendFileSync(jsonl, JSON.stringify(r) + "\n");
        log(`${d.id.padEnd(12)} ${r.combo.padEnd(28)} wet ${r.wet_ft} ft: ${r.status === "done" ? `CSI ${r.csi.toFixed(2)}, area ${r.area_ratio}×, depth bias ${r.depth_bias_ft ?? "—"} ft (${r.seconds}s)` : `failed: ${r.reason}`}`);
      }
    }
  }
  // The CSV and summary keep the last row per dam × combination × threshold (a retried failure supersedes itself).
  const last = [...new Map(rows.map((r) => [`${r.id}\u0000${r.combo}\u0000${r.wet_ft}`, r])).values()];
  writeFileSync(join(o.out, "scores.csv"), toCsv(last));
  const sum = summarize(last);
  writeFileSync(join(o.out, "SUMMARY.md"), summaryMd(sum, { runs: o.runs, dams: dams.length, combos: combos.length }));
  log(`wrote ${join(o.out, "scores.csv")} and SUMMARY.md`);
  return sum;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main(process.argv.slice(2)).catch((e) => {
    console.error(e instanceof Error ? e.stack : String(e));
    process.exit(1);
  });
}
