// scripts/build-nd-structures.mjs (the batch structure list) and scripts/local-dem.mjs (the --dem terrain) on small
// made-up rows: no network, no GDAL.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { buildStructures, COLS, DROP_STATUS, huc8Lookup } from "./build-nd-structures.mjs";
import { warpArgs } from "./local-dem.mjs";

const square = (x0, y0, x1, y1) => [
  [x0, y0],
  [x1, y0],
  [x1, y1],
  [x0, y1],
  [x0, y0],
];
const WBD = {
  type: "FeatureCollection",
  features: [
    { type: "Feature", properties: { huc8: "09020205", name: "Maple" }, geometry: { type: "Polygon", coordinates: [square(-98, 46, -97, 47), square(-97.6, 46.4, -97.4, 46.6)] } },
    { type: "Feature", properties: { huc8: "09020204", name: "Lower Sheyenne" }, geometry: { type: "MultiPolygon", coordinates: [[square(-98, 47, -97, 48)], [square(-97.6, 46.4, -97.4, 46.6)]] } },
  ],
};
const dam = (o) => ({ structure_: "1", national_i: "", status: "Intact", county: "Cass", latitude: "46.2", longitude: "-97.5", max_dam_he: "12", max_pool_v: "40", max_pool_a: "6", dam_length: "300", year_built: "1985", hazard_cla: "Low", purpose: "Livestock", dam_name: "Owner Pond", primary_ow: "J. Doe", hu_sub_bas: "Maple River", ...o });
const dug = (o) => ({ structure_: "7", national_i: "", county: "Barnes", latitude: "47.5", longitude: "-97.5", max_height: "", max_storag: "3", max_surfac: "1", total_leng: "", fed_hazard: "Undetermined", purpose: "Livestock", site_owner: "J. Doe", ...o });

describe("huc8Lookup", () => {
  const at = huc8Lookup(WBD);
  it("names the HUC8 a point falls in, as Name (code), holes respected", () => {
    assert.equal(at(-97.8, 46.2), "Maple (09020205)");
    assert.equal(at(-97.5, 47.5), "Lower Sheyenne (09020204)");
    // In Maple's hole, which is a part of Lower Sheyenne.
    assert.equal(at(-97.5, 46.5), "Lower Sheyenne (09020204)");
    assert.equal(at(-90, 46.5), null);
  });
});

