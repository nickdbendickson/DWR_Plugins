import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { readFileSync } from "node:fs";

import { analyze, damLocation } from "./engine.ts";
import { exportDamJson, exportHydrographCsv, generateLisfloodPackage, toUtm } from "./export.ts";
import { dwr2801 } from "./fixtures.ts";
import { SAMPLE_DAMS } from "./samples.ts";
import { vertconShiftFt } from "./vertcon_nd.ts";

const SITE = { lat: 46.98364, lon: -97.39064 };

describe("NGVD29 to NAVD88 (VERTCON)", () => {
  it("CHECK: DWR-2801 1070.0 ft NGVD29 ties to 1071.09 ft NAVD88 (+1.09 ft)", () => {
    const shift = vertconShiftFt(SITE.lon, SITE.lat)!;
    assert.ok(Math.abs(shift - 1.0886) < 0.001, `shift ${shift}`);
    const a = analyze(
      dwr2801({ crestFt: 1070.0, streambedFt: 1042.5, statedDatum: "NGVD29", ...SITE }),
    );
    assert.equal(a.datum.crestNavdFt.toFixed(2), "1071.09");
    assert.equal(a.datum.streambedNavdFt!.toFixed(2), "1043.59");
    assert.match(a.datum.rule, /VERTCON/);
  });

  it("is per site, not a flat +1.0 ft", () => {
    const west = vertconShiftFt(-103.5, 47.5)!;
    const east = vertconShiftFt(SITE.lon, SITE.lat)!;
    assert.ok(Math.abs(west - east) > 0.1, `${west} vs ${east}`);
    assert.equal(vertconShiftFt(-90, 46), null, "outside the ND grid");
  });

  it("uses an entered offset over the grid, and warns when they disagree", () => {
    const a = analyze(
      dwr2801({ crestFt: 1070.0, statedDatum: "NGVD29", ...SITE, datumOffsetFt: 1.5 }),
    );
    assert.equal(a.datum.offsetFt, 1.5);
    assert.ok(a.datum.warnings.some((w) => w.includes("differs from VERTCON")));
  });

  it("falls back to the drawn breach point for the location", () => {
    const a = analyze(
      dwr2801({
        crestFt: 1070.0,
        statedDatum: "NGVD29",
        lat: undefined,
        lon: undefined,
        geometry: {
          breachPoint: {
            type: "Feature",
            geometry: { type: "Point", coordinates: [SITE.lon, SITE.lat] },
            properties: {},
          },
        },
      }),
    );
    assert.equal(a.datum.crestNavdFt.toFixed(2), "1071.09");
    assert.match(a.datum.rule, /breach point/);
  });

  it("leaves NGVD29 unconverted with a warning when there is no location or offset", () => {
    const a = analyze(
      dwr2801({
        crestFt: 1070.0,
        statedDatum: "NGVD29",
        lat: undefined,
        lon: undefined,
        geometry: undefined,
      }),
    );
    assert.equal(a.datum.crestNavdFt, 1070.0);
    assert.ok(a.datum.warnings.some((w) => w.startsWith("NGVD29 offset unknown")));
  });
});

describe("ND00199 sample (Nick, 2026-10-29: crest 1071.09 NAVD88 from NGVD29 1070.0)", () => {
  const sample = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;

  it("carries crest 1071.09 / bottom 1043.59 ft NAVD88, H 27.50 ft, piping, with the conversion noted", () => {
    assert.equal(sample.crestFt, 1071.09);
    assert.equal(sample.streambedFt, 1043.59);
    assert.equal(sample.heightFt, 27.5);
    assert.equal(sample.statedDatum, "NAVD88");
    assert.equal(sample.storageAcFt, 1612.59);
    assert.equal(sample.surfaceAreaAc, 154.5);
    assert.equal(sample.failureMode, "piping");
    assert.match(sample.datumNote ?? "", /converted from NGVD29 1070\.0/);
  });

  it("CHECK: loading the sample gives crest 1071.09 and H 27.50 with no second VERTCON shift", () => {
    const a = analyze(sample);
    assert.equal(a.datum.offsetFt, null, "no offset applied to a NAVD88 record");
    assert.equal(a.datum.crestNavdFt, 1071.09);
    assert.equal(a.datum.streambedNavdFt, 1043.59);
    assert.equal(a.breach.headFt.toFixed(2), "27.50");
    assert.match(a.datum.rule, /treated as NAVD88\. Crest converted from NGVD29 1070\.0/);
    assert.doesNotMatch(a.datum.rule, /by VERTCON/);
    assert.equal(a.datum.warnings.length, 0, a.datum.warnings.join("; "));
  });

  it("CHECK: the same dam entered as NGVD29 1070.0 ties to the same 1071.09 and H 27.50", () => {
    const a = analyze({
      ...sample,
      crestFt: 1070.0,
      streambedFt: 1042.5,
      statedDatum: "NGVD29",
      datumNote: undefined,
    });
    assert.equal(a.datum.crestNavdFt.toFixed(2), "1071.09");
    assert.equal(a.datum.streambedNavdFt!.toFixed(2), "1043.59");
    assert.equal(a.breach.headFt.toFixed(2), "27.50");
    assert.match(a.datum.rule, /by VERTCON/);
    assert.equal(Math.round(a.breach.peakCfs), Math.round(analyze(sample).breach.peakCfs));
  });

  it("desk peak: 28,623 cfs with the 154.5 ac surface area, 26,205 cfs without (m = 2)", () => {
    assert.equal(Math.round(analyze(sample).breach.peakCfs), 28623);
    assert.equal(
      Math.round(analyze({ ...sample, surfaceAreaAc: undefined }).breach.peakCfs),
      26205,
    );
  });
});

