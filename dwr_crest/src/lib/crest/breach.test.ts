import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { analyze, MAX_REACH_KM } from "./engine.ts";
import { dwr2801 } from "./fixtures.ts";
import { SAMPLE_DAMS } from "./samples.ts";

// DamSafety_Runner core/breach.py piping_hydrograph on DWR-2801 (top 1072.24 NAVD88, H 27.5 ft,
// SA 154.5 ac): 1615.5 ac-ft -> 28,671 cfs; 1612.59 ac-ft -> 28,623 cfs; no SA (m = 2) -> 26,205 cfs.
describe("routed breach hydrograph (DWR-2801 regression)", () => {
  it("CHECK: DWR-2801 piping peak near 28,671 cfs (Runner), not the old 20,540", () => {
    const b = analyze(dwr2801({ surfaceAreaAc: 154.5 })).breach;
    assert.ok(Math.abs(b.peakCfs - 28671) / 28671 < 0.005, `peak ${b.peakCfs}`);
    assert.ok(Math.abs(b.peakCfs - 20540) > 5000);
    assert.ok(Math.abs(b.timeToPeakHr - 0.944) < 0.01, `tp ${b.timeToPeakHr}`);
    assert.ok(Math.abs(b.bottomWidthFt - 80.7) < 0.5, `bottom ${b.bottomWidthFt}`);
    assert.ok(Math.abs(b.durationHr - 1.75) < 0.05, `duration ${b.durationHr}`);
  });

  it("matches the Runner to 0.1 % at the Runner's storage", () => {
    const b = analyze(dwr2801({ storageAcFt: 1615.5, surfaceAreaAc: 154.5 })).breach;
    assert.ok(Math.abs(b.peakCfs - 28671) / 28671 < 0.001, `peak ${b.peakCfs}`);
    assert.ok(Math.abs(b.empiricalPeakCfs - 21599) / 21599 < 0.002, `F95 ${b.empiricalPeakCfs}`);
  });

  it("the ND00199 sample is the same DWR-2801 dam as the fixture (1072.24 / 1044.74 NAVD88, H 27.50)", () => {
    const s = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;
    const f = dwr2801();
    for (const k of ["crestFt", "streambedFt", "heightFt", "storageAcFt", "statedDatum", "failureMode"] as const) {
      assert.equal(s[k], f[k], k);
    }
    const ps = analyze(s).breach.peakCfs;
    const pf = analyze(dwr2801({ surfaceAreaAc: 154.5 })).breach.peakCfs;
    assert.ok(Math.abs(ps - pf) < 1, `sample ${ps} vs fixture ${pf}`);
  });

  it("without a surface area uses m = 2 (Runner 26,205 cfs)", () => {
    const b = analyze(dwr2801()).breach;
    assert.equal(b.storageExponent, 2);
    assert.ok(Math.abs(b.peakCfs - 26205) / 26205 < 0.002, `peak ${b.peakCfs}`);
  });

  it("Froehlich 1995 check uses the head, not the dam height", () => {
    // Raise the breach bottom 5 ft (streambed above the reservoir bottom): head 22.5 ft, height 27.5 ft.
    const base = dwr2801({ surfaceAreaAc: 154.5 });
    const a = analyze(base).breach;
    const b = analyze({ ...base, streambedFt: base.streambedFt + 5 }).breach;
    assert.ok(Math.abs(b.headFt - 22.5) < 1e-6);
    const expected = a.empiricalPeakCfs * (22.5 / 27.5) ** 1.24;
    assert.ok(Math.abs(b.empiricalPeakCfs - expected) / expected < 1e-6);
  });

  it("outflow is driven by width: a wider breach peaks higher", () => {
    const narrow = analyze(dwr2801({ method: "tr60" })).breach;
    const froe = analyze(dwr2801()).breach;
    assert.ok(narrow.widthFt < froe.widthFt);
    assert.ok(narrow.peakCfs < froe.peakCfs);
  });

  it("releases the storage and ends at zero", () => {
    const b = analyze(dwr2801({ surfaceAreaAc: 154.5 })).breach;
    assert.ok(Math.abs(b.volumeAcFt - 1612.59) / 1612.59 < 0.01, `released ${b.volumeAcFt}`);
    assert.equal(b.series[0].tHr, 0);
    assert.equal(b.series[b.series.length - 1].qCfs, 0);
    const max = Math.max(...b.series.map((p) => p.qCfs));
    assert.ok(Math.abs(max - b.peakCfs) < 1e-6, "series keeps the peak");
  });
});

describe("desk valley reach", () => {
  it("is not capped at 6 km and decays to the wet limit", () => {
    const f = analyze(dwr2801({ surfaceAreaAc: 154.5 })).flood;
    assert.ok(f.lengthM > 6000, `reach ${f.lengthM}`);
    const last = f.stations[f.stations.length - 1];
    assert.ok(Math.abs(last.depthFt - 0.1) < 0.01, `end depth ${last.depthFt}`);
  });

  it("flags the DWR-2801 benchmark structures past the reach as unresolved, not end depth", () => {
    const base = dwr2801({ surfaceAreaAc: 154.5 });
    const seeds = [
      {
        id: "435",
        name: "435",
        kind: "house" as const,
        station: 0,
        distanceKm: 14.8,
        lateral: 0,
        padFt: 0,
      },
      {
        id: "4069",
        name: "4069",
        kind: "house" as const,
        station: 0,
        distanceKm: 17.2,
        lateral: 0,
        padFt: 0,
      },
      {
        id: "near",
        name: "near",
        kind: "house" as const,
        station: 0,
        distanceKm: 2,
        lateral: 0,
        padFt: 0,
      },
    ];
    const a = analyze({ ...base, receptors: seeds });
    const [r435, r4069, near] = a.flood.receptors;
    assert.ok(a.flood.lengthM < 14800);
    for (const r of [r435, r4069]) {
      assert.equal(r.unresolved, true);
      assert.equal(r.flooded, false);
      assert.equal(r.ndClass, null);
      assert.ok(r.needsJudgment);
    }
    assert.equal(near.unresolved, false);
    assert.ok(near.depthFt > 0);
    assert.ok(a.screen.judgment.some((j) => j.startsWith("435")));
    assert.ok(a.screen.reasons.some((j) => j.includes("unresolved")));

    // A user reach past 17.2 km resolves them on the decayed curve.
    const long = analyze({ ...base, receptors: seeds, reachKm: 25 });
    assert.equal(long.flood.lengthM, 25000);
    assert.equal(long.flood.receptors[1].unresolved, false);
    assert.equal(long.flood.reachSource, "user");
  });

  it("caps a user reach at MAX_REACH_KM", () => {
    const a = analyze(dwr2801({ reachKm: 1000 }));
    assert.equal(a.flood.lengthM, MAX_REACH_KM * 1000);
  });

  it("labels velocity as an estimate and the decay curves as schematic", () => {
    const f = analyze(dwr2801()).flood;
    assert.match(f.velocityBasis, /^Estimate/);
    assert.doesNotMatch(f.engineLabel, /-style/);
    assert.match(f.engineLabel, /schematic/);
  });
});
