// scripts/build-nd-dams-dwr.mjs on CSV fixtures in the Docs & QA folder format (numbers from the 2026-10-30 DWR files;
// owner / name / remark text made up).
import assert from "node:assert/strict";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";
import { buildDwr, COLS, DUGOUT_COLS, EXCLUDED, heldFields, main, normDatum, parseCsv } from "./build-nd-dams-dwr.mjs";
import { DAM_FIELDS, DAMS_FIXTURE, damsCsv, dugoutsCsv, FIXTURE_MARKERS, fixtureCsvs, fixtureDam, toCsv } from "./fixtures/dwr-fixture.mjs";
import { tempDir } from "./test-tmp.mjs";

const here = fileURLToPath(new URL(".", import.meta.url));
const NID = [
  { nid: "ND00199", name: "Swan Buffalo Detention Dam 12", county: "Cass" },
  { nid: "ND00145", name: "Garrison Dam", county: "Mercer" },
  { nid: "ND00182", name: "Hickson Dam", county: "Clay" },
  { nid: "ND00486", name: "Mud Creek Impoundment", county: "Bowman" },
  { nid: "ND00001", name: "Beaver Creek Diversion Dam", county: "Steele" },
  { nid: "ND00012", name: "Some Dam", county: "Grand Forks" },
  { nid: "ND01032", name: null, county: "Stark" },
  { nid: "ND00450", name: null, county: "Morton" },
  { nid: "ND00971", name: null, county: "McHenry" },
];
const PULLED = "2026-10-30T14:31:02Z";
const build = (csvs = fixtureCsvs(), o = {}) => buildDwr(csvs, NID, { pulledUtc: PULLED, ...o });
const rec = (b, nid) => {
  const r = b.rows.find((x) => x[0] === nid);
  return r && Object.fromEntries(b.cols.map((c, i) => [c, r[i]]));
};

