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

describe("spreadVolume: a slice is never smeared across an untopped saddle (M13)", () => {
  // A wall on the reservoir side, a flat floor at 100, a 3 m ridge (crest 103) at e = 60..80, and a deep, wide pit
  // (ground 95) beyond it. A slice big enough to reach the ridge but too small to fill the pit up to the ridge must
  // not drop the channel surface below the ridge it had to top: it caps at the crest and perches the overflow in the
  // pit. The old single running-average pour averaged the deep pit cells in and pulled the channel surface below the
  // crest while still wetting the pit — water laid across a saddle the pool never actually topped.
  const ground = (e: number) => (e < -5 ? 200 : e >= 60 && e < 80 ? 103 : e >= 80 && e < 300 ? 95 : 100 + 0.002 * Math.abs(e));
  const dem = synthDem(160, 10, (e) => ground(e));
  const line = straight(400, -400);
  const at = idxOf(dem);
  const r = spreadVolume(dem, line, sections(17, () => 40_000), { minDepthM: 0.01 });

  it("pins the channel at the ridge crest when the pit beyond it is wet", () => {
    assert.ok(r.depthM[at(120, 0)] > 0, "the pit beyond the ridge takes the overflow");
    const chWse = r.wseM[at(0, 0)];
    assert.ok(Number.isFinite(chWse), "the channel is wet");
    // Water is in the pit only because the pool topped the 103 crest, so the channel surface cannot sit below it.
    assert.ok(chWse >= 103 - 1e-3, `channel surface ${chWse} must reach the ridge crest 103`);
    // The pit's surface perches below the crest (the overflow sitting in the low), not level with the channel.
    assert.ok(r.wseM[at(120, 0)] < chWse + 1e-6);
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

describe("spreadVolume: a closed basin that holds the flood ends it", () => {
  // DWR-3034's case: a road fill with no culvert in the DEM across the valley 800 m down closes a basin far bigger
  // than the release. The routed sections know nothing of it (they carry the wave straight on), so without the basin
  // rule the slices keep pouring below the fill.
  const fill = (n: number) => (Math.abs(n) <= 20 ? 6 : 0);
  const dem = synthDem(200, 10, (e, n) => 100 + 0.01 * n + 0.02 * Math.abs(e) + fill(n));
  const line = straight(800, -800);
  const secs = sections(33, () => 20_000);
  const released = 30_000;
  const at = idxOf(dem);
  const stopped = spreadVolume(dem, line, secs, { minDepthM: 0.01, releasedM3: released });
  const through = spreadVolume(dem, line, secs, { minDepthM: 0.01, releasedM3: released, basins: false });

  it("stops at the fill as one level pool and leaves the valley below it dry", () => {
    assert.ok(stopped.stoppedIn, "the flood ends in the basin");
    assert.ok(stopped.stoppedIn!.capacityM3 > released);
    // The fill's pond backs up to n ≈ 600 (6 m of fill on a 1% valley), 200 m down the channel: the flood ends there.
    assert.ok(Math.abs(stopped.stoppedIn!.atM - 200) <= 60, `ends where the pond starts (${stopped.stoppedIn!.atM} m)`);
    assert.equal(stopped.travellingM3, 0);
    for (let n = -30; n >= -700; n -= 50) assert.equal(stopped.depthM[at(0, n)], 0, `dry below the fill at n=${n}`);
    // The pond is level: the same water surface at the fill and 100 m up the valley.
    const w1 = stopped.wseM[at(0, 30)];
    const w2 = stopped.wseM[at(0, 100)];
    assert.ok(Number.isFinite(w1) && Math.abs(w1 - w2) < 1e-4, `${w1} vs ${w2}`);
    assert.ok(w1 < 106, "below the fill's crest");
  });

  it("basins: false keeps the old per-cell dead storage only", () => {
    assert.equal(through.stoppedIn, null);
    assert.equal(through.basinsFilled, 0);
  });

  it("a basin shallower than basinMinDepthM is not a barrier", () => {
    const low = synthDem(200, 10, (e, n) => 100 + 0.01 * n + 0.02 * Math.abs(e) + (Math.abs(n) <= 20 ? 0.6 : 0));
    const r = spreadVolume(low, line, secs, { minDepthM: 0.01, releasedM3: released });
    assert.equal(r.stoppedIn, null);
    assert.equal(r.basinsFilled, 0);
  });
});

describe("spreadVolume: a smaller basin fills to its spill level and the rest goes on", () => {
  const dem = synthDem(200, 10, (e, n) => 100 + 0.01 * n + 0.02 * Math.abs(e) + (Math.abs(n) <= 20 ? 1.5 : 0));
  const line = straight(800, -800);
  const r = spreadVolume(dem, line, sections(33, () => 20_000), { minDepthM: 0.01, releasedM3: 60_000 });
  const at = idxOf(dem);

  it("fills the basin to its spill and still floods below the fill", () => {
    assert.equal(r.stoppedIn, null);
    assert.equal(r.basinsFilled, 1);
    assert.ok(r.travellingM3 < 60_000);
    assert.ok(r.depthM[at(0, -300)] > 0);
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

describe("spreadVolume: a stretch that runs out of ground is held at the cap, not stacked", () => {
  // A narrow DEM (600 m across, 2 % side slopes: the ground within reach rises only ~6 m above the channel) and one
  // stretch handed far more water than that ground can hold. Before the cap the whole slice stacked on the stretch as
  // depth (277 ft on a 27.5 ft dam on DWR-2801); now the pool is held at the cap and the surplus is withheld.
  const dem = synthDem(60, 10, (e, n) => 100 + 0.002 * n + 0.02 * Math.abs(e));
  const line = straight(250, -250);
  const at = idxOf(dem);
  const H = 8; // dam height (m): the hard cap is 2 H = 16 m
  const RELEASED = 5_000_000;
  const secs = sections(11, (j) => (j === 4 ? RELEASED : 0)); // stretch 4 is x = 200..250 m (n = 50..0)
  const r = spreadVolume(dem, line, secs, { minDepthM: 0.01, releasedM3: RELEASED, maxLevelM: 2 * H });
  const maxDepth = (d: Float32Array) => {
    let m = 0;
    for (const v of d) if (v > m) m = v;
    return m;
  };

  it("holds the pool at the level that just reaches the last cell (above the routed stage + 1 ft)", () => {
    assert.equal(r.exhaustedStretches, 1);
    assert.equal(r.cappedStretches, 1);
    // The highest reachable ground is ~5.9 m above the channel bed (2 % over 295 m), above stage 1 m + 0.3 m.
    const m = maxDepth(r.depthM);
    assert.ok(m > 5.5 && m < 6.5, `max depth ${m} m`);
    assert.ok(r.depthM[at(280, 25)] > 0.2 && r.depthM[at(280, 25)] < 0.5, "the ground just below the last cell is barely wet");
    assert.ok(r.unpouredM3 > 0.9 * RELEASED, `almost all of the slice is withheld (${r.unpouredM3})`);
  });

  it("balances: poured + withheld = the slice, and the released ledger still holds", () => {
    assert.ok(Math.abs(r.pouredM3 + r.unpouredM3 - r.slicesM3) / r.slicesM3 < 1e-9, `${r.pouredM3} + ${r.unpouredM3} vs ${r.slicesM3}`);
    assert.ok(r.pouredM3 > 0);
    assert.ok(Math.abs(r.retainedM3 + r.travellingM3 - RELEASED) / RELEASED < 0.01);
    assert.ok(Math.abs(r.volumeM3 - r.pouredM3) / r.pouredM3 < 0.03, "what is laid down is what was poured");
  });

  it("never stands deeper than the hard cap, and the ground above it stays dry", () => {
    const low = spreadVolume(dem, line, secs, { minDepthM: 0.01, releasedM3: RELEASED, maxLevelM: 3 });
    const m = maxDepth(low.depthM);
    assert.ok(m <= 3 + 1e-6 && m > 2.9, `max depth ${m} m`);
    assert.equal(low.depthM[at(290, 25)], 0, "ground 5.8 m above the bed stays dry under a 3 m cap");
    assert.ok(low.depthM[at(0, 25)] > 2.9, "the channel stands at the cap");
    assert.ok(low.unpouredM3 > r.unpouredM3);
    assert.ok(Math.abs(low.pouredM3 + low.unpouredM3 - low.slicesM3) / low.slicesM3 < 1e-9);
  });

  it("the routed stage + 1 ft is the floor of the cap when the reachable ground is lower", () => {
    // Flat ground: every cell is reachable at ~0 m above the bed, so the cap is the stage (1 m) + 0.3048 m.
    const flat = synthDem(60, 10, (e, n) => 100 + 0.002 * n + 0.0001 * Math.abs(e));
    const f = spreadVolume(flat, line, secs, { minDepthM: 0.01, releasedM3: RELEASED, maxLevelM: 2 * H });
    const m = maxDepth(f.depthM);
    assert.equal(f.cappedStretches, 1);
    assert.ok(Math.abs(m - 1.3048) < 0.05, `max depth ${m} m`);
  });
});

describe("spreadVolume: the cap leaves a pool the terrain contains untouched", () => {
  const dem = synthDem(200, 10, (e, n) => 100 + 0.002 * n + 0.02 * Math.abs(e));
  const line = straight(800, -800);
  const secs = sections(33, () => 20_000);
  const a = spreadVolume(dem, line, secs, { minDepthM: 0.001, releasedM3: 100_000 });
  const b = spreadVolume(dem, line, secs, { minDepthM: 0.001, releasedM3: 100_000, maxLevelM: 0.5 });

  it("is bit-identical with a cap far below the pools when no stretch runs out of ground", () => {
    assert.equal(a.exhaustedStretches, 0);
    assert.equal(b.cappedStretches, 0);
    assert.equal(b.unpouredM3, 0);
    assert.deepEqual(b.depthM, a.depthM);
    assert.ok(Math.abs(a.pouredM3 - a.slicesM3) / a.slicesM3 < 1e-9);
  });
});

describe("spreadVolume: a stretch's ground is the land nearest it in a straight line, not by grid steps", () => {
  // A channel running diagonally (south-east) across a gently tilted plain, a V across it so every stretch floods its
  // own ground. Each stretch's arrival is its own (300 + 60 j s), so a wet cell's arrival says which stretch it was
  // poured from. Counted in grid steps (|Δi| + |Δj|, the old 4-neighbour search) a point 300 m off a 45° channel is
  // equally near ~600 m of channel and was handed to whichever cell the search reached first: whole grid columns went
  // to one stretch, so a cell 300 m off the channel could carry a stretch several hundred metres up or down it.
  const r2 = Math.SQRT1_2;
  const along = (e: number, n: number) => (e - n) * r2; // distance down the diagonal from the origin
  const across = (e: number, n: number) => (e + n) * r2; // offset from it (north-east positive)
  const dem = synthDem(240, 10, (e, n) => 100 - 0.001 * along(e, n) + 0.003 * Math.abs(across(e, n)));
  const pts: { e: number; n: number; x: number }[] = [];
  for (let x = 0; x <= 1500; x += 10) pts.push({ e: (x - 750) * r2, n: -(x - 750) * r2, x });
  const line: Centerline = { loc: LOC, pts };
  const secs = sections(31, () => 60_000).map((s) => ({ ...s, bedM: 100 - 0.001 * (s.x - 750) }));
  const r = spreadVolume(dem, line, secs, { minDepthM: 0.001 });
  const at = idxOf(dem);

  it("pours a cell 300 m off the channel from the stretch beside it", () => {
    let checked = 0;
    for (const x of [400, 600, 800, 1000]) {
      for (const side of [-1, 1]) {
        // The foot of the perpendicular is at channel distance x; step 300 m out across the channel.
        const e = (x - 750) * r2 + side * 300 * r2;
        const n = -(x - 750) * r2 + side * 300 * r2;
        const k = at(e, n);
        assert.ok(k >= 0 && r.depthM[k] > 0, `cell 300 m off at x=${x} is wet`);
        const j = Math.round((r.arrivalS[k] - 300) / 60);
        // Its stretch covers x (50 m stretches; one either side for the cell's own size).
        assert.ok(Math.abs(j * 50 + 25 - x) <= 75, `cell beside x=${x} m poured from stretch ${j} (${j * 50}-${j * 50 + 50} m)`);
        checked++;
      }
    }
    assert.equal(checked, 8);
  });
});

describe("spreadVolume: water that tops a saddle fills the low behind it lowest ground first", () => {
  // Channel at e = 0, a bank (crest 101) at e = 20..40, and beyond it a wide low whose ground varies cell to cell
  // between 99.0 and 99.9 (all of it below the bank, so it all shares the bank's spill height). A slice that tops the
  // bank but cannot fill the low must stand at the bank on the channel side and lie in the low's LOWEST cells, and
  // exactly hold its volume. The old pour perched it over the low in raster order (north rows first).
  const lowG = (e: number, n: number) => 99 + 0.9 * (((Math.round(e / 10) * 7 + Math.round(n / 10) * 13) % 10 + 10) % 10) / 10;
  const ground = (e: number, n: number) => (e < -5 ? 200 : e >= 20 && e < 40 ? 101 : e >= 40 && e < 600 ? lowG(e, n) : 100);
  const dem = synthDem(160, 10, (e, n) => ground(e, n));
  const line = straight(400, -400);
  const at = idxOf(dem);
  const secs = sections(17, () => 5_000);
  const r = spreadVolume(dem, line, secs, { minDepthM: 1e-6 });

  it("stands at the bank on the channel side", () => {
    const w = r.wseM[at(0, 0)];
    assert.ok(Math.abs(w - 101) < 1e-6, `channel surface ${w} is the bank crest 101`);
  });

  it("wets the low's lowest cells first", () => {
    // Within one stretch (n from 0 to -40 lies in stretch 8: x 400..450), every wet cell in the low is lower than
    // every dry one.
    let maxWet = -Infinity;
    let minDry = Infinity;
    let wet = 0;
    for (let e = 45; e < 590; e += 10)
      for (let n = -5; n > -45; n -= 10) {
        const k = at(e, n);
        if (r.depthM[k] > 0) {
          wet++;
          maxWet = Math.max(maxWet, dem.z[k]);
        } else minDry = Math.min(minDry, dem.z[k]);
      }
    assert.ok(wet > 0 && minDry < Infinity, `the low is partly wet (${wet} wet, dry from ${minDry})`);
    assert.ok(maxWet <= minDry + 1e-6, `wet ground up to ${maxWet} but dry ground from ${minDry}`);
  });

  it("lays down exactly the slices", () => {
    const want = secs.reduce((s, x) => s + x.storeMaxM3, 0);
    assert.ok(Math.abs(r.volumeM3 - want) / want < 1e-6, `${r.volumeM3} vs ${want}`);
  });
});
