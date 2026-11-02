// The inundation-layer gate: routed for large floods, bathtub outline for small ones, decided on released volume.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { GATE_VOLUME_ACFT, chooseInundationLayer } from "./inundation.ts";
import type { DemGrid } from "./outline.ts";
import { polygonizeDepth } from "./outline.ts";
import type { Analysis } from "./types.ts";

const withVolume = (volumeAcFt: number) => ({ breach: { volumeAcFt } }) as unknown as Analysis;

describe("chooseInundationLayer", () => {
  it("routes a large flood and outlines a small one, split at the threshold", () => {
    assert.equal(chooseInundationLayer(withVolume(GATE_VOLUME_ACFT + 1)).layer, "routed");
    assert.equal(chooseInundationLayer(withVolume(GATE_VOLUME_ACFT)).layer, "routed"); // ≥ is routed
    assert.equal(chooseInundationLayer(withVolume(GATE_VOLUME_ACFT - 1)).layer, "outline");
  });

  it("picks the right layer for each baseline dam's released volume", () => {
    // Released breach volumes measured on the four-dam baseline (ac-ft).
    assert.equal(chooseInundationLayer(withVolume(1612)).layer, "routed"); // DWR-2801, the one routed wins
    assert.equal(chooseInundationLayer(withVolume(167)).layer, "outline"); // DWR-2865
    assert.equal(chooseInundationLayer(withVolume(71)).layer, "outline"); // DWR-3034
    assert.equal(chooseInundationLayer(withVolume(7)).layer, "outline"); // DWR-4031
  });

  it("carries the released volume and a reason, and outlines a non-finite volume", () => {
    const c = chooseInundationLayer(withVolume(1612));
    assert.equal(c.releasedAcFt, 1612);
    assert.ok(c.reason.includes("1612") && c.reason.toLowerCase().includes("routed"));
    assert.equal(chooseInundationLayer(withVolume(Number.NaN)).layer, "outline");
  });
});

describe("polygonizeDepth", () => {
  // A 20×20 DEM frame with a 6×6 wet block (3 ft) in the middle, everything else dry.
  const W = 20;
  const H = 20;
  const g: DemGrid = { bbox: [-100, 47, -99.99, 47.01], width: W, height: H, cellM: 10, z: new Float32Array(W * H) };
  const depthFt = new Float32Array(W * H);
  for (let j = 7; j < 13; j++) for (let i = 7; i < 13; i++) depthFt[j * W + i] = 3;
  const common = { dam_id: "TEST", kind: "flood_strip" } as unknown as Parameters<typeof polygonizeDepth>[2]["common"];
  const poly = polygonizeDepth(g, depthFt, { common, label: "test flood", title: "TEST routed terrain flood" });

  it("traces the wet block into a FloodStrip-shaped band polygon", () => {
    assert.equal(poly.outline.type, "FeatureCollection");
    assert.ok(poly.outline.features.length >= 1);
    assert.ok(poly.areaAc > 0);
    assert.ok(poly.wetCells > 0);
    const f = poly.outline.features[0];
    assert.equal(f.properties.disclaimer, "test flood");
    assert.ok(f.geometry.type === "Polygon" || f.geometry.type === "MultiPolygon");
  });

  it("puts the polygon inside the DEM bbox and leaves dry grids empty", () => {
    const ring = poly.outline.features[0].geometry.type === "Polygon"
      ? (poly.outline.features[0].geometry.coordinates[0] as [number, number][])
      : (poly.outline.features[0].geometry.coordinates[0][0] as [number, number][]);
    for (const [lon, lat] of ring) {
      assert.ok(lon >= g.bbox[0] - 1e-6 && lon <= g.bbox[2] + 1e-6);
      assert.ok(lat >= g.bbox[1] - 1e-6 && lat <= g.bbox[3] + 1e-6);
    }
    const empty = polygonizeDepth(g, new Float32Array(W * H), { common, label: "x", title: "y" });
    assert.equal(empty.outline.features.length, 0);
    assert.equal(empty.wetCells, 0);
  });
});
