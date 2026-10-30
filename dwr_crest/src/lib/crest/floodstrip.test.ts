import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { describe, it } from "node:test";

import { downstreamDirection, polygonKm, selfIntersects, toLocal, type XY } from "./aoi.ts";
import { analyze, damLocation, ENGINE_VERSION } from "./engine.ts";
import { toUtm } from "./export.ts";
import {
  bandFor,
  clipToConvex,
  DEPTH_BANDS,
  exportFloodStripGeoJson,
  exportFloodStripKml,
  FLOOD_STRIP_DISCLAIMER,
  floodFootprint,
  floodStrip,
  type FloodStrip,
} from "./floodstrip.ts";
import { SAMPLE_DAMS } from "./samples.ts";
import type { Analysis, DamRecord, GeoJSONFeature } from "./types.ts";

const ND = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;
const LOC = damLocation(ND)!;
const U = downstreamDirection(ND.geometry, LOC).u!;
const ACRE = 4046.8564224;

/** Corridor along the engine's downstream direction: from `a0` to `a1` km along it, ±`w` km across. */
function corridor(a0: number, a1: number, w: number, skewDeg = 0): GeoJSONFeature {
  const b = Math.atan2(U[0], U[1]) + (skewDeg * Math.PI) / 180;
  const u = [Math.sin(b), Math.cos(b)];
  const v = [u[1], -u[0]];
  const c = (a: number, x: number): [number, number] => [a * u[0] + x * v[0], a * u[1] + x * v[1]];
  return polygonKm(LOC, [c(a0, -w), c(a1, -w), c(a1, w), c(a0, w)]);
}

const withAoi = (aoi: GeoJSONFeature): DamRecord => ({ ...ND, geometry: { ...ND.geometry, aoi } });

function rings(strip: FloodStrip): number[][][][] {
  return strip.features.flatMap((f) =>
    f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates,
  );
}

const local = (ring: number[][]): XY[] =>
  ring.slice(0, -1).map(([lon, lat]) => toLocal(lon, lat, LOC));

function areaAc(strip: FloodStrip) {
  const a = (r: XY[]) => {
    let s = 0;
    for (let i = 0; i < r.length; i++)
      s += r[i][0] * r[(i + 1) % r.length][1] - r[(i + 1) % r.length][0] * r[i][1];
    return Math.abs(s / 2);
  };
  return (
    rings(strip).reduce(
      (s, p) => s + a(local(p[0])) - p.slice(1).reduce((h, r) => h + a(local(r)), 0),
      0,
    ) / ACRE
  );
}

function inPoly(rs: XY[][], x: number, y: number) {
  const inRing = (r: XY[]) => {
    let c = false;
    for (let i = 0, j = r.length - 1; i < r.length; j = i++) {
      const [xi, yi] = r[i];
      const [xj, yj] = r[j];
      if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) c = !c;
    }
    return c;
  };
  return inRing(rs[0]) && !rs.slice(1).some(inRing);
}

function distToRings(rs: XY[][], p: XY) {
  let d = Infinity;
  for (const r of rs)
    for (let i = 0; i < r.length; i++) {
      const a = r[i];
      const b = r[(i + 1) % r.length];
      const dx = b[0] - a[0];
      const dy = b[1] - a[1];
      const t = Math.max(
        0,
        Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / (dx * dx + dy * dy || 1)),
      );
      d = Math.min(d, Math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy));
    }
  return d;
}

function assertValid(strip: FloodStrip) {
  for (const p of rings(strip)) {
    for (const r of p) {
      assert.deepEqual(r[0], r[r.length - 1], "closed ring");
      assert.ok(r.length >= 4);
      assert.equal(selfIntersects(local(r)), false, "ring crosses itself");
    }
  }
}

function assertDownstream(strip: FloodStrip) {
  const res = ND.geometry!.reservoirPoint!.geometry.coordinates as number[];
  const R = toLocal(res[0], res[1], LOC);
  assert.ok(R[0] * U[0] + R[1] * U[1] < -50, "reservoir upstream");
  for (const p of rings(strip))
    for (const [x, y] of local(p[0]))
      assert.ok(x * U[0] + y * U[1] >= -0.05, `vertex upstream: ${x * U[0] + y * U[1]}`);
}

function assertInside(strip: FloodStrip, aoi: GeoJSONFeature) {
  const a = (aoi.geometry.coordinates as number[][][]).map(local);
  for (const p of rings(strip))
    for (const r of p)
      for (const q of local(r))
        assert.ok(
          inPoly(a, q[0], q[1]) || distToRings(a, q) < 0.05,
          `vertex outside the drawn area by ${distToRings(a, q)} m`,
        );
}

