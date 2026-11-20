import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { combinations, comboLabel, damFolders, findRecord, parseArgs, summarize, SWEEPABLE, toCsv } from "./score-batch.mjs";

test("parseArgs: sweeps, thresholds, required folders", () => {
  const o = parseArgs(["--runs", "r", "--out", "o", "--sweep", "nFloodplain=0.04,0.06", "--sweep", "nChannel=0.035", "--wet-ft", "0.1,1"]);
  assert.deepEqual(o.sweep, [{ key: "nFloodplain", values: ["0.04", "0.06"] }, { key: "nChannel", values: ["0.035"] }]);
  assert.deepEqual(o.wetFt, [0.1, 1]);
  assert.throws(() => parseArgs(["--runs", "r"]), /required/);
  assert.throws(() => parseArgs(["--runs", "r", "--out", "o", "--sweep", "bogus=1"]), /--sweep takes/);
  assert.throws(() => parseArgs(["--runs", "r", "--out", "o", "--wet-ft", "0"]), /feet/);
});

test("combinations: the full grid, and 'as recorded' with no sweep", () => {
  assert.deepEqual(combinations([]), [{}]);
  assert.equal(comboLabel({}), "as recorded");
  const c = combinations([{ key: "nChannel", values: ["0.03", "0.04"] }, { key: "nFloodplain", values: ["0.05", "0.06", "0.08"] }]);
  assert.equal(c.length, 6);
  assert.equal(comboLabel(c[0]), "nChannel=0.03 nFloodplain=0.05");
});

test("SWEEPABLE sets the record's own roughness and starting pool, keeping the other n", () => {
  const dam = { id: "x", roughness: { nChannel: 0.03 } };
  assert.deepEqual(SWEEPABLE.nFloodplain(dam, "0.08").roughness, { nChannel: 0.03, nFloodplain: 0.08 });
  assert.equal(SWEEPABLE.startPool(dam, "normal").startPool, "normal");
  assert.equal(dam.roughness.nFloodplain, undefined, "the input record is not changed");
});

test("damFolders: only folders holding a Runner depth raster, filtered by id", () => {
  const root = mkdtempSync(join(tmpdir(), "score-batch-"));
  mkdirSync(join(root, "DWR-1", "pass1"), { recursive: true });
  writeFileSync(join(root, "DWR-1", "pass1", "Depth (Max).tif"), "x");
  mkdirSync(join(root, "DWR-2"));
  writeFileSync(join(root, "DWR-2", "notes.txt"), "x");
  mkdirSync(join(root, "DWR-3"));
  writeFileSync(join(root, "DWR-3", "Depth.tif"), "x");
  assert.deepEqual(damFolders(root).map((d) => d.id), ["DWR-1", "DWR-3"]);
  assert.deepEqual(damFolders(root, ["DWR-3"]).map((d) => d.id), ["DWR-3"]);
});

test("findRecord: --records first, then the run folder, then the deriver", async () => {
  const root = mkdtempSync(join(tmpdir(), "score-batch-"));
  const recs = join(root, "recs");
  const run = join(root, "run");
  mkdirSync(recs);
  mkdirSync(run);
  writeFileSync(join(run, "A_record.json"), JSON.stringify({ dam: { id: "A", from: "run" } }));
  assert.equal((await findRecord("A", run, recs, null)).dam.from, "run");
  writeFileSync(join(recs, "A_record.json"), JSON.stringify({ id: "A", from: "records" }));
  assert.equal((await findRecord("A", run, recs, null)).dam.from, "records");
  assert.equal((await findRecord("B", run, recs, async () => ({ id: "B", from: "derived" }))).dam.from, "derived");
  assert.equal(await findRecord("C", run, recs, async () => null), null);
});

test("summarize: per combination and threshold, best median CSI first, failures counted apart", () => {
  const rows = [
    { id: "a", combo: "n=1", wet_ft: 0.1, status: "done", csi: 0.2, area_ratio: 1, depth_bias_ft: 0.5 },
    { id: "b", combo: "n=1", wet_ft: 0.1, status: "done", csi: 0.4, area_ratio: 2, depth_bias_ft: -0.5 },
    { id: "a", combo: "n=2", wet_ft: 0.1, status: "done", csi: 0.5, area_ratio: 1, depth_bias_ft: 0 },
    { id: "b", combo: "n=2", wet_ft: 0.1, status: "failed", reason: "x" },
  ];
  const s = summarize(rows);
  assert.equal(s[0].combo, "n=2");
  assert.equal(s[0].scored, 1);
  assert.equal(s[0].failed, 1);
  assert.ok(Math.abs(s[1].medianCsi - 0.3) < 1e-9);
  assert.equal(s[1].medianAreaRatio, 1.5);
  const csv = toCsv(rows);
  assert.match(csv.split("\n")[0], /^id,combo,wet_ft,status/);
  assert.equal(csv.trim().split("\n").length, 5);
});
