// scripts/build-nd-dams-dwr.mjs on a fixture Dams.zip (numbers from the 2026-10-29 DWR file; owner/remark text made up).
import assert from "node:assert/strict";
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";
import { buildDwr, COLS, EXCLUDED, main, normDatum, readDbf, unzip } from "./build-nd-dams-dwr.mjs";
import { DWR_FIXTURE, FIXTURE_MARKERS, fixtureZip, makeDbf, makeZip } from "./fixtures/dwr-fixture.mjs";
import { tempDir } from "./test-tmp.mjs";

const here = fileURLToPath(new URL(".", import.meta.url));
const NID = [
  { nid: "ND00199", name: "Swan Buffalo Detention Dam 12", county: "Cass" },
  { nid: "ND00145", name: "Garrison Dam", county: "Mercer" },
  { nid: "ND00182", name: "Hickson Dam", county: "Clay" },
  { nid: "ND00486", name: "Mud Creek Impoundment", county: "Bowman" },
  { nid: "ND00001", name: "Beaver Creek Diversion Dam", county: "Steele" },
  { nid: "ND00012", name: "Some Dam", county: "Grand Forks" },
  { nid: "ND00971", name: null, county: "McHenry" },
];
const build = (zip = fixtureZip(), o = {}) => buildDwr(zip, NID, { pulledUtc: "2026-10-29T22:07:32Z", ...o });
const rec = (b, nid) => {
  const r = b.rows.find((x) => x[0] === nid);
  return r && Object.fromEntries(b.cols.map((c, i) => [c, r[i]]));
};

