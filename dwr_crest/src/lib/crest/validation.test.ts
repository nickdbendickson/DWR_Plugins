// Validation harness on tiny synthetic grids (no real model output is in the repo).
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { zone } from "./acer.ts";
import { projector, type Retained } from "./raster.ts";
import {
  type Candidate,
  featureDepth,
  fillPolygon,
  formatReport,
  rasterizeCandidate,
  type RefLayer,
  scoreExtent,
  scoreReceptors,
  summarizeReceptors,
  validate,
} from "./validation.ts";

const BREACH = { lon: -97.3252, lat: 46.9474 };
const CRS = "EPSG:32614";
const W = 100;
const H = 60;
const DX = 10;

/**
 * Reference: 100 × 60 cells of 10 m in UTM 14N. Wet 2 ft deep (1 ft/s, arrival (x − bx)/1000 h) in a 200 m band
 * (|y − by| < 100) from 200 m west of the breach to the east edge (800 m east of it).
 */
function ref(): RefLayer & { bx: number; by: number; x0: number; y0: number } {
  const proj = projector(CRS);
  const [bx0, by0] = proj.fwd(BREACH.lon, BREACH.lat);
  const bx = Math.round(bx0);
  const by = Math.round(by0);
  const x0 = bx - 200;
  const y0 = by + 300;
  const depth = new Float32Array(W * H);
  const velocity = new Float32Array(W * H);
  const arrival = new Float32Array(W * H).fill(NaN);
  for (let r = 0; r < H; r++)
    for (let c = 0; c < W; c++) {
      const x = x0 + (c + 0.5) * DX;
      const y = y0 - (r + 0.5) * DX;
      if (Math.abs(y - by) < 100) {
        depth[r * W + c] = 2;
        velocity[r * W + c] = 1;
        arrival[r * W + c] = Math.max(0, (x - bx) / 1000);
      }
    }
  const grid: Retained = { width: W, height: H, x0, y0, dx: DX, dy: DX, factor: 1, depth, velocity, arrival };
  return { grid, proj, bx, by, x0, y0 };
}

/** A lon/lat ring of the projected rectangle [xa, xb] × [ya, yb]. */
function rect(proj: RefLayer["proj"], xa: number, xb: number, ya: number, yb: number): number[][] {
  return [
    [xa, ya],
    [xb, ya],
    [xb, yb],
    [xa, yb],
    [xa, ya],
  ].map(([x, y]) => proj.inv(x, y));
}

const poly = (rings: number[][][], props: Record<string, unknown> = { depth_max_ft: 2 }): Candidate => ({
  kind: "polygons",
  features: [{ properties: props, geometry: { type: "Polygon", coordinates: rings } }],
});

const near = (a: number, b: number, tol: number, what: string) => assert.ok(Math.abs(a - b) <= tol, `${what}: ${a} vs ${b}`);

describe("featureDepth", () => {
  it("takes the band's middle, its max alone, or just wet", () => {
    assert.equal(featureDepth({ properties: { depth_min_ft: 1, depth_max_ft: 3 }, geometry: null }), 2);
    assert.equal(featureDepth({ properties: { depth_max_ft: 4 }, geometry: null }), 4);
    assert.equal(featureDepth({ properties: { band: "d2" }, geometry: null }), 0.1);
  });
});

describe("fillPolygon", () => {
  it("fills the cells whose centres are inside, holes excluded, and clips to the grid", () => {
    const R = ref();
    const { bx, by } = R;
    const hit = new Set<number>();
    // 100 m × 50 m rectangle east of the breach with a 20 m × 20 m hole: 50 − 4 = 46 cells.
    fillPolygon(
      R.grid,
      R.proj,
      [rect(R.proj, bx, bx + 100, by, by + 50), rect(R.proj, bx + 40, bx + 60, by + 10, by + 30)],
      (i) => hit.add(i),
    );
    assert.equal(hit.size, 46);
    const far = new Set<number>();
    fillPolygon(R.grid, R.proj, [rect(R.proj, bx + 10_000, bx + 10_100, by, by + 50)], (i) => far.add(i));
    assert.equal(far.size, 0);
    const over = new Set<number>();
    fillPolygon(R.grid, R.proj, [rect(R.proj, bx - 1000, bx + 2000, by - 1000, by + 1000)], (i) => over.add(i));
    assert.equal(over.size, W * H);
  });
});