describe("flood strip on the ND00199 sample (no drawn area)", () => {
  const result = analyze(ND);
  const s = floodStrip(ND, result);
  const strip = s.strip!;

  it("is built, banded by depth, with the facts on every feature", () => {
    assert.equal(s.reason, null);
    assert.equal(strip.type, "FeatureCollection");
    assert.ok(strip.features.length >= 4);
    const ids = strip.features.map((f) => f.properties.band);
    assert.equal(ids[0], bandFor(result.flood.maxDepthFt).id, "deepest band at the breach");
    assert.equal(ids[ids.length - 1], DEPTH_BANDS[0].id, "0.1–0.5 ft band at the end");
    for (const f of strip.features) {
      assert.equal(f.properties.disclaimer, FLOOD_STRIP_DISCLAIMER);
      assert.equal(f.properties.dam_id, "ND00199");
      assert.equal(f.properties.crest_navd88_ft, 1072.24);
      assert.equal(f.properties.peak_cfs, Math.round(result.breach.peakCfs));
      assert.equal(f.properties.engine_version, ENGINE_VERSION);
      assert.ok(f.properties.depth_max_ft >= f.properties.depth_min_ft);
    }
    assert.deepEqual(floodFootprint(ND, result), strip);
  });

  it("is valid (closed, no self-intersection) and lies downstream of the dam line", () => {
    assertValid(strip);
    assertDownstream(strip);
  });

  it("is 2·halfM wide at every wet station (measured in UTM, within 1%)", () => {
    for (const st of result.flood.stations.filter((x) => x.depthFt >= 0.1 - 1e-9)) {
      const x = st.s * result.flood.lengthM;
      const at = rings(strip)
        .flatMap((p) => p[0].slice(0, -1))
        .filter(([lon, lat]) => {
          const q = toLocal(lon, lat, LOC);
          return Math.abs(q[0] * U[0] + q[1] * U[1] - x) < 0.05;
        });
      assert.ok(at.length >= 2, `station ${st.xKm} km`);
      let w = 0;
      for (const a of at)
        for (const b of at) {
          const ua = toUtm(a[0], a[1], 14);
          const ub = toUtm(b[0], b[1], 14);
          w = Math.max(w, Math.hypot(ua.x - ub.x, ua.y - ub.y));
        }
      assert.ok(
        Math.abs(w / (2 * st.halfM) - 1) < 0.01,
        `station ${st.xKm.toFixed(2)} km: ${w.toFixed(1)} m vs ${(2 * st.halfM).toFixed(1)} m`,
      );
    }
  });

  it("area matches the engine's wet area (ratio reported)", (t) => {
    const ratio = areaAc(strip) / result.flood.acres;
    t.diagnostic(
      `ND00199 no AOI: strip ${areaAc(strip).toFixed(1)} ac / flood.acres ${result.flood.acres.toFixed(1)} ac = ${ratio.toFixed(4)}`,
    );
    assert.ok(Math.abs(ratio - 1) < 0.01, `ratio ${ratio}`);
    assert.ok(Math.abs(strip.properties.strip_area_ac / result.flood.acres - 1) < 0.01);
  });
});

describe("flood strip with a drawn area on ND00199", () => {
  it("a corridor narrower than the strip and shorter than the reach: clipped, inside it, ratio reported", (t) => {
    const aoi = corridor(-0.05, 6, 0.4, 3);
    const d = withAoi(aoi);
    const r = analyze(d);
    assert.equal(r.flood.aoi!.applied, true);
    assert.equal(r.flood.reachSource, "aoi");
    const strip = floodFootprint(d, r)!;
    assert.ok(strip);
    assert.equal(strip.properties.clipped_to_aoi, true);
    assertValid(strip);
    assertDownstream(strip);
    assertInside(strip, aoi);
    const ratio = areaAc(strip) / r.flood.acres;
    t.diagnostic(
      `ND00199 6 km x 0.8 km corridor: strip ${areaAc(strip).toFixed(1)} ac / flood.acres ${r.flood.acres.toFixed(1)} ac = ${ratio.toFixed(4)}`,
    );
    assert.ok(ratio > 0.95 && ratio < 1.05, `ratio ${ratio}`);
  });

  it("breach outside the drawn area: the strip starts where the valley enters it", (t) => {
    const aoi = corridor(1.5, 5, 2);
    const d = withAoi(aoi);
    const r = analyze(d);
    assert.ok(r.flood.aoi!.nearKm > 1.4);
    const strip = floodFootprint(d, r)!;
    assertValid(strip);
    assertInside(strip, aoi);
    for (const p of rings(strip))
      for (const [x, y] of local(p[0]))
        assert.ok(x * U[0] + y * U[1] >= r.flood.aoi!.nearKm * 1000 - 0.05);
    const ratio = areaAc(strip) / r.flood.acres;
    t.diagnostic(
      `ND00199 corridor 1.5–5 km: strip ${areaAc(strip).toFixed(1)} ac / flood.acres ${r.flood.acres.toFixed(1)} ac = ${ratio.toFixed(4)}`,
    );
    assert.ok(ratio > 0.95 && ratio < 1.05, `ratio ${ratio}`);
  });

  // A hole across the centreline ends the engine's reach there (first exit along the axis), so this one sits to the side.
  it("a hole in the drawn area is cut out of the strip", (t) => {
    const b = Math.atan2(U[0], U[1]);
    const u = [Math.sin(b), Math.cos(b)];
    const v = [u[1], -u[0]];
    const c = (a: number, x: number): [number, number] => [
      a * u[0] + x * v[0],
      a * u[1] + x * v[1],
    ];
    const aoi = polygonKm(
      LOC,
      [c(-0.05, -3), c(20, -3), c(20, 3), c(-0.05, 3)],
      [[c(3, 0.1), c(3.4, 0.1), c(3.4, 0.25), c(3, 0.25)]],
    );
    const d = withAoi(aoi);
    const r = analyze(d);
    const strip = floodFootprint(d, r)!;
    assertValid(strip);
    assertInside(strip, aoi);
    assert.ok(
      rings(strip).some((p) => p.length > 1),
      "a ring with a hole",
    );
    const ratio = areaAc(strip) / r.flood.acres;
    t.diagnostic(
      `ND00199 corridor with a 0.4 x 0.15 km hole off the centreline: strip ${areaAc(strip).toFixed(1)} ac / flood.acres ${r.flood.acres.toFixed(1)} ac = ${ratio.toFixed(4)}`,
    );
    assert.ok(ratio > 0.95 && ratio < 1.05, `ratio ${ratio}`);
  });
});

