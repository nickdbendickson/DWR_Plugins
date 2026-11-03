import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { gunzipSync } from "node:zlib";

import { analyze } from "./engine.ts";
import { floodStrip } from "./floodstrip.ts";
import { exportFloodGeoJson, exportFloodKml, fetchNd911, nd911State, nd911Table } from "./nd911.ts";
import {
  buildOutline,
  corridorHalfM,
  DEM_ATTRIBUTION,
  DEM_SERVICE,
  type DemGrid,
  demRequest,
  demUrl,
  fetchDem,
  lookupArea,
  OUTLINE_LABEL,
  OUTLINE_OFFLINE,
  findToe,
  outlineAcresNote,
  outlineInputs,
  outlineState,
  parseBsq,
  traceStartNote,
} from "./outline.ts";
import { bboxContains, type PlssCache, plssState, plssTable, stripBbox } from "./plss.ts";
import { SAMPLE_DAMS } from "./samples.ts";
import type { DamRecord } from "./types.ts";

const ND = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;
const RESULT = analyze(ND);
const STRIP = floodStrip(ND, RESULT).strip!;
const fx = (name: string) => new URL(`./__fixtures__/${name}`, import.meta.url);
/**
 * The real 3DEP terrain for the ND00199 outline box, fetched once on 2026-10-30 with the desk's own exportImage
 * request at 30 m (the desk asks for 10 m; 30 m keeps the fixture small). Stored as uint16 cm above 250 m (±0.5 cm).
 */
const META = JSON.parse(readFileSync(fx("dem3dep_nd00199_30m.json"), "utf8"));
const U16 = new Uint16Array(new Uint8Array(gunzipSync(readFileSync(fx("dem3dep_nd00199_30m.u16cm.gz")))).buffer);
const DEM: DemGrid = { bbox: META.bbox, width: META.width, height: META.height, cellM: META.cellM, z: Float32Array.from(U16, (v) => v / 100 + 250) };
const PLSS = JSON.parse(readFileSync(fx("plss_nd00199.json"), "utf8")) as PlssCache;
const PAGE911 = JSON.parse(readFileSync(fx("nd911_nd00199.json"), "utf8"));
const NOW = new Date("2026-10-30T15:30:00Z");
const INP = (() => {
  const io = outlineInputs(ND, RESULT);
  assert.ok(io.ok);
  return io.inputs;
})();
const BUILT = await buildOutline(ND, RESULT, DEM, { builtAt: NOW });
assert.ok(BUILT.ok, BUILT.ok ? "" : BUILT.reason);
const CACHE = BUILT.cache;
const OUTLINE = CACHE.outline;

const ringsOf = (o: typeof OUTLINE) =>
  o.features.flatMap((f) => (f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates));
const signedArea = (r: number[][]) => {
  let s = 0;
  for (let i = 0; i + 1 < r.length; i++) s += r[i][0] * r[i + 1][1] - r[i + 1][0] * r[i][1];
  return s / 2;
};

describe("3DEP request", () => {
  it("ND00199 at 10 m: 1090 × 996 cells (4.3 MB of float32) over the corridor box plus the 500 m trace margin", () => {
    const r = demRequest(INP);
    assert.equal(r.width, 1090);
    assert.equal(r.height, 996);
    assert.equal(r.cellM, 10);
    assert.equal(r.width * r.height * 4, 4_342_560);
    assert.deepEqual(r.bbox, [-97.39979801, 46.90014443, -97.25626988, 46.99021985]);
  });
  it("the fixture is the same request at 30 m", () => {
    const r = demRequest(INP, 30);
    assert.deepEqual([r.bbox, r.width, r.height, r.cellM], [META.bbox, META.width, META.height, META.cellM]);
    assert.equal(demUrl(r), META.url);
  });
  it("asks the 3DEP ImageServer for raw float32, WGS 84, bilinear, with a no-data value", () => {
    const u = new URL(demUrl(demRequest(INP)));
    assert.equal(`${u.origin}${u.pathname}`, `${DEM_SERVICE}/exportImage`);
    for (const [k, v] of Object.entries({ format: "bsq", pixelType: "F32", bboxSR: "4326", imageSR: "4326", noData: "-9999", f: "image", size: "1090,996", adjustAspectRatio: "false" }))
      assert.equal(u.searchParams.get(k), v, k);
  });
  it("caps very long reaches at 3000 cells a side (coarser cells)", () => {
    const r = demRequest(INP, 10, 300);
    assert.ok(Math.max(r.width, r.height) <= 300);
    assert.ok(r.cellM > 30);
  });
  it("corridor: three half-widths, at least 300 m, at most 2 km", () => {
    assert.deepEqual([corridorHalfM(20), corridorHalfM(200), corridorHalfM(900)], [300, 600, 2000]);
  });
});

