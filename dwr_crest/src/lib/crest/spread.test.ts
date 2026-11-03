// Quasi-dynamic volume spread: pour each stretch's peak storage as a level pool on its own ground.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { DemGrid } from "./outline.ts";
import { routeMuskingumCunge, type RoutedSection, sectionTable } from "./routing.ts";
import type { Centerline } from "./sections.ts";
import { breachPath, spreadVolume } from "./spread.ts";

const M_PER_DEG_LAT = 110574;
const kxOf = (lat: number) => 111320 * Math.cos((lat * Math.PI) / 180);
const LOC = { lon: -100, lat: 47 };

function synthDem(cells: number, cellM: number, ground: (e: number, n: number) => number): DemGrid {
  const kx = kxOf(LOC.lat);
  const dlon = cellM / kx;
  const dlat = cellM / M_PER_DEG_LAT;
  const bbox: [number, number, number, number] = [
    LOC.lon - (cells / 2) * dlon,
    LOC.lat - (cells / 2) * dlat,
    LOC.lon + (cells / 2) * dlon,
    LOC.lat + (cells / 2) * dlat,
  ];
  const z = new Float32Array(cells * cells);
  for (let j = 0; j < cells; j++)
    for (let i = 0; i < cells; i++) {
      const e = (bbox[0] + (i + 0.5) * dlon - LOC.lon) * kx;
      const n = (bbox[3] - (j + 0.5) * dlat - LOC.lat) * M_PER_DEG_LAT;
      z[j * cells + i] = ground(e, n);
    }
  return { bbox, width: cells, height: cells, cellM, z };
}

function idxOf(g: DemGrid) {
  const kx = kxOf(LOC.lat);
  const dlon = (g.bbox[2] - g.bbox[0]) / g.width;
  const dlat = (g.bbox[3] - g.bbox[1]) / g.height;
  return (e: number, n: number) => {
    const i = Math.floor((LOC.lon + e / kx - g.bbox[0]) / dlon);
    const j = Math.floor((g.bbox[3] - (LOC.lat + n / M_PER_DEG_LAT)) / dlat);
    return i < 0 || j < 0 || i >= g.width || j >= g.height ? -1 : j * g.width + i;
  };
}

/** Straight channel at e = 0 from n = fromN south to n = toN; x = distance from the top. */
function straight(fromN: number, toN: number, step = 10): Centerline {
  const pts: { e: number; n: number; x: number }[] = [];
  for (let n = fromN; n >= toN - 1e-9; n -= step) pts.push({ e: 0, n, x: fromN - n });
  return { loc: LOC, pts };
}

/** Sections every 50 m with the given peak storage per stretch (m³). */
function sections(n: number, store: (j: number) => number, slope = 0.002): RoutedSection[] {
  const out: RoutedSection[] = [];
  for (let j = 0; j < n; j++)
    out.push({
      x: j * 50,
      bedM: 100,
      slopeStage: slope,
      slopeRouting: slope,
      peakCms: 50,
      tPeakS: 600 + 60 * j,
      tArrivalS: 300 + 60 * j,
      stageM: 1,
      wseM: 101,
      areaM2: 50,
      topWidthM: 50,
      velocityMps: 1,
      capped: false,
      storeMaxM3: j + 1 < n ? store(j) : 0,
      tStoreMaxS: 600 + 60 * j,
    });
  return out;
}


describe("spreadVolume: level pools that hold exactly their slice", () => {
  // Valley falling to the south at 0.2 %, 2 % side slopes.
  const dem = synthDem(200, 10, (e, n) => 100 + 0.002 * n + 0.02 * Math.abs(e));
  const line = straight(800, -800);
  const secs = sections(33, () => 20_000);
  const r = spreadVolume(dem, line, secs, { minDepthM: 0.001, releasedM3: 100_000 });

  it("lays down the sum of the slices (volume-conserving pour)", () => {
    const want = secs.reduce((s, x) => s + x.storeMaxM3, 0);
    assert.ok(Math.abs(r.slicesM3 - want) < 1e-6);
    // Only the thin fringe under minDepth is not counted; the pool is otherwise exact (cells are ~100 m²).
    assert.ok(Math.abs(r.volumeM3 - want) / want < 0.03, `${r.volumeM3} vs ${want}`);
    assert.ok(r.volumeRatio != null && Math.abs(r.volumeRatio - r.volumeM3 / 100_000) < 1e-9);
  });

  it("each stretch's pool is level relative to its channel bed, deepest on the thalweg", () => {
    const at = idxOf(dem);
    const k0 = at(0, 0);
    const k1 = at(40, 0);
    assert.ok(r.depthM[k0] > r.depthM[k1] && r.depthM[k1] > 0);
    // Same stretch, same level: depth falls by exactly the ground's rise.
    assert.ok(Math.abs(r.depthM[k0] - r.depthM[k1] - (dem.z[k1] - dem.z[k0])) < 1e-3);
  });

  it("carries velocity and arrival on every wet cell", () => {
    let wet = 0;
    for (let k = 0; k < r.depthM.length; k++)
      if (r.depthM[k] > 0) {
        wet++;
        assert.ok(Number.isFinite(r.velocityMps[k]) && r.velocityMps[k] >= 0);
        assert.ok(Number.isFinite(r.arrivalS[k]));
        assert.ok(r.velocityMps[k] <= Math.sqrt(9.80665 * r.depthM[k]) + 1e-6, "Froude ≤ 1");
      }
    assert.equal(wet, r.wetCells);
  });
});

