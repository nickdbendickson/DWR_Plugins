import assert from "node:assert/strict";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { describe, it } from "node:test";

import { analyze } from "./engine.ts";
import { exportFloodStripGeoJson, floodStrip } from "./floodstrip.ts";
import {
  addNd911Seeds,
  bandAt,
  exportFloodGeoJson,
  exportFloodKml,
  exportNd911Csv,
  fetchNd911,
  ND911_ATTRIBUTION,
  ND911_DROPPED_FIELDS,
  ND911_FIELDS,
  ND911_LAYER,
  ND911_OFFLINE,
  nd911Bbox,
  nd911QueryUrl,
  nd911Seed,
  nd911State,
  nd911Table,
  readPoints,
  removeNd911Seeds,
} from "./nd911.ts";
import { SAMPLE_DAMS } from "./samples.ts";
import type { DamRecord } from "./types.ts";

const ND = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;
/**
 * The real ND GIS Hub answer for the ND00199 strip box (one page, 45 points near Absaraka, Cass Co.), saved from
 * one lookup on 2026-10-30 with the desk's own outFields.
 */
const PAGE = JSON.parse(readFileSync(new URL("./__fixtures__/nd911_nd00199.json", import.meta.url), "utf8"));
const RESULT = analyze(ND);
const STRIP = floodStrip(ND, RESULT).strip!;
const fake = (pages: unknown[]) => {
  const urls: string[] = [];
  const fn = async (url: string) => {
    urls.push(url);
    const body = pages[Math.min(urls.length - 1, pages.length - 1)];
    return { ok: true, status: 200, json: async () => body };
  };
  return { fn, urls };
};
const NOW = new Date("2026-10-30T15:14:00Z");

describe("query: real service URL, kept fields only", () => {
  it("asks layer 0 for the padded strip box, WGS 84 GeoJSON, paged by the OID", () => {
    const u = new URL(nd911QueryUrl(nd911Bbox(STRIP), 2000));
    assert.equal(`${u.origin}${u.pathname}`, `${ND911_LAYER}/query`);
    assert.equal(u.searchParams.get("f"), "geojson");
    assert.equal(u.searchParams.get("outSR"), "4326");
    assert.equal(u.searchParams.get("resultOffset"), "2000");
    assert.equal(u.searchParams.get("resultRecordCount"), "2000");
    assert.equal(u.searchParams.get("orderByFields"), "OBJECTID_1");
    assert.deepEqual(u.searchParams.get("outFields")!.split(","), [...ND911_FIELDS]);
    assert.deepEqual(nd911Bbox(STRIP), [-97.39417, 46.911221, -97.270613, 46.986078]);
  });
  it("never asks for the free-text fields (names) and lists what it drops", () => {
    for (const f of ["LandmkName", "Addtl_Loc", "NOTES"]) {
      assert.ok(!(ND911_FIELDS as readonly string[]).includes(f));
      assert.ok(ND911_DROPPED_FIELDS.some((d) => d.field.includes(f)));
    }
    // The fixture is what the service sent for those outFields: nothing else came back.
    const keys = new Set(PAGE.features.flatMap((f: { properties: object }) => Object.keys(f.properties)));
    assert.deepEqual([...keys].sort(), [...ND911_FIELDS].sort());
  });
  it("reads only the kept fields even if the service sent more", () => {
    const [p] = readPoints([
      {
        properties: { ADDRESS: "1 MAIN ST", Unit: "2", Post_Comm: "ABSARAKA", State: "ND", Post_Code: "58002", LandmkName: "Somebody's Farm", NOTES: "x" },
        geometry: { type: "Point", coordinates: [-97.3, 46.9] },
      },
    ]);
    assert.equal(p.address, "1 MAIN ST Unit 2, ABSARAKA, ND 58002");
    assert.ok(!JSON.stringify(p).includes("Somebody"));
  });
});