describe("scoreExtent", () => {
  it("scores an identical polygon as a perfect match", () => {
    const R = ref();
    const c = rasterizeCandidate(poly([rect(R.proj, R.x0, R.x0 + W * DX, R.by - 100, R.by + 100)]), R);
    const e = scoreExtent(R, c);
    near(e.recall, 1, 0.02, "recall");
    near(e.precision, 1, 0.02, "precision");
    near(e.csi, 1, 0.03, "csi");
    near(e.areaRatio, 1, 0.03, "area ratio");
    near(e.refAc, (W * 20 * DX * DX) / 4046.8564224, 0.5, "ref acres");
    assert.equal(e.meanAbsDepthFt, 0);
    assert.equal(e.cells, W * H);
  });

  it("gives half recall for a half-width strip and half precision for a double-width one", () => {
    const R = ref();
    const half = scoreExtent(R, rasterizeCandidate(poly([rect(R.proj, R.x0, R.x0 + W * DX, R.by - 50, R.by + 50)]), R));
    near(half.recall, 0.5, 0.03, "half recall");
    near(half.precision, 1, 0.02, "half precision");
    near(half.areaRatio, 0.5, 0.03, "half area ratio");
    const twice = scoreExtent(R, rasterizeCandidate(poly([rect(R.proj, R.x0, R.x0 + W * DX, R.by - 200, R.by + 200)]), R));
    near(twice.recall, 1, 0.02, "double recall");
    near(twice.precision, 0.5, 0.03, "double precision");
    near(twice.areaRatio, 2, 0.06, "double area ratio");
    near(twice.csi, 0.5, 0.03, "double csi");
  });

  it("reports depth bias and |diff| where both are wet, and takes the deepest band where polygons overlap", () => {
    const R = ref();
    const c = rasterizeCandidate(
      {
        kind: "polygons",
        features: [
          { properties: { depth_max_ft: 1 }, geometry: { type: "Polygon", coordinates: [rect(R.proj, R.x0, R.x0 + W * DX, R.by - 100, R.by + 100)] } },
          { properties: { depth_max_ft: 3 }, geometry: { type: "Polygon", coordinates: [rect(R.proj, R.x0, R.x0 + W * DX, R.by - 100, R.by + 100)] } },
        ],
      },
      R,
    );
    const e = scoreExtent(R, c);
    near(e.depthBiasFt, 1, 1e-6, "bias");
    near(e.meanAbsDepthFt, 1, 1e-6, "mean abs");
    near(e.p90AbsDepthFt, 1, 1e-6, "p90");
  });

  it("counts only cells downstream of the breach when a direction is given", () => {
    const R = ref();
    const c = rasterizeCandidate(poly([rect(R.proj, R.x0, R.x0 + W * DX, R.by - 100, R.by + 100)]), R);
    const all = scoreExtent(R, c);
    const down = scoreExtent(R, c, { ...BREACH, u: [1, 0] });
    near(down.refAc / all.refAc, 0.8, 0.02, "downstream share");
    assert.ok(down.cells < all.cells);
  });

  it("takes a lon/lat grid and another projected grid as candidates, and flags model flood outside the grid", () => {
    const R = ref();
    // Lon/lat grid over the west half of the reference, 3 ft wet in the band.
    const [w, s] = R.proj.inv(R.x0, R.y0 - H * DX);
    const [e, n] = R.proj.inv(R.x0 + (W / 2) * DX, R.y0);
    const gw = 50;
    const gh = 60;
    const depth = new Float32Array(gw * gh);
    for (let r = 0; r < gh; r++)
      for (let cc = 0; cc < gw; cc++) {
        const lon = w + ((cc + 0.5) / gw) * (e - w);
        const lat = n - ((r + 0.5) / gh) * (n - s);
        const [, y] = R.proj.fwd(lon, lat);
        if (Math.abs(y - R.by) < 100) depth[r * gw + cc] = 3;
      }
    const half = scoreExtent(R, rasterizeCandidate({ kind: "lonlat", bbox: [w, s, e, n], width: gw, height: gh, depth }, R));
    near(half.recall, 0.5, 0.04, "lonlat recall");
    near(half.precision, 1, 0.02, "lonlat precision");
    near(half.depthBiasFt, 1, 1e-6, "lonlat bias");
    near(half.refOutsideCandAc / half.refAc, 0.5, 0.04, "outside share");
    const same = scoreExtent(R, rasterizeCandidate({ kind: "layer", grid: R.grid, proj: R.proj }, R));
    assert.equal(same.recall, 1);
    assert.equal(same.precision, 1);
    assert.equal(same.refOutsideCandAc, 0);
  });
});

