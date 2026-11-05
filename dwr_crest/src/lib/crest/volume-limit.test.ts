import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { gunzipSync } from "node:zlib";

import { analyze, damLocation } from "./engine.ts";
import { volumeCheck } from "./inundation.ts";
import {
  bowlM3,
  buildOutline,
  channelPitBudget,
  channelPitsNote,
  CROSSING_STORAGE_MULT,
  crossingChargeM3,
  type DemGrid,
  SMALL_RELEASE_ACFT,
  SMALL_RELEASE_LABEL,
  smallRelease,
  TRACE_VERSION,
  VOLUME_SHRINK_ABOVE,
  VOLUME_TARGET_RATIO,
  volumeLimitNote,
} from "./outline.ts";
import { SAMPLE_DAMS } from "./samples.ts";
import type { DamRecord } from "./types.ts";

// The ND00199 3DEP fixture (30 m), as outline.test.ts uses it.
const fx = (name: string) => new URL(`./__fixtures__/${name}`, import.meta.url);
const META = JSON.parse(readFileSync(fx("dem3dep_nd00199_30m.json"), "utf8"));
const U16 = new Uint16Array(new Uint8Array(gunzipSync(readFileSync(fx("dem3dep_nd00199_30m.u16cm.gz")))).buffer);
const DEM: DemGrid = { bbox: META.bbox, width: META.width, height: META.height, cellM: META.cellM, z: Float32Array.from(U16, (v) => v / 100 + 250) };
const ND = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;
const NOW = new Date("2026-11-03T20:30:00-06:00");
const ACRE_FT_M3 = 1233.48184;

/** ND00199 with its storage (the released volume) set to `acFt`. */
const withRelease = (acFt: number): DamRecord => ({ ...ND, storageAcFt: acFt });
async function build(dam: DamRecord, g: DemGrid = DEM) {
  const result = analyze(dam);
  const b = await buildOutline(dam, result, g, { builtAt: NOW });
  assert.ok(b.ok, b.ok ? "" : b.reason);
  return { result, released: result.breach.volumeAcFt, stats: b.cache.stats, outline: b.cache.outline, centerline: b.centerline };
}

