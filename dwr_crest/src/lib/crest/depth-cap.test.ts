import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { volumeCheck, VOLUME_RATIO_BAND } from "./inundation.ts";
import { bandDepthAcc, deepCellsNote, deepCellsOf, depthCapFt, type DemGrid, polygonizeDepth } from "./outline.ts";
import { featureDepth } from "./validation.ts";

describe("depth cap (QA 4-dam, 2026-11-03: band maxima of 215 ft on 27.5 ft DWR-2801)", () => {
  it("caps at the dam height with no margin, and not at all without a height", () => {
    assert.equal(depthCapFt(27.5), 27.5);
    assert.equal(depthCapFt(7), 7);
    assert.equal(depthCapFt(0), Infinity);
    assert.equal(depthCapFt(undefined), Infinity);
  });

  it("keeps a band's min/max/mean inside its own range after the mode pass", () => {
    const a = bandDepthAcc(3, 27.5); // 2–5 ft
    for (const d of [0.1, 3, 142.8]) a.add(d);
    const r = a.done();
    assert.equal(r.dMin, 2);
    assert.equal(r.dMax, 5);
    assert.ok(r.dMean >= 2 && r.dMean <= 5);
    const top = bandDepthAcc(5, 27.5); // 10 ft and deeper: up to the cap
    top.add(215);
    assert.equal(top.done().dMax, 27.5);
  });

  it("counts and flags cells deeper than the cap", () => {
    const d = deepCellsOf(Float32Array.from([1, 27.5, 40, 215, 0]), 27.5);
    assert.deepEqual(d, { capFt: 27.5, cells: 2, rawMaxFt: 215 });
    assert.equal(deepCellsOf(Float32Array.from([1, 40]), Infinity), undefined);
    assert.match(deepCellsNote({ deepCells: d })!, /2 cells .*dam height \(27\.5 ft; deepest 215 ft\)/);
    assert.equal(deepCellsNote({}), null);
  });

  it("a 200 ft pit inside a 3 ft flood no longer sets the band depth", () => {
    const W = 20;
    const H = 20;
    const g: DemGrid = { bbox: [-100, 47, -99.99, 47.01], width: W, height: H, cellM: 10, z: new Float32Array(W * H) };
    const depthFt = new Float32Array(W * H);
    for (let j = 5; j < 15; j++) for (let i = 5; i < 15; i++) depthFt[j * W + i] = 3;
    depthFt[10 * W + 10] = 200; // one sink cell, mode-filtered into the 2–5 ft band
    const common = { dam_id: "TEST", kind: "flood_strip" } as unknown as Parameters<typeof polygonizeDepth>[2]["common"];
    const before = polygonizeDepth(g, depthFt, { common, label: "t", title: "t" });
    const after = polygonizeDepth(g, depthFt, { common, label: "t", title: "t", capFt: 30 });
    for (const p of [before, after]) {
      const f = p.outline.features.find((x) => x.properties.band === "d3")!;
      assert.ok(f.properties.depth_max_ft <= 5, `band max ${f.properties.depth_max_ft}`);
      assert.ok(featureDepth(f) <= 5);
    }
    assert.equal(before.deepCells, undefined);
    assert.equal(after.deepCells?.cells, 1);
    assert.equal(after.deepCells?.rawMaxFt, 200);
    assert.equal(after.areaAc, before.areaAc); // extent unchanged
  });

  it("featureDepth prefers the band mean, then the min–max middle", () => {
    const geometry = null;
    assert.equal(featureDepth({ properties: { depth_min_ft: 2, depth_max_ft: 5, depth_mean_ft: 3.2 }, geometry }), 3.2);
    assert.equal(featureDepth({ properties: { depth_min_ft: 2, depth_max_ft: 5 }, geometry }), 3.5);
  });
});

describe("volumeCheck (laid down vs released, warn outside 2–6×)", () => {
  it("warns on the QA dams outside the band and not inside it", () => {
    assert.deepEqual(VOLUME_RATIO_BAND, { lo: 2, hi: 6 });
    const d2865 = volumeCheck(2130, 167)!; // ≈13×
    assert.ok(d2865.warn && /Above 6×/.test(d2865.text));
    const d3034 = volumeCheck(310, 71)!; // ≈4.4×
    assert.ok(!d3034.warn && /4\.4×/.test(d3034.text));
    assert.ok(volumeCheck(150, 100)!.warn); // 1.5×, under
    assert.equal(volumeCheck(100, 0), null);
    assert.equal(volumeCheck(undefined, 10), null);
  });
});

describe("bands on a dam under 10 ft (Docs & QA on #57: the 10 ft band reported 10 ft over a 7 ft cap)", () => {
  it("cells over the cap go to the top band at or under the cap, never the 10 ft and deeper band", () => {
    const W = 20;
    const H = 20;
    const g: DemGrid = { bbox: [-100, 47, -99.99, 47.01], width: W, height: H, cellM: 10, z: new Float32Array(W * H) };
    const depthFt = new Float32Array(W * H);
    for (let j = 4; j < 16; j++) for (let i = 4; i < 16; i++) depthFt[j * W + i] = j < 10 ? 3 : 14; // 14 ft sink half
    const common = { dam_id: "TEST", kind: "flood_strip" } as unknown as Parameters<typeof polygonizeDepth>[2]["common"];
    const p = polygonizeDepth(g, depthFt, { common, label: "t", title: "t", capFt: 7 });
    assert.equal(p.outline.features.find((f) => f.properties.band === "d5"), undefined);
    const top = p.outline.features.find((f) => f.properties.band === "d4")!;
    assert.ok(top, "the 5–10 ft band holds the capped cells");
    assert.equal(top.properties.depth_max_ft, 7);
    for (const f of p.outline.features) assert.ok(f.properties.depth_max_ft <= 7, `${f.properties.band} ${f.properties.depth_max_ft}`);
    assert.equal(p.deepCells?.cells, 72);
  });
});
