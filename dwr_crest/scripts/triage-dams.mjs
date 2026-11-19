#!/usr/bin/env node
/**
 * Triage a list of dams the way the desk does (triage.ts): build each dam's inundation layer exactly as the desk's
 * build button does (inundation.ts buildInundationReach: the flood-size gate, the demand-driven reach, the extent
 * check), then print the recommendation, the terrain confidence and the edge shift. Crest is a screening stand-in:
 * this sorts a list into "clearly trivial", "uncertain: run it" and "needs a Runner run"; it classifies nothing.
 *
 *   node --experimental-strip-types scripts/triage-dams.mjs <record.json> [<record.json> ...] [--csv out.csv] [--json out.json]
 *
 * A record file holds a DamRecord, or { dam: DamRecord } (the baseline's *_record.json). Needs the 3DEP terrain service.
 */
import { readFileSync, writeFileSync } from "node:fs";

import { analyze } from "../src/lib/crest/engine.ts";
import { buildInundationReach } from "../src/lib/crest/inundation.ts";
import { outlineState } from "../src/lib/crest/outline.ts";
import { triageCsv, triageDam } from "../src/lib/crest/triage.ts";

const args = process.argv.slice(2);
const files = [];
const opt = {};
for (let i = 0; i < args.length; i++) {
  if (args[i] === "--csv" || args[i] === "--json") opt[args[i].slice(2)] = args[++i];
  else files.push(args[i]);
}
if (!files.length) {
  console.error("usage: triage-dams.mjs <record.json> [...] [--csv out.csv] [--json out.json]");
  process.exit(2);
}

const rows = [];
for (const f of files) {
  const raw = JSON.parse(readFileSync(f, "utf8"));
  const dam = raw.dam ?? raw;
  const result = analyze(dam);
  const built = await buildInundationReach(dam, result, { onStatus: (s) => process.stderr.write(`${dam.id}: ${s}\n`) });
  const withLayer = built.ok ? { ...dam, outline: built.cache } : dam;
  const t = triageDam(withLayer, result, outlineState(withLayer, result, true));
  const e = built.ok ? built.cache.stats.extent : undefined;
  rows.push({ dam, result, layer: built.ok ? built.layer : null, triage: t, extent: e ?? null, reach: built.ok ? built.cache.routed?.reach ?? null : null });
  console.log(`${dam.id.padEnd(10)} ${t.label.padEnd(30)} ${t.confidence.padEnd(6)} edge shift ${t.edgeShiftM == null ? "—" : Math.round(t.edgeShiftM) + " m"}`);
  for (const r of t.reasons) console.log(`             · ${r}`);
}
if (opt.csv) writeFileSync(opt.csv, triageCsv(rows.map(({ dam, result, triage }) => ({ dam, result, triage }))));
if (opt.json)
  writeFileSync(
    opt.json,
    JSON.stringify(
      rows.map(({ dam, layer, triage, extent, reach }) => ({ id: dam.id, layer, triage, extent, reach })),
      null,
      1,
    ),
  );