describe("build-nd-dams-dwr (CSV fixture)", () => {
  it("parses RFC 4180 CSV: quoted commas and doubled quotes", () => {
    const t = parseCsv('a,b,c\r\n1,"x, ""y""",3\r\n');
    assert.deepEqual(t.fields, ["a", "b", "c"]);
    assert.deepEqual(t.rows, [{ a: "1", b: 'x, "y"', c: "3" }]);
  });

  it("ND00199: stated NGVD 29 1070.0 / 1042.5 kept; NAVD 88 1071.09 / 1043.59 carried as is (no second shift), H 27.5", () => {
    const d = rec(build(), "ND00199");
    assert.deepEqual(
      { ...d },
      {
        nid: "ND00199", dwrId: 2801, heightFt: 27.5, topElevFt: 1070, streambedElevFt: 1042.5, datum: "NGVD29",
        auxSpillwayElevFt: 1066, normalPoolElevFt: 1051, maxPoolStorageAcFt: 1615.5, maxPoolAreaAc: 154.5,
        normalStorageAcFt: 87.8, normalAreaAc: 29.8, lengthFt: 1700, topWidthFt: 12, county: null,
        topNavdFt: 1071.09, streambedNavdFt: 1043.59, auxSpillwayNavdFt: 1067.09, normalPoolNavdFt: 1052.09,
        vshiftFt: 1.0887, held: null,
      },
    );
    assert.equal((d.topNavdFt - d.streambedNavdFt).toFixed(2), "27.50");
  });

  it("drawdown_e is treated as local: its elev_qc entry does not hold ND00199, and it is never carried", () => {
    const b = build();
    assert.ok(!b.cols.some((c) => /drawdown/i.test(c)));
    assert.equal(rec(b, "ND00199").held, null);
    assert.deepEqual(heldFields(DAMS_FIXTURE[0].elev_qc), ["drawdown_e"]);
  });

  it("elev_qc hold: ND01032 (NGVD 29 label, 128 / 100 ft) carries no NAVD 88 values and is listed as held", () => {
    const b = build();
    const d = rec(b, "ND01032");
    assert.equal(d.datum, "NGVD29");
    assert.equal(d.held, "max_pool_e,streambed_,emergenc_2,normal_ele");
    assert.deepEqual([d.topNavdFt, d.streambedNavdFt, d.auxSpillwayNavdFt, d.normalPoolNavdFt], [null, null, null, null]);
    assert.deepEqual(b.source.held, ["ND01032:max_pool_e,streambed_,emergenc_2,normal_ele"]);
    // A held field stays null even if the folder had (wrongly) filled its NAVD 88 value.
    const bad = DAMS_FIXTURE.map((r) => (r.national_i === "ND01032" ? { ...r, max_pool_e_navd88: 129.76 } : r));
    assert.equal(rec(build({ ...fixtureCsvs(), damsCsv: toCsv(DAM_FIELDS, bad) }), "ND01032").topNavdFt, null);
  });

  it("NAVD 88 offers only for NGVD 29 / NAVD 88; ND00182 NAVD 88 shift 0; Local and blank stay relative", () => {
    const b = build();
    assert.equal(b.source.navd88Offers, 2);
    const h = rec(b, "ND00182");
    assert.deepEqual([h.datum, h.topNavdFt, h.streambedNavdFt, h.vshiftFt], ["NAVD88", 894.08, 877.25, 0]);
    for (const id of ["ND00012", "ND00001", "ND00486", "ND00145"]) {
      const r = rec(b, id);
      assert.deepEqual([r.topNavdFt, r.streambedNavdFt, r.vshiftFt], [null, null, null], id);
      assert.ok(r.topElevFt != null, id);
    }
  });

  it("refuses a double (or missing) conversion and a NAVD 88 value on a local datum", () => {
    const twice = DAMS_FIXTURE.map((r) => (r.national_i === "ND00199" ? { ...r, max_pool_e_navd88: 1072.18 } : r));
    assert.throws(() => build({ ...fixtureCsvs(), damsCsv: toCsv(DAM_FIELDS, twice) }), /ND00199: max_pool_e_navd88 1072\.18 != 1070 \+ 1\.0887/);
    const local = DAMS_FIXTURE.map((r) => (r.national_i === "ND00012" ? { ...r, max_pool_e_navd88: 824.2 } : r));
    assert.throws(() => build({ ...fixtureCsvs(), damsCsv: toCsv(DAM_FIELDS, local) }), /Local datum but max_pool_e_navd88/);
    assert.ok(fixtureDam({ structure_: 1, national_i: "ND00199", datum_raw: "Local", max_pool_e: 1 }).max_pool_e_local === 1);
  });

  it("ND00145: two DWR records, picks Garrison Dam (structure 2753) by name; 0 values are missing", () => {
    const log = [];
    const b = build(fixtureCsvs(), { log: (m) => log.push(m) });
    const d = rec(b, "ND00145");
    assert.equal(d.dwrId, 2753);
    assert.equal(d.heightFt, 202);
    assert.equal(d.datum, null);
    assert.equal(d.county, "McLean");
    assert.ok(log.some((m) => /ND00145: 2 DWR records; chose structure 2753/.test(m)));
    const zeros = rec(b, "ND00182");
    assert.equal(zeros.maxPoolStorageAcFt, null);
    assert.equal(zeros.maxPoolAreaAc, null);
  });

  it("skips an NID ID with several records and no single name match", () => {
    const b = buildDwr(fixtureCsvs(), [{ nid: "ND00145", name: "Something Else", county: "McLean" }]);
    assert.equal(b.rows.length, 0);
    assert.deepEqual(b.source.skipped, [{ nid: "ND00145", candidates: [6192, 2753] }]);
  });

  it("datum codes and counts: NGVD 29, NAVD 88, Local, blank", () => {
    const b = build();
    assert.equal(rec(b, "ND00012").datum, "Local");
    assert.deepEqual(b.source.datums, { Local: 1, NAVD88: 1, NGVD29: 2, blank: 3 });
    assert.equal(normDatum("ngvd 29"), "NGVD29");
    assert.equal(normDatum("MSL"), "Other");
    assert.equal(rec(b, "ND00182").county, "Cass");
    assert.equal(rec(b, "ND00486").county, null);
  });

  it("dugouts: only records whose NID ID is a main dam (ND00450), five numeric reference fields; 0 is missing", () => {
    const b = build();
    assert.deepEqual(b.dugouts.cols, DUGOUT_COLS);
    assert.deepEqual(b.dugouts.rows, [["ND00450", 301, 8, 8, null, 56.9, 230]]);
    assert.deepEqual(b.source.dugouts, { records: 3, withNid: 2, matched: 1 });
  });

  it("carries numeric fields only, plus datum, held field names and a differing county: no owner, name, remark or permit text", () => {
    const b = build();
    const text = JSON.stringify(b);
    for (const m of FIXTURE_MARKERS) assert.ok(!text.includes(m), m);
    assert.ok(!JSON.stringify(b.rows).includes("Garrison") && !JSON.stringify(b.rows).includes("Hickson"));
    for (const r of [...b.rows, ...b.dugouts.rows])
      r.forEach((v, i) => {
        const c = (r.length === COLS.length ? b.cols : b.dugouts.cols)[i];
        if (c === "nid") assert.match(v, /^ND\d{5}$/);
        else if (c === "datum") assert.ok(v === null || ["NGVD29", "NAVD88", "Local", "Other"].includes(v));
        else if (c === "county") assert.ok(v === null || /^[A-Za-z .]+$/.test(v));
        else if (c === "held") assert.ok(v === null || /^[a-z_0-9,]+$/.test(v));
        else assert.ok(v === null || typeof v === "number", `${c}=${v}`);
      });
    assert.deepEqual(b.source.excludedFields, EXCLUDED);
  });

  it("records the pull date, layers, hashes, conversion, disclaimer and attribution (Dams and Dugouts)", () => {
    const b = build(fixtureCsvs(), { rawSha256: { "Dams.zip": "a".repeat(64) }, gridSha256: { "us_noaa_vertconc.tif": "b".repeat(64) } });
    assert.equal(b.v, 2);
    assert.equal(b.source.pulledUtc, PULLED);
    assert.match(b.source.layers, /Dams .*Dugouts/);
    assert.match(b.source.sha256["Dams_navd88.csv"], /^[0-9a-f]{64}$/);
    assert.equal(b.source.sha256["Dams.zip"], "a".repeat(64));
    assert.match(b.source.conversion, /VERTCON/);
    assert.match(b.source.disclaimer, /provisional.*'AS IS'/i);
    assert.match(b.source.attribution, /North Dakota Department of Water Resources, Dams and Dugouts layers .*downloaded 2026-10-30\. Provisional, provided 'as is'\. NGVD 29 elevations converted to NAVD 88 with NOAA VERTCON\./);
  });

  it("fails on a schema change", () => {
    assert.throws(() => build({ ...fixtureCsvs(), damsCsv: damsCsv({ omit: ["max_pool_e_navd88"] }) }), /DWR Dams schema changed: missing field\(s\) max_pool_e_navd88/);
    assert.throws(() => build({ ...fixtureCsvs(), dugoutsCsv: dugoutsCsv({ omit: ["freeboard"] }) }), /DWR Dugouts schema changed: missing field\(s\) freeboard/);
  });

  it("main --from <folder> writes the bundle and reads the SHA256SUMS", () => {
    const dir = tempDir("nd-dwr-");
    const from = join(dir, "folder");
    mkdirSync(join(from, "raw"), { recursive: true });
    writeFileSync(join(from, "Dams_navd88.csv"), damsCsv());
    writeFileSync(join(from, "Dugouts_navd88.csv"), dugoutsCsv());
    writeFileSync(join(from, "raw", "SHA256SUMS.txt"), `${"c".repeat(64)}  Dams.zip\n`);
    const nid = join(dir, "nd.json");
    writeFileSync(nid, JSON.stringify({ cols: ["nid", "name", "county"], rows: NID.map((d) => [d.nid, d.name, d.county]) }));
    const out = join(dir, "dwr.json");
    const log = console.log;
    console.log = () => {};
    try {
      main(["--from", from, "--nid", nid, "--out", out, "--pulled", PULLED]);
    } finally {
      console.log = log;
    }
    const b = JSON.parse(readFileSync(out, "utf8"));
    assert.equal(b.rows.length, 7);
    assert.equal(b.source.sha256["Dams.zip"], "c".repeat(64));
  });
});

