import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { describe, it } from "node:test";

import { fromLocal, toLocal, type XY } from "./aoi.ts";
import { analyze, damLocation } from "./engine.ts";
import { floodFootprint, floodStrip, type FloodStrip } from "./floodstrip.ts";
import {
  exportFloodStripGeoJsonWithPlss,
  exportFloodStripKmlWithPlss,
  exportPlssCsv,
  fetchPlss,
  padBbox,
  parsePlssid,
  PLSS_ATTRIBUTION,
  PLSS_OFFLINE,
  PLSS_SECTION_LAYER,
  plssState,
  plssTable,
  readSections,
  sectionLabel,
  SLIVER_AC,
  stripBbox,
  triangulate,
  type PlssCache,
  type PlssSection,
} from "./plss.ts";
import { SAMPLE_DAMS } from "./samples.ts";
import type { Analysis } from "./types.ts";

const ND = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;
const ACRE = 4046.8564224;
/** Real BLM CadNSDI sections around ND00199 (Absaraka, Cass Co.), saved from one lookup on 2026-10-30. */
const FIXTURE = JSON.parse(
  readFileSync(new URL("./__fixtures__/plss_nd00199.json", import.meta.url), "utf8"),
) as PlssCache;
const STRIP = floodStrip(ND, analyze(ND)).strip!;
const O = { lon: STRIP.properties.breach_lon, lat: STRIP.properties.breach_lat };

const area = (r: XY[]) => {
  let s = 0;
  for (let i = 0; i < r.length; i++) s += r[i][0] * r[(i + 1) % r.length][1] - r[(i + 1) % r.length][0] * r[i][1];
  return s / 2;
};

/** A synthetic section from local-metre corners (around the breach), closed lon/lat ring. */
function section(id: string, corners: XY[], no = "01"): PlssSection {
  const ring = corners.map((p) => fromLocal(p, O));
  return { id, plssid: "ND051400N0530W0", typ: "SN", txt: "Section", no, dup: "0", coordinates: [[[...ring, ring[0]]]] };
}

describe("PLSS labels from the CadNSDI identifiers", () => {
  it("PLSSID: meridian, township and range", () => {
    assert.deepEqual(parsePlssid("ND051400N0520W0"), {
      meridianCode: "05",
      meridian: "5th PM",
      township: "140N",
      range: "52W",
    });
    assert.equal(parsePlssid("ND051392N0520W0")?.township, "139½N");
    assert.equal(parsePlssid("bad"), null);
  });
  it('"T139N R52W Sec 12 (5th PM)"', () => {
    assert.equal(
      sectionLabel({ plssid: "ND051390N0520W0", typ: "SN", txt: "Section", no: "12", dup: "0" }).label,
      "T139N R52W Sec 12 (5th PM)",
    );
    assert.equal(
      sectionLabel({ plssid: "ND051400N0530W0", typ: "SN", txt: "Section", no: "04", dup: "1" }).label,
      "T140N R53W Sec 4 (dup 1) (5th PM)",
    );
    assert.equal(
      sectionLabel({ plssid: "ND051400N0530W0", typ: "TR", txt: "Tract", no: "37", dup: "0" }).label,
      "T140N R53W Tract 37 (5th PM)",
    );
  });
});

