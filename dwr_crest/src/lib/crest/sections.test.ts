// Cutting cross-sections from a synthetic DEM along a centreline, and feeding them to the router.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { DemGrid } from "./outline.ts";
import { DEFAULT_ROUGHNESS, routeMuskingumCunge, sectionTable } from "./routing.ts";
import { type Centerline, cutSections, elevAt, stations } from "./sections.ts";

const M_PER_DEG_LAT = 110574;
const M_PER_DEG_LON_EQ = 111320;
const kxOf = (lat: number) => M_PER_DEG_LON_EQ * Math.cos((lat * Math.PI) / 180);
const LOC = { lon: -100, lat: 47 };

/**
 * A DEM centred on LOC, `cells` square at `cellM` m. `ground(e, n)` gives the elevation (m) at a local-frame point;
 * returns NaN to punch a NoData hole.
 */
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
      const g = ground(e, n);
      z[j * cells + i] = Number.isNaN(g) ? -9999 : g;
    }
  return { bbox, width: cells, height: cells, cellM, z };
}

/** V-valley along the north-south axis: thalweg at e = 0, 5 % side slopes, bed dropping 0.002 m/m toward the south. */
const vValley = (e: number, n: number) => 100 + 0.002 * n + 0.05 * Math.abs(e);

/** A straight centreline down the thalweg from north (x = 0) to south, every `step` m. */
function straightLine(fromN: number, toN: number, step: number): Centerline {
  const pts: { e: number; n: number; x: number }[] = [];
  let x = 0;
  let prev: { e: number; n: number } | null = null;
  for (let n = fromN; n >= toN - 1e-9; n -= step) {
    const p = { e: 0, n };
    if (prev) x += Math.hypot(p.e - prev.e, p.n - prev.n);
    pts.push({ ...p, x });
    prev = p;
  }
  return { loc: LOC, pts };
}

describe("elevAt", () => {
  it("bilinearly reproduces a planar DEM and falls back near a NoData hole", () => {
    const planar = synthDem(40, 10, (e, n) => 50 + 0.01 * e + 0.02 * n);
    for (const [e, n] of [
      [0, 0],
      [33, -47],
      [-12.5, 18.3],
    ]) {
      const got = elevAt(planar, LOC, e, n)!;
      assert.ok(Math.abs(got - (50 + 0.01 * e + 0.02 * n)) < 0.05, `planar at ${e},${n}: ${got}`);
    }
    assert.equal(elevAt(planar, LOC, 1e6, 0), null, "off the grid is null");
    // A hole at the four cells around the origin: elevAt still returns a nearby value, not null.
    const holed = synthDem(40, 10, (e, n) => (Math.abs(e) < 11 && Math.abs(n) < 11 ? NaN : 50 + 0.01 * e + 0.02 * n));
    assert.equal(elevAt(holed, LOC, 60, 60)! > 0, true);
  });
});

describe("stations", () => {
  it("walks the line at the asked spacing with unit downstream tangents", () => {
    const line = straightLine(500, -500, 10);
    const sts = stations(line, 50, 60);
    assert.ok(sts.length >= 20 && sts.length <= 22, String(sts.length));
    for (const s of sts) assert.ok(Math.abs(Math.hypot(s.te, s.tn) - 1) < 1e-9, "unit tangent");
    // South-flowing channel: tangent points to -n (te≈0, tn≈-1); x rises along the run.
    for (const s of sts) assert.ok(Math.abs(s.te) < 1e-6 && s.tn < -0.99);
    for (let i = 1; i < sts.length; i++) assert.ok(sts[i].x > sts[i - 1].x, "x increases");
  });
});

