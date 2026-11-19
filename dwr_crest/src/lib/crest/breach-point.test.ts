// Automatic breach point (breach-point.ts) on a synthetic valley, and the local DEM terrain source (dem-source.ts).
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { structureRecord, type Structure } from "./batch.ts";
import { autoBreachOn, autoBreachPoint } from "./breach-point.ts";
import { fetchLocalDem, localDemUrl } from "./dem-source.ts";
import { demSourceLabel, fetchDem, setDemOverride, type DemGrid, type DemRequest } from "./outline.ts";
import type { DamRecord, GeoJSONFeature } from "./types.ts";

const M_PER_DEG_LAT = 110574;
const kx = (lat: number) => 111320 * Math.cos((lat * Math.PI) / 180);
const LON = -100;
const LAT = 47;
/** Channel 40 m east of the record's point, so "the middle of the line" is not the right answer by luck. */
const CH_E = 40;

/** Valley running south, channel at e = CH_E, banks 3 %, embankment 8 m high across it at n = 0 (|e - CH_E| < 150). */
function valleyZ(lon: number, lat: number) {
  const e = (lon - LON) * kx(LAT);
  const n = (lat - LAT) * M_PER_DEG_LAT;
  let z = 500 + 0.01 * n + 0.03 * Math.abs(e - CH_E);
  if (Math.abs(n) < 10 && Math.abs(e - CH_E) < 150) z += 8;
  return z;
}
const fakeDem = async (req: DemRequest): Promise<DemGrid> => {
  const z = new Float32Array(req.width * req.height);
  for (let j = 0; j < req.height; j++)
    for (let i = 0; i < req.width; i++) {
      const lon = req.bbox[0] + ((i + 0.5) / req.width) * (req.bbox[2] - req.bbox[0]);
      const lat = req.bbox[3] - ((j + 0.5) / req.height) * (req.bbox[3] - req.bbox[1]);
      z[j * req.width + i] = valleyZ(lon, lat);
    }
  return { ...req, z };
};
const at = (eM: number, nM: number) => [LON + eM / kx(LAT), LAT + nM / M_PER_DEG_LAT];
const eOf = (c: number[]) => (c[0] - LON) * kx(LAT);
const nOf = (c: number[]) => (c[1] - LAT) * M_PER_DEG_LAT;

const S: Structure = {
  id: "DWR-9",
  kind: "dam",
  label: "DWR-9",
  nid: null,
  status: "Intact",
  county: "Cass",
  lat: LAT,
  lon: LON,
  heightFt: 20,
  storageAcFt: 120,
  areaAc: 15,
  lengthFt: 600,
  year: 1980,
  hazard: "Low",
  purpose: "Livestock",
  huc8: null,
};
const base = (): DamRecord => structureRecord(S);
const line = (): GeoJSONFeature => ({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: [at(-100, 0), at(180, 0)] } });