describe("buildStructures", () => {
  const nid = [{ nid: "ND00199", lat: 46.98, lon: -97.39 }];
  const rows = {
    dams: [
      dam({}),
      dam({ structure_: "2", national_i: "ND00199" }), // the NID main dam: listed once, as the NID entry
      dam({ structure_: "3", status: "Removed" }),
      dam({ structure_: "4", status: "Not Built" }),
      dam({ structure_: "5", latitude: "", longitude: "" }),
      dam({ structure_: "6", status: "Breached", max_dam_he: "", max_pool_v: "0" }),
    ],
    dugouts: [dug({})],
    unknown: [
      // Same point, height and storage as DWR-1: DWR moved it into the Dams layer under a new number.
      { structure_id: "9", status: "", county: "Cass", latitude: "46.2", longitude: "-97.5", max_dam_height: "12", max_pool_vol: "40", dam_name: "Owner Pond" },
      // On the NID main dam (within 5 m).
      { structure_id: "10", status: "", latitude: "46.98002", longitude: "-97.39", max_dam_height: "20", max_pool_vol: "100" },
      // Nowhere else: listed as an unknown structure.
      { structure_id: "11", status: "", county: "Cass", latitude: "46.3", longitude: "-97.5", max_dam_height: "8", max_pool_vol: "", year_built: "1960", purpose: "Livestock", owner_name: "J. Doe" },
      { structure_id: "12", status: "Removed", latitude: "46.4", longitude: "-97.5" },
    ],
  };
  const b = buildStructures(rows, nid, { huc8: huc8Lookup(WBD), pulledUtc: "2026-11-05T15:05:00Z" });

  it("keeps every other DWR dam and every dugout, drops NID main dams, removed / not built and unlocated records", async () => {
    const out = await b;
    const id = (r) => r[COLS.indexOf("id")];
    assert.deepEqual(out.rows.map(id), ["DUG-7", "DWR-1", "DWR-6", "UNK-11"]);
    assert.deepEqual(out.source.counts, { dams: 2, dugouts: 1, unknown: 1, unknownAliased: 2, droppedStatus: 3, noLocation: 1, nidMain: 1, noHuc8: 0 });
    assert.ok(DROP_STATUS.has("Removed") && DROP_STATUS.has("Not Built"));
  });

  it("carries numbers, county, status and the WBD watershed (never DWR's hu_sub_bas), and no owner or name fields", async () => {
    const out = await b;
    const r = Object.fromEntries(COLS.map((c, i) => [c, out.rows[1][i]]));
    assert.deepEqual(r, {
      id: "DWR-1",
      kind: "dam",
      nid: null,
      status: "Intact",
      county: "Cass",
      lat: 46.2,
      lon: -97.5,
      heightFt: 12,
      storageAcFt: 40,
      areaAc: 6,
      lengthFt: 300,
      year: 1985,
      hazard: "Low",
      purpose: "Livestock",
      huc8: "Maple (09020205)",
      alias: "UNK-9",
    });
    const text = JSON.stringify(out);
    assert.ok(!text.includes("J. Doe") && !text.includes("Owner Pond") && !text.includes("Maple River"));
    // Blank or zero height / storage stay null (the batch lists the structure as "needs data").
    const six = Object.fromEntries(COLS.map((c, i) => [c, out.rows[2][i]]));
    assert.equal(six.heightFt, null);
    assert.equal(six.storageAcFt, null);
    assert.deepEqual(out.nidHuc.rows, [["ND00199", "Maple (09020205)", "UNK-10"]]);
  });

  it("lists an Unknown Structure that is not already a DWR or NID dam, status blank = Unknown", async () => {
    const out = await b;
    const r = Object.fromEntries(COLS.map((c, i) => [c, out.rows[3][i]]));
    assert.equal(r.id, "UNK-11");
    assert.equal(r.kind, "unknown");
    assert.equal(r.status, "Unknown");
    assert.equal(r.heightFt, 8);
    assert.equal(r.storageAcFt, null);
    assert.equal(r.year, 1960);
    assert.equal(r.alias, null);
    assert.match(out.source.attribution, /Unknown Structures/);
  });
});

describe("local DEM (gdalwarp arguments)", () => {
  const req = { bbox: [-97.5, 46.9, -97.4, 47], width: 100, height: 120, cellM: 10 };
  it("cuts the request's lon/lat box at its size, metres float32 with -9999 NODATA, raw ENVI", () => {
    const a = warpArgs("/data/ND_1m.vrt", req, "/tmp/x.bin");
    const at = (k) => a.slice(a.indexOf(k) + 1, a.indexOf(k) + 1 + (k === "-te" ? 4 : k === "-ts" ? 2 : 1));
    assert.deepEqual(at("-te"), ["-97.5", "46.9", "-97.4", "47"]);
    assert.deepEqual(at("-ts"), ["100", "120"]);
    assert.deepEqual(at("-t_srs"), ["EPSG:4326"]);
    assert.deepEqual(at("-dstnodata"), ["-9999"]);
    assert.deepEqual(at("-of"), ["ENVI"]);
    assert.deepEqual(a.slice(-2), ["/data/ND_1m.vrt", "/tmp/x.bin"]);
  });
  it("averages a 1 m source down to a coarser grid and interpolates otherwise", () => {
    const r = (cellM) => {
      const a = warpArgs("s.vrt", { ...req, cellM }, "o.bin", { sourceCellM: 1 });
      return a[a.indexOf("-r") + 1];
    };
    assert.equal(r(10), "average");
    assert.equal(r(1), "bilinear");
  });
});
