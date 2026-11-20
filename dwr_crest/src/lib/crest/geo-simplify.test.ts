// Display simplification of the map's rings (geo-simplify.ts): exact exports untouched, the drawn copy lighter.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { gunzipSync } from "node:zlib";

import { analyze } from "./engine.ts";
import { displayZoomKey, type LonLat, metresPerPixel, simplifyForDisplay, simplifyRing, vertexCount } from "./geo-simplify.ts";
import { buildOutline, type DemGrid } from "./outline.ts";
import { SAMPLE_DAMS } from "./samples.ts";

const LAT = 47;
const kx = 111320 * Math.cos((LAT * Math.PI) / 180);
/** A ring in lon/lat from local metres around (-100, 47). */
const ring = (pts: [number, number][]): LonLat[] => {
  const ll = pts.map(([e, n]) => [-100 + e / kx, LAT + n / 110574] as LonLat);
  return [...ll, ll[0]];
};
const signedArea = (r: LonLat[]) => {
  let s = 0;
  for (let i = 0; i + 1 < r.length; i++) s += r[i][0] * r[i + 1][1] - r[i + 1][0] * r[i][1];
  return s / 2;
};

describe("simplifyRing", () => {
  it("drops the points of a jagged edge that lie within the tolerance, keeps the corners, stays closed", () => {
    // A 100 × 100 m square with a 0.2 m sawtooth along its north edge (21 points) — invisible at 1 m.
    const pts: [number, number][] = [[0, 0], [100, 0], [100, 100]];
    for (let i = 10; i >= 1; i--) pts.push([i * 10, 100 + (i % 2 ? 0.2 : -0.2)]);
    pts.push([0, 100]);
    const r = ring(pts);
    const s = simplifyRing(r, 1);
    assert.deepEqual(s[0], s[s.length - 1], "closed");
    assert.ok(s.length <= 6, `kept ${s.length} points of ${r.length}`);
    assert.ok(Math.abs(signedArea(s) - signedArea(r)) / Math.abs(signedArea(r)) < 1e-3, "area kept");
    // A tolerance under the sawtooth keeps it.
    assert.equal(simplifyRing(r, 0.1).length, r.length);
  });

  it("never collapses a ring: a small ring far under the tolerance comes back whole", () => {
    const r = ring([[0, 0], [2, 0], [2, 2], [1, 3], [0, 2]]);
    assert.deepEqual(simplifyRing(r, 50), r);
    assert.deepEqual(simplifyRing(r, 0), r);
  });

  it("keeps a thin long ring's two ends and its width", () => {
    // 1,000 m long, 4 m wide, with 100 points down each side.
    const pts: [number, number][] = [];
    for (let i = 0; i <= 100; i++) pts.push([i * 10, 0]);
    for (let i = 100; i >= 0; i--) pts.push([i * 10, 4]);
    const s = simplifyRing(ring(pts), 1);
    assert.ok(s.length >= 5 && s.length <= 7, `${s.length} points`);
    assert.ok(Math.abs(signedArea(s) - signedArea(ring(pts))) / Math.abs(signedArea(ring(pts))) < 1e-6);
  });

  it("metres per pixel: zoom 15 at 47° is about 3.3 m; whole-zoom keys", () => {
    assert.ok(Math.abs(metresPerPixel(15, 47) - 3.26) < 0.05);
    assert.ok(metresPerPixel(12, 47) > 8 * metresPerPixel(15, 47) - 1e-6);
    assert.equal(displayZoomKey(13.4), 13);
    assert.equal(displayZoomKey(13.6), 14);
  });
});

describe("simplifyForDisplay on the ND00199 outline (30 m fixture)", async () => {
  const ND = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;
  const RESULT = analyze(ND);
  const fx = (name: string) => new URL(`./__fixtures__/${name}`, import.meta.url);
  const META = JSON.parse(readFileSync(fx("dem3dep_nd00199_30m.json"), "utf8"));
  const U16 = new Uint16Array(new Uint8Array(gunzipSync(readFileSync(fx("dem3dep_nd00199_30m.u16cm.gz")))).buffer);
  const DEM: DemGrid = { bbox: META.bbox, width: META.width, height: META.height, cellM: META.cellM, z: Float32Array.from(U16, (v) => v / 100 + 250) };
  const built = await buildOutline(ND, RESULT, DEM);
  assert.ok(built.ok);
  const outline = built.cache.outline;
  const before = JSON.stringify(outline);
  const count = (fc: typeof outline) => fc.features.reduce((s, f) => s + vertexCount(f.geometry), 0);

  it("draws far fewer vertices zoomed out, the same bands, the extent line untouched, and leaves the exact layer alone", () => {
    const n0 = count(outline);
    const z11 = simplifyForDisplay(outline, 11);
    const z15 = simplifyForDisplay(outline, 15);
    assert.ok(count(z11) < 0.5 * n0, `zoom 11: ${count(z11)} of ${n0}`);
    assert.ok(count(z15) <= n0 && count(z15) >= count(z11));
    assert.deepEqual(z11.features.map((f) => f.properties.band), outline.features.map((f) => f.properties.band));
    assert.equal(z11.extent, outline.extent);
    assert.equal(z11.properties, outline.properties);
    for (const f of z11.features) for (const ring of f.geometry.type === "Polygon" ? f.geometry.coordinates : f.geometry.coordinates.flat()) assert.deepEqual(ring[0], ring[ring.length - 1]);
    assert.equal(JSON.stringify(outline), before, "the exact layer is not touched");
  });

  it("at a zoom where a pixel is under the cell edge it returns the collection itself", () => {
    assert.equal(simplifyForDisplay(outline, 19), outline);
  });
});
