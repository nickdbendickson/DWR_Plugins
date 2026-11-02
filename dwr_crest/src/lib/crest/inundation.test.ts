// The inundation-layer gate: routed for large floods, bathtub outline for small ones, decided on released volume.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { GATE_VOLUME_ACFT, chooseInundationLayer } from "./inundation.ts";
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
