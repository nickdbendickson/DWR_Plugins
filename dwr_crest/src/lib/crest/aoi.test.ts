import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { aoiExtent, squareAoi } from "./aoi.ts";
import { analyze, screenReceptors } from "./engine.ts";
import { exportDamJson } from "./export.ts";
import { dwr2801 } from "./fixtures.ts";
import type { DamRecord } from "./types.ts";

const SITE = { lat: 46.98364, lon: -97.39064 };
const withAoi = (halfKm: number, over: Partial<DamRecord> = {}, at = SITE) =>
  dwr2801({ ...SITE, ...over, geometry: { ...over.geometry, aoi: squareAoi(at.lon, at.lat, halfKm) } });

describe("drawn area (AOI) geometry", () => {
  it("measures the band from the breach and the polygon area", () => {
    const e = aoiExtent(squareAoi(SITE.lon, SITE.lat, 3), SITE)!;
    assert.ok(e.breachInside);
    assert.equal(e.nearM, 0);
    assert.ok(Math.abs(e.farM - 3000 * Math.SQRT2) < 5, `far ${e.farM}`);
    assert.ok(Math.abs(e.areaAc - 36e6 / 4046.8564224) < 5, `area ${e.areaAc}`);
    const off = aoiExtent(squareAoi(SITE.lon + 0.05, SITE.lat, 1), SITE)!;
    assert.equal(off.breachInside, false);
    assert.ok(off.nearM > 2000 && off.nearM < 3000, `near ${off.nearM}`);
  });
});

