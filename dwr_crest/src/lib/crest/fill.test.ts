// FLDPLN-style fill: lay the routed water surface on a synthetic DEM and spread it.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { fillFlood } from "./fill.ts";
import type { DemGrid } from "./outline.ts";
import type { RoutedSection } from "./routing.ts";
import type { Centerline } from "./sections.ts";

const M_PER_DEG_LAT = 110574;
const M_PER_DEG_LON_EQ = 111320;
const kxOf = (lat: number) => M_PER_DEG_LON_EQ * Math.cos((lat * Math.PI) / 180);
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

/** The local-frame (e, n) of a cell centre, and a lookup of the cell index at (e, n). */
function frame(g: DemGrid) {
  const kx = kxOf(LOC.lat);
  const dlon = (g.bbox[2] - g.bbox[0]) / g.width;
  const dlat = (g.bbox[3] - g.bbox[1]) / g.height;
  const idx = (e: number, n: number) => {
    const lon = LOC.lon + e / kx;
    const lat = LOC.lat + n / M_PER_DEG_LAT;
    const i = Math.floor((lon - g.bbox[0]) / dlon);
    const j = Math.floor((g.bbox[3] - lat) / dlat);
    return i < 0 || j < 0 || i >= g.width || j >= g.height ? -1 : j * g.width + i;
  };
  return { idx };
}

/** A RoutedSection with the fields the fill reads (x, wseM, slopeStage) and sane defaults for the rest. */
function sec(x: number, wseM: number, slopeStage: number, bedM = wseM - 2): RoutedSection {
  return {
    x,
    bedM,
    slopeStage,
    slopeRouting: slopeStage,
    peakCms: 100,
    tPeakS: 0,
    tArrivalS: 0,
    stageM: wseM - bedM,
    wseM,
    areaM2: 50,
    topWidthM: 30,
    velocityMps: 1,
    capped: false,
  };
}

/** Straight centreline down the thalweg (e = 0) from n = fromN to n = toN; x = distance from the top. */
function straight(fromN: number, toN: number, step = 10): Centerline {
  const pts: { e: number; n: number; x: number }[] = [];
  let x = 0;
  let prev: number | null = null;
  for (let n = fromN; n >= toN - 1e-9; n -= step) {
    if (prev != null) x += Math.abs(prev - n);
    pts.push({ e: 0, n, x });
    prev = n;
  }
  return { loc: LOC, pts };
}

describe("fillFlood: bathtub on a sloping valley", () => {
  // Thalweg at e = 0, bed = 100 + 0.002 n (falls to the south); cross slope 2 % up each side.
  const dem = synthDem(240, 10, (e, n) => 100 + 0.002 * n + 0.02 * Math.abs(e));
  const line = straight(1000, -1000);
  // Routed WSE = channel bed + 2 m; slope = the 0.002 long slope. n = 1000 - x along the line.
  const sections: RoutedSection[] = [];
  for (let x = 0; x <= 2000; x += 50) {
    const n = 1000 - x;
    const bed = 100 + 0.002 * n;
    sections.push(sec(x, bed + 2, 0.002, bed));
  }
  const r = fillFlood(dem, line, sections, { minDepthM: 0.03 });
  const { idx } = frame(dem);

  it("wets the valley to about where the 2 m stage meets the 2 % banks", () => {
    assert.ok(r.wetCells > 0);
    // At mid-channel the depth is ~2 m; it falls away from the thalweg.
    const mid = idx(0, 0);
    assert.ok(Math.abs(r.depthM[mid] - 2) < 0.2, `channel depth ${r.depthM[mid]}`);
    // 2 m of stage on a 2 % bank reaches ~100 m each side; ~120 m is dry, ~80 m is wet.
    assert.ok(r.depthM[idx(80, 0)] > 0, "80 m from the channel is wet");
    assert.equal(r.depthM[idx(130, 0)], 0, "130 m from the channel is dry");
    // Depth falls with distance from the thalweg.
    assert.ok(r.depthM[idx(0, 0)] > r.depthM[idx(60, 0)] && r.depthM[idx(60, 0)] > r.depthM[idx(95, 0)]);
  });

  it("the water surface equals ground + depth and never exceeds the seeded surface", () => {
    const k = idx(70, 0);
    if (r.depthM[k] > 0) assert.ok(Math.abs(r.wseM[k] - (dem.z[k] + r.depthM[k])) < 1e-4);
    // Carried surface at a floodplain cell is at or below the channel seed's surface (102 at n=0).
    assert.ok(r.wseM[k] <= 102 + 1e-6);
  });
});

describe("fillFlood: backfill and spillover", () => {
  const line = straight(500, -500);
  const sections: RoutedSection[] = [];
  for (let x = 0; x <= 1000; x += 50) sections.push(sec(x, 104, 0.001, 102)); // flat routed WSE = 104

  it("backs water up into a depression below the water surface", () => {
    // Plain at 103 (1 m under the 104 surface) with a pit at (150, 0) down to 99: all wet, pit deepest.
    const dem = synthDem(160, 10, (e, n) => (Math.hypot(e - 150, n) < 25 ? 99 : 103));
    const r = fillFlood(dem, line, sections, { minDepthM: 0.03 });
    const { idx } = frame(dem);
    assert.ok(r.depthM[idx(150, 0)] > 4, `pit fills to ~5 m: ${r.depthM[idx(150, 0)]}`);
    assert.ok(r.depthM[idx(80, 0)] > 0, "the plain between is wet (~1 m)");
  });

  it("spills over a saddle only when the surface clears it", () => {
    // Channel plain at 103; a ridge at e≈200 up to 110 separates a far basin (e>220) at 99.
    const ridge = (e: number) => (e >= 190 && e <= 210 ? 110 : 99);
    const high = synthDem(200, 10, (e) => (e < 190 ? 103 : ridge(e)));
    const rHigh = fillFlood(high, line, sections, { minDepthM: 0.03 });
    const { idx: iHigh } = frame(high);
    assert.equal(rHigh.depthM[iHigh(300, 0)], 0, "far basin dry behind a 110 ridge (surface 104)");
    // Now notch the ridge to 103.5 over a 30 m gate near n = 0: water spills through to the far basin.
    const gated = synthDem(200, 10, (e, n) => {
      if (e < 190) return 103;
      if (e >= 190 && e <= 210) return Math.abs(n) < 15 ? 103.5 : 110;
      return 99;
    });
    const rGate = fillFlood(gated, line, sections, { minDepthM: 0.03 });
    const { idx: iGate } = frame(gated);
    assert.ok(rGate.depthM[iGate(300, 0)] > 0, "far basin wet after the saddle drops below the surface");
  });
});

describe("fillFlood: volume flag and the DEM edge", () => {
  const line = straight(400, -400);
  const sections: RoutedSection[] = [];
  for (let x = 0; x <= 800; x += 50) sections.push(sec(x, 104, 0.001, 102));
  const dem = synthDem(120, 10, () => 103); // a flat plain 1 m under the surface, so the flood runs off every edge

  it("flags when the filled volume exceeds what the breach supplies, and sees the DEM edge", () => {
    const r = fillFlood(dem, line, sections, { minDepthM: 0.03, availableVolumeM3: 1000 });
    assert.ok(r.volumeM3 > 1000, `laid down ${r.volumeM3.toFixed(0)} m³`);
    assert.equal(r.volumeFlag, true);
    assert.equal(r.reachedEdge, true, "a flat plain under the surface floods to the DEM edge");
    const ok = fillFlood(dem, line, sections, { minDepthM: 0.03, availableVolumeM3: 1e12 });
    assert.equal(ok.volumeFlag, false);
  });
});
