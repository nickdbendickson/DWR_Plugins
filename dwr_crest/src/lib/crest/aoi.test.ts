import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  aoiExtent,
  damGeometryKm,
  downstreamDirection,
  polygonKm,
  readAoi,
  squareAoi,
} from "./aoi.ts";
import { analyze, screenReceptors } from "./engine.ts";
import { SAMPLE_DAMS } from "./samples.ts";
import { exportDamJson } from "./export.ts";
import { dwr2801 } from "./fixtures.ts";
import type { DamGeometry, DamRecord, GeoJSONFeature } from "./types.ts";

const SITE = { lat: 46.98364, lon: -97.39064 };
/** Dam line east–west through the site, reservoir point 0.5 km north: downstream is south. */
const DAM = damGeometryKm(SITE);
const rect = (e0: number, e1: number, n0: number, n1: number, holes: [number, number][][] = []) =>
  polygonKm(
    SITE,
    [
      [e0, n0],
      [e1, n0],
      [e1, n1],
      [e0, n1],
    ],
    holes,
  );
const dam = (
  aoi: GeoJSONFeature | undefined,
  over: Partial<DamRecord> = {},
  g: DamGeometry = DAM,
) => dwr2801({ ...SITE, ...over, geometry: { ...g, ...over.geometry, aoi } });
const house = (name: string, km: number, padFt = 0) => ({
  id: name,
  name,
  kind: "house" as const,
  station: 0,
  distanceKm: km,
  lateral: 0,
  padFt,
});
const free = analyze(dwr2801(SITE));

describe("downstream direction from the record geometry", () => {
  it("dam line normal away from the reservoir point; reservoir-to-breach bearing; none", () => {
    const s = downstreamDirection(DAM, SITE);
    assert.equal(s.basis, "dam line");
    assert.ok(Math.abs(s.u![0]) < 1e-6 && Math.abs(s.u![1] + 1) < 1e-6, `south ${s.u}`);
    const n = downstreamDirection(damGeometryKm(SITE, { resNorthKm: -0.5 }), SITE);
    assert.ok(n.u![1] > 0.999, "reservoir south: downstream north");
    const r = downstreamDirection(
      { reservoirPoint: damGeometryKm(SITE, { resEastKm: -1, resNorthKm: 0 }).reservoirPoint },
      SITE,
    );
    assert.equal(r.basis, "reservoir to breach");
    assert.ok(r.u![0] > 0.999, "reservoir west of the breach: downstream east");
    const none = downstreamDirection({ damLine: DAM.damLine }, SITE);
    assert.equal(none.u, null);
    assert.match(none.note, /no reservoir point/);
    assert.equal(downstreamDirection(undefined, SITE).basis, "none");
  });
});