describe("reading and fetching the raster (fetch is faked)", () => {
  it("parses little-endian float32 and ignores a trailing mask", () => {
    const buf = new ArrayBuffer(4 * 4 + 2);
    const v = new DataView(buf);
    [301.5, 302.25, -9999, 299].forEach((z, i) => v.setFloat32(i * 4, z, true));
    assert.deepEqual([...parseBsq(buf, 2, 2)], [301.5, 302.25, -9999, 299]);
    assert.throws(() => parseBsq(new ArrayBuffer(8), 2, 2), /returned 8 bytes; expected 16/);
  });
  it("one request, the grid back; a failure says it needs a connection", async () => {
    const req = demRequest(INP, 30);
    const raw = new Float32Array(DEM.z);
    const urls: string[] = [];
    const ok = async (url: string) => {
      urls.push(url);
      return { ok: true, status: 200, arrayBuffer: async () => raw.buffer };
    };
    const g = await fetchDem(req, ok);
    assert.equal(urls.length, 1);
    assert.equal(g.z.length, req.width * req.height);
    const down = async () => {
      throw new TypeError("Failed to fetch");
    };
    await assert.rejects(fetchDem(req, down), (e: Error) => e.message.includes(OUTLINE_OFFLINE));
    let calls = 0;
    const bad = async () => {
      calls++;
      return { ok: false, status: 503, arrayBuffer: async () => new ArrayBuffer(0) };
    };
    await assert.rejects(fetchDem(req, bad, [0, 0]), /answered 503/);
    assert.equal(calls, 3, "a 5xx is retried twice");
    calls = 0;
    const flaky = async () => (++calls < 3 ? { ok: false, status: 502, arrayBuffer: async () => new ArrayBuffer(0) } : { ok: true, status: 200, arrayBuffer: async () => raw.buffer });
    assert.equal((await fetchDem(req, flaky, [0, 0])).z.length, req.width * req.height);
    calls = 0;
    const missing = async () => {
      calls++;
      return { ok: false, status: 404, arrayBuffer: async () => new ArrayBuffer(0) };
    };
    await assert.rejects(fetchDem(req, missing, [0, 0]), /answered 404/);
    assert.equal(calls, 1, "a 4xx is not retried");
  });
});