describe("no strip: no geometry, nothing wet, or nothing inside the drawn area", () => {
  it("no breach location", () => {
    const d: DamRecord = { ...ND, lat: undefined, lon: undefined, geometry: undefined };
    const s = floodStrip(d, analyze(d));
    assert.equal(s.strip, null);
    assert.match(s.reason!, /no breach location/);
  });
  it("no downstream direction (a location but no dam line or reservoir point)", () => {
    const d: DamRecord = { ...ND, geometry: undefined };
    const s = floodStrip(d, analyze(d));
    assert.equal(s.strip, null);
    assert.match(s.reason!, /Downstream direction unknown/);
  });
  it("no wet station", () => {
    const r = analyze(ND);
    const dry: Analysis = {
      ...r,
      flood: { ...r.flood, stations: r.flood.stations.map((x) => ({ ...x, depthFt: 0.05 })) },
    };
    const s = floodStrip(ND, dry);
    assert.equal(s.strip, null);
    assert.match(s.reason!, /under 0\.1 ft/);
  });
  it("a drawn area upstream of the dam", () => {
    const d = withAoi(corridor(-5, -1, 1));
    const s = floodStrip(d, analyze(d));
    assert.equal(s.strip, null);
    assert.match(s.reason!, /does not reach/);
  });
});

describe("clipToConvex", () => {
  it("clips a concave L to a square and keeps the area", () => {
    const sq: XY[] = [
      [0, 0],
      [2, 0],
      [2, 2],
      [0, 2],
    ];
    const L: XY[] = [
      [-1, -1],
      [3, -1],
      [3, 1],
      [1, 1],
      [1, 3],
      [-1, 3],
    ];
    const out = clipToConvex(L, sq);
    let a = 0;
    for (let i = 0; i < out.length; i++)
      a += out[i][0] * out[(i + 1) % out.length][1] - out[(i + 1) % out.length][0] * out[i][1];
    assert.ok(Math.abs(Math.abs(a / 2) - 3) < 1e-9);
  });
});

// Export snapshot: ND00199 with the stations thinned to every 20th, so the files stay small enough to read.
// UPDATE_SNAPSHOTS=1 npm test rewrites them after an intended change.
describe("flood strip exports (snapshot)", () => {
  const r = analyze(ND);
  const thin: Analysis = {
    ...r,
    flood: { ...r.flood, stations: r.flood.stations.filter((_, i) => i % 20 === 0) },
  };
  const strip = floodFootprint(ND, thin)!;
  const files: [string, string][] = [
    ["flood_strip_nd00199_thin.geojson", exportFloodStripGeoJson(strip)],
    ["flood_strip_nd00199_thin.kml", exportFloodStripKml(strip)],
  ];
  for (const [name, text] of files) {
    it(name, () => {
      const url = new URL(`./__snapshots__/${name}`, import.meta.url);
      if (process.env.UPDATE_SNAPSHOTS || !existsSync(url)) writeFileSync(url, text);
      assert.equal(text, readFileSync(url, "utf8"));
    });
  }
  it("GeoJSON is WGS 84 lon/lat with the disclaimer, dam, crest, peak and engine version", () => {
    const g = JSON.parse(files[0][1]);
    assert.equal(g.properties.disclaimer, FLOOD_STRIP_DISCLAIMER);
    assert.equal(g.properties.dam_id, "ND00199");
    assert.equal(g.properties.engine_version, ENGINE_VERSION);
    const [lon, lat] = g.features[0].geometry.coordinates[0][0];
    assert.ok(lon > -98 && lon < -97 && lat > 46.9 && lat < 47.1);
  });
  it("KML carries the disclaimer, dam, crest, peak and engine version", () => {
    const k = files[1][1];
    for (const s of [
      FLOOD_STRIP_DISCLAIMER,
      "ND00199",
      "1072.24 ft NAVD88",
      `${Math.round(r.breach.peakCfs).toLocaleString("en-US")} cfs`,
      ENGINE_VERSION,
    ])
      assert.ok(k.includes(s), s);
    assert.equal((k.match(/<Placemark>/g) ?? []).length, strip.features.length);
  });
});