describe("drawn area limits the flood", () => {
  it("no AOI: the analysis is unchanged (aoi null, no flood warnings)", () => {
    const a = analyze(dwr2801());
    assert.equal(a.flood.aoi, null);
    assert.deepEqual(a.flood.warnings, []);
    assert.equal(a.flood.reachSource, "wet");
    // A non-polygon AOI is ignored the same way.
    const b = analyze(
      dwr2801({ geometry: { aoi: { type: "Feature", properties: {}, geometry: { type: "Point", coordinates: [SITE.lon, SITE.lat] } } } }),
    );
    const strip = (x: ReturnType<typeof analyze>) => JSON.stringify({ ...x, flood: { ...x.flood, aoi: null } });
    assert.equal(strip(b), strip(a));
    assert.equal(exportDamJson(dwr2801(), a).includes("aoi_limit"), false);
  });

  it("clips the reach and wet area to the AOI, and warns when still wet at its edge", () => {
    const free = analyze(dwr2801(SITE));
    const a = analyze(withAoi(2));
    assert.equal(a.flood.reachSource, "aoi");
    assert.ok(Math.abs(a.flood.lengthM - 2000 * Math.SQRT2) < 5, `reach ${a.flood.lengthM}`);
    assert.ok(a.flood.lengthM < free.flood.lengthM);
    assert.ok(a.flood.acres < free.flood.acres, `${a.flood.acres} vs ${free.flood.acres}`);
    assert.ok(a.flood.acres <= a.flood.aoi!.areaAc);
    assert.equal(a.flood.maxDepthFt, free.flood.maxDepthFt, "breach inside: the max depth is at the breach");
    assert.match(a.flood.aoi!.label, /^Limited by drawn area: 2\.8 km \/ [\d,]+ ac$/);
    assert.ok(a.flood.aoi!.wetAtEdge);
    assert.ok(a.flood.warnings.some((w) => w.includes("The drawn area may be too small")));
    assert.ok(a.datum.warnings.some((w) => w.includes("The drawn area may be too small")), "also in Things to check");
    assert.match(exportDamJson(withAoi(2), a), /"aoi_limit"/);
  });

  it("an AOI larger than the wet reach changes nothing in the flood and does not warn", () => {
    const free = analyze(dwr2801(SITE));
    const a = analyze(withAoi(20));
    assert.equal(a.flood.reachSource, "wet");
    assert.equal(a.flood.lengthM, free.flood.lengthM);
    assert.equal(a.flood.acres, free.flood.acres);
    assert.equal(a.flood.aoi!.limitsReach, false);
    assert.equal(a.flood.aoi!.wetAtEdge, false);
    assert.equal(a.screen.label, free.screen.label);
    assert.match(a.flood.aoi!.label, /wet limit ends first/);
  });

  it("does not warn when the flood is dry before the AOI edge", () => {
    const free = analyze(dwr2801(SITE));
    // Square whose corner sits just past the wet limit, but the user reach is longer: AOI limits, edge dry.
    const half = (free.flood.lengthM + 300) / 1000 / Math.SQRT2;
    const a = analyze(withAoi(half, { reachKm: 25 }));
    assert.equal(a.flood.reachSource, "aoi");
    assert.equal(a.flood.aoi!.wetAtEdge, false);
    assert.ok(!a.flood.warnings.some((w) => w.includes("too small")));
  });

  it("a house outside the AOI is 'unresolved (outside drawn area)' and forces Review, never Low", () => {
    // Only a far house seeded, at 3.5 km: dry without an AOI would be Low.
    const seeds = [{ id: "h", name: "House 3.5 km", kind: "house" as const, station: 0, distanceKm: 3.5, lateral: 0, padFt: 20 }];
    const free = analyze(dwr2801({ ...SITE, receptors: seeds }));
    assert.equal(free.screen.level, "Low");
    const a = analyze(withAoi(2, { receptors: seeds }));
    const r = a.flood.receptors[0];
    assert.equal(r.unresolved, true);
    assert.equal(r.outsideAoi, true);
    assert.equal(r.flooded, false);
    assert.match(r.rankReason, /^unresolved \(outside drawn area\)/);
    assert.equal(a.screen.level, "Review");
    assert.equal(a.screen.label, "Review (Low on resolved receptors; 1 unresolved (outside drawn area))");
    assert.ok(a.screen.reasons.some((x) => x.includes("outside the drawn area")));
  });

  it("a resolved High stays High and names the house outside the drawn area", () => {
    const s = screenReceptors(
      [
        { name: "A", kind: "house", depthFt: 8, velocityFps: 6, zone: "high", ndClass: "High", rankReason: "high", needsJudgment: false },
        { name: "B", kind: "house", depthFt: 0, velocityFps: 0, zone: null, ndClass: null, rankReason: "unresolved (outside drawn area)", needsJudgment: true, unresolved: true, outsideAoi: true },
      ],
      [],
    );
    assert.equal(s.level, "High");
    assert.equal(s.label, "High (1 unresolved, outside drawn area)");
  });

  it("breach outside the AOI: only the valley inside counts, upstream receptors are outside", () => {
    const a = analyze(withAoi(1, {}, { lon: SITE.lon + 0.05, lat: SITE.lat }));
    assert.ok(a.flood.aoi!.nearKm > 2);
    assert.ok(a.flood.maxDepthFt < analyze(dwr2801(SITE)).flood.maxDepthFt);
    const upstream = a.flood.receptors.filter((r) => r.distanceKm < a.flood.aoi!.nearKm);
    assert.ok(upstream.length > 0 && upstream.every((r) => r.outsideAoi));
    assert.ok(a.flood.warnings.some((w) => w.includes("outside the drawn area")));
  });

  it("AOI without any breach location is not applied, and says so", () => {
    const a = analyze(dwr2801({ lat: undefined, lon: undefined, geometry: { aoi: squareAoi(SITE.lon, SITE.lat, 2) } }));
    assert.equal(a.flood.aoi!.applied, false);
    assert.equal(a.flood.reachSource, "wet");
    assert.ok(a.flood.warnings[0].startsWith("Drawn area not applied"));
  });

  it("CHECK (DWR-2801-like): a 5 km AOI around the dam leaves the 14.8 / 17.2 km benchmark houses outside, Review, wet at the edge", () => {
    const seeds = [
      { id: "b1", name: "Benchmark house 14.8 km", kind: "house" as const, station: 0, distanceKm: 14.8, lateral: 0, padFt: 0 },
      { id: "b2", name: "Benchmark house 17.2 km", kind: "house" as const, station: 0, distanceKm: 17.2, lateral: 0, padFt: 0 },
    ];
    const a = analyze(withAoi(5, { surfaceAreaAc: 154.5, storageAcFt: 1615.5, receptors: seeds }));
    assert.equal(Math.round(a.breach.peakCfs), 28671, "Runner peak unchanged by the AOI");
    assert.equal(a.flood.reachSource, "aoi");
    assert.ok(a.flood.receptors.every((r) => r.outsideAoi && r.unresolved));
    assert.equal(a.screen.level, "Review");
    assert.match(a.screen.label, /2 unresolved \(outside drawn area\)/);
    assert.ok(a.flood.aoi!.wetAtEdge);
  });
});