describe("ND00199: the 45 real points around the strip", () => {
  it("one page, all points, cached with source and fields", async () => {
    const { fn, urls } = fake([PAGE]);
    const c = await fetchNd911(STRIP, fn, NOW);
    assert.equal(urls.length, 1);
    assert.equal(c.points.length, 45);
    assert.equal(c.source, ND911_LAYER);
    assert.equal(c.attribution, ND911_ATTRIBUTION);
    assert.equal(c.fetchedAt, "2026-10-30T15:14:00.000Z");
    assert.equal(c.points[0].address, "14716 31ST ST SE, WHEATLAND, ND 58079");
    assert.equal(c.points[0].updated, "2025-05-08");
  });
  it("5 are inside the strip, in downstream order with their depth band", async () => {
    const c = await fetchNd911(STRIP, fake([PAGE]).fn, NOW);
    const t = nd911Table(ND, STRIP, c);
    assert.equal(t.total, 45);
    assert.equal(t.rows.length, 5);
    assert.equal(t.outside.length, 40);
    assert.deepEqual(
      t.rows.map((r) => [r.address.split(",")[0], r.bandLabel, r.downstreamKm]),
      [
        ["3210 150TH AVE SE", "0.5–1 ft", 6.383],
        ["15011 32ND ST SE", "0.5–1 ft", 6.448],
        ["3298 151ST AVE SE", "0.1–0.5 ft", 8.523],
        ["15134 33RD ST SE", "0.1–0.5 ft", 9.147],
        ["15149 34TH ST SE", "0.1–0.5 ft", 10.073],
      ],
    );
    for (const r of t.rows) assert.ok(r.downstreamKm >= 0 && r.downstreamKm <= RESULT.flood.lengthM / 1000);
  });
});

describe("paging and errors (fetch is faked)", () => {
  it("follows exceededTransferLimit page by page", async () => {
    const feats = PAGE.features;
    const pages = [
      { type: "FeatureCollection", features: feats.slice(0, 20), properties: { exceededTransferLimit: true } },
      { type: "FeatureCollection", features: feats.slice(20, 40), properties: { exceededTransferLimit: true } },
      { type: "FeatureCollection", features: feats.slice(40) },
    ];
    const { fn, urls } = fake(pages);
    const c = await fetchNd911(STRIP, fn, NOW);
    assert.equal(urls.length, 3);
    assert.deepEqual(urls.map((u) => new URL(u).searchParams.get("resultOffset")), ["0", "2000", "4000"]);
    assert.equal(c.points.length, 45);
  });
  it("a failed lookup says it needs a connection", async () => {
    const fn = async () => {
      throw new TypeError("Failed to fetch");
    };
    await assert.rejects(fetchNd911(STRIP, fn), (e: Error) => e.message.includes(ND911_OFFLINE));
    const err = async () => ({ ok: true, status: 200, json: async () => ({ error: { message: "Invalid query" } }) });
    await assert.rejects(fetchNd911(STRIP, err), /Invalid query/);
  });
});

describe("state: on request, then offline from the dam record", async () => {
  const cache = await fetchNd911(STRIP, fake([PAGE]).fn, NOW);
  it("nothing saved: a lookup online, the connection note offline", () => {
    assert.equal(nd911State(ND, STRIP, true).state, "lookup");
    const off = nd911State(ND, STRIP, false);
    assert.equal(off.state, "offline");
    assert.equal(off.message, ND911_OFFLINE);
    assert.equal(nd911State(ND, null, true).state, "none");
  });
  it("saved: ready even offline; a strip past the saved box asks again", () => {
    const dam: DamRecord = { ...ND, nd911: cache };
    const s = nd911State(dam, STRIP, false);
    assert.equal(s.state, "ready");
    const far: DamRecord = { ...dam, nd911: { ...cache, bbox: [-97.3, 46.95, -97.28, 46.96] } };
    assert.equal(nd911State(far, STRIP, true).state, "lookup");
    assert.equal(nd911State(far, STRIP, false).state, "offline");
  });
});