describe("1. the reach runs to the nearest downstream exit, not the farthest corner", () => {
  it("6 km square around the dam: reach 3.0 km (was 4.24 km at the corner); edge depth at the exit", () => {
    const a = analyze(dam(squareAoi(SITE.lon, SITE.lat, 3)));
    assert.equal(a.flood.reachSource, "aoi");
    assert.equal(a.flood.aoi!.reachBasis, "downstream");
    assert.equal(a.flood.aoi!.directionBasis, "dam line");
    assert.ok(Math.abs(a.flood.lengthM - 3000) < 2, `reach ${a.flood.lengthM}`);
    assert.match(a.flood.aoi!.label, /^Limited by drawn area: 3\.0 km \/ 8,896 ac$/);
    const at = a.flood.stations[a.flood.stations.length - 1].depthFt;
    assert.ok(
      Math.abs(a.flood.aoi!.edgeDepthFt - at) < 1e-9 && a.flood.aoi!.edgeDepthFt > 2.5,
      `edge ${a.flood.aoi!.edgeDepthFt}`,
    );
    assert.ok(
      a.flood.warnings.some((w) =>
        /deep where the valley leaves the drawn area \(3\.0 km downstream/.test(w),
      ),
    );
    // A house at 3.5 km was inside the old 4.24 km corner reach; now it is outside the drawn area.
    const h = analyze(dam(squareAoi(SITE.lon, SITE.lat, 3), { receptors: [house("H 3.5", 3.5)] }));
    assert.equal(h.flood.receptors[0].outsideAoi, true);
  });

  it("a long, thin AOI drawn downstream: the reach is its length, the wet area is clipped to its width", () => {
    const a = analyze(dam(rect(-0.05, 0.05, -5, 0.05)));
    assert.ok(Math.abs(a.flood.lengthM - 5000) < 2);
    // 100 m wide strip, 5 km long: at most ~124 ac (polygon), far below the unclipped schematic strip.
    assert.ok(
      a.flood.acres <= a.flood.aoi!.areaAc + 1e-6,
      `${a.flood.acres} vs ${a.flood.aoi!.areaAc}`,
    );
    assert.ok(a.flood.acres > (0.9 * 100 * 5000) / 4046.86, `${a.flood.acres}`);
    assert.equal(a.flood.aoi!.areaBasis, "clipped");
  });

  it("an AOI drawn upstream does not count as downstream, and warns", () => {
    const up = analyze(dam(rect(-1, 1, 0.3, 3.3), { receptors: [house("H 1", 1)] }));
    assert.equal(up.flood.acres, 0);
    assert.ok(up.flood.warnings.some((w) => w.includes("does not reach the valley downstream")));
    assert.ok(
      up.flood.warnings.some((w) => w.startsWith("Most of the drawn area (100%) is upstream")),
    );
    assert.ok(up.flood.receptors[0].outsideAoi);
    assert.equal(up.screen.level, "Review");
    // Mostly upstream but over the breach: reach to the 1 km downstream edge, 75% upstream warning.
    const m = analyze(dam(rect(-1, 1, -1, 3)));
    assert.ok(Math.abs(m.flood.lengthM - 1000) < 2);
    assert.ok(
      m.flood.warnings.some((w) => w.startsWith("Most of the drawn area (75%) is upstream")),
    );
    assert.ok(Math.abs(m.flood.aoi!.upstreamFrac! - 0.75) < 0.01);
    // The same rectangle flipped downstream does not warn about upstream.
    const d = analyze(dam(rect(-1, 1, -3, 1)));
    assert.ok(!d.flood.warnings.some((w) => w.includes("upstream of the breach")));
  });

  it("no direction: falls back to the nearest boundary (shortest exit) and warns", () => {
    const a = analyze(dam(rect(-1, 1, -5, 1), {}, {}));
    assert.equal(a.flood.aoi!.reachBasis, "nearest-boundary");
    assert.equal(a.flood.aoi!.areaBasis, "band");
    assert.ok(Math.abs(a.flood.lengthM - 1000) < 2, `reach ${a.flood.lengthM}`);
    assert.ok(
      a.flood.warnings.some(
        (w) => w.startsWith("Downstream direction unknown") && w.includes("nearest edge"),
      ),
    );
    assert.ok(a.flood.acres <= a.flood.aoi!.areaAc);
  });
});

describe("2. a known High is never hidden by the drawn area", () => {
  const seeds = [house("House 0.5 km", 0.5)];
  it("house at 0.5 km is High without an AOI; a 0.2 km AOI gives 'Review (would be High: … outside drawn area)'", () => {
    const base = analyze(dwr2801({ ...SITE, receptors: seeds }));
    assert.equal(base.screen.level, "High");
    for (const aoi of [squareAoi(SITE.lon, SITE.lat, 0.2), rect(-1, 1, -4, -2)]) {
      const a = analyze(dam(aoi, { receptors: seeds }));
      const r = a.flood.receptors[0];
      assert.equal(r.outsideAoi, true);
      assert.equal(r.wouldBe!.ndClass, "High");
      assert.match(r.rankReason, /without the drawn area it would be High/);
      assert.equal(a.screen.level, "Review");
      assert.equal(a.screen.label, "Review (would be High: House 0.5 km outside drawn area)");
      assert.ok(!a.screen.label.includes("Low"));
      assert.ok(a.screen.reasons[0].includes("would screen High"));
    }
  });

  it("a house outside the AOI that would be dry stays 'unresolved (outside drawn area)', Review, never Low", () => {
    const s = [house("House 3.5 km", 3.5, 20)];
    assert.equal(analyze(dwr2801({ ...SITE, receptors: s })).screen.level, "Low");
    const a = analyze(dam(squareAoi(SITE.lon, SITE.lat, 2), { receptors: s }));
    assert.match(a.flood.receptors[0].rankReason, /^unresolved \(outside drawn area\)/);
    assert.equal(
      a.screen.label,
      "Review (Low on resolved receptors; 1 unresolved (outside drawn area))",
    );
  });

  it("a resolved High stays High and names the house outside the drawn area", () => {
    const s = screenReceptors(
      [
        {
          name: "A",
          kind: "house",
          depthFt: 8,
          velocityFps: 6,
          zone: "high",
          ndClass: "High",
          rankReason: "high",
          needsJudgment: false,
        },
        {
          name: "B",
          kind: "house",
          depthFt: 0,
          velocityFps: 0,
          zone: null,
          ndClass: null,
          rankReason: "unresolved (outside drawn area)",
          needsJudgment: true,
          unresolved: true,
          outsideAoi: true,
        },
      ],
      [],
    );
    assert.equal(s.label, "High (1 unresolved, outside drawn area)");
  });

  it("mixed: Medium resolved, a would-be High outside plus another unresolved", () => {
    const s = screenReceptors([
      {
        name: "A",
        kind: "house",
        depthFt: 1,
        velocityFps: 1,
        zone: "low",
        ndClass: "Medium",
        rankReason: "low zone",
        needsJudgment: false,
      },
      {
        name: "B",
        kind: "house",
        depthFt: 0,
        velocityFps: 0,
        zone: null,
        ndClass: null,
        rankReason: "u",
        needsJudgment: true,
        unresolved: true,
        outsideAoi: true,
        wouldBe: { ndClass: "High", zone: "high", depthFt: 9, velocityFps: 6 },
      },
      {
        name: "C",
        kind: "house",
        depthFt: 0,
        velocityFps: 0,
        zone: null,
        ndClass: null,
        rankReason: "u",
        needsJudgment: true,
        unresolved: true,
        outsideAoi: false,
        wouldBe: null,
      },
    ]);
    assert.equal(s.label, "Review (would be High: B outside drawn area; 1 more unresolved)");
  });
});

describe("3. bad polygons are rejected or handled, never silently", () => {
  const feature = (geometry: GeoJSONFeature["geometry"]): GeoJSONFeature => ({
    type: "Feature",
    properties: {},
    geometry,
  });
  const ll = (e: number, n: number) => [SITE.lon + e / 76, SITE.lat + n / 110.574];

  it("bow-tie and collinear rings are rejected with a warning; the flood is the no-AOI flood", () => {
    const bow = feature({
      type: "Polygon",
      coordinates: [[ll(-2, -2), ll(2, 2), ll(2, -2), ll(-2, 2), ll(-2, -2)]],
    });
    const a = analyze(dam(bow));
    assert.equal(a.flood.aoi!.applied, false);
    assert.match(a.flood.aoi!.label, /^Drawn area not applied: it crosses itself/);
    assert.equal(a.flood.lengthM, free.flood.lengthM);
    assert.equal(a.flood.acres, free.flood.acres);
    const line = feature({
      type: "Polygon",
      coordinates: [[ll(0, 0), ll(0, -1), ll(0, -2), ll(0, 0)]],
    });
    assert.match(analyze(dam(line)).flood.aoi!.label, /has no area/);
    const two = feature({ type: "Polygon", coordinates: [[ll(0, 0), ll(0, -1), ll(0, 0)]] });
    assert.match(analyze(dam(two)).flood.aoi!.label, /fewer than 3/);
  });

  it("a Point or LineString AOI is not applied, with a warning (was: ignored silently)", () => {
    const a = analyze(dam(feature({ type: "Point", coordinates: [SITE.lon, SITE.lat] })));
    assert.equal(a.flood.aoi!.applied, false);
    assert.ok(a.flood.warnings[0].includes("it is a Point, not a polygon"));
    assert.equal(a.flood.lengthM, free.flood.lengthM);
  });

  it("MultiPolygon: disjoint parts are a union; overlapping parts use the largest, with a warning", () => {
    const sq = (e0: number, n0: number, e1: number, n1: number) => [
      [ll(e0, n0), ll(e1, n0), ll(e1, n1), ll(e0, n1), ll(e0, n0)],
    ];
    const m = analyze(
      dam(feature({ type: "MultiPolygon", coordinates: [sq(-1, -2, 1, 1), sq(-1, -6, 1, -4)] })),
    );
    assert.equal(m.flood.aoi!.applied, true);
    assert.ok(Math.abs(m.flood.lengthM - 2000) < 30, `first exit ${m.flood.lengthM}`);
    const one = readAoi(feature({ type: "Polygon", coordinates: sq(-1, -2, 1, 1) }), SITE);
    assert.ok(one.ok);
    assert.ok(
      m.flood.aoi!.areaAc >
        1.6 *
          aoiExtent(one as Extract<typeof one, { ok: true }>, downstreamDirection(DAM, SITE))
            .areaAc,
    );
    assert.match(m.flood.aoi!.label, /2 parts/);
    const o = analyze(
      dam(feature({ type: "MultiPolygon", coordinates: [sq(-1, -2, 1, 1), sq(0, -3, 2, 0)] })),
    );
    assert.ok(
      o.flood.warnings.some((w) => w.includes("2 overlapping parts; only the largest is used")),
    );
  });

  it("holes are excluded from the area and end the reach where the valley enters them; a bad hole warns", () => {
    const hole: [number, number][] = [
      [-2, -1.5],
      [2, -1.5],
      [2, -1],
      [-2, -1],
    ];
    const solid = analyze(dam(rect(-3, 3, -3, 1)));
    const holed = analyze(dam(rect(-3, 3, -3, 1, [hole])));
    assert.ok(
      Math.abs(holed.flood.aoi!.areaAc - (solid.flood.aoi!.areaAc - (4 * 0.5 * 1e6) / 4046.86)) < 2,
    );
    assert.ok(Math.abs(holed.flood.lengthM - 1000) < 2, `reach ${holed.flood.lengthM}`);
    const badHole = analyze(
      dam(
        rect(-3, 3, -3, 1, [
          [
            [-1, -1],
            [1, -2],
            [1, -1],
            [-1, -2],
          ],
        ]),
      ),
    );
    assert.ok(
      badHole.flood.warnings.some((w) =>
        /Hole 1 in the drawn area crosses itself; it is not excluded/.test(w),
      ),
    );
  });

  it("a tiny AOI warns and reports metres, not '0.0 km / 0 ac'", () => {
    const a = analyze(dam(squareAoi(SITE.lon, SITE.lat, 0.005)));
    assert.ok(a.flood.warnings.some((w) => w.startsWith("The drawn area is tiny")));
    assert.match(a.flood.aoi!.label, /^Limited by drawn area: 5 m \/ 0\.02 ac$/);
  });
});

describe("4. breach outside the AOI", () => {
  it("reports the true max depth and flags the depth inside the drawn area", () => {
    const a = analyze(dam(rect(-1, 1, -4, -2)));
    assert.equal(
      a.flood.maxDepthFt,
      free.flood.maxDepthFt,
      "true max at the breach (was the near-edge depth)",
    );
    assert.ok(a.flood.aoi!.maxDepthInsideFt < a.flood.maxDepthFt);
    assert.ok(Math.abs(a.flood.aoi!.nearKm - 2) < 0.01 && Math.abs(a.flood.aoi!.farKm - 4) < 0.01);
    assert.ok(
      a.flood.warnings.some((w) => w.includes("max depth") && w.includes("outside the drawn area")),
    );
  });
});

describe("no AOI and large AOI", () => {
  it("no AOI: the analysis is unchanged (aoi null, no flood warnings, no aoi_limit)", () => {
    const a = analyze(dwr2801());
    assert.equal(a.flood.aoi, null);
    assert.deepEqual(a.flood.warnings, []);
    assert.equal(a.flood.reachSource, "wet");
    assert.ok(a.flood.receptors.every((r) => r.wouldBe === null));
    assert.equal(exportDamJson(dwr2801(), a).includes("aoi_limit"), false);
  });

  it("an AOI covering the whole wet strip changes nothing in the flood (clipped area equals the free area)", () => {
    const a = analyze(dam(squareAoi(SITE.lon, SITE.lat, 20)));
    assert.equal(a.flood.reachSource, "wet");
    assert.equal(a.flood.lengthM, free.flood.lengthM);
    assert.ok(Math.abs(a.flood.acres - free.flood.acres) < 1e-6);
    assert.equal(a.flood.aoi!.wetAtEdge, false);
    assert.equal(a.screen.label, free.screen.label);
    assert.match(a.flood.aoi!.label, /wet limit ends first/);
    assert.match(
      exportDamJson(dam(squareAoi(SITE.lon, SITE.lat, 20)), a),
      /"direction_basis": "dam line"/,
    );
  });

  it("CHECK (DWR-2801-like): 10 km square, benchmark houses at 14.8 / 17.2 km outside, Review, wet at the edge", () => {
    const seeds = [house("Benchmark house 14.8 km", 14.8), house("Benchmark house 17.2 km", 17.2)];
    const a = analyze(
      dam(squareAoi(SITE.lon, SITE.lat, 5), {
        surfaceAreaAc: 154.5,
        storageAcFt: 1615.5,
        receptors: seeds,
      }),
    );
    assert.equal(Math.round(a.breach.peakCfs), 28671, "Runner peak unchanged by the AOI");
    assert.ok(Math.abs(a.flood.lengthM - 5000) < 2);
    assert.ok(a.flood.receptors.every((r) => r.outsideAoi && r.unresolved));
    assert.equal(a.screen.level, "Review");
    assert.match(a.screen.label, /2 unresolved \(outside drawn area\)/);
    assert.ok(a.flood.aoi!.wetAtEdge);
  });
});

describe("PR #6 follow-ups", () => {
  // A. A breach on the outline (an edge or a corner) counts as inside within BOUNDARY_TOL_M.
  it("A: breach on the top edge of a 6 km-wide area gives the same 3.0 km / 275 ac as a centred 6 km square", () => {
    const centred = analyze(dam(squareAoi(SITE.lon, SITE.lat, 3)));
    assert.equal(Math.round(centred.flood.acres), 275);
    const edge = analyze(dam(rect(-3, 3, -3, 0)));
    assert.equal(edge.flood.aoi!.applied, true);
    assert.ok(Math.abs(edge.flood.lengthM - 3000) < 2, `reach ${edge.flood.lengthM}`);
    assert.ok(Math.abs(edge.flood.acres - centred.flood.acres) < 0.5, `acres ${edge.flood.acres}`);
    assert.equal(Math.round(edge.flood.acres), 275);
    assert.equal(edge.screen.label, centred.screen.label);
    assert.ok(!edge.flood.warnings.some((w) => w.includes("does not reach")));
    assert.ok(
      edge.flood.receptors.some((r) => !r.outsideAoi),
      "receptors inside are resolved",
    );
  });

  it("A: breach on a corner or a triangle vertex, and 0.5 m outside, count from the breach", () => {
    const nudge = (aoi: GeoJSONFeature) => analyze(dam(aoi)).flood;
    const corner = nudge(rect(0, 3, -3, 0));
    const cornerIn = nudge(rect(-0.00001, 3, -3, 0.00001));
    assert.ok(Math.abs(corner.lengthM - 3000) < 2);
    assert.ok(
      corner.acres > 100 && Math.abs(corner.acres - cornerIn.acres) < 0.5,
      `${corner.acres}`,
    );
    const tri = nudge(
      polygonKm(SITE, [
        [0, 0],
        [-3, -3],
        [3, -3],
      ]),
    );
    const triIn = nudge(
      polygonKm(SITE, [
        [0, 0.00001],
        [-3, -3],
        [3, -3],
      ]),
    );
    assert.ok(Math.abs(tri.lengthM - 3000) < 2 && Math.abs(tri.acres - triIn.acres) < 0.5);
    const near = analyze(dam(rect(-3, 3, -3, -0.0005)));
    assert.ok(!near.flood.warnings.some((w) => w.includes("breach location is outside")));
    assert.ok(Math.abs(near.flood.acres - 275) < 0.5);
  });

  // B. The samples carry a dam line and a reservoir point.
  it("B: every sample has a dam-line direction; ND00199's runs SE (~130 deg) like the DSS-WISE flood", () => {
    for (const d of SAMPLE_DAMS) {
      assert.equal(d.geometry?.damLine?.geometry.type, "LineString", d.id);
      assert.equal(d.geometry?.reservoirPoint?.geometry.type, "Point", d.id);
      const loc = { lat: d.lat ?? NaN, lon: d.lon ?? NaN };
      const line = d.geometry!.damLine!.geometry.coordinates as number[][];
      const at = Number.isFinite(loc.lat)
        ? loc
        : { lon: (line[0][0] + line[1][0]) / 2, lat: (line[0][1] + line[1][1]) / 2 };
      assert.equal(downstreamDirection(d.geometry, at).basis, "dam line", d.id);
    }
    const nd = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;
    const u = downstreamDirection(nd.geometry, { lat: nd.lat!, lon: nd.lon! }).u!;
    const deg = ((Math.atan2(u[0], u[1]) * 180) / Math.PI + 360) % 360;
    assert.ok(deg > 120 && deg < 140, `bearing ${deg}`);
    assert.equal(analyze(nd).flood.aoi, null, "no AOI on the sample itself");
  });

  it("B: a 22 km corridor drawn along the ND00199 sample's valley keeps the full natural reach", () => {
    const nd = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;
    const loc = { lat: nd.lat!, lon: nd.lon! };
    const b = (130 * Math.PI) / 180;
    const u = [Math.sin(b), Math.cos(b)];
    const v = [u[1], -u[0]];
    const c = (a: number, w: number): [number, number] => [
      a * u[0] + w * v[0],
      a * u[1] + w * v[1],
    ];
    const corridor = polygonKm(loc, [c(-0.05, -1), c(22, -1), c(22, 1), c(-0.05, 1)]);
    const base = analyze(nd);
    const a = analyze({ ...nd, geometry: { ...nd.geometry, aoi: corridor } });
    assert.equal(a.flood.aoi!.applied, true);
    assert.equal(a.flood.aoi!.directionBasis, "dam line");
    assert.equal(a.flood.reachSource, "wet");
    assert.equal(a.flood.lengthM, base.flood.lengthM);
    assert.ok(a.flood.lengthM > 9000, `reach ${a.flood.lengthM}`);
    assert.ok(
      Math.abs(a.flood.acres - base.flood.acres) < 1,
      `${a.flood.acres} vs ${base.flood.acres}`,
    );
    assert.equal(a.screen.label, base.screen.label);
  });

  // C. No direction and the breach outside: not applied (never assumed downstream).
  it("C: no direction, AOI 2-5 km upstream: not applied, full flood, warns; never 5.0 km / 487 ac", () => {
    const up = rect(-1, 1, 2, 5);
    const a = analyze(dam(up, {}, {}));
    assert.equal(a.flood.aoi!.applied, false);
    assert.match(a.flood.aoi!.label, /^Drawn area not applied/);
    assert.ok(
      a.flood.warnings.some((w) =>
        /breach location is outside the drawn area \(2\.0 km from it\).*downstream or upstream/.test(
          w,
        ),
      ),
    );
    assert.equal(a.flood.lengthM, free.flood.lengthM);
    assert.ok(Math.abs(a.flood.acres - free.flood.acres) < 1e-6);
    assert.notEqual(Math.round(a.flood.acres), 487);
    assert.equal(a.screen.label, free.screen.label);
    assert.ok(a.flood.receptors.every((r) => !r.outsideAoi));
    // A known High stays High (screened in full), it is not hidden behind the AOI.
    const seeds = [house("House 0.5 km", 0.5)];
    const h = analyze(dam(up, { receptors: seeds }, {}));
    assert.equal(h.screen.level, "High");
    // With a direction the same AOI is upstream: applied, nothing counted.
    const d = analyze(dam(up));
    assert.equal(d.flood.aoi!.applied, true);
    assert.equal(d.flood.acres, 0);
  });

  it("C: no direction, breach on the outline: not applied, says 'on the drawn area's edge'", () => {
    const a = analyze(dam(rect(-1, 1, -5, 0), {}, {}));
    assert.equal(a.flood.aoi!.applied, false);
    assert.ok(a.flood.warnings.some((w) => w.includes("on the drawn area's edge")));
  });

  // D. Wording.
  it("D: a hole does not count as upstream area; the nearest-edge warning does not say 'downstream'", () => {
    const pond: [number, number][][] = [
      [
        [-2, -1.5],
        [2, -1.5],
        [2, -1],
        [-2, -1],
      ],
    ];
    const h = analyze(dam(rect(-3, 3, -3, 1, pond)));
    assert.ok(!h.flood.warnings.some((w) => w.includes("upstream of the breach")));
    assert.ok(Math.abs(h.flood.aoi!.upstreamFrac! - 0.25) < 0.01, `${h.flood.aoi!.upstreamFrac}`);
    const big = analyze(
      dam(
        rect(-3, 3, -2, 3, [
          [
            [-2, -1.5],
            [2, -1.5],
            [2, 2],
            [-2, 2],
          ],
        ]),
      ),
    );
    assert.ok(Math.abs(big.flood.aoi!.upstreamFrac! - 0.6) < 0.01, "filled outline, not net area");
    const n = analyze(dam(squareAoi(SITE.lon, SITE.lat, 1), {}, {}));
    assert.equal(n.flood.aoi!.reachBasis, "nearest-boundary");
    const edge = n.flood.warnings.find((w) => w.startsWith("The schematic flood is still"))!;
    assert.match(edge, /from the breach, direction unknown/);
    assert.ok(!edge.includes("downstream"), edge);
  });
});
