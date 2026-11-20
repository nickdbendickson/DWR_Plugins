// Lakes on the traced channel as level pools: detection, outlet weir, storage, and the routing's mass balance.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { embankmentTop, findPools, flatRuns, medianBeds, POOL_MIN_LEN_M, riseAt, weirOutflow } from "./pools.ts";
import { routeMuskingumCunge, type Section, sectionTable } from "./routing.ts";

/** A V-ish valley profile: bed at `bed`, flat floor `floorW` wide, side slope 1:`side`, 1 km each side. */
function valley(x: number, bed: number, floorW: number, side = 50): Section {
  const offsets: number[] = [];
  const z: number[] = [];
  for (let o = -1000; o <= 1000; o += 10) {
    offsets.push(o);
    z.push(bed + Math.max(0, Math.abs(o) - floorW / 2) / side);
  }
  return { x, offsets, z };
}

/** A dam: the profile crosses an embankment `topM` high everywhere but a spillway notch `notchW` wide at `crestM`. */
function damSection(x: number, crestM: number, topM: number, notchW: number): Section {
  const offsets: number[] = [];
  const z: number[] = [];
  for (let o = -1000; o <= 1000; o += 10) {
    offsets.push(o);
    z.push(Math.abs(o) <= notchW / 2 ? crestM : Math.abs(o) <= 600 ? topM : topM + (Math.abs(o) - 600) / 50);
  }
  return { x, offsets, z };
}

/**
 * River (0.1 % fall) for 3 km, a 4 km lake (hydro-flattened at 200 m, 1.2 km wide), a dam whose spillway crest is the
 * lake surface (embankment 6 m above), then river again below.
 */
function lakeReach(): Section[] {
  const s: Section[] = [];
  let x = 0;
  for (; x < 3000; x += 200) s.push(valley(x, 203 + (3000 - x) * 0.001, 40));
  for (; x <= 7000; x += 200) s.push(valley(x, 200, 1200, 100));
  s.push(damSection(x, 200, 206, 60));
  x += 200;
  for (let k = 0; k < 10; k++, x += 200) s.push(valley(x, 196 - k * 0.2, 40));
  return s;
}

describe("flatRuns and medianBeds", () => {
  it("finds a flat run of beds at least POOL_MIN_LEN_M long, and nothing in a falling river", () => {
    const flat = Array.from({ length: 10 }, (_, k) => ({ x: k * 200, bedM: 100 }));
    assert.deepEqual(flatRuns(flat), [[0, 9]]);
    const river = Array.from({ length: 10 }, (_, k) => ({ x: k * 200, bedM: 100 - k * 0.2 }));
    assert.deepEqual(flatRuns(river), []);
    const short = Array.from({ length: 3 }, (_, k) => ({ x: k * 200, bedM: 100 }));
    assert.ok(short[2].x < POOL_MIN_LEN_M);
    assert.deepEqual(flatRuns(short), []);
  });
  it("the running median keeps one lifted section (a narrows) from splitting a lake", () => {
    const beds = Array.from({ length: 20 }, (_, k) => ({ x: k * 200, bedM: k === 10 ? 101.5 : 100 }));
    assert.equal(flatRuns(beds).length, 2);
    assert.deepEqual(flatRuns(medianBeds(beds)), [[0, 19]]);
  });
});

describe("weirOutflow and embankmentTop", () => {
  it("nothing leaves at or below the lake surface; a notch passes C·w·h^1.5 above it", () => {
    const d = damSection(0, 200, 206, 60);
    assert.equal(weirOutflow(d, 200, 200), 0);
    const q = weirOutflow(d, 201, 200);
    // 7 points 10 m apart across the 60 m notch: about 70 m of crest at 1 m head.
    assert.ok(q > 1.7 * 60 && q < 1.7 * 80, `${q}`);
    assert.ok(weirOutflow(d, 207, 200) > 2 * weirOutflow(d, 205, 200), "over the embankment the whole crest flows");
  });
  it("the embankment top is the lower of the two sides' highest ground near the outlet", () => {
    assert.equal(embankmentTop(damSection(0, 200, 206, 60)), 206);
  });
});

describe("findPools", () => {
  it("finds the lake, its dam as the outlet, and a storage table that rises with the level", () => {
    const secs = lakeReach();
    const tables = secs.map((s) => sectionTable(s));
    const pools = findPools(secs, tables);
    assert.equal(pools.length, 1);
    const p = pools[0];
    assert.equal(secs[p.outlet].x, 7200, "the dam is the outlet");
    assert.ok(Math.abs(secs[p.from].x - 3000) <= 200);
    assert.ok(Math.abs(p.surfaceM - 200) < 1e-6);
    assert.equal(p.embankmentM, 206);
    for (let r = 1; r < p.volume.length; r++) assert.ok(p.volume[r] > p.volume[r - 1]);
    assert.equal(p.outflow[0], 0);
    // ~4 km × ~1.2 km of lake: 1 m of rise holds roughly 5 million m³.
    const v1 = p.volume[20];
    assert.ok(v1 > 3e6 && v1 < 8e6, `${v1}`);
    assert.ok(Math.abs(riseAt(p, v1) - 1) < 1e-6);
    assert.ok(p.surfaceAc > 800 && p.surfaceAc < 1600, `${p.surfaceAc} ac`);
  });
  it("a falling river has no pools", () => {
    const secs = Array.from({ length: 30 }, (_, k) => valley(k * 200, 300 - k * 0.3, 40));
    assert.deepEqual(findPools(secs, secs.map((s) => sectionTable(s))), []);
  });
});

describe("routeMuskingumCunge with a level pool", () => {
  it("the lake stores the wave: lower, later peak below it, and the volume still balances", () => {
    const secs = lakeReach();
    const tables = secs.map((s) => sectionTable(s));
    const pools = findPools(secs, tables);
    const tS: number[] = [];
    const qCms: number[] = [];
    for (let t = 0; t <= 24 * 3600; t += 300) {
      tS.push(t);
      qCms.push(t <= 3600 ? 5 + 2000 * (t / 3600) : 5 + 2000 * Math.max(0, 1 - (t - 3600) / (5 * 3600)));
    }
    const opts = { maxHours: 96, router: "mc" as const };
    const free = routeMuskingumCunge({ tS, qCms }, tables, opts);
    const held = routeMuskingumCunge({ tS, qCms }, tables, { ...opts, pools });
    const last = held.sections.length - 1;
    assert.ok(held.sections[last].peakCms < 0.8 * free.sections[last].peakCms, `${held.sections[last].peakCms} vs ${free.sections[last].peakCms}`);
    assert.ok(held.sections[last].tPeakS > free.sections[last].tPeakS);
    assert.equal(held.pools.length, 1);
    assert.ok(held.pools[0].peakOutCms < held.pools[0].peakInCms);
    assert.ok(held.pools[0].peakRiseM > 0.1);
    const p = pools[0];
    for (let j = p.from; j < p.outlet; j++) assert.equal(held.sections[j].pool, 0);
    // Mass balance: what left plus what the lake still holds is what came in.
    const err = Math.abs(held.volumeInM3 - held.volumeOutM3) / held.volumeInM3;
    assert.ok(err < 0.05, `volume error ${(err * 100).toFixed(2)} %`);
  });
});