describe("spreadVolume: no down-valley leak", () => {
  // A steep valley: one big slice near the top must stay in its own stretch, not run down the valley floor.
  const dem = synthDem(200, 10, (e, n) => 100 + 0.02 * n + 0.05 * Math.abs(e));
  const line = straight(800, -800);
  const secs = sections(33, (j) => (j === 2 ? 200_000 : 0));
  const r = spreadVolume(dem, line, secs, { minDepthM: 0.03 });
  const at = idxOf(dem);

  it("wets only the ground nearest stretch 2 (100–150 m down the channel)", () => {
    assert.ok(r.depthM[at(0, 800 - 125)] > 0, "stretch 2 is wet");
    assert.equal(r.depthM[at(0, 800 - 400)], 0, "300 m further down stays dry");
    assert.equal(r.depthM[at(0, 0)], 0);
  });
});

describe("spreadVolume: spill order", () => {
  // Flat floor at 100 with a 3 m ridge at e = 60..80 and a deep pit beyond it (e = 100..140, ground 95).
  const ground = (e: number) => (e >= 60 && e < 80 ? 103 : e >= 100 && e < 140 ? 95 : 100 + 0.002 * Math.abs(e));
  const dem = synthDem(120, 10, (e) => ground(e));
  const line = straight(500, -500);
  const at = idxOf(dem);

  it("leaves the pit dry until the pool tops the ridge", () => {
    const small = spreadVolume(dem, line, sections(21, () => 2_000), { minDepthM: 0.01 });
    assert.equal(small.depthM[at(120, 0)], 0, "pit behind the ridge stays dry for a small slice");
    const big = spreadVolume(dem, line, sections(21, () => 400_000), { minDepthM: 0.01 });
    assert.ok(big.depthM[at(120, 0)] > 0, "a big slice spills over the ridge into the pit");
  });
});

describe("spreadVolume: water left in depressions leaves the wave", () => {
  // A valley with a deep closed pit across it just below the top: what fills the pit no longer travels.
  const dem = synthDem(200, 10, (e, n) => {
    const base = 100 + 0.002 * n + 0.02 * Math.abs(e);
    return n < 650 && n > 550 && Math.abs(e) < 150 ? base - 2 : base;
  });
  const line = straight(800, -800);
  const secs = sections(33, () => 20_000);
  const withLoss = spreadVolume(dem, line, secs, { minDepthM: 0.01, releasedM3: 60_000 });
  const noLoss = spreadVolume(dem, line, secs, { minDepthM: 0.01 });

  it("retains water in the pit and pours less downstream", () => {
    assert.ok(withLoss.retainedM3 > 0);
    assert.ok(withLoss.volumeM3 < noLoss.volumeM3);
    assert.ok(withLoss.travellingM3 < 60_000);
  });
});

describe("routeMuskingumCunge: stretch storage from continuity", () => {
  // A rectangular channel, 10 sections 200 m apart; a triangular inflow of known volume.
  const tables = Array.from({ length: 10 }, (_, j) => {
    const offs = Array.from({ length: 41 }, (_, i) => -20 + i);
    const z = offs.map((o) => (Math.abs(o) <= 10 ? 100 - 0.001 * j * 200 : 105));
    return sectionTable({ x: j * 200, offsets: offs, z });
  });
  const inflow = { tS: [0, 600, 1800], qCms: [0, 40, 0] };
  const vIn = 0.5 * 1800 * 40;
  const r = routeMuskingumCunge(inflow, tables, { maxHours: 6 });

  it("keeps each stretch's peak storage, bounded by the inflow, zero on the last section", () => {
    for (let j = 0; j + 1 < r.sections.length; j++) {
      assert.ok(r.sections[j].storeMaxM3 > 0, `stretch ${j}`);
      assert.ok(r.sections[j].storeMaxM3 <= vIn * 1.001);
    }
    assert.equal(r.sections[r.sections.length - 1].storeMaxM3, 0);
  });

  it("the stretches peak in order down the reach", () => {
    for (let j = 1; j + 1 < r.sections.length; j++)
      assert.ok(r.sections[j].tStoreMaxS >= r.sections[j - 1].tStoreMaxS);
  });
});

describe("breachPath: follow the water from the breach", () => {
  // The valley falls to the north-east; the reservoir (behind the dam, to the south-west) is lower still, so a walk
  // that could go back through the breach would end there.
  const dem = synthDem(200, 10, (e, n) => {
    const a = (e + n) / Math.SQRT2;
    const c = (e - n) / Math.SQRT2;
    if (a < -20) return 90; // reservoir floor behind the dam
    return 100 - 0.003 * a + 0.02 * Math.abs(c);
  });
  const u: [number, number] = [Math.SQRT1_2, Math.SQRT1_2];
  const p = breachPath(dem, LOC, u, { e: 40, n: 40 });

  it("runs down the valley to the far end of the DEM, never behind the dam", () => {
    assert.ok(p && p.pts.length > 10);
    for (const q of p!.pts) assert.ok(q.e * u[0] + q.n * u[1] >= -1e-6, "behind the dam");
    const end = p!.pts[p!.pts.length - 1];
    assert.ok(end.e * u[0] + end.n * u[1] > 600, "reaches the downstream end");
    for (let i = 1; i < p!.pts.length; i++) assert.ok(p!.pts[i].x > p!.pts[i - 1].x);
  });
});
