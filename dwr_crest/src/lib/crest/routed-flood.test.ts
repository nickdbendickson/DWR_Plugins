// The routed terrain flood end to end, on the real 30 m ND00199 fixture (same fixture as outline.test).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { gunzipSync } from "node:zlib";

import { analyze } from "./engine.ts";
import type { DemGrid } from "./outline.ts";
import { buildRoutedFlood, recutCapped, TRACE_EDGE_CELLS, traceEndsAtEdge } from "./routed-flood.ts";
import { DEFAULT_ROUGHNESS, type Section, sectionTable } from "./routing.ts";
import { SAMPLE_DAMS } from "./samples.ts";

const ND = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;
const RESULT = analyze(ND);
const fx = (name: string) => new URL(`./__fixtures__/${name}`, import.meta.url);
const META = JSON.parse(readFileSync(fx("dem3dep_nd00199_30m.json"), "utf8"));
const U16 = new Uint16Array(new Uint8Array(gunzipSync(readFileSync(fx("dem3dep_nd00199_30m.u16cm.gz")))).buffer);
const DEM: DemGrid = { bbox: META.bbox, width: META.width, height: META.height, cellM: META.cellM, z: Float32Array.from(U16, (v) => v / 100 + 250) };

const RF = await buildRoutedFlood(ND, RESULT, DEM, { method: "stage" });
const SP = await buildRoutedFlood(ND, RESULT, DEM, { method: "spread" });
const DEF = await buildRoutedFlood(ND, RESULT, DEM); // no method → the shipped default

describe("buildRoutedFlood on ND00199 (30 m fixture)", () => {
  it("runs the whole chain and returns a depth grid on the DEM frame", () => {
    assert.ok(RF.ok, RF.ok ? "" : RF.reason);
    if (!RF.ok) return;
    assert.deepEqual(RF.bbox, DEM.bbox);
    assert.equal(RF.width, DEM.width);
    assert.equal(RF.height, DEM.height);
    assert.equal(RF.depthFt.length, DEM.width * DEM.height);
    // Some cells are wet, and every depth is finite and non-negative.
    let wet = 0;
    for (const d of RF.depthFt) {
      assert.ok(Number.isFinite(d) && d >= 0);
      if (d > 0) wet++;
    }
    assert.ok(wet > 0, "the routed flood wets some ground");
    assert.ok(RF.stats.floodedAc > 0);
  });

  it("routes a breach peak that attenuates downstream and carries the step-2/3/4 outputs", () => {
    assert.ok(RF.ok);
    if (!RF.ok) return;
    assert.ok(RF.stats.peakInCfs > 0);
    assert.ok(RF.stats.peakOutCfs <= RF.stats.peakInCfs + 1, "the peak does not grow downstream");
    assert.ok(RF.sections.length >= 2);
    // WSE where wet equals ground + depth is enforced inside fill.ts; here just check the grids line up.
    assert.equal(RF.wseM.length, RF.depthFt.length);
    // The stats carry the flags the method surfaces.
    assert.equal(typeof RF.stats.volumeFlag, "boolean");
    assert.equal(typeof RF.stats.reachedEdge, "boolean");
  });

  it("routes the Breach step's own peak: the Flood step's breach peak equals the routed breach peak", () => {
    for (const r of [RF, SP]) {
      assert.ok(r.ok);
      if (!r.ok) continue;
      assert.equal(r.stats.peakInCfs, Math.round(RESULT.breach.peakCfs));
    }
    // The QA record (overtopping, NID storage and length, no surface area): 29,321 cfs on both steps, not 29,077.
    const qa = { ...ND, failureMode: "overtopping" as const, storageAcFt: 1615.5, lengthFt: 1700, surfaceAreaAc: undefined };
    const res = analyze(qa);
    assert.equal(Math.round(res.breach.peakCfs), 29321);
    assert.equal(res.breach.series.reduce((m, p) => Math.max(m, p.qCfs), 0), res.breach.peakCfs);
  });

  it("carries the volume-ratio provenance fields", () => {
    assert.ok(RF.ok);
    if (!RF.ok) return;
    assert.equal(RF.stats.releasedAcFt, Math.round(RESULT.breach.volumeAcFt));
    assert.ok(RF.stats.fillAcFt >= 0);
    // volumeRatio is fillAcFt over the released volume, so it reproduces the fill from the ratio.
    if (RF.stats.releasedAcFt > 0) {
      const expected = Math.round((RF.stats.fillAcFt / RF.stats.releasedAcFt) * 10) / 10;
      // The stat is computed from the unrounded volumes, so allow the two roundings (fill to 1 ac-ft, ratio to 0.1).
      assert.ok(Math.abs(RF.stats.volumeRatio - expected) <= 0.1 + 1e-9, `${RF.stats.volumeRatio} vs ${expected}`);
    }
  });

  it("fails cleanly when the breach has no hydrograph", async () => {
    const noSeries = { ...RESULT, breach: { ...RESULT.breach, series: [] } };
    const r = await buildRoutedFlood(ND, noSeries, DEM);
    assert.equal(r.ok, false);
  });
});

