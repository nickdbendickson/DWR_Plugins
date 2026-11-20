import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { extentSensitivity } from "./extent.ts";
import type { DemGrid } from "./outline.ts";

/** A W × H grid whose ground rises from a centre column at `slopeM` per cell, with a level pool at `poolM`. */
function valley(W: number, H: number, slopeM: number, poolM: number, cellM = 10) {
  const mid = (W - 1) / 2;
  const z = new Float32Array(W * H);
  const depthFt = new Float32Array(W * H);
  for (let r = 0; r < H; r++)
    for (let c = 0; c < W; c++) {
      const k = r * W + c;
      z[k] = Math.abs(c - mid) * slopeM;
      depthFt[k] = z[k] < poolM ? (poolM - z[k]) / 0.3048 : 0;
    }
  const g: DemGrid = { bbox: [0, 0, 1, 1], width: W, height: H, cellM, z };
  return { g, depthFt };
}

describe("extent sensitivity", () => {
  it("a steep valley barely moves with a foot of depth error", () => {
    const { g, depthFt } = valley(41, 20, 0.5, 0.6); // 5 % banks: a foot of rise wets nothing new
    const e = extentSensitivity(g, depthFt);
    assert.equal(e.outerAc, 0);
    assert.equal(e.outShiftM, 0);
    assert.equal(e.edgeM, 2 * 20 * 10); // two side edges, the DEM border is not edge
  });

  it("a flat plain moves its edge a long way: edge shift = 1 ft ÷ slope", () => {
    const { g, depthFt } = valley(201, 20, 0.02, 1.01); // 0.2 % plain, 2 cm per 10 m cell
    const e = extentSensitivity(g, depthFt);
    // 0.3048 m / 0.02 m per cell = 15 cells = 150 m each side, plus the two cells under 0.1 ft that the layer
    // already counts dry (they sit below the surface, so a foot of rise wets them too).
    assert.equal(e.outShiftM, 170);
    assert.ok(e.sensitivity > 0.5);
  });

  it("the edge shift is a distance, so a narrower flood on the same slope scores the same", () => {
    const wide = extentSensitivity(...(Object.values(valley(301, 10, 0.02, 2)) as [DemGrid, Float32Array]));
    const narrow = extentSensitivity(...(Object.values(valley(301, 10, 0.02, 0.5)) as [DemGrid, Float32Array]));
    assert.equal(wide.outShiftM, narrow.outShiftM);
    assert.ok(narrow.sensitivity > wide.sensitivity, "the area ratio does grow as the flood shrinks");
  });

  it("stops at the grow limit and says so", () => {
    const { g, depthFt } = valley(401, 6, 0.001, 0.1, 10); // nearly dead flat
    const e = extentSensitivity(g, depthFt, { reachM: 100 });
    assert.equal(e.truncated, true);
    assert.equal(e.outShiftM, 100); // ten cells, the limit
  });

  it("does not climb over a ridge into a low pocket behind it", () => {
    const W = 30;
    const H = 4;
    const z = new Float32Array(W * H);
    const depthFt = new Float32Array(W * H);
    for (let r = 0; r < H; r++)
      for (let c = 0; c < W; c++) {
        const k = r * W + c;
        z[k] = c < 5 ? 0 : c < 8 ? 3 : 0; // channel, a 3 m ridge, then low ground
        depthFt[k] = c < 5 ? 1 / 0.3048 : 0;
      }
    const e = extentSensitivity({ bbox: [0, 0, 1, 1], width: W, height: H, cellM: 10, z }, depthFt);
    assert.equal(e.outerAc, 0);
  });

  it("missing ground is neither fringe nor edge", () => {
    const { g, depthFt } = valley(41, 10, 0.02, 1);
    for (let k = 0; k < g.z.length; k++) if (depthFt[k] === 0) g.z[k] = -9999;
    const e = extentSensitivity(g, depthFt);
    assert.equal(e.outerAc, 0);
    assert.equal(e.edgeM, 0);
    assert.equal(e.outShiftM, 0);
  });

  it("counts water under a foot as the inner fringe", () => {
    const { g, depthFt } = valley(201, 10, 0.02, 1);
    const e = extentSensitivity(g, depthFt);
    assert.ok(e.innerAc > 0);
    assert.ok(Math.abs(e.inShiftM - 150) <= 10, `inShift ${e.inShiftM}`);
  });
});
