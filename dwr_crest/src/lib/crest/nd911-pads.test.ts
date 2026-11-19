import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { analyze } from "./engine.ts";
import { applyPads, computePads, demAt, FT_PER_M, lookupPads, PAD_NO_TERRAIN_NOTE, padsToRetry, resetFailedPads, sectionBedM } from "./nd911-pads.ts";
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

  it("a pit below the thalweg is clamped to the schematic bed, never inflating the pad (anti-conservative guard)", () => {
    // A 10 m borrow-pit gouged into the channel line (|y| < 40 m) at the 1.0 km station. Without the clamp the
    // section's lowest cell reads ~10 m below the thalweg and inflates the pad; with the clamp the pad bed holds at
    // the schematic streambed (the valley slope fitted from the clean anchor sections downstream).
    const pit = (x: number, y: number) => x > 980 && x < 1020 && Math.abs(y) < 40;
    const bbox: [number, number, number, number] = [LOC.lon - 200 / KX, LOC.lat - 800 / KY, LOC.lon + 3200 / KX, LOC.lat + 800 / KY];
    const width = 340,
      height = 160;
    const z = new Float32Array(width * height);
    for (let j = 0; j < height; j++)
      for (let i = 0; i < width; i++) {
        const lon = bbox[0] + ((i + 0.5) / width) * (bbox[2] - bbox[0]);
        const lat = bbox[3] - ((j + 0.5) / height) * (bbox[3] - bbox[1]);
        const x = (lon - LOC.lon) * KX;
        const y = (lat - LOC.lat) * KY;
        z[j * width + i] = bedM(x) + 0.02 * Math.abs(y) - (pit(x, y) ? 10 : 0);
      }
    const gp: DemGrid = { bbox, width, height, cellM: 10, z };
    const yFor11 = (11 / FT_PER_M) / 0.02; // ~168 m off the channel, clear of the pit
    const house = { id: "h", ...at(1000, yFor11), distanceKm: 1.0 };
    // Clean anchor sections (no pit) downstream fix the valley slope so the schematic bed sits near the true bed.
    const anchors = [1500, 2000, 2500, 3000].map((d) => ({ id: `a${d}`, ...at(d, 50), distanceKm: d / 1000 }));
    const pads = computePads([house, ...anchors], gp, FRAME);
    const r = pads.find((p) => p.id === "h")!;
    assert.equal(r.padSource, "dem", JSON.stringify(r));
    assert.match(r.note, /clamped up to the schematic streambed/);
    // The raw pit bed would have given ground - pitBed (~44 ft). Clamped pad is ~ground - schematic bed (~12 ft).
    const pitPad = r.groundFt! - (bedM(1000) - 10) * FT_PER_M;
    assert.ok(r.padFt < pitPad - 20, `clamped pad ${r.padFt} must be far below the unclamped ${pitPad.toFixed(1)}`);
    assert.ok(Math.abs(r.padFt - 11) <= 2.5, `clamped pad ${r.padFt} should be near the true 11 ft`);
  });

  it("no terrain at the point (a NoData hole in a grid that came back): pad 0, flagged estimated, says no terrain", () => {
    const g = grid((x, y) => x > 970 && x < 1000 && y > 100 && y < 140);
    const [r] = computePads([{ id: "a", ...at(982, 120), distanceKm: 0.982 }], g, FRAME);
    assert.deepEqual([r.padFt, r.padSource], [0, "estimated"]);
    assert.equal(r.note, PAD_NO_TERRAIN_NOTE);
  });

  it("the terrain REQUEST failed (502, timeout): pad 0, flagged estimated, but the note names the failure, not the ground", () => {
    const [r] = computePads([{ id: "a", ...at(982, 120), distanceKm: 0.982 }], null, FRAME, { error: "The 3DEP terrain service answered 502." });
    assert.deepEqual([r.padFt, r.padSource], [0, "estimated"]);
    assert.match(r.note, /^pad estimated: terrain request failed \(The 3DEP terrain service answered 502\)/);
    assert.doesNotMatch(r.note, /no terrain at the point/);
    const [bare] = computePads([{ id: "a", ...at(982, 120), distanceKm: 0.982 }], null, FRAME);
    assert.match(bare.note, /terrain request failed \(no reason given\)/);
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
    assert.ok(a.screen.judgment.some((j) => /Pad estimated/.test(j) && /no terrain at the point/.test(j)));
  });

  it("a pad estimated because the terrain request failed says so in the screen, not 'no terrain at the point'", async () => {
    const nd911 = { source: "t", attribution: "t", fetchedAt: "", bbox: [-98, 46, -97, 48] as [number, number, number, number], fields: [], points: [{ nguid: "x", lon: -97.3252, lat: 46.9474 } as never] };
    const dam: DamRecord = { ...base, nd911, receptors: [{ ...seed, padSource: undefined }] };
    const result = analyze(dam);
    const receptors = await lookupPads(dam, result, undefined, async () => {
      throw new Error("The 3DEP terrain service answered 502.");
    });
    assert.ok(receptors);
    const r = receptors![0];
    assert.equal(r.padSource, "estimated");
    assert.match(r.padNote ?? "", /terrain request failed \(The 3DEP terrain service answered 502\)/);
    const a = analyze({ ...dam, receptors: receptors! });
    assert.equal(a.screen.level, "Review");
    const j = a.screen.judgment.find((x) => /Pad estimated/.test(x))!;
    assert.match(j, /terrain request failed .*502.*look the pads up again/);
    assert.doesNotMatch(j, /no terrain at the point/);
    // The desk can reset exactly those seeds and read them again.
    assert.deepEqual(padsToRetry(receptors!).map((x) => x.id), ["nd911-x"]);
    const reset = resetFailedPads(receptors!);
    assert.equal(reset[0].padSource, undefined);
    assert.equal(padsToRetry(reset).length, 0);
    const noData = applyPads(base.receptors, [{ id: "nd911-x", padFt: 0, padSource: "estimated", groundFt: null, bedFt: null, note: PAD_NO_TERRAIN_NOTE }]);
    assert.equal(padsToRetry(noData).length, 0, "no terrain at the point is not retried");
  });
});