describe("committed nd_dams_dwr.json", () => {
  const text = readFileSync(join(here, "../src/lib/crest/data/nd_dams_dwr.json"), "utf8");
  const b = JSON.parse(text);
  const nidText = readFileSync(join(here, "../src/lib/crest/data/nd_dams.json"), "utf8");
  const col = (r, c) => r[b.cols.indexOf(c)];
  it("joins 839 of the 948 main dams; 830 with a DWR height; 91 NAVD 88 offers; 108 dugouts", () => {
    assert.equal(b.rows.length, 839);
    assert.equal(b.rows.filter((r) => col(r, "heightFt") != null).length, 830);
    assert.deepEqual(b.cols, COLS);
    assert.equal(b.rows.filter((r) => col(r, "topNavdFt") != null && col(r, "streambedNavdFt") != null).length, 91);
    assert.equal(b.source.navd88Offers, 91);
    assert.equal(b.dugouts.rows.length, 108);
    assert.equal(b.source.pulledUtc, "2026-10-30T14:31:02Z");
  });
  it("ND00199 is DWR 2801: 1070.0 / 1042.5 NGVD 29 -> 1071.09 / 1043.59 NAVD 88, H 27.5", () => {
    const r = b.rows.find((x) => x[0] === "ND00199");
    assert.deepEqual([r[1], r[2], r[3], r[4], r[5]], [2801, 27.5, 1070, 1042.5, "NGVD29"]);
    assert.deepEqual([col(r, "topNavdFt"), col(r, "streambedNavdFt"), col(r, "vshiftFt")], [1071.09, 1043.59, 1.0887]);
  });
  it("only ND01032 is held; no Local or blank record carries a NAVD 88 value", () => {
    assert.deepEqual(b.source.held, ["ND01032:max_pool_e,streambed_,emergenc_2,normal_ele"]);
    for (const r of b.rows)
      if (col(r, "datum") === "Local" || col(r, "datum") == null) assert.equal(col(r, "topNavdFt"), null, r[0]);
  });
  it("both bundles together fit 330 KB; DWR <= 30 KB gzipped", () => {
    assert.ok(Buffer.byteLength(text) + Buffer.byteLength(nidText) <= 330_000);
    assert.ok(gzipSync(text, { level: 9 }).length <= 30_000);
  });
});