describe("input range warnings (no silent clamps)", () => {
  it("reports a clamped height, storage and length", () => {
    const a = analyze(dwr2801({ heightFt: 400, storageAcFt: 0.2, lengthFt: 25000 }));
    const w = a.datum.warnings.join("\n");
    assert.match(w, /Height 400 ft is outside the desk range 4–200 ft\. Using 200 ft/);
    assert.match(w, /Storage 0\.2 ac-ft is outside/);
    assert.match(w, /Length 25,000 ft is outside/);
  });

  it("reports a missing value", () => {
    const a = analyze(dwr2801({ storageAcFt: Number.NaN }));
    assert.ok(a.datum.warnings.some((w) => w.startsWith("Storage is missing")));
  });
});

describe("LISFLOOD-FP export", () => {
  const dam = dwr2801({ surfaceAreaAc: 154.5, ...SITE });
  const a = analyze(dam);
  const pkg = generateLisfloodPackage(dam, a.breach.series, { cellSizeM: 10 });

  it(".par has the keywords LISFLOOD needs, valid ones only, and runs 72 h", () => {
    const keys = pkg.par
      .split("\n")
      .filter((l) => l && !l.startsWith("#"))
      .map((l) => l.split(/\s+/)[0]);
    for (const k of ["DEMfile", "resroot", "dirroot", "sim_time", "bcifile", "bdyfile", "fpfric"])
      assert.ok(keys.includes(k), k);
    const valid = new Set([
      "DEMfile",
      "resroot",
      "dirroot",
      "sim_time",
      "initial_tstep",
      "massint",
      "saveint",
      "bcifile",
      "bdyfile",
      "acceleration",
      "hazard",
      "fpfric",
    ]);
    for (const k of keys) assert.ok(valid.has(k), `unexpected keyword ${k}`);
    for (const bad of ["start", "end", "solver", "mass", "mint_hk", "drycheck"])
      assert.ok(!keys.includes(bad), bad);
    assert.match(pkg.par, /^sim_time 259200$/m);
  });

  it(".bci puts the point sources at the breach (UTM 14N), not (0,0)", () => {
    const u = toUtm(SITE.lon, SITE.lat);
    assert.equal(u.zone, 14);
    assert.ok(u.x > 600000 && u.x < 700000 && u.y > 5_150_000 && u.y < 5_250_000, `${u.x} ${u.y}`);
    const lines = pkg.bci.trim().split("\n");
    assert.equal(lines.length, 6);
    const [p, x, y, kind, name] = lines[0].split(/\s+/);
    assert.deepEqual([p, kind, name], ["P", "QVAR", "breach"]);
    assert.ok(Math.abs(Number(x) - u.x) < 0.01 && Math.abs(Number(y) - u.y) < 0.01);
    assert.notEqual(Number(x), 0);
  });

  it("CHECK: .bci has FREE N/S/E/W edges (was none: LISFLOOD's default closed edges ponded)", () => {
    const edges = pkg.bci.trim().split("\n").slice(2);
    assert.deepEqual(
      edges.map((l) => [l.split(/\s+/)[0], l.split(/\s+/)[3]]),
      [["N", "FREE"], ["S", "FREE"], ["E", "FREE"], ["W", "FREE"]],
    );
    const ext = generateLisfloodPackage(dam, a.breach.series, { demExtent: { xmin: 0, xmax: 2000, ymin: 0, ymax: 400 } });
    // Same lines as the Runner's golden run.bci for a 2000 x 400 m DEM.
    assert.match(ext.bci, /^N 0\.000 2000\.000 FREE\nS 0\.000 2000\.000 FREE\nE 0\.000 400\.000 FREE\nW 0\.000 400\.000 FREE$/m);
  });

  it("cell size and point are options (the desk export panel passes them)", () => {
    const p30 = generateLisfloodPackage(dam, a.breach.series, { cellSizeM: 30, pointX: -106278, pointY: 2666658 });
    const [l1, l2] = p30.bci.split("\n");
    assert.equal(l1, "P -106278.000 2666658.000 QVAR breach");
    assert.equal(l2, "P -106248.000 2666658.000 QVAR breach");
    assert.match(p30.bdy, /m3\/s \/ \(cell size x 2 inflow cells\)/);
    const peak30 = Math.max(...p30.bdy.trim().split("\n").slice(3).map((l) => Number(l.split(" ")[0])));
    assert.ok(Math.abs(peak30 - a.breach.peakCfs / 35.3146667 / 60) < 1e-4);
    assert.match(p30.notes, /30 m cells/);
    // A blank or bad cell size falls back to the 10 m default.
    assert.equal(generateLisfloodPackage(dam, a.breach.series, { cellSizeM: 0 }).bci, pkg.bci);
  });

  it("CHECK: a drawn breach point wins over the record lat/lon (was: record first)", () => {
    const bp = { lon: SITE.lon + 0.01, lat: SITE.lat - 0.005 };
    const drawn = {
      ...dam,
      geometry: {
        breachPoint: { type: "Feature" as const, geometry: { type: "Point" as const, coordinates: [bp.lon, bp.lat] }, properties: {} },
      },
    };
    const loc = damLocation(drawn)!;
    assert.equal(loc.source, "breach point");
    const u = toUtm(bp.lon, bp.lat);
    const [, x, y] = generateLisfloodPackage(drawn, a.breach.series).bci.split("\n")[0].split(" ");
    assert.ok(Math.abs(Number(x) - u.x) < 0.01 && Math.abs(Number(y) - u.y) < 0.01);
    assert.equal(damLocation(dam)!.source, "record");
  });

  it(".bdy is comment, name, 'N seconds', then value/time pairs in Q / (cell x 2 cells)", () => {
    const lines = pkg.bdy.trim().split("\n");
    assert.match(lines[0], /m3\/s \/ \(cell size x 2 inflow cells\)/);
    assert.equal(lines[1], "breach");
    const m = lines[2].match(/^(\d+) seconds$/);
    assert.ok(m);
    assert.equal(Number(m![1]), lines.length - 3);
    const pairs = lines.slice(3).map((l) => l.split(" ").map(Number));
    const peak = Math.max(...pairs.map((p) => p[0]));
    const expected = a.breach.peakCfs / 35.3146667 / (10 * 2);
    assert.ok(Math.abs(peak - expected) / expected < 1e-4, `${peak} vs ${expected}`);
    assert.equal(pairs[pairs.length - 1][1], 259200);
    for (let i = 1; i < pairs.length; i++)
      assert.ok(pairs[i][1] > pairs[i - 1][1], "times increase");
  });

  it("refuses to write a point source with no breach location", () => {
    const bare = dwr2801({ lat: undefined, lon: undefined, geometry: undefined });
    assert.throws(() => generateLisfloodPackage(bare, a.breach.series), /No breach location/);
  });

  it("CSV and dam.json carry the routed peak", () => {
    const csv = exportHydrographCsv(a.breach.series);
    assert.ok(csv.startsWith("time_hours,discharge_cms\n"));
    const j = JSON.parse(exportDamJson(dam, a));
    assert.equal(j.hydrograph_peak_cfs, a.breach.peakCfs);
    assert.equal(j.lat, SITE.lat);
    assert.equal(j.surface_area_top_ac, 154.5);
  });
});