describe("add as structures for review: opt-in and reversible", async () => {
  const cache = await fetchNd911(STRIP, fake([PAGE]).fn, NOW);
  const dam: DamRecord = { ...ND, nd911: cache };
  const t = nd911Table(dam, STRIP, cache);
  it("a lookup alone leaves the screen exactly as it was", () => {
    const before = analyze(ND).screen;
    const after = analyze(dam).screen;
    assert.deepEqual(after, before);
  });
  it("adding makes unclassified home seeds at the point's distance, flagged for a call", () => {
    const receptors = addNd911Seeds(dam.receptors, t.rows, RESULT);
    assert.equal(receptors.length, ND.receptors.length + 5);
    const s = nd911Seed(t.rows[0], RESULT);
    assert.equal(s.kind, "house");
    assert.equal(s.habitable, undefined);
    assert.equal(s.source, "nd911");
    assert.equal(s.distanceKm, t.rows[0].downstreamKm);
    assert.ok(s.lateral >= -1 && s.lateral <= 1);
    assert.equal(addNd911Seeds(receptors, t.rows, RESULT).length, receptors.length, "adding twice adds nothing");
    const a = analyze({ ...dam, receptors });
    const flagged = a.screen.judgment.filter((j) => j.includes("added from a 911 address point"));
    assert.equal(flagged.length, 5);
    assert.equal(nd911Table({ ...dam, receptors }, STRIP, cache).rows.every((r) => r.added), true);
  });
  it("classifying clears the flag; not habitable does not count", () => {
    const receptors = addNd911Seeds(dam.receptors, t.rows, RESULT).map((r) => (r.source === "nd911" ? { ...r, habitable: false } : r));
    const a = analyze({ ...dam, receptors });
    assert.equal(a.screen.judgment.filter((j) => j.includes("911 address point")).length, 0);
    assert.equal(a.screen.level, analyze(ND).screen.level);
  });
  it("remove takes away only the 911 seeds (one or all)", () => {
    const receptors = addNd911Seeds(dam.receptors, t.rows, RESULT);
    const one = removeNd911Seeds(receptors, [nd911Seed(t.rows[0], RESULT).id]);
    assert.equal(one.length, receptors.length - 1);
    assert.deepEqual(removeNd911Seeds(receptors), ND.receptors);
    assert.deepEqual(analyze({ ...dam, receptors: removeNd911Seeds(receptors) }).screen, analyze(ND).screen);
  });
});

describe("911 exports (snapshot)", async () => {
  const cache = await fetchNd911(STRIP, fake([PAGE]).fn, NOW);
  const t = nd911Table(ND, STRIP, cache);
  const gj = JSON.parse(exportFloodGeoJson(STRIP, null, t));
  const csv = exportNd911Csv(t, cache);
  const kml = exportFloodKml(STRIP, null, t);
  it("nd911_points_nd00199.csv", () => {
    const url = new URL("./__snapshots__/nd911_points_nd00199.csv", import.meta.url);
    if (process.env.UPDATE_SNAPSHOTS || !existsSync(url)) writeFileSync(url, csv);
    assert.equal(csv, readFileSync(url, "utf8"));
  });
  it("CSV, GeoJSON and KML carry the points inside and the attribution", () => {
    assert.equal(csv.trim().split("\n").length, 6);
    assert.ok(csv.includes("ND GIS Hub NG911 Site/Structure Address Points"));
    const pts = gj.features.filter((f: { properties: { kind?: string } }) => f.properties.kind === "nd911_address_point");
    assert.equal(pts.length, 5);
    assert.equal(gj.properties.nd911_points_inside, 5);
    assert.ok(gj.properties.nd911_attribution.includes("ND GIS Hub NG911 Site/Structure Address Points"));
    assert.equal((kml.match(/<Point>/g) ?? []).length, 5);
    assert.ok(kml.includes("ND GIS Hub NG911 Site/Structure Address Points"));
    assert.equal(exportFloodGeoJson(STRIP, null, null), exportFloodStripGeoJson(STRIP));
  });
  it("band lookup respects the strip and nothing outside it", () => {
    assert.equal(bandAt(STRIP, -96, 46), null);
    assert.ok(bandAt(STRIP, t.rows[0].lon, t.rows[0].lat));
  });
});