describe("sections the ND00199 strip touches (real BLM polygons)", () => {
  const t = plssTable(ND, STRIP, FIXTURE);
  it("lists 15 sections, downstream order, labelled, with the first few as expected", () => {
    assert.equal(t.rows.length, 15);
    assert.deepEqual(
      t.rows.slice(0, 4).map((r) => r.label),
      [
        "T141N R53W Sec 33 (5th PM)",
        "T141N R53W Sec 34 (5th PM)",
        "T140N R53W Sec 4 (5th PM)",
        "T140N R53W Sec 3 (5th PM)",
      ],
    );
    for (let i = 1; i < t.rows.length; i++) assert.ok(t.rows[i].fromKm >= t.rows[i - 1].fromKm);
    assert.deepEqual(t.rows.map((r) => r.order), t.rows.map((_, i) => i + 1));
  });
  it("wet acres add up to the strip's own area (every part of the strip is in a section)", () => {
    const sum = t.rows.reduce((s, r) => s + r.wetAc, 0) + t.sliverAc;
    assert.ok(Math.abs(sum - STRIP.properties.strip_area_ac) < 0.02, `${sum} vs ${STRIP.properties.strip_area_ac}`);
    assert.ok(t.uncoveredAc < 0.02);
    for (const r of t.rows) assert.ok(r.wetAc >= SLIVER_AC && r.wetAc < 700, `${r.label} ${r.wetAc}`);
  });
  it("deepest band: 10 ft+ at the breach, shallow at the far end", () => {
    assert.equal(t.rows[0].bandLabel, "10 ft and deeper");
    assert.equal(t.rows[t.rows.length - 1].bandLabel, "0.1–0.5 ft");
  });
  it("each section's wet acres match a direct clip of that section (independent grid count, 1%)", () => {
    const r = t.rows.find((x) => x.label === "T140N R53W Sec 11 (5th PM)")!;
    const sec = FIXTURE.sections.find((s) => s.id === r.id)!;
    const ring = sec.coordinates[0][0].map(([x, y]) => toLocal(x, y, O));
    const strips = STRIP.features.flatMap((f) => (f.geometry.type === "Polygon" ? [f.geometry.coordinates] : f.geometry.coordinates));
    const rings = strips.map((p) => p[0].map(([x, y]) => toLocal(x, y, O)));
    const inR = (rr: XY[], x: number, y: number) => {
      let c = false;
      for (let i = 0, j = rr.length - 1; i < rr.length; j = i++)
        if (rr[i][1] > y !== rr[j][1] > y && x < ((rr[j][0] - rr[i][0]) * (y - rr[i][1])) / (rr[j][1] - rr[i][1]) + rr[i][0]) c = !c;
      return c;
    };
    const xs = ring.map((p) => p[0]);
    const ys = ring.map((p) => p[1]);
    const step = 10;
    let n = 0;
    for (let x = Math.min(...xs); x < Math.max(...xs); x += step)
      for (let y = Math.min(...ys); y < Math.max(...ys); y += step)
        if (inR(ring, x + step / 2, y + step / 2) && rings.some((rr) => inR(rr, x + step / 2, y + step / 2))) n++;
    const grid = (n * step * step) / ACRE;
    assert.ok(Math.abs(grid - r.wetAc) / r.wetAc < 0.01, `grid ${grid} vs ${r.wetAc}`);
  });
});