describe("buildRoutedFlood, method spread (quasi-dynamic volume spread)", () => {
  it("builds a layer with depth, velocity and arrival on the DEM frame", () => {
    assert.ok(SP.ok, SP.ok ? "" : SP.reason);
    if (!SP.ok) return;
    assert.equal(SP.method, "spread");
    assert.ok(SP.velocityFps && SP.arrivalHr);
    let wet = 0;
    for (let k = 0; k < SP.depthFt.length; k++)
      if (SP.depthFt[k] > 0) {
        wet++;
        assert.ok(Number.isFinite(SP.velocityFps![k]) && SP.velocityFps![k] >= 0);
        assert.ok(Number.isFinite(SP.arrivalHr![k]) && SP.arrivalHr![k] >= 0);
      }
    assert.ok(wet > 0);
  });

  it("records the method and an envelope-to-release ratio far below the stage fill's", () => {
    assert.ok(SP.ok && RF.ok);
    if (!SP.ok || !RF.ok) return;
    assert.ok(SP.notes.some((n) => n.startsWith("Volume spread:")));
    assert.ok(SP.stats.volumeRatio > 0);
    assert.ok(SP.stats.volumeRatio < RF.stats.volumeRatio, `${SP.stats.volumeRatio} vs ${RF.stats.volumeRatio}`);
  });

  it("carries the exhausted-stretch cap's accounting in the stats (zero for the stage fill)", () => {
    assert.ok(SP.ok && RF.ok);
    if (!SP.ok || !RF.ok) return;
    for (const s of [SP.stats, RF.stats]) {
      assert.ok(Number.isInteger(s.exhaustedStretches) && s.exhaustedStretches >= 0);
      assert.ok(Number.isInteger(s.cappedStretches) && s.cappedStretches <= s.exhaustedStretches);
      assert.ok(Number.isFinite(s.unpouredAcFt) && s.unpouredAcFt >= 0);
    }
    assert.equal(RF.stats.exhaustedStretches, 0);
    assert.equal(RF.stats.unpouredAcFt, 0);
    if (SP.stats.cappedStretches) assert.ok(SP.notes.some((n) => /held at the routed stage \+ 1 ft/.test(n)));
    else assert.equal(SP.stats.unpouredAcFt, 0);
  });

  it("defaults to the spread method when none is given", () => {
    assert.ok(DEF.ok);
    if (!DEF.ok) return;
    assert.equal(DEF.method, "spread");
  });

  it("the explicit stage method carries no velocity or arrival", () => {
    assert.ok(RF.ok);
    if (!RF.ok) return;
    assert.equal(RF.method, "stage");
    assert.equal(RF.velocityFps, undefined);
  });
});

describe("traceEndsAtEdge", () => {
  const loc = { lon: -100, lat: 46 };
  const kx = 111320 * Math.cos((46 * Math.PI) / 180);
  // 100 × 100 cells of 10 m around the start.
  const g = { bbox: [-100 - 500 / kx, 46 - 500 / 110574, -100 + 500 / kx, 46 + 500 / 110574] as [number, number, number, number], width: 100, height: 100 };
  const line = (e: number) => ({ loc, pts: [{ e: 0, n: 0, x: 0 }, { e, n: 0, x: e }] });
  it("is true when the channel ends within TRACE_EDGE_CELLS of the border, false inside", () => {
    assert.ok(traceEndsAtEdge(g, line(500 - (TRACE_EDGE_CELLS - 1) * 10)));
    assert.ok(!traceEndsAtEdge(g, line(300)));
    assert.ok(!traceEndsAtEdge(g, { loc, pts: [] }));
  });
});

describe("recutCapped", () => {
  /** A channel 20 m wide and 1 m deep, its left bank low (1 m) and its right bank a 6 m bluff, floodplain beyond the left. */
  const sec = (x: number, bed: number): Section => ({
    x,
    offsets: [-400, -30, -11, -10, 10, 11, 60],
    z: [bed + 0.5, bed + 0.5, bed + 1, bed, bed, bed + 6, bed + 6],
  });
  it("cuts taller only the sections that cannot carry the peak; a carrying valley is untouched", () => {
    const secs = Array.from({ length: 6 }, (_, k) => sec(k * 500, 100 - k * 0.5));
    const opts = { rough: DEFAULT_ROUGHNESS, tableOpts: {} };
    const small = secs.map((q) => sectionTable(q));
    const before = small.map((t) => t.maxDepthM);
    const none = recutCapped(secs, small, 1, opts);
    assert.deepEqual(none, { taller: 0, still: 0, note: null });
    assert.deepEqual(small.map((t) => t.maxDepthM), before);
    const big = secs.map((q) => sectionTable(q));
    const r = recutCapped(secs, big, 2000, opts);
    assert.equal(r.taller, secs.length);
    assert.ok(big.every((t, j) => t.maxDepthM > before[j]), "the tables rise past the lower bank");
    assert.match(r.note ?? "", /cut taller/);
  });
});