describe("automatic breach point", () => {
  it("puts the breach on the old channel along a drawn dam line", async () => {
    const dam = { ...base(), geometry: { damLine: line(), reservoirPoint: { type: "Feature", properties: {}, geometry: { type: "Point", coordinates: at(CH_E, 200) } } as GeoJSONFeature } };
    const r = autoBreachOn(dam, await fakeDem({ bbox: [LON - 0.01, LAT - 0.006, LON + 0.01, LAT + 0.006], width: 300, height: 260, cellM: 5 }));
    assert.ok(r.ok, r.ok ? "" : r.reason);
    const c = r.geometry.breachPoint!.geometry.coordinates as number[];
    assert.ok(Math.abs(eOf(c) - CH_E) <= 6, `breach ${eOf(c).toFixed(1)} m east, channel at ${CH_E}`);
    assert.ok(Math.abs(nOf(c)) < 1, "on the dam line");
    assert.equal(r.geometry.breachPoint!.properties?.role, "breach");
    assert.match(String(r.geometry.breachPoint!.properties?.source), /^auto: lowest downstream toe/);
    assert.equal(r.geometry.reservoirPoint!.geometry.coordinates[1], dam.geometry.reservoirPoint.geometry.coordinates[1], "a drawn reservoir point is kept");
  });

  it("finds downstream from the ground when there is no reservoir point, and adds one upstream", async () => {
    const dam = { ...base(), geometry: { damLine: line() } };
    const r = await autoBreachPoint(dam, fakeDem);
    assert.ok(r.ok, r.ok ? "" : r.reason);
    const res = r.geometry.reservoirPoint!.geometry.coordinates as number[];
    assert.ok(nOf(res) > 100, "reservoir north of the dam (upstream)");
    assert.match(r.note, /reservoir point/);
  });

  it("lays a dam line when none is drawn", async () => {
    const r = await autoBreachPoint(base(), fakeDem);
    assert.ok(r.ok, r.ok ? "" : r.reason);
    assert.equal(r.geometry.damLine!.geometry.type, "LineString");
    assert.ok(r.geometry.breachPoint);
    assert.match(r.note, /No dam line was drawn/);
  });

  it("says why when there is no location or no terrain", async () => {
    const r = await autoBreachPoint({ ...base(), lat: NaN, lon: NaN }, fakeDem);
    assert.equal(r.ok, false);
    const down = await autoBreachPoint(base(), async () => {
      throw new Error("offline");
    });
    assert.deepEqual(down, { ok: false, reason: "offline" });
  });
});

describe("structureRecord", () => {
  it("fills DWR numbers and leaves elevations blank", () => {
    const d = base();
    assert.equal(d.heightFt, 20);
    assert.equal(d.storageAcFt, 120);
    assert.equal(d.lengthFt, 600);
    assert.ok(Number.isNaN(d.crestFt) && Number.isNaN(d.streambedFt));
    assert.equal(d.lat, LAT);
    assert.equal(structureRecord(S, undefined, "x1").id, "x1");
  });
});

describe("local DEM terrain source", () => {
  const req: DemRequest = { bbox: [-97.1, 46.8, -97.0, 46.9], width: 3, height: 2, cellM: 2 };
  const bytes = () => {
    const b = new ArrayBuffer(24);
    const v = new DataView(b);
    for (let i = 0; i < 6; i++) v.setFloat32(i * 4, 300 + i, true);
    return b;
  };

  it("asks the desktop server for the window and reads float32", async () => {
    let url = "";
    const g = await fetchLocalDem(req, "nd.vrt", async (u) => ((url = u), { ok: true, status: 200, arrayBuffer: async () => bytes(), text: async () => "" }));
    assert.equal(url, localDemUrl(req));
    assert.equal(url, "/api/dem?bbox=-97.1,46.8,-97,46.9&w=3&h=2&cell=2");
    assert.deepEqual([...g.z], [300, 301, 302, 303, 304, 305]);
  });

  it("names the file and the server's reason on failure", async () => {
    await assert.rejects(
      fetchLocalDem(req, "nd.vrt", async () => ({ ok: false, status: 502, arrayBuffer: async () => new ArrayBuffer(0), text: async () => '{"error":"gdalwarp failed: no such file"}' })),
      /local DEM nd\.vrt .*502: gdalwarp failed/,
    );
  });

  it("answers fetchDem once chosen; a caller's own fetch still goes to 3DEP", async () => {
    const asked: DemRequest[] = [];
    setDemOverride({ fetch: async (r) => (asked.push(r), { ...r, z: new Float32Array(r.width * r.height).fill(7) }), label: "local DEM nd.vrt" });
    try {
      assert.equal(demSourceLabel(), "local DEM nd.vrt");
      const g = await fetchDem(req);
      assert.equal(g.z[0], 7);
      assert.equal(asked.length, 1);
      const own = await fetchDem(req, async () => ({ ok: true, status: 200, arrayBuffer: async () => bytes() }));
      assert.equal(own.z[0], 300);
      assert.equal(asked.length, 1);
    } finally {
      setDemOverride(null);
    }
    assert.equal(demSourceLabel(), "3DEP");
  });
});