describe("intersection on known shapes", () => {
  // A square strip band 0..1000 m east, -500..500 m north of the breach (same local metres).
  const band = (corners: XY[]): FloodStrip => {
    const ring = corners.map((p) => fromLocal(p, O));
    return {
      ...STRIP,
      properties: { ...STRIP.properties, strip_area_ac: Math.abs(area(corners)) / ACRE },
      features: [{ ...STRIP.features[0], geometry: { type: "Polygon", coordinates: [[...ring, ring[0]]] } }],
    };
  };
  const sq = band([[0, -500], [1000, -500], [1000, 500], [0, 500]]);
  const cache = (sections: PlssSection[]): PlssCache => ({ ...FIXTURE, bbox: padBbox(stripBbox(sq)), sections });

  it("a section over half the band gets half its acres; a non-convex (L) section is exact", () => {
    const half = section("A", [[0, -600], [500, -600], [500, 600], [0, 600]], "01");
    const L = section("B", [[500, -500], [1000, -500], [1000, 0], [750, 0], [750, 500], [500, 500]], "02");
    const t = plssTable(ND, sq, cache([half, L]));
    const a = t.rows.find((r) => r.id === "A")!.wetAc;
    const b = t.rows.find((r) => r.id === "B")!.wetAc;
    assert.ok(Math.abs(a - (500 * 1000) / ACRE) < 0.01, String(a));
    assert.ok(Math.abs(b - (500 * 1000 - 250 * 500) / ACRE) < 0.01, String(b));
  });
  it("slivers under 0.1 ac are counted, not listed; a section that only shares an edge is not touched", () => {
    const sliver = section("S", [[0, 499.5], [1000, 499.5], [1000, 900], [0, 900]], "03"); // 0.5 m x 1000 m = 0.12 ac?
    const thin = section("T", [[0, 499.8], [1000, 499.8], [1000, 900], [0, 900]], "04"); // 0.2 m x 1000 m = 0.05 ac
    const edge = section("E", [[1000, -500], [2000, -500], [2000, 500], [1000, 500]], "05");
    const t = plssTable(ND, sq, cache([thin, edge, sliver]));
    assert.deepEqual(t.rows.map((r) => r.id), ["S"]);
    assert.equal(t.sliversIgnored, 1);
    assert.ok(Math.abs(t.sliverAc - 0.049) < 0.002, String(t.sliverAc));
  });
  it("ear clipping keeps the area of a concave ring", () => {
    const L: XY[] = [[0, 0], [2, 0], [2, 1], [1, 1], [1, 2], [0, 2]];
    const tris = triangulate(L);
    assert.ok(Math.abs(tris.reduce((s, t) => s + area(t), 0) - 3) < 1e-9);
    assert.ok(tris.every((t) => area(t) > 0));
  });
});

describe("online lookup, cache and offline message (no network: fetch is faked)", () => {
  const page = (features: unknown[], more: boolean) => ({
    ok: true,
    status: 200,
    json: async () => ({ type: "FeatureCollection", features, ...(more ? { properties: { exceededTransferLimit: true } } : {}) }),
  });
  const asService = (s: PlssSection) => ({
    type: "Feature",
    properties: { PLSSID: s.plssid, FRSTDIVID: s.id, FRSTDIVTYP: s.typ, FRSTDIVTXT: s.txt, FRSTDIVNO: s.no, FRSTDIVDUP: s.dup },
    geometry: { type: "MultiPolygon", coordinates: s.coordinates },
  });

  it("uncached and offline: a clear 'needs a connection' note; online: asks for a lookup", () => {
    const dam = { ...ND, plss: undefined };
    const off = plssState(dam, STRIP, false);
    assert.equal(off.state, "offline");
    assert.equal(off.message, PLSS_OFFLINE);
    assert.match(off.message!, /need a connection/);
    assert.equal(plssState(dam, STRIP, true).state, "lookup");
    assert.equal(plssState(dam, null, true).state, "none");
  });

  it("one lookup (paged) is cached on the record and then serves offline, with no further fetch", async () => {
    const urls: string[] = [];
    const secs = FIXTURE.sections;
    const fake = async (url: string) => {
      urls.push(url);
      return urls.length === 1 ? page(secs.slice(0, 20).map(asService), true) : page(secs.slice(20).map(asService), false);
    };
    const cache = await fetchPlss(STRIP, fake, new Date("2026-10-30T14:57:43Z"));
    assert.equal(urls.length, 2);
    assert.ok(urls[0].startsWith(`${PLSS_SECTION_LAYER}/query?`));
    assert.match(urls[0], /f=geojson/);
    assert.match(urls[0], /outSR=4326/);
    assert.match(urls[1], /resultOffset=1000/);
    assert.equal(cache.sections.length, secs.length);
    assert.equal(cache.attribution, PLSS_ATTRIBUTION);
    const dam = { ...ND, plss: JSON.parse(JSON.stringify(cache)) as PlssCache };
    const st = plssState(dam, STRIP, false);
    assert.equal(st.state, "ready");
    assert.equal(st.state === "ready" && st.table.rows.length, 15);
    assert.equal(urls.length, 2);
  });

  it("a strip past the cached box asks again (offline: says it needs a connection)", () => {
    const small: PlssCache = { ...FIXTURE, bbox: [O.lon - 0.01, O.lat - 0.01, O.lon + 0.01, O.lat + 0.01] };
    const off = plssState({ ...ND, plss: small }, STRIP, false);
    assert.equal(off.state, "offline");
    assert.match(off.message!, /reaches past/);
    assert.equal(plssState({ ...ND, plss: small }, STRIP, true).state, "lookup");
  });

  it("a failed lookup says so and how to fix it", async () => {
    await assert.rejects(
      fetchPlss(STRIP, async () => {
        throw new TypeError("Failed to fetch");
      }),
      /PLSS lookup failed: Failed to fetch\. PLSS sections need a connection/,
    );
    await assert.rejects(fetchPlss(STRIP, async () => ({ ok: false, status: 503, json: async () => ({}) })), /HTTP 503/);
  });

  it("reads the service's GeoJSON fields", () => {
    const [s] = readSections([asService(FIXTURE.sections[0])]);
    assert.deepEqual(Object.keys(s).sort(), ["coordinates", "dup", "id", "no", "plssid", "txt", "typ"]);
    assert.equal(readSections([{ properties: {}, geometry: null }]).length, 0);
  });
});