describe("volume soft limit: about 5× the release (Nick, 2026-11-03)", () => {
  it("the target and the trigger are 5× and 6×, and saved layers rebuild (trace version 6)", () => {
    assert.equal(VOLUME_TARGET_RATIO, 5);
    assert.equal(VOLUME_SHRINK_ABOVE, 6);
    assert.equal(TRACE_VERSION, 7);
  });

  it("the outline GeoJSON carries the trace version it was built with", async () => {
    const { outline } = await build(ND);
    assert.equal(outline.properties.trace_version, TRACE_VERSION);
  });

  it("inside the band (2.4× on the full ND00199 release) the outline is left as the desk lays it", async () => {
    const { stats, released } = await build(ND);
    const ratio = stats.laidAcFt! / released;
    assert.ok(ratio > 2 && ratio < VOLUME_SHRINK_ABOVE, String(ratio));
    assert.equal(stats.volumeLimit, undefined);
    assert.equal(stats.volumeLabel, undefined);
    const vc = volumeCheck(stats.laidAcFt, released, {})!;
    assert.equal(vc.warn, false);
    assert.equal(vc.label, null);
  });

  for (const acFt of [300, 100]) {
    it(`over 6× (${acFt} ac-ft released) it lowers the water surface until the outline lays about 5×`, async () => {
      const { stats, released, outline } = await build(withRelease(acFt));
      const v = stats.volumeLimit;
      assert.ok(v, "the limit applied");
      // As first laid it was over the trigger…
      assert.ok(v.rawLaidAcFt / released > VOLUME_SHRINK_ABOVE, String(v.rawLaidAcFt / released));
      // …and now it lays about 5×, never over it.
      const ratio = stats.laidAcFt! / released;
      assert.ok(ratio <= VOLUME_TARGET_RATIO + 0.01 && ratio >= 0.9 * VOLUME_TARGET_RATIO, String(ratio));
      // Depth and extent shrink together: one factor on the desk depths, a smaller wet area.
      assert.ok(v.depthScale > 0 && v.depthScale < 1, String(v.depthScale));
      assert.ok(stats.wetCellAc < v.rawWetAc, `${stats.wetCellAc} vs ${v.rawWetAc}`);
      // Now about 5×, inside the 2–6× band: the note says it was limited, in neutral (not amber) unless the release is small.
      const vc = volumeCheck(stats.laidAcFt, released, { rawLaidAcFt: v.rawLaidAcFt })!;
      assert.equal(vc.warn, false);
      assert.match(vc.text, /Volume-limited: as first laid it held [\d,]+ ac-ft \(\d+\.\d×, above 6×\), so its water surface was lowered to about 5×, inside the 2–6× band\./);
      const note = volumeLimitNote(stats, released)!;
      assert.match(note, new RegExp(`lowered to ${Math.round(v.depthScale * 100)} % of the desk depths, aiming for about 5×`));
      assert.ok(outline.properties.volume_limit?.startsWith("Volume-limited:"));
    });
  }

  it("a smaller release lowers the surface further", async () => {
    const a = await build(withRelease(300));
    const b = await build(withRelease(100));
    assert.ok(b.stats.volumeLimit!.depthScale < a.stats.volumeLimit!.depthScale);
    assert.ok(b.stats.outlineAc < a.stats.outlineAc);
  });

  it("the warning without the limit is unchanged", () => {
    const over = volumeCheck(2130, 167)!;
    assert.ok(over.warn && /Above 6×/.test(over.text));
    assert.equal(volumeCheck(310, 71)!.warn, false);
    // A limited layer whose first lay was inside the band is not called limited.
    assert.equal(volumeCheck(310, 71, { rawLaidAcFt: 400 })!.warn, false);
  });

  it("neutral vs amber (Nick, 2026-11-03 22:43 CST): limited into the band is neutral; small or still outside stays amber", () => {
    // Limited from 9× to 5× on 100 ac-ft: neutral.
    const ok = volumeCheck(500, 100, { rawLaidAcFt: 900 })!;
    assert.equal(ok.warn, false);
    assert.match(ok.text, /Volume-limited: .*lowered to about 5×, inside the 2–6× band\./);
    // The same on a release under 25 ac-ft: amber, with the label.
    const small = volumeCheck(100, 20, { rawLaidAcFt: 180 })!;
    assert.equal(small.warn, true);
    assert.equal(small.label, SMALL_RELEASE_LABEL);
    // Limited but still outside 2–6× (the limit could not get it down): amber, and says so.
    const still = volumeCheck(700, 100, { rawLaidAcFt: 900 })!;
    assert.equal(still.warn, true);
    assert.match(still.text, /but it still lies outside the 2–6× band\. Check against a model run\./);
    // Below 2× without any limit: amber, as before.
    assert.equal(volumeCheck(150, 100)!.warn, true);
  });
});

describe('releases under 25 ac-ft: "volume-limited, run the Runner"', () => {
  it("labels by the release alone", () => {
    assert.equal(SMALL_RELEASE_ACFT, 25);
    assert.equal(SMALL_RELEASE_LABEL, "volume-limited, run the Runner");
    assert.equal(smallRelease(6.9), true);
    assert.equal(smallRelease(24.9), true);
    assert.equal(smallRelease(25), false);
    assert.equal(smallRelease(167), false);
    assert.equal(smallRelease(0), false);
    assert.equal(smallRelease(Number.NaN), false);
    assert.equal(volumeCheck(34, 6.9)!.label, SMALL_RELEASE_LABEL);
    assert.equal(volumeCheck(120, 25)!.label, null);
    // Small releases read to a tenth of an ac-ft, not rounded to 7.
    assert.match(volumeCheck(34, 6.9)!.text, /for 6\.9 ac-ft released \(4\.9×\)/);
  });

  it("an outline built on a 20 ac-ft release carries the label on its stats and its export", async () => {
    const { stats, outline } = await build(withRelease(20));
    assert.equal(stats.volumeLabel, SMALL_RELEASE_LABEL);
    assert.equal(outline.properties.volume_label, SMALL_RELEASE_LABEL);
    const big = await build(withRelease(100));
    assert.equal(big.stats.volumeLabel, undefined);
    assert.equal(big.outline.properties.volume_label, undefined);
  });
});