describe("build-nd-dams-dwr (fixture)", () => {
  it("reads the zip and dbf, skipping deleted records", () => {
    const files = unzip(fixtureZip());
    assert.deepEqual(Object.keys(files).sort(), ["Dams.dbf", "Dams.prj"]);
    assert.equal(readDbf(files["Dams.dbf"]).rows.length, DWR_FIXTURE.length);
  });

  it("joins main dams on national_i; ND00199 carries the DWR numbers, datum NGVD29", () => {
    const d = rec(build(), "ND00199");
    assert.deepEqual(
      { ...d },
      {
        nid: "ND00199", dwrId: 2801, heightFt: 27.5, topElevFt: 1070, streambedElevFt: 1042.5, datum: "NGVD29",
        auxSpillwayElevFt: 1066, normalPoolElevFt: 1051, maxPoolStorageAcFt: 1615.5, maxPoolAreaAc: 154.5,
        normalStorageAcFt: 87.8, normalAreaAc: 29.8, lengthFt: 1700, topWidthFt: 12, county: null,
      },
    );
  });

  it("ND00145: two DWR records, picks Garrison Dam (structure 2753) by name; 0 values are missing", () => {
    const log = [];
    const b = build(fixtureZip(), { log: (m) => log.push(m) });
    const d = rec(b, "ND00145");
    assert.equal(d.dwrId, 2753);
    assert.equal(d.heightFt, 202);
    assert.equal(d.datum, null);
    assert.equal(d.county, "McLean");
    assert.ok(log.some((m) => /ND00145: 2 DWR records; chose structure 2753/.test(m)));
    const zeros = rec(build(), "ND00182");
    assert.equal(zeros.maxPoolStorageAcFt, null);
    assert.equal(zeros.maxPoolAreaAc, null);
  });

  it("skips an NID ID with several records and no single name match", () => {
    const b = buildDwr(fixtureZip(), [{ nid: "ND00145", name: "Something Else", county: "McLean" }]);
    assert.equal(b.rows.length, 0);
    assert.deepEqual(b.source.skipped, [{ nid: "ND00145", candidates: [6192, 2753] }]);
  });

  it("ND00182: DWR county Cass is kept (differs from the NID's Clay); same-county rows carry null", () => {
    assert.equal(rec(build(), "ND00182").county, "Cass");
    assert.equal(rec(build(), "ND00486").county, null);
  });

  it("datum codes: NGVD 29, NAVD 88, Local, blank", () => {
    const b = build();
    assert.equal(rec(b, "ND00182").datum, "NAVD88");
    assert.equal(rec(b, "ND00012").datum, "Local");
    assert.equal(rec(b, "ND00001").datum, null);
    assert.deepEqual(b.source.datums, { Local: 1, NAVD88: 1, NGVD29: 1, blank: 3 });
    assert.equal(normDatum("ngvd 29"), "NGVD29");
    assert.equal(normDatum("MSL"), "Other");
  });

  it("carries numeric fields only, plus the datum code and a differing county: no owner, remark, permit or name text", () => {
    const b = build();
    const text = JSON.stringify(b.rows);
    for (const m of FIXTURE_MARKERS) assert.ok(!text.includes(m), m);
    assert.ok(!text.includes("Garrison") && !text.includes("Hickson"));
    for (const r of b.rows)
      r.forEach((v, i) => {
        const c = b.cols[i];
        if (c === "nid") assert.match(v, /^ND\d{5}$/);
        else if (c === "datum") assert.ok(v === null || ["NGVD29", "NAVD88", "Local", "Other"].includes(v));
        else if (c === "county") assert.ok(v === null || /^[A-Za-z .]+$/.test(v));
        else assert.ok(v === null || typeof v === "number", `${c}=${v}`);
      });
    assert.deepEqual(b.source.excludedFields, EXCLUDED);
  });

  it("records the pull date, hash, disclaimer and attribution", () => {
    const b = build();
    assert.equal(b.source.pulledUtc, "2026-10-29T22:07:32Z");
    assert.match(b.source.sha256Zip, /^[0-9a-f]{64}$/);
    assert.match(b.source.disclaimer, /provisional.*'AS IS'/i);
    assert.match(b.source.attribution, /North Dakota Department of Water Resources, Dams layer .*downloaded 2026-10-29\. Provisional, provided 'as is'\./);
  });

  it("fails on a schema change", () => {
    const zip = makeZip({ "Dams.dbf": makeDbf(DWR_FIXTURE, { omit: ["max_pool_e"] }) });
    assert.throws(() => build(zip), /DWR schema changed: missing field\(s\) max_pool_e/);
  });

  it("main --from writes the bundle", () => {
    const dir = tempDir("nd-dwr-");
    const zip = join(dir, "Dams.zip");
    writeFileSync(zip, fixtureZip());
    const nid = join(dir, "nd.json");
    writeFileSync(nid, JSON.stringify({ cols: ["nid", "name", "county"], rows: NID.map((d) => [d.nid, d.name, d.county]) }));
    const out = join(dir, "dwr.json");
    const log = console.log;
    console.log = () => {};
    try {
      main(["--from", zip, "--nid", nid, "--out", out, "--pulled", "2026-10-29T22:07:32Z"]);
    } finally {
      console.log = log;
    }
    assert.equal(JSON.parse(readFileSync(out, "utf8")).rows.length, 6);
  });
});

describe("committed nd_dams_dwr.json", () => {
  const text = readFileSync(join(here, "../src/lib/crest/data/nd_dams_dwr.json"), "utf8");
  const b = JSON.parse(text);
  const nidText = readFileSync(join(here, "../src/lib/crest/data/nd_dams.json"), "utf8");
  it("joins 839 of the 948 main dams; 830 with a DWR height", () => {
    assert.equal(b.rows.length, 839);
    assert.equal(b.rows.filter((r) => r[b.cols.indexOf("heightFt")] != null).length, 830);
    assert.deepEqual(b.cols, COLS);
  });
  it("ND00199 is DWR 2801: 1070.0 / 1042.5 NGVD 29, H 27.5", () => {
    const r = b.rows.find((x) => x[0] === "ND00199");
    assert.deepEqual([r[1], r[2], r[3], r[4], r[5]], [2801, 27.5, 1070, 1042.5, "NGVD29"]);
  });
  it("both bundles together fit 330 KB", () => {
    assert.ok(Buffer.byteLength(text) + Buffer.byteLength(nidText) <= 330_000);
    assert.ok(gzipSync(text, { level: 9 }).length <= 30_000);
  });
});
