import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { analyze } from "./engine.ts";
import { applyPads, computePads, demAt, FT_PER_M, sectionBedM } from "./nd911-pads.ts";
import type { DemGrid } from "./outline.ts";
import { SAMPLE_DAMS } from "./samples.ts";
import type { DamRecord } from "./types.ts";

// Synthetic valley running east from the dam at (-97.4, 46.95): bed 320 m dropping 1 m/km, banks rising 0.02 m/m.
const LOC = { lon: -97.4, lat: 46.95 };
const KX = 111320 * Math.cos((LOC.lat * Math.PI) / 180);
const KY = 110574;
const bedM = (x: number) => 320 - x / 1000;
function grid(hole?: (x: number, y: number) => boolean): DemGrid {
  const bbox: [number, number, number, number] = [LOC.lon - 200 / KX, LOC.lat - 800 / KY, LOC.lon + 3000 / KX, LOC.lat + 800 / KY];
  const width = 320;
  const height = 160;
  const z = new Float32Array(width * height);
  for (let j = 0; j < height; j++)
    for (let i = 0; i < width; i++) {
      const lon = bbox[0] + ((i + 0.5) / width) * (bbox[2] - bbox[0]);
      const lat = bbox[3] - ((j + 0.5) / height) * (bbox[3] - bbox[1]);
      const x = (lon - LOC.lon) * KX;
      const y = (lat - LOC.lat) * KY;
      z[j * width + i] = hole?.(x, y) ? -9999 : bedM(x) + 0.02 * Math.abs(y);
    }
  return { bbox, width, height, cellM: 10, z };
}
const FRAME = { loc: LOC, u: [1, 0] as [number, number], halfAt: () => 100, streambedFt: 320 * FT_PER_M };
const at = (x: number, y: number) => ({ lon: LOC.lon + x / KX, lat: LOC.lat + y / KY });

describe("911 pads: ground at the point minus the channel bed at its station", () => {
  it("reads ground and the section's low point from the DEM", () => {
    const g = grid();
    const p = at(1000, 170);
    assert.ok(Math.abs(demAt(g, p.lon, p.lat)! - (bedM(1000) + 3.4)) < 0.15);
    assert.ok(Math.abs(sectionBedM(g, FRAME, 1000)! - bedM(1000)) < 0.15);
  });

  it("a house 11 ft above the bed gets an 11 ft pad; a point on the bed gets 0, never negative", () => {
    const yFor11 = (11 / FT_PER_M) / 0.02;
    const pads = computePads(
      [
        { id: "a", ...at(982, yFor11), distanceKm: 0.982 },
        { id: "b", ...at(500, 0), distanceKm: 0.5 },
      ],
      grid(),
      FRAME,
    );
    const a = pads.find((p) => p.id === "a")!;
    const b = pads.find((p) => p.id === "b")!;
    assert.equal(a.padSource, "dem", JSON.stringify(a));
    assert.ok(Math.abs(a.padFt - 11) <= 0.5, JSON.stringify(a));
    assert.ok(b.padFt >= 0 && b.padFt < 0.5, JSON.stringify(b));
  });

  it("no terrain at the point: pad 0, flagged estimated", () => {
    const [r] = computePads([{ id: "a", ...at(982, 120), distanceKm: 0.982 }], null, FRAME);
    assert.deepEqual([r.padFt, r.padSource], [0, "estimated"]);
    assert.match(r.note, /pad estimated/);
  });

  it("no terrain across the section: bed from the dam's streambed along the fitted valley slope", () => {
    // section at 2.0 km is a hole except right at the point; the slope comes from the 1.0 km point
    const g = grid((x) => x > 2020 && x < 2080);
    const pads = computePads(
      [
        { id: "ok", ...at(1000, 100), distanceKm: 1.0 },
        { id: "s", ...at(2000, 200), distanceKm: 2.05 },
      ],
      g,
      FRAME,
    );
    const s = pads.find((p) => p.id === "s")!;
    assert.equal(s.padSource, "dem-slope");
    assert.ok(Math.abs(s.padFt - 4.05 * FT_PER_M) < 1.0, `pad ${s.padFt}`);
  });
});

describe("2801-like: channel depth 9.56 ft, house 11 ft above the bed", () => {
  const nd = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;
  const seed = { id: "nd911-x", name: "911: 14751 30TH ST SE", kind: "house" as const, station: 0, distanceKm: 0.982, lateral: 0.68, padFt: 0, source: "nd911" as const, sourceRef: "x" };
  const base: DamRecord = { ...nd, receptors: [seed], receptorSource: "nd911", nd911AutoAdded: true, judgmentAsProbable: false };

  it("on the bed (pad 0, not looked up) the screen holds at Review instead of High", () => {
    const a = analyze(base);
    assert.ok(a.flood.receptors[0].depthFt > 9);
    assert.equal(a.screen.level, "Review");
    assert.match(a.screen.label, /ground heights not looked up/);
  });

  it("with its 11 ft pad the house is dry: not High", () => {
    const dam = { ...base, receptors: applyPads(base.receptors, [{ id: "nd911-x", padFt: 11, padSource: "dem", groundFt: 1040.8, bedFt: 1029.8, note: "" }]) };
    const a = analyze(dam);
    const r = a.flood.receptors[0];
    assert.equal(r.depthFt, 0);
    assert.equal(r.flooded, false);
    assert.notEqual(a.screen.level, "High");
    assert.equal(a.screen.level, "Low");
  });

  it("an estimated pad holds the screen at Review with a note", () => {
    const dam = { ...base, receptors: applyPads(base.receptors, [{ id: "nd911-x", padFt: 0, padSource: "estimated", groundFt: null, bedFt: null, note: "pad estimated" }]) };
    const a = analyze(dam);
    assert.equal(a.screen.level, "Review");
    assert.match(a.screen.label, /pad estimated/);
    assert.ok(a.screen.judgment.some((j) => /Pad estimated/.test(j)));
  });
});