describe("cutSections on a V-valley", () => {
  const line = straightLine(500, -500, 10);
  const secs = cutSections(synthDem(140, 10, vValley), line, { stationM: 50, halfWidthM: 150 });

  it("puts the thalweg at the centre and recovers the V and the side slope", () => {
    assert.ok(secs.length >= 18, String(secs.length));
    for (const s of secs) {
      // Lowest point near offset 0.
      let it = 0;
      for (let i = 1; i < s.z.length; i++) if (s.z[i] < s.z[it]) it = i;
      assert.ok(Math.abs(s.offsets[it]) <= 12, `thalweg near centre: off ${s.offsets[it]}`);
      // 5 % side slope: ~5 m of rise per 100 m of offset from the thalweg.
      const far = s.z.length - 1;
      const rise = s.z[far] - s.z[it];
      const run = Math.abs(s.offsets[far] - s.offsets[it]);
      assert.ok(Math.abs(rise / run - 0.05) < 0.01, `side slope ${(rise / run).toFixed(3)}`);
    }
  });

  it("x increases downstream and the bed falls at the valley's long slope", () => {
    for (let i = 1; i < secs.length; i++) assert.ok(secs[i].x > secs[i - 1].x);
    const bed = (s: (typeof secs)[number]) => Math.min(...Array.from(s.z));
    const first = secs[0];
    const last = secs[secs.length - 1];
    const drop = bed(first) - bed(last);
    const run = last.x - first.x;
    // Bed drops 0.002 m/m along the channel (n falls as x rises).
    assert.ok(Math.abs(drop / run - 0.002) < 0.0005, `long slope ${(drop / run).toFixed(4)}`);
  });

  it("the cut sections drive the router: a routed peak that conserves mass", () => {
    const tables = secs.map((s) => sectionTable(s, DEFAULT_ROUGHNESS, 20, 0.05));
    const tS: number[] = [];
    const qCms: number[] = [];
    for (let t = 0; t <= 6 * 3600; t += 60) {
      tS.push(t);
      qCms.push(t <= 1800 ? (200 * t) / 1800 : t <= 5400 ? 200 * (1 - (t - 1800) / 3600) : 0);
    }
    const r = routeMuskingumCunge({ tS, qCms }, tables, { maxHours: 48, recessionFraction: 0.001 });
    assert.ok(r.sections.every((s) => Number.isFinite(s.peakCms) && s.peakCms >= 0));
    assert.ok(Math.abs(r.volumeOutM3 / r.volumeInM3 - 1) < 0.02, `mass ${(r.volumeOutM3 / r.volumeInM3).toFixed(4)}`);
    assert.ok(r.sections.at(-1)!.tPeakS >= r.sections[0].tPeakS, "peak delayed downstream");
  });
});

describe("cutSections on a bent channel", () => {
  it("follows a 90° bend, keeping the thalweg centred through the turn", () => {
    // Valley that runs south then east, thalweg a quarter-circle; ground = base + 0.05 * distance-to-thalweg.
    const R = 200;
    const centre = { e: R, n: 0 };
    const dem = synthDem(160, 10, (e, n) => {
      // Straight south reach for n > 0 (thalweg at e = 0); a quarter arc of radius R for n <= 0 turning east.
      let d: number;
      if (n > 0) d = Math.abs(e);
      else d = Math.abs(Math.hypot(e - centre.e, n - centre.n) - R);
      return 100 + 0.05 * d;
    });
    const pts: { e: number; n: number; x: number }[] = [];
    let x = 0;
    let prev: { e: number; n: number } | null = null;
    for (let n = 500; n >= 0; n -= 10) {
      const p = { e: 0, n };
      if (prev) x += Math.hypot(p.e - prev.e, p.n - prev.n);
      pts.push({ ...p, x });
      prev = p;
    }
    // a: π → 3π/2 sweeps the thalweg from the junction (0,0) heading south round to heading east, ending at (R,-R).
    for (let a = Math.PI; a <= 1.5 * Math.PI + 1e-9; a += 10 / R) {
      const p = { e: centre.e + R * Math.cos(a), n: centre.n + R * Math.sin(a) };
      x += Math.hypot(p.e - prev!.e, p.n - prev!.n);
      pts.push({ ...p, x });
      prev = p;
    }
    const secs = cutSections(dem, { loc: LOC, pts }, { stationM: 40, halfWidthM: 120 });
    assert.ok(secs.length >= 20, String(secs.length));
    let near = 0;
    for (const s of secs) {
      let it = 0;
      for (let i = 1; i < s.z.length; i++) if (s.z[i] < s.z[it]) it = i;
      if (Math.abs(s.offsets[it]) <= 25) near++;
    }
    // The great majority of sections, including through the bend, find the thalweg near their centre.
    assert.ok(near >= secs.length - 3, `${near}/${secs.length} centred`);
  });
});
