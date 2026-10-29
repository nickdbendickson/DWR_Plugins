import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { analyze } from "./engine.ts";
import { exportDamJson, exportHydrographCsv, generateLisfloodPackage, toUtm } from "./export.ts";
import { dwr2801 } from "./fixtures.ts";
import { SAMPLE_DAMS } from "./samples.ts";
import { vertconShiftFt } from "./vertcon_nd.ts";

const SITE = { lat: 46.98364, lon: -97.39064 };

describe("NGVD29 to NAVD88 (VERTCON)", () => {
  it("CHECK: DWR-2801 1071.15 ft NGVD29 ties to 1072.24 ft NAVD88 (+1.09 ft)", () => {
    const shift = vertconShiftFt(SITE.lon, SITE.lat)!;
    assert.ok(Math.abs(shift - 1.0886) < 0.001, `shift ${shift}`);
    const a = analyze(
      dwr2801({ crestFt: 1071.15, streambedFt: 1043.65, statedDatum: "NGVD29", ...SITE }),
    );
    assert.equal(a.datum.crestNavdFt.toFixed(2), "1072.24");
    assert.equal(a.datum.streambedNavdFt!.toFixed(2), "1044.74");
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
      dwr2801({ crestFt: 1071.15, statedDatum: "NGVD29", ...SITE, datumOffsetFt: 1.5 }),
    );
    assert.equal(a.datum.offsetFt, 1.5);
    assert.ok(a.datum.warnings.some((w) => w.includes("differs from VERTCON")));
  });

  it("falls back to the drawn breach point for the location", () => {
    const a = analyze(
      dwr2801({
        crestFt: 1071.15,
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
    assert.equal(a.datum.crestNavdFt.toFixed(2), "1072.24");
    assert.match(a.datum.rule, /breach point/);
  });

  it("leaves NGVD29 unconverted with a warning when there is no location or offset", () => {
    const a = analyze(
      dwr2801({
        crestFt: 1071.15,
        statedDatum: "NGVD29",
        lat: undefined,
        lon: undefined,
        geometry: undefined,
      }),
    );
    assert.equal(a.datum.crestNavdFt, 1071.15);
    assert.ok(a.datum.warnings.some((w) => w.startsWith("NGVD29 offset unknown")));
  });
});

describe("ND00199 sample (Nick, 2026-10-29: crest 1072.24 NAVD88 from NGVD29 1071.15)", () => {
  const sample = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;

  it("carries crest 1072.24 / bottom 1044.74 ft NAVD88, H 27.50 ft, piping, with the conversion noted", () => {
    assert.equal(sample.crestFt, 1072.24);
    assert.equal(sample.streambedFt, 1044.74);
    assert.equal(sample.heightFt, 27.5);
    assert.equal(sample.statedDatum, "NAVD88");
    assert.equal(sample.storageAcFt, 1612.59);
    assert.equal(sample.surfaceAreaAc, 154.5);
    assert.equal(sample.failureMode, "piping");
    assert.match(sample.datumNote ?? "", /converted from NGVD29 1071\.15/);
  });

  it("CHECK: loading the sample gives crest 1072.24 and H 27.50 with no second VERTCON shift", () => {
    const a = analyze(sample);
    assert.equal(a.datum.offsetFt, null, "no offset applied to a NAVD88 record");
    assert.equal(a.datum.crestNavdFt, 1072.24);
    assert.equal(a.datum.streambedNavdFt, 1044.74);
    assert.equal(a.breach.headFt.toFixed(2), "27.50");
    assert.match(a.datum.rule, /treated as NAVD88\. Crest converted from NGVD29 1071\.15/);
    assert.doesNotMatch(a.datum.rule, /by VERTCON/);
    assert.equal(a.datum.warnings.length, 0, a.datum.warnings.join("; "));
  });

  it("CHECK: the same dam entered as NGVD29 1071.15 ties to the same 1072.24 and H 27.50", () => {
    const a = analyze({
      ...sample,
      crestFt: 1071.15,
      streambedFt: 1043.65,
      statedDatum: "NGVD29",
      datumNote: undefined,
    });
    assert.equal(a.datum.crestNavdFt.toFixed(2), "1072.24");
    assert.equal(a.datum.streambedNavdFt!.toFixed(2), "1044.74");
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
    assert.equal(lines.length, 2);
    const [p, x, y, kind, name] = lines[0].split(/\s+/);
    assert.deepEqual([p, kind, name], ["P", "QVAR", "breach"]);
    assert.ok(Math.abs(Number(x) - u.x) < 0.01 && Math.abs(Number(y) - u.y) < 0.01);
    assert.notEqual(Number(x), 0);
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