describe("channel pits count only up to the release (Nick, 2026-11-03)", () => {
  const cellM2 = 100;
  // Two pits in channel order: 1 m and 2 m deep, 10 and 5 cells: bowls of 1000 m³ each.
  const p1 = { z: Array(10).fill(9), spill: Array(10).fill(10) };
  const p2 = { z: Array(5).fill(8), spill: Array(5).fill(10) };

  it("a large release counts every pit in full", () => {
    assert.equal(bowlM3(p1, 10, cellM2), 1000);
    const b = channelPitBudget([p1, p2], 5000, cellM2);
    assert.equal(b.stop, null);
    assert.equal(b.storageM3, 2000);
    assert.equal(b.countedM3, 2000);
    assert.equal(channelPitBudget([p1, p2], Infinity, cellM2).countedM3, 2000);
  });

  it("a small release fills the pits upstream first and runs out in the next only when even its footprint at the flow depth is too much", () => {
    // p2 at a 2 m flow depth: its footprint charge is its whole bowl, more than the 500 m³ left.
    const b = channelPitBudget([p1, { ...p2, flowM: 2 }], 1500, cellM2);
    assert.ok(b.stop);
    assert.equal(b.stop.crossing, true);
    assert.deepEqual(b.stop.why, ["over_remaining_release"]);
    assert.equal(b.stop.index, 1);
    assert.ok(Math.abs(b.stop.heldM3 - 500) < 1e-3, String(b.stop.heldM3));
    assert.ok(Math.abs(b.stop.levelM - 9) < 1e-6, String(b.stop.levelM)); // 500 m³ over 5 cells of 100 m²: 1 m up from 8 m
    assert.ok(Math.abs(b.countedM3 - 1500) < 1e-3);
    assert.equal(b.storageM3, 2000);
    // Not even the first pit fills: it holds the whole release.
    const tiny = channelPitBudget([{ ...p1, flowM: 1 }, p2], 400, cellM2);
    assert.equal(tiny.stop?.index, 0);
    assert.ok(Math.abs(tiny.countedM3 - 400) < 1e-3);
  });

  it("crossing pass-through: a pit bigger than the release left is charged its footprint at the flow depth and the flood moves on", () => {
    // 500 m³ left at p2 (1000 m³ bowl); at a 0.5 m flow depth its 5 cells take 250 m³.
    const b = channelPitBudget([p1, { ...p2, flowM: 0.5 }], 1500, cellM2);
    assert.equal(b.stop, null);
    assert.deepEqual(b.crossings, [{ index: 1, pitM3: 1000, chargeM3: 250, why: ["over_remaining_release"] }]);
    assert.equal(b.countedM3, 1250);
    assert.equal(crossingChargeM3({ ...p2, flowM: 0.5 }, cellM2), 250);
    // Never more than the bowl.
    assert.equal(crossingChargeM3({ ...p2, flowM: 9 }, cellM2), 1000);
    // A large release fills the same pit in full: no crossing.
    assert.equal(channelPitBudget([p1, { ...p2, flowM: 0.5 }], 5000, cellM2).crossings.length, 0);
  });

  it(`a pit over ${CROSSING_STORAGE_MULT}× the dam's storage, or at a mapped road, passes the flood even when the release could fill it`, () => {
    assert.equal(CROSSING_STORAGE_MULT, 1.5);
    const big = channelPitBudget([p1, { ...p2, flowM: 0.5 }], 1e6, cellM2, { storageM3: 600 });
    // p1 (1000 m³) and p2 (1000 m³) are both over 1.5 × 600 = 900 m³.
    assert.deepEqual(big.crossings.map((x) => x.why), [["over_storage"], ["over_storage"]]);
    assert.equal(big.crossings[0].chargeM3, 0); // no flow depth given: nothing charged
    assert.equal(big.countedM3, 250);
    const under = channelPitBudget([p1, p2], 1e6, cellM2, { storageM3: 700 });
    assert.equal(under.crossings.length, 0);
    const road = channelPitBudget([{ ...p1, road: true, flowM: 0.2 }, p2], 1e6, cellM2);
    assert.deepEqual(road.crossings, [{ index: 0, pitM3: 1000, chargeM3: 200, why: ["road"] }]);
    assert.equal(road.countedM3, 1200);
  });

  it("the desk note says where the release runs out", () => {
    assert.match(
      channelPitsNote({ channelPits: { pits: 3, storageAcFt: 35, countedAcFt: 6.9, stop: { pitAcFt: 9.7, heldAcFt: 6.5, atKm: 0.437 } } })!,
      /runs through 3 pits holding 35 ac-ft below their spill levels; they take their storage out of the release before the flood moves on downstream\. The release runs out in the pit at 0\.44 km \(it fills 6\.5 of its 9\.7 ac-ft\), so the outline stops there\./,
    );
    assert.match(channelPitsNote({ channelPits: { pits: 1, storageAcFt: 2, countedAcFt: 2 } })!, /runs through 1 pit .* The release covers them all \(2\.0 ac-ft charged\)\./);
    assert.match(
      channelPitsNote({
        channelPits: {
          pits: 2,
          storageAcFt: 230,
          countedAcFt: 40,
          crossings: [{ atKm: 1.711, lon: -100.1, lat: 47.2, pitAcFt: 222.8, chargedAcFt: 33.4, why: ["over_remaining_release", "over_storage"] }],
        },
      })!,
      /Suspected crossing the flood passes through, charged only its footprint at the flow depth: 1\.71 km \(47\.20000, -100\.10000\), 223 ac-ft bowl, 33 charged \(bigger than the release left, over 1\.5× the storage\)\. The release covers them all \(40 ac-ft charged\)\./,
    );
    assert.equal(channelPitsNote({}), null);
  });

  // A pit dug into the traced channel `x` m down: 5 × 5 cells (30 m) lowered 4 m, about 73 ac-ft of bowl.
  const dug = async (x: number) => {
    const base = await build(withRelease(300));
    const pt = base.centerline.pts.find((p) => p.x > x)!;
    const loc = damLocation(ND)!;
    const kx = 111320 * Math.cos((loc.lat * Math.PI) / 180);
    const dlon = (DEM.bbox[2] - DEM.bbox[0]) / DEM.width;
    const dlat = (DEM.bbox[3] - DEM.bbox[1]) / DEM.height;
    const i = Math.round((pt.e / kx + loc.lon - DEM.bbox[0]) / dlon - 0.5);
    const j = Math.round((DEM.bbox[3] - (pt.n / 110574 + loc.lat)) / dlat - 0.5);
    const z = Float32Array.from(DEM.z);
    for (let dj = -2; dj <= 2; dj++) for (let di = -2; di <= 2; di++) z[(j + dj) * DEM.width + i + di] -= 4;
    return { g: { ...DEM, z }, atKm: pt.x / 1000, lon: DEM.bbox[0] + (i + 0.5) * dlon, lat: DEM.bbox[3] - (j + 0.5) * dlat };
  };

  it("on terrain: a large release counts the dug pit in full and the flood runs on past it", async () => {
    const { g, atKm } = await dug(3000);
    const plain = await build(ND);
    const big = await build(ND, g);
    const c = big.stats.channelPits!;
    assert.equal(c.stop, undefined);
    assert.equal(c.countedAcFt, c.storageAcFt);
    // The dug bowl (25 × 900 m² × 4 m ≈ 73 ac-ft, less what the natural ground there already held) is now counted.
    const added = c.storageAcFt - plain.stats.channelPits!.storageAcFt;
    assert.ok(added > 40 && added < (25 * 900 * 4.5) / ACRE_FT_M3, String(added));
    assert.ok(big.stats.reachKm! > atKm + 3, `${big.stats.reachKm} vs pit at ${atKm}`);
  });

  it("on terrain: a small release meets a pit bigger than what is left, passes through it as a suspected crossing and runs on", async () => {
    const { g, atKm } = await dug(1200);
    const small = await build(withRelease(50), g);
    const c = small.stats.channelPits!;
    assert.equal(c.stop, undefined, JSON.stringify(c));
    const x = c.crossings?.find((k) => Math.abs(k.atKm - atKm) < 0.3);
    assert.ok(x, JSON.stringify(c.crossings));
    assert.ok(x.why.includes("over_remaining_release"));
    // Charged only a shallow sheet over its footprint, far less than its bowl.
    assert.ok(x.chargedAcFt > 0 && x.chargedAcFt < 0.5 * x.pitAcFt, JSON.stringify(x));
    assert.ok(c.countedAcFt <= small.released + 0.01, `${c.countedAcFt} vs ${small.released}`);
    // The flood continues downstream past the pit, as on the undug ground.
    assert.ok(small.stats.reachKm! > atKm + 0.5, `${small.stats.reachKm} vs ${atKm}`);
    // The GeoJSON records the crossing for Docs & QA, and no stop point.
    const p = small.outline.properties;
    assert.equal(p.channel_pit_stop, null);
    assert.ok(p.suspected_crossings!.some((k) => Math.abs(k.at_km - x.atKm) < 1e-9 && k.pit_ac_ft === x.pitAcFt && k.charged_ac_ft === x.chargedAcFt));
    assert.ok(p.suspected_crossings!.every((k) => Number.isFinite(k.lon) && Number.isFinite(k.lat) && k.why.length > 0));
    assert.match(channelPitsNote(small.stats)!, /Suspected crossings? the flood passes through/);
  });

  it("on terrain: a road receptor at a pit the release could fill flags it as a crossing", async () => {
    const { g, atKm, lon, lat } = await dug(3000);
    const road = { id: "r1", name: "Test road", kind: "road" as const, station: 0, distanceKm: atKm, lateral: 0, padFt: 0, lon, lat };
    const withRoad = await build({ ...ND, receptors: [...(ND.receptors ?? []), road] }, g);
    const c = withRoad.stats.channelPits!;
    const x = c.crossings?.find((k) => k.why.includes("road"));
    assert.ok(x, JSON.stringify(c));
    assert.ok(Math.abs(x.atKm - atKm) < 0.3, `${x.atKm} vs ${atKm}`);
    assert.ok(x.chargedAcFt < x.pitAcFt);
    assert.ok(c.countedAcFt < c.storageAcFt);
    assert.ok(withRoad.stats.reachKm! > atKm + 3);
  });

  it("GeoJSON: the pit totals, the stop point (km along the trace, ac-ft held) and the suspected crossings", async () => {
    // ND00199 at 6.9 ac-ft (as DWR-4031): the release runs out in a channel pit.
    const tiny = await build(withRelease(6.9));
    const c = tiny.stats.channelPits!;
    const p = tiny.outline.properties;
    assert.deepEqual(p.channel_pits, { pits: c.pits, storage_ac_ft: c.storageAcFt, counted_ac_ft: c.countedAcFt });
    assert.ok(Array.isArray(p.suspected_crossings));
    if (c.stop) {
      assert.deepEqual(p.channel_pit_stop, {
        at_km: c.stop.atKm,
        lon: c.stop.lon,
        lat: c.stop.lat,
        held_ac_ft: c.stop.heldAcFt,
        pit_ac_ft: c.stop.pitAcFt,
        crossing: !!c.stop.crossing,
        why: c.stop.why ?? [],
      });
      assert.ok(Number.isFinite(c.stop.lon) && Number.isFinite(c.stop.lat));
      assert.ok(c.stop.heldAcFt <= c.stop.pitAcFt);
    } else assert.equal(p.channel_pit_stop, null);
    // Without a road or crossing the full ND00199 release has an empty crossing list and no stop.
    const full = await build(ND);
    assert.equal(full.outline.properties.channel_pit_stop, null);
    assert.equal(full.outline.properties.channel_pits!.pits, full.stats.channelPits!.pits);
  });
});