describe("ND00199 outline on the real 3DEP terrain (30 m fixture)", () => {
  const s = CACHE.stats;
  it("follows the creek from the breach, wet ground under the desk water surface", () => {
    assert.equal(s.dem.missing, 0);
    assert.ok(s.channelKm > s.reachKm, "the traced creek bends, so it is longer than the straight reach");
    assert.ok(s.reachKm > 10 && s.reachKm <= RESULT.flood.lengthM / 1000 + 0.05, String(s.reachKm));
    // Bed: starts at the toe, 1046.2 ft, next to the record streambed (1043.59 ft) and below the crest (1071.09 ft);
    // ~950 ft at the end. Before 2026-11-03 it started at 1070.6 ft on the upland: the DEM sat ~1.5× stretched
    // north–south (the ImageServer widened the latitude extent) and the start was a fixed 60 m step down the bearing.
    assert.equal(s.traceStart?.how, "toe");
    assert.ok(Math.abs(s.bedTopFt - 1046.2) < 1, String(s.bedTopFt));
    assert.ok(s.bedTopFt < ND.crestFt - ND.heightFt / 2);
    assert.ok(s.bedEndFt < 960 && s.bedEndFt > 940, String(s.bedEndFt));
    assert.ok(s.outlineAc > 1200 && s.outlineAc < 1500, String(s.outlineAc));
    assert.equal(s.engineAc, STRIP.properties.engine_wet_area_ac);
    assert.ok(s.outlineAc <= s.wetCellAc && s.outlineAc > 0.9 * s.wetCellAc);
  });
  it("bands add up to the outline and every band is a real depth band", () => {
    const sum = s.bands.reduce((t, b) => t + b.ac, 0);
    assert.ok(Math.abs(sum - s.outlineAc) < 0.01);
    assert.deepEqual(
      OUTLINE.features.map((f) => f.properties.band),
      s.bands.map((b) => b.band),
    );
    for (const f of OUTLINE.features) assert.ok(f.properties.depth_min_ft >= 0.1 && f.properties.depth_max_ft >= f.properties.depth_min_ft);
  });
  it("valid GeoJSON rings: closed, outer counter-clockwise, holes clockwise, inside the DEM box", () => {
    let n = 0;
    for (const poly of ringsOf(OUTLINE))
      poly.forEach((r, i) => {
        n++;
        assert.deepEqual(r[0], r[r.length - 1]);
        assert.ok(r.length >= 4);
        assert.equal(signedArea(r) > 0, i === 0);
        for (const [x, y] of r) assert.ok(x >= DEM.bbox[0] - 1e-7 && x <= DEM.bbox[2] + 1e-7 && y >= DEM.bbox[1] - 1e-7 && y <= DEM.bbox[3] + 1e-7);
      });
    assert.equal(n, s.rings);
  });
  it("is labelled as approximate and carries the 3DEP source", () => {
    assert.equal(OUTLINE.properties.disclaimer, OUTLINE_LABEL);
    assert.equal(OUTLINE_LABEL, "Approximate inundation outline from 3DEP terrain and desk depths, not a hydraulic model");
    assert.equal(OUTLINE.properties.kind, "terrain_outline");
    assert.equal(OUTLINE.properties.outline_area_ac, s.outlineAc);
    assert.equal(CACHE.attribution, DEM_ATTRIBUTION);
    assert.equal(CACHE.source, DEM_SERVICE);
    assert.equal(CACHE.builtAt, "2026-10-30T15:30:00.000Z");
  });
  it("the acres note compares with the engine and says why", () => {
    const note = outlineAcresNote(s);
    assert.match(note, /^Outline \d+\.\d ac against the engine's 2358\.3 wet ac \(-\d+%\)\./);
    assert.match(note, /schematic valley/);
    assert.match(note, /not from the terrain/);
  });
  it("chunked: yields to the page and builds the same outline", async () => {
    let yields = 0;
    const again = await buildOutline(ND, RESULT, DEM, {
      builtAt: NOW,
      yieldFn: async () => {
        yields++;
      },
    });
    assert.ok(again.ok);
    assert.ok(yields > 3, String(yields));
    assert.deepEqual(again.cache, CACHE);
  });
  it("no terrain under the water surface: says so", async () => {
    const high = { ...DEM, z: new Float32Array(DEM.z.length).fill(-9999) };
    const r = await buildOutline(ND, RESULT, high);
    assert.equal(r.ok, false);
  });
});

describe("state: built on request, saved on the dam, offline after", () => {
  it("not built: build online, the connection note offline", () => {
    assert.equal(outlineState(ND, RESULT, true).state, "build");
    const off = outlineState(ND, RESULT, false);
    assert.equal(off.state, "offline");
    assert.equal(off.message, OUTLINE_OFFLINE);
  });
  it("saved with the same inputs: ready, online or not", () => {
    const dam: DamRecord = { ...ND, outline: CACHE };
    for (const online of [true, false]) {
      const st = outlineState(dam, RESULT, online);
      assert.equal(st.state, "ready");
      if (st.state === "ready") assert.equal(st.stats.outlineAc, CACHE.stats.outlineAc);
    }
  });
  it("desk inputs changed: asks to build again (offline: needs a connection)", () => {
    const changed: DamRecord = { ...ND, storageAcFt: ND.storageAcFt * 2, outline: CACHE };
    const r2 = analyze(changed);
    assert.notEqual(outlineInputs(changed, r2).ok && (outlineInputs(changed, r2) as { inputs: { fingerprint: string } }).inputs.fingerprint, INP.fingerprint);
    assert.match(outlineState(changed, r2, true).message ?? "", /changed since the terrain outline was built/);
    assert.equal(outlineState(changed, r2, false).state, "offline");
  });
  it("a layer saved before the 2026-11-03 terrain fix (no trace start) is rebuilt, and says why", () => {
    const { traceStart: _t, ...oldStats } = CACHE.stats;
    const old: DamRecord = { ...ND, outline: { ...CACHE, fingerprint: "old", stats: oldStats } };
    const st = outlineState(old, RESULT, true);
    assert.equal(st.state, "build");
    assert.match(st.message ?? "", /before the 2026-11-03 terrain fix/);
  });
});

describe("trace start: the toe below the dam (the Breach Runner's inflow rule)", () => {
  // A 41 × 41 grid of 10 m cells, dam point at the centre, downstream = east. Ground 100 m (the dam at 100 m, height
  // 10 m: the toe must be at or below 95 m).
  const W = 41;
  const mk = (z: (x: number, y: number) => number) => {
    const g: DemGrid = { bbox: [0, 0, 1, 1], width: W, height: W, cellM: 10, z: new Float32Array(W * W) };
    const along = new Float32Array(W * W);
    const dom = new Uint8Array(W * W);
    const cellX = (i: number) => (i - 20) * 10;
    const cellY = (j: number) => (20 - j) * 10;
    for (let j = 0; j < W; j++)
      for (let i = 0; i < W; i++) {
        const k = j * W + i;
        g.z[k] = z(cellX(i), cellY(j));
        along[k] = cellX(i);
        dom[k] = cellX(i) >= 0 ? 1 : 0;
      }
    const t = { W, H: W, cellX, cellY, along, dom, a0: 0, cellM: 10, valid: () => true };
    const xy = (k: number) => [cellX(k % W), cellY(Math.floor(k / W))];
    return { g, t, xy };
  };
  const dam = { heightFt: 10 / 0.3048 };
  const crestFt = 100 / 0.3048;
  it("the lowest qualifying ground within the usual radius, not the cell on the axis", () => {
    // A creek 50 m north of the axis, parallel to it; everything else is upland at 100 m.
    const { g, t, xy } = mk((x, y) => (y === 50 && x > 0 ? 94 - x / 1000 : 100));
    const r = findToe(g, dam, crestFt, t);
    assert.equal(r.start.how, "toe");
    assert.deepEqual(xy(r.k), [70, 50], "lowest within max(90 m, 8 × 10 m) = 90 m: the farthest creek cell inside it");
  });
  it("no qualifying ground within 90 m: the nearest out to 400 m", () => {
    const { g, t, xy } = mk((x, y) => (y === -150 && x > 0 ? 90 : 100));
    const r = findToe(g, dam, crestFt, t);
    assert.equal(r.start.how, "toe-wide");
    assert.deepEqual(xy(r.k), [10, -150]);
    assert.match(traceStartNote({ ...CACHE.stats, traceStart: r.start }) ?? "", /outside the usual search radius/);
  });
  it("upstream ground never counts, and none at all falls back to the axis", () => {
    const { g, t } = mk((x) => (x < 0 ? 50 : 100));
    const r = findToe(g, dam, crestFt, t);
    assert.equal(r.k, -1);
    assert.equal(r.start.how, "axis");
    assert.match(traceStartNote({ ...CACHE.stats, traceStart: r.start }) ?? "", /Check the dam point/);
  });
  it("no crest: the top of dam is the ground at the dam point", () => {
    const { g, t, xy } = mk((x, y) => (y === 0 && x > 0 ? 94 : 100));
    const r = findToe(g, dam, Number.NaN, t);
    assert.deepEqual(xy(r.k), [10, 0]);
    assert.equal(r.start.belowTopFt, Math.round((6 / 0.3048) * 1000) / 1000);
  });
});

describe("PLSS and 911 lists against the outline", async () => {
  const fake = async () => ({ ok: true, status: 200, json: async () => PAGE911 });
  const c911 = await fetchNd911(STRIP, fake, NOW);
  it("the saved 911 points inside the outline (4 at 30 m), with depth bands from the outline", () => {
    const t = nd911Table(ND, OUTLINE, c911, "terrain outline");
    assert.equal(t.against, "terrain outline");
    assert.equal(t.total, 45);
    assert.deepEqual(
      t.rows.map((r) => r.address.split(",")[0]),
      ["14751 30TH ST SE", "3118 150TH AVE SE", "3298 151ST AVE SE", "15134 33RD ST SE"],
    );
  });
  it("PLSS sections carry the outline's acres; the lookup box covers the strip and the outline together", () => {
    const t = plssTable(ND, OUTLINE, PLSS);
    assert.equal(t.stripAc, CACHE.stats.outlineAc);
    assert.ok(t.rows.length >= 10);
    const both = stripBbox(lookupArea(STRIP, OUTLINE));
    assert.ok(bboxContains(both, stripBbox(STRIP)) && bboxContains(both, stripBbox(OUTLINE)));
    // The strip-only lookup does not reach the outline's south-east corner here, so the desk asks again with both.
    const strip911 = nd911State({ ...ND, nd911: c911 }, OUTLINE, false, "terrain outline");
    const plssOnly = plssState({ ...ND, plss: PLSS }, OUTLINE, false);
    assert.equal(bboxContains(PLSS.bbox, stripBbox(OUTLINE)), plssOnly.state === "ready");
    assert.equal(bboxContains(c911.bbox, stripBbox(OUTLINE)), strip911.state === "ready");
  });
  it("exports: the outline GeoJSON / KML with the lists and the label", () => {
    const t911 = nd911Table(ND, OUTLINE, c911, "terrain outline");
    const plss = plssTable(ND, OUTLINE, PLSS);
    const gj = JSON.parse(exportFloodGeoJson(OUTLINE, plss, t911));
    assert.equal(gj.properties.kind, "terrain_outline");
    assert.equal(gj.properties.disclaimer, OUTLINE_LABEL);
    assert.equal(gj.features.filter((f: { geometry: { type: string } }) => f.geometry.type === "Point").length, 4);
    const kml = exportFloodKml(OUTLINE, plss, t911);
    assert.match(kml, /ND00199 terrain outline \(approximate\)/);
    assert.ok(kml.includes(OUTLINE_LABEL));
    assert.ok(kml.includes("ND GIS Hub NG911"));
  });
});