describe("sample note, DEM crest check and store key", () => {
  const sample = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;

  it("CHECK: the ND00199 sample screens Medium by rule (judgment-zone Farmstead), High (flagged) only with the old option", () => {
    const a = analyze(sample);
    assert.equal(a.screen.level, "Medium");
    assert.equal(a.screen.label, "Medium");
    assert.equal(a.screen.judgmentAsProbable, false);
    assert.ok(a.screen.reasons.some((r) => /judgment zone .*Medium by rule/.test(r)), a.screen.reasons.join("\n"));
    assert.match(a.screen.classOfRecord ?? "", /^ND MEDIUM \/ TM11 Significant/);
    assert.match(a.screen.reasons[0], /^Class of record: ND MEDIUM .*is not the class of record\.$/);
    assert.match(sample.sampleNote ?? "", /generic receptor seeds.*Medium by rule.*High \(flagged\).*not the class of record/);
    const old = analyze({ ...sample, judgmentAsProbable: true });
    assert.equal(old.screen.level, "High");
    assert.equal(old.screen.label, "High (flagged: judgment zone)");
  });

  it("warns when the DEM crest differs from the tied crest (NGVD29 value labelled NAVD88)", () => {
    assert.ok(!analyze(sample).datum.warnings.some((w) => w.startsWith("DEM crest")), "sample DEM crest is NAVD88 now");
    const a = analyze(dwr2801({ demCrestFt: 1070.0 }));
    assert.ok(a.datum.warnings.some((w) => w.startsWith("DEM crest 1070.00 ft differs from the tied crest 1071.09 ft NAVD88 by 1.09 ft")));
  });

  it("the store key is bumped so old browsers drop the 912 ft sample", () => {
    const src = readFileSync(new URL("./store.ts", import.meta.url), "utf8");
    assert.match(src, /name: "crest-desk-v2"/);
    assert.ok(!src.includes('name: "crest-desk-v1"'));
  });
});
