// scripts/build-nd-dams.mjs on a fixture CSV (real NID rows for ND00199, ND00145 + S001/S002, ND00182, ND00971,
// ND00486, ND00001; two made-up ND9xxxx rows for blank max storage and zero area/length; every Owner Names value
// replaced by a made-up marker) and the committed bundle.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";
import { buildNdDams, COLS, main, parseCsv, REQUIRED } from "./build-nd-dams.mjs";
import { tempDir } from "./test-tmp.mjs";

const here = fileURLToPath(new URL(".", import.meta.url));
const csv = readFileSync(join(here, "fixtures/nid_nd_fixture.csv"), "utf8");
const build = (text = csv, o = {}) => buildNdDams(text, { allowShrink: true, fetchedUtc: "2026-10-29T21:56:00Z", ...o });
const row = (b, nid) => {
  const r = b.rows.find((x) => x[0] === nid);
  return r && Object.fromEntries(b.cols.map((c, i) => [c, r[i]]));
};

describe("build-nd-dams (fixture)", () => {
  it("keeps main dams only (Federal ID == NID ID) and logs the dropped associated structures", () => {
    const log = [];
    const b = build(csv, { log: (m) => log.push(m) });
    assert.equal(b.rows.length, 8);
    assert.deepEqual(b.source.dropped.map((d) => d.federalId), ["ND00145S001", "ND00145S002"]);
    assert.ok(log.some((m) => m.includes("ND00145S001")));
    assert.equal(b.rows.filter((r) => r[0] === "ND00145").length, 1);
    assert.equal(row(b, "ND00145").nidHeightFt, 210);
  });

  it("ND00199: name, other names, H 28 (NID, whole ft), V 1615.5, normal-pool area, NID hazard as reported", () => {
    const d = row(build(), "ND00199");
    assert.equal(d.name, "Swan Buffalo Detention Dam 12");
    assert.equal(d.otherNames, "Absaraka Dam");
    assert.equal(d.county, "Cass");
    assert.equal(d.nidHeightFt, 28);
    assert.equal(d.nidStorageAcFt, 1615.5);
    assert.equal(d.surfaceAreaNormalAc, 29.8);
    assert.equal(d.nidHazard, "Significant");
    assert.equal(d.lengthFt, 1700);
  });

  it("ND00182 stays in Clay with a note that the point lies in Cass Co., ND", () => {
    const d = row(build(), "ND00182");
    assert.equal(d.county, "Clay");
    assert.match(d.countyNote, /not an ND county; point lies in Cass Co\., ND/);
  });

  it("blanks and zeros become null (never 0)", () => {
    const b = build();
    assert.equal(row(b, "ND90001").maxStorageAcFt, null);
    assert.equal(row(b, "ND90002").surfaceAreaNormalAc, null);
    assert.equal(row(b, "ND90002").lengthFt, null);
    assert.equal(row(b, "ND00971").name, null);
  });

  it("keeps the NID heights apart (ND00001: NID Height 24, Dam Height 16)", () => {
    const d = row(build(), "ND00001");
    assert.equal(d.nidHeightFt, 24);
    assert.equal(d.damHeightFt, 16);
  });

  it("records the source, date, hash and attribution; no --inventory pass", () => {
    const b = build();
    assert.equal(b.source.fetchedUtc, "2026-10-29T21:56:00Z");
    assert.match(b.source.sha256Raw, /^[0-9a-f]{64}$/);
    assert.equal(b.source.inventoryPass, null);
    assert.match(b.source.attribution, /National Inventory of Dams .*downloaded 2026-10-29/);
    assert.deepEqual(b.cols, COLS);
  });

  it("fails on a schema change, a duplicate main dam, and too few rows without --allow-shrink", () => {
    const t = parseCsv(csv);
    const renamed = csv.replace("NID Height (Ft)", "NID Height");
    assert.throws(() => build(renamed), /missing column\(s\) NID Height \(Ft\)/);
    const dup = csv + csv.split("\n")[1] + "\n";
    assert.throws(() => build(dup), /duplicate main dam NID ID ND00199/);
    assert.throws(() => buildNdDams(csv, {}), /minimum 800/);
    assert.ok(REQUIRED.every((c) => t[0].includes(c)));
  });

  it("never reads or carries Owner Names (no owner text in the bundle)", () => {
    assert.ok(!REQUIRED.includes("Owner Names"));
    assert.ok(!COLS.some((c) => /owner/i.test(c)));
    assert.ok(parseCsv(csv)[0].includes("Owner Names"));
    const text = JSON.stringify(build());
    assert.ok(!text.includes("FIXTURE-OWNER-NOT-REAL"));
    assert.ok(!/"owner/i.test(text)); // a key, not the county "Towner"
  });

  it("main --from writes the bundle and never downloads", async () => {
    const dir = tempDir("nd-dams-");
    const out = join(dir, "nd.json");
    const realFetch = globalThis.fetch;
    globalThis.fetch = () => {
      throw new Error("no network in this test");
    };
    try {
      await main(["--from", join(here, "fixtures/nid_nd_fixture.csv"), "--out", out, "--allow-shrink", "--fetched", "2026-10-29T21:56:00Z"]);
    } finally {
      globalThis.fetch = realFetch;
    }
    assert.equal(JSON.parse(readFileSync(out, "utf8")).rows.length, 8);
  });
});

describe("committed nd_dams.json", () => {
  const text = readFileSync(join(here, "../src/lib/crest/data/nd_dams.json"), "utf8");
  const b = JSON.parse(text);
  it("has the 948 main dams of the 2026-10-29 NID download, no associated structures", () => {
    assert.equal(b.rows.length, 948);
    assert.equal(b.source.rows, 948);
    assert.ok(!b.rows.some((r) => /S\d{3}$/.test(r[0])));
    assert.equal(new Set(b.rows.map((r) => r[0])).size, 948);
  });
  it("carries no owner field or owner text", () => {
    assert.ok(!b.cols.some((c) => /owner/i.test(c)));
    assert.ok(!/"owner/i.test(text)); // a key, not the county "Towner"
  });
  it("fits the size budget: <= 250 KB raw, <= 70 KB gzipped", () => {
    assert.ok(Buffer.byteLength(text) <= 250_000, `${Buffer.byteLength(text)} B`);
    assert.ok(gzipSync(text, { level: 9 }).length <= 70_000);
  });
});