describe("scoreReceptors", () => {
  it("classifies points as both wet, both dry, missed, extra or not covered, with zones from both layers", () => {
    const R = ref();
    // Candidate: the band's north half only, 2 ft, no velocity.
    const c = rasterizeCandidate(poly([rect(R.proj, R.x0, R.x0 + W * DX, R.by, R.by + 100)]), R);
    const at = (dx: number, dy: number) => R.proj.inv(R.bx + dx, R.by + dy);
    const pts = [
      { id: "north", ...xy(at(300, 50)) },
      { id: "south", ...xy(at(300, -50)) },
      { id: "dry", ...xy(at(300, 250)) },
      { id: "away", ...xy(at(50_000, 0)) },
    ];
    const rows = scoreReceptors(pts, R, c);
    assert.deepEqual(
      rows.map((r) => r.verdict),
      ["both wet", "missed", "both dry", "not covered"],
    );
    assert.equal(rows[0].ref.depthFt, 2);
    assert.equal(rows[0].ref.velocityFps, 1);
    near(rows[0].ref.arrivalHr!, 0.3, 0.011, "arrival");
    assert.equal(rows[0].depthDiffFt, 0);
    assert.equal(rows[0].cand.velocityFps, null);
    assert.equal(rows[0].cand.zone, null);
    assert.equal(rows[0].ref.zone, zone("house", 2, 1));
    const s = summarizeReceptors(rows);
    assert.equal(s.covered, 3);
    assert.equal(s.missed, 1);
    assert.equal(s.bothWet, 1);
    assert.equal(s.bothDry, 1);
    near(s.wetAgreement, 2 / 3, 1e-9, "agreement");
    assert.equal(s.depthMaeFt, 0);
    assert.equal(s.velocityMaeFps, null);
    assert.equal(s.zoned, 0);

    // A candidate grid with velocity and arrival gives zones and the time difference.
    const g: Candidate = { kind: "layer", grid: { ...R.grid, depth: R.grid.depth.map((d) => d * 2), arrival: R.grid.arrival!.map((a) => a + 0.5) }, proj: R.proj };
    const rows2 = scoreReceptors(pts.slice(0, 1), R, rasterizeCandidate(g, R));
    assert.equal(rows2[0].cand.zone, zone("house", 4, 1));
    assert.notEqual(rows2[0].cand.zone, rows2[0].ref.zone);
    near(rows2[0].arrivalDiffHr!, 0.5, 1e-6, "arrival diff");
    near(rows2[0].depthDiffFt!, 2, 1e-6, "depth diff");
    const s2 = summarizeReceptors(rows2);
    assert.equal(s2.zoned, 1);
    assert.equal(s2.zoneAgree, 0);
    near(s2.depthRmseFt!, 2, 1e-6, "rmse");
  });
});

describe("validate and formatReport", () => {
  it("puts the numbers in a readable block", () => {
    const R = ref();
    const r = validate(poly([rect(R.proj, R.x0, R.x0 + W * DX, R.by - 50, R.by + 50)]), R, {
      label: "flood strip",
      reference: "LISFLOOD-FP (test)",
      points: [{ id: "a", ...xy(R.proj.inv(R.bx + 300, R.by)) }],
      downstream: { ...BREACH, u: [1, 0] },
    });
    const text = formatReport(r);
    assert.match(text, /flood strip vs LISFLOOD-FP \(test\) \(downstream of the breach only\)/);
    assert.match(text, /recall 5\d %/);
    assert.match(text, /receptors 1\/1 inside the model grid: both wet 1/);
    assert.equal(r.receptors.summary.n, 1);
  });
});

function xy([lon, lat]: [number, number]) {
  return { lon, lat };
}
