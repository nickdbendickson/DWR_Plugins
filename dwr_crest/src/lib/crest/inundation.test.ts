// The inundation-layer gate: routed for large floods, bathtub outline for small ones, decided on released volume.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { gunzipSync } from "node:zlib";

import { analyze } from "./engine.ts";
import { buildInundation, GATE_VOLUME_ACFT, chooseInundationLayer } from "./inundation.ts";
import type { DemGrid } from "./outline.ts";
import { outlineInputs, outlineState, polygonizeDepth } from "./outline.ts";
import { SAMPLE_DAMS } from "./samples.ts";
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

describe("buildInundation on ND00199 (30 m fixture, 1,609 ac-ft → routed)", () => {
  const ND = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;
  const RESULT = analyze(ND);
  const fx = (name: string) => new URL(`./__fixtures__/${name}`, import.meta.url);
  const META = JSON.parse(readFileSync(fx("dem3dep_nd00199_30m.json"), "utf8"));
  const U16 = new Uint16Array(new Uint8Array(gunzipSync(readFileSync(fx("dem3dep_nd00199_30m.u16cm.gz")))).buffer);
  const DEM: DemGrid = { bbox: META.bbox, width: META.width, height: META.height, cellM: META.cellM, z: Float32Array.from(U16, (v) => v / 100 + 250) };

  it("builds the routed layer the gate picks and returns a ready-to-save cache", async () => {
    const r = await buildInundation(ND, RESULT, DEM);
    assert.ok(r.ok, r.ok ? "" : r.reason);
    if (!r.ok) return;
    assert.equal(r.layer, "routed");
    assert.equal(r.fellBackToOutline, false);
    assert.ok(r.routed?.ok);
    // The cache is a routed OutlineCache: tagged, carries the routed summary, draws like the outline.
    assert.equal(r.cache.layer, "routed");
    assert.ok(r.cache.routed);
    assert.equal(r.cache.outline.type, "FeatureCollection");
    assert.ok(r.cache.stats.outlineAc >= 0);
    // The routed layer traces no channel, so the bed/reach/cleanup stats are null, not a fake 0 that a
    // reader would take as measured. channelKm (from the routing) is still a real number.
    assert.equal(r.cache.stats.bedTopFt, null);
    assert.equal(r.cache.stats.bedEndFt, null);
    assert.equal(r.cache.stats.reachKm, null);
    assert.equal(r.cache.stats.droppedPatches, null);
    assert.equal(r.cache.stats.filledHoles, null);
    assert.ok(r.cache.stats.channelKm >= 0);
    // The fingerprint matches the outline inputs, so outlineState reads it as "ready" for this dam.
    const io = outlineInputs(ND, RESULT);
    assert.ok(io.ok);
    if (io.ok) assert.equal(r.cache.fingerprint, io.inputs.fingerprint);
    // The routed summary carries the flags and the attenuated peak.
    assert.ok(r.cache.routed!.peakInCfs > 0);
    assert.ok(r.cache.routed!.peakOutCfs <= r.cache.routed!.peakInCfs + 1);
    assert.equal(typeof r.cache.routed!.volumeFlag, "boolean");
    // The desk's routed layer is the volume spread, and the saved summary says so.
    assert.equal(r.routed?.ok && r.routed.method, "spread");
    assert.equal(r.cache.routed!.method, "spread");
    assert.equal(outlineState({ ...ND, outline: r.cache }, RESULT, true).state, "ready");
    // The triage's extent check is measured on the routed layer itself and saved with it.
    const e = r.cache.stats.extent!;
    assert.equal(e.basis, "routed-layer");
    assert.ok(e.wetAc > 0 && e.edgeM > 0 && e.outShiftM >= 0);
    assert.equal(e.volumeRatio, r.cache.routed!.volumeRatio);
  });

  it("measures a terrain outline's extent check on a routed-spread probe, and keeps the outline as the layer", async () => {
    const smallNd = { ...ND, storageAcFt: 120, surfaceAreaAc: 20 };
    const res = analyze(smallNd);
    assert.ok(res.breach.volumeAcFt < GATE_VOLUME_ACFT);
    const r = await buildInundation(smallNd, res, DEM);
    assert.ok(r.ok, r.ok ? "" : r.reason);
    if (!r.ok) return;
    assert.equal(r.layer, "outline");
    assert.equal(r.cache.routed, undefined);
    const e = r.cache.stats.extent!;
    assert.equal(e.basis, "routed-probe");
    assert.ok(e.wetAc > 0);
    assert.equal(typeof e.stoppedInBasin, "boolean");
    assert.equal(typeof e.reachedEdge, "boolean");
  });

  it("still builds the stage fill when asked, and reads a saved stage-fill layer as stale", async () => {
    const r = await buildInundation(ND, RESULT, DEM, { method: "stage" });
    assert.ok(r.ok && r.layer === "routed");
    if (!r.ok) return;
    assert.equal(r.cache.routed!.method, "stage");
    const { method: _drop, ...legacy } = r.cache.routed!;
    void _drop;
    for (const routed of [r.cache.routed!, legacy]) {
      const st = outlineState({ ...ND, outline: { ...r.cache, routed } }, RESULT, true);
      assert.equal(st.state, "build");
      assert.match(st.message ?? "", /old stage fill/);
    }
  });
});