// Export snapshot: the thinned ND00199 strip (as in floodstrip.test.ts) with the fixture sections.
// UPDATE_SNAPSHOTS=1 npm test rewrites them after an intended change.
describe("PLSS exports (snapshot)", () => {
  const r = analyze(ND);
  const thin: Analysis = { ...r, flood: { ...r.flood, stations: r.flood.stations.filter((_, i) => i % 20 === 0) } };
  const strip = floodFootprint(ND, thin)!;
  const t = plssTable(ND, strip, FIXTURE);
  const gj = JSON.parse(exportFloodStripGeoJsonWithPlss(strip, t));
  const files: [string, string][] = [
    ["plss_sections_nd00199_thin.csv", exportPlssCsv(t)],
    ["flood_strip_nd00199_thin_plss.kml", exportFloodStripKmlWithPlss(strip, t)],
    [
      "flood_strip_nd00199_thin_plss.properties.json",
      JSON.stringify(Object.fromEntries(Object.entries(gj.properties).filter(([k]) => k.startsWith("plss_"))), null, 2) + "\n",
    ],
  ];
  for (const [name, text] of files) {
    it(name, () => {
      const url = new URL(`./__snapshots__/${name}`, import.meta.url);
      if (process.env.UPDATE_SNAPSHOTS || !existsSync(url)) writeFileSync(url, text);
      assert.equal(text, readFileSync(url, "utf8"));
    });
  }
  it("carry the attribution and the list; the plain strip exports are unchanged without a lookup", () => {
    assert.ok(files[0][1].split("\n")[1].includes("BLM National PLSS CadNSDI"));
    assert.equal(files[0][1].trim().split("\n").length, t.rows.length + 1);
    assert.ok(files[1][1].includes("PLSS sections the strip touches"));
    assert.ok(files[1][1].includes("BLM National PLSS CadNSDI"));
    assert.equal(gj.plss_sections, undefined);
    assert.equal(gj.properties.plss_sections.length, t.rows.length);
    assert.equal(gj.properties.plss_sections[0].label, t.rows[0].label);
    const plain = readFileSync(new URL("./__snapshots__/flood_strip_nd00199_thin.geojson", import.meta.url), "utf8");
    assert.equal(exportFloodStripGeoJsonWithPlss(strip, null), plain);
  });
  it("label point and outlines are lon/lat near the dam", () => {
    const loc = damLocation(ND)!;
    for (const row of t.rows) {
      assert.ok(Math.abs(row.labelAt[0] - loc.lon) < 0.2 && Math.abs(row.labelAt[1] - loc.lat) < 0.2);
    }
  });
});
