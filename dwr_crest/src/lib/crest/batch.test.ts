// Batch screening (batch.ts): the structure list, the selection, the auto-derived record and one structure through the
// desk's own path on a synthetic valley (no network).
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  AIM_CHORD_M,
  AIM_MIN_TRACE_M,
  BATCH_CSV_HEAD,
  aimAlongChannel,
  channelAim,
  batchCsv,
  deriveFromDem,
  deriveRecord,
  estimateSeconds,
  facets,
  loadStructures,
  makeStructures,
  missingData,
  nearRequest,
  runBatch,
  runStructure,
  selectStructures,
  sortRows,
  type BatchRow,
  type Structure,
  type StructuresBundle,
} from "./batch.ts";
import { ENGINE_VERSION } from "./engine.ts";
import type { FloodStrip } from "./floodstrip.ts";
import { nd911Bbox } from "./nd911.ts";
import type { NdData } from "./nd-dams.ts";
import type { DemGrid, DemRequest } from "./outline.ts";
import { CREST_VERSION } from "./version.ts";

const M_PER_DEG_LAT = 110574;
const kx = (lat: number) => 111320 * Math.cos((lat * Math.PI) / 180);
const LON = -100;
const LAT = 47;

/**
 * A valley running south (downstream = -north), 1 % slope, banks rising 3 % to each side, and an embankment 8 m high
 * and 20 m thick across it at the dam point. Ground at the dam point (no embankment) is 500 m.
 */
function valleyZ(lon: number, lat: number) {
  const e = (lon - LON) * kx(LAT);
  const n = (lat - LAT) * M_PER_DEG_LAT;
  let z = 500 + 0.01 * n + 0.03 * Math.abs(e);
  if (Math.abs(n) < 10 && Math.abs(e) < 150) z += 8;
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
const noNd911 = (async (area: FloodStrip) => ({ bbox: nd911Bbox(area), fetchedAt: "2026-11-05T00:00:00Z", source: "test", attribution: "test", fields: [], points: [] })) as never;

const S = (over: Partial<Structure> = {}): Structure => ({
  id: "DWR-1",
  kind: "dam",
  label: "DWR-1",
  nid: null,
  status: "Intact",
  county: "Cass",
  lat: LAT,
  lon: LON,
  heightFt: 20,
  storageAcFt: 120,
  areaAc: 15,
  lengthFt: 400,
  year: 1980,
  hazard: "Low",
  purpose: "Livestock",
  huc8: "Maple (09020205)",
  ...over,
});

describe("structure list", () => {
  const nd = {
    dams: [
      { nid: "ND00001", name: "Test Dam", otherNames: null, county: "Cass", lat: 47, lon: -97, nidHeightFt: 20, damHeightFt: 20, nidStorageAcFt: 300, maxStorageAcFt: 300, normalStorageAcFt: 100, lengthFt: 500, yearCompleted: 1970, nidHazard: "Low", purpose: "Flood Control" },
      { nid: "DWR-99", name: "DWR-99 (DWR only)", source: "DWR", county: "Cass", lat: 47, lon: -97 },
    ],
    dwr: new Map(),
  } as unknown as Pick<NdData, "dams" | "dwr">;
  const bundle: StructuresBundle = {
    source: {},
    nidHuc: { cols: ["nid", "huc8"], rows: [["ND00001", "Maple (09020205)"]] },
    cols: ["id", "kind", "nid", "status", "county", "lat", "lon", "heightFt", "storageAcFt", "areaAc", "lengthFt", "year", "hazard", "purpose", "huc8"],
    rows: [
      ["DWR-99", "dam", "ND10099", "Intact", "Cass", 47.1, -97.1, 10, 40, 5, 300, 1990, "Low", "Livestock", "Maple (09020205)"],
      ["DUG-5", "dugout", null, "Dugout", "Barnes", 46.9, -98, null, 12, 2, null, null, "Undetermined", "Livestock", "Lower Sheyenne (09020204)"],
    ],
  };
  const all = makeStructures(nd, bundle);

  it("takes NID main dams from the NID bundle and every DWR dam and dugout from the structures bundle, once each", () => {
    assert.deepEqual(
      all.map((s) => [s.id, s.kind]),
      [
        ["ND00001", "nid"],
        ["DWR-99", "dam"],
        ["DUG-5", "dugout"],
      ],
    );
    const n = all[0];
    assert.equal(n.label, "Test Dam");
    assert.equal(n.heightFt, 20);
    assert.equal(n.storageAcFt, 300);
    assert.equal(n.huc8, "Maple (09020205)");
    assert.equal(all[1].label, "DWR-99 (ND10099)");
  });

  it("selects by pick, county, watershed name or HUC code, and statewide, filtered by kind and status", () => {
    const base = { kinds: ["nid", "dam", "dugout"] as const, statuses: ["NID", "Intact", "Dugout"] };
    const ids = (sel: Parameters<typeof selectStructures>[1]) => selectStructures(all, sel).map((s) => s.id);
    assert.deepEqual(ids({ ...base, kinds: [...base.kinds], by: "county", counties: ["Cass"] }), ["ND00001", "DWR-99"]);
    assert.deepEqual(ids({ ...base, kinds: [...base.kinds], by: "watershed", watersheds: ["Lower Sheyenne (09020204)"] }), ["DUG-5"]);
    assert.deepEqual(ids({ ...base, kinds: [...base.kinds], by: "watershed", watersheds: ["09020205"] }), ["ND00001", "DWR-99"]);
    assert.deepEqual(ids({ ...base, kinds: ["dugout"], by: "state" }), ["DUG-5"]);
    assert.deepEqual(ids({ ...base, kinds: [...base.kinds], statuses: ["NID"], by: "state" }), ["ND00001"]);
    // A structure ticked by name stays in whatever the filters say.
    assert.deepEqual(ids({ ...base, kinds: ["nid"], statuses: [], by: "pick", ids: ["DUG-5"] }), ["DUG-5"]);
  });

  it("counts facets and flags structures that cannot run", () => {
    assert.deepEqual(facets(all, "county"), [
      { value: "Barnes", count: 1 },
      { value: "Cass", count: 2 },
    ]);
    assert.equal(missingData(all[2]), "No height in the inventory.");
    assert.equal(missingData(S({ heightFt: null, storageAcFt: null, lat: null })), "No location, height or storage in the inventory.");
    assert.equal(missingData(all[0]), null);
    assert.equal(estimateSeconds([all[0], all[2]]), 8);
  });
});

describe("auto-derived record", () => {
  it("snaps the crest to the embankment top and points downstream along the valley", async () => {
    const g = await fakeDem(nearRequest(LON, LAT, 0.6, 5));
    const d = deriveFromDem(g, LON, LAT)!;
    // The embankment top at the point is 508 m; the snap takes the highest cell within 30 m, which on these 1 % / 3 %
    // slopes is up to ~1.2 m higher. Without the embankment the point reads 500 m: the snap must find the fill.
    const topFt = (500 + 8) / 0.3048;
    assert.ok(d.crestFt >= topFt - 1 && d.crestFt < topFt + 5, `crest ${d.crestFt} vs ${topFt}`);
    assert.ok(d.u[1] < -0.9, `downstream should point south, got ${d.u}`);
  });

  it("builds a desk record: H and V from the inventory, streambed = crest - H, synthesized line and reservoir upstream", async () => {
    const g = await fakeDem(nearRequest(LON, LAT));
    const r = deriveRecord(S(), g);
    assert.ok(r.ok);
    if (!r.ok) return;
    const { dam } = r;
    assert.equal(dam.heightFt, 20);
    assert.equal(dam.storageAcFt, 120);
    assert.equal(dam.surfaceAreaAc, 15);
    assert.equal(dam.statedDatum, "NAVD88");
    assert.ok(Math.abs(dam.crestFt - dam.streambedFt - 20) < 0.02);
    assert.equal(dam.receptorSource, "nd911");
    assert.equal(r.derived.crestFrom, "3DEP");
    assert.equal(dam.provenance?.crestFt?.src, "DEM");
    const res = dam.geometry!.reservoirPoint!.geometry.coordinates as number[];
    assert.ok(res[1] > LAT, "reservoir point is upstream (north)");
    assert.match(dam.sampleNote ?? "", /auto-derived/);
    const local = deriveRecord(S(), g, undefined, "local DEM");
    assert.equal(local.ok && local.derived.crestFrom, "local DEM");
  });

  it("refuses a structure without height or storage", async () => {
    const g = await fakeDem(nearRequest(LON, LAT));
    const r = deriveRecord(S({ storageAcFt: null }), g);
    assert.equal(r.ok, false);
  });
});

describe("one structure through the desk path", () => {
  it("analyzes, builds a terrain layer, looks up 911 points and triages", async () => {
    const { row, dam } = await runStructure(S(), { fetchDem: fakeDem, fetchNd911: noNd911 });
    assert.equal(row.status, "done", row.reason ?? "");
    assert.ok(dam);
    assert.equal(row.crestFrom, "3DEP");
    assert.ok((row.releasedAcFt ?? 0) > 0);
    assert.ok((row.peakCfs ?? 0) > 0);
    assert.ok(row.layer === "outline" || row.layer === "routed");
    assert.equal(row.structuresInside, 0);
    assert.ok(row.triage);
    assert.equal(row.screen, "Low");
  });

  it("never calls a dam trivial when the 911 lookup failed: structures unknown (null), at least uncertain, and the note says so", async () => {
    const fail = (async () => {
      throw new Error("the ND GIS Hub service answered HTTP 502");
    }) as never;
    const { row } = await runStructure(S(), { fetchDem: fakeDem, fetchNd911: fail });
    assert.equal(row.status, "done");
    assert.ok(row.triage);
    assert.notEqual(row.triage!.recommendation, "trivial");
    assert.equal(row.structuresInside, null, "a failed lookup is unknown, never 0");
    assert.equal(row.structuresFlooded, null);
    assert.match(row.reason ?? "", /^911 lookup failed \(the ND GIS Hub service answered HTTP 502\); structures unknown\./);
    // The same structure with the lookup working is trivial on this synthetic valley, so the downgrade is what held it.
    const ok = await runStructure(S(), { fetchDem: fakeDem, fetchNd911: noNd911 });
    assert.equal(ok.row.structuresInside, 0);
    if (ok.row.triage?.recommendation === "trivial") {
      assert.equal(row.triage!.recommendation, "uncertain");
      assert.match(row.triage!.summary, /911 lookup failed/);
    }
  });

  it("reports needs-data without fetching anything, and a terrain failure as failed", async () => {
    let calls = 0;
    const counted = async (r: DemRequest) => {
      calls++;
      return fakeDem(r);
    };
    const nd = await runStructure(S({ heightFt: null }), { fetchDem: counted });
    assert.equal(nd.row.status, "needs-data");
    assert.equal(calls, 0);
    const bad = await runStructure(S(), {
      fetchDem: async () => {
        throw new Error("3DEP down");
      },
    });
    assert.equal(bad.row.status, "failed");
    assert.match(bad.row.reason ?? "", /3DEP down/);
  });

  it("runs a list two at a time, skips what is done, and stops on abort", async () => {
    const list = [S({ id: "A" }), S({ id: "B", heightFt: null }), S({ id: "C", storageAcFt: null })];
    const seen: string[] = [];
    const rows = await runBatch(list, { fetchDem: fakeDem, fetchNd911: noNd911, skip: new Set(["A"]), onRow: (r) => seen.push(r.id) });
    assert.deepEqual(seen.sort(), ["B", "C"]);
    assert.equal(rows.length, 2);
    const ctl = new AbortController();
    ctl.abort();
    assert.equal((await runBatch(list, { fetchDem: fakeDem, signal: ctl.signal })).length, 0);
  });
});

describe("worklist order and CSV", () => {
  const row = (id: string, over: Partial<BatchRow>): BatchRow =>
    ({
      id,
      kind: "dam",
      label: id,
      county: "Cass",
      huc8: null,
      status: "done",
      reason: null,
      heightFt: 10,
      storageAcFt: 50,
      crestFt: 1000,
      crestFrom: "3DEP",
      bearingDeg: 180,
      releasedAcFt: 50,
      peakCfs: 1000,
      layer: "outline",
      floodedAc: 10,
      screen: "Low",
      screenLabel: "Low",
      structuresInside: 0,
      structuresFlooded: 0,
      triage: { recommendation: "trivial", label: "Clearly trivial: screen only", confidence: "high", edgeShiftM: 20, summary: "s" },
      seconds: 1,
      ...over,
    }) as BatchRow;
  const rows = [
    row("T", {}),
    row("N", { status: "needs-data", triage: null, screen: null, reason: "No height in the inventory." }),
    row("R", { triage: { recommendation: "needs-runner", label: "Needs a Runner run", confidence: "low", edgeShiftM: 120, summary: "s" }, releasedAcFt: 900 }),
    row("U", { triage: { recommendation: "uncertain", label: "Uncertain: run it", confidence: "medium", edgeShiftM: 90, summary: "s" } }),
  ];

  it("puts Runner jobs first, then uncertain, trivial, and not-run rows last", () => {
    assert.deepEqual(
      sortRows(rows).map((r) => r.id),
      ["R", "U", "T", "N"],
    );
  });

  it("writes the disclaimer, the versions, the inventory ages, the header and one line per row", () => {
    const now = new Date("2027-06-01T00:00:00Z");
    const csv = batchCsv(rows, { nidFetchedUtc: "2026-10-29T21:56:00Z", dwrPulledUtc: "2026-10-30T14:31:02Z", structuresPulledUtc: null, now }).trim().split("\n");
    assert.match(csv[0], /^# Screening estimate/);
    assert.equal(csv[1], `# Crest ${CREST_VERSION}; ${ENGINE_VERSION}. Written 2027-06-01.`);
    assert.match(csv[2], /^# NID inventory pulled 2026-10-29 \(7 months old; stale, past 180 days: `npm run data:nd-dams` refreshes it\)$/);
    assert.match(csv[3], /^# DWR dam values pulled 2026-10-30 \(6 months old; stale/);
    assert.equal(csv[4], BATCH_CSV_HEAD.join(","));
    assert.equal(csv.length, 5 + rows.length);
    assert.match(csv[5], /^R,DWR dam,R,Cass,/);
    assert.match(csv[8], /needs-data/);
    // Without the bundle sources: the disclaimer and the version row only.
    const bare = batchCsv(rows, { now }).trim().split("\n");
    assert.equal(bare[2], BATCH_CSV_HEAD.join(","));
    // A fresh inventory is not called stale.
    const fresh = batchCsv(rows, { nidFetchedUtc: "2026-10-29T21:56:00Z", now: new Date("2026-11-07T00:00:00Z") }).trim().split("\n");
    assert.equal(fresh[2], "# NID inventory pulled 2026-10-29 (8 days old)");
  });
});

describe("committed bundle", () => {
  it("loads, and lists NID dams, DWR dams and dugouts with HUC8 watersheds", async () => {
    const { structures } = await loadStructures();
    const kinds = new Set(structures.map((s) => s.kind));
    assert.deepEqual([...kinds].sort(), ["dam", "dugout", "nid", "unknown"]);
    assert.ok(structures.length > 10000);
    assert.equal(new Set(structures.map((s) => s.id)).size, structures.length, "ids are unique");
    const withHuc = structures.filter((s) => s.huc8 && /\(\d{8}\)$/.test(s.huc8)).length;
    assert.ok(withHuc / structures.length > 0.999);
    // ND00199 (Absaraka) is the NID entry, not a duplicate DWR one.
    assert.equal(structures.filter((s) => s.nid === "ND00199").length, 1);
    // UNK-2400 (Nordell) is DWR-16431 now: found by its old number, not listed twice.
    const nordell = selectStructures(structures, { by: "pick", ids: ["UNK-2400"], kinds: [], statuses: [] });
    assert.deepEqual(nordell.map((s) => s.id), ["DWR-16431"]);
    assert.match(nordell[0].label, /UNK-2400/);
    assert.ok(!structures.some((s) => s.id === "UNK-2400"));
  });
});

// ---- the two-pass aim (Nick 2026-11-05): the strip down the traced channel, not the first few hundred metres of it.

describe("channelAim", () => {
  // A channel leaving the dam north for 1.5 km, then east (DWR-2662 / ND00049: Buffalo Creek leaves north and bends east).
  const bend = (): { e: number; n: number; x: number }[] => {
    const pts: { e: number; n: number; x: number }[] = [];
    for (let x = 0; x <= 1500; x += 10) pts.push({ e: 0, n: x, x });
    for (let x = 1510; x <= 8000; x += 10) pts.push({ e: x - 1500, n: 1500, x });
    return pts;
  };
  it("aims at the chord to AIM_CHORD_M down the channel", () => {
    const a = channelAim(bend(), [0, 1])!;
    assert.ok(a, "re-aimed");
    // At 4 km down the channel the trace is at (2500 E, 1500 N): bearing atan2(2500, 1500) = 59.0°.
    assert.ok(Math.abs(a.bearingDeg - 59) < 0.5, `bearing ${a.bearingDeg}`);
    assert.equal(a.chordM, AIM_CHORD_M);
    assert.equal(a.fromDeg, 0);
  });
  it("keeps the downhill on a short trace or a straight valley", () => {
    const short = bend().filter((p) => p.x < AIM_MIN_TRACE_M - 10);
    assert.equal(channelAim(short, [0, 1]), null);
    const straight = Array.from({ length: 500 }, (_, i) => ({ e: i * 0.5, n: i * 10, x: i * 10 }));
    assert.equal(channelAim(straight, [0, 1]), null, "a turn under AIM_MIN_TURN_DEG keeps the downhill");
  });
  it("uses the trace's end when it is shorter than the chord", () => {
    const pts = bend().filter((p) => p.x <= 2500);
    const a = channelAim(pts, [0, 1])!;
    // End (1000 E, 1500 N): bearing 33.7°.
    assert.ok(Math.abs(a.bearingDeg - 33.7) < 0.5, `bearing ${a.bearingDeg}`);
    assert.equal(a.chordM, 2500);
  });
});

describe("two-pass aim on a bending valley (DWR-2662-like)", () => {
  // Toe at the point; the valley runs north 1.5 km then east, 0.2 % down the channel, banks 3 %; a pool and embankment
  // south of the point (the reservoir side).
  const along = (e: number, n: number) => {
    // Distance down the bent channel of the nearest channel point, and the distance from the channel.
    const legA = { s: Math.max(0, Math.min(1500, n)), d: Math.hypot(e, n - Math.max(0, Math.min(1500, n))) };
    const eb = Math.max(0, e);
    const legB = { s: 1500 + eb, d: Math.hypot(e - eb, n - 1500) };
    return legA.d <= legB.d ? legA : legB;
  };
  const z = (lon: number, lat: number) => {
    const e = (lon - LON) * kx(LAT);
    const n = (lat - LAT) * M_PER_DEG_LAT;
    if (n < -40) return 505 + 0.0005 * -n; // reservoir side: pool floor rising upstream
    if (n < 0) return 512; // embankment
    const a = along(e, n);
    return 500 - 0.002 * a.s + 0.03 * a.d;
  };
  const bendDem = async (req: DemRequest): Promise<DemGrid> => {
    const out = new Float32Array(req.width * req.height);
    for (let j = 0; j < req.height; j++)
      for (let i = 0; i < req.width; i++) {
        const lon = req.bbox[0] + ((i + 0.5) / req.width) * (req.bbox[2] - req.bbox[0]);
        const lat = req.bbox[3] - ((j + 0.5) / req.height) * (req.bbox[3] - req.bbox[1]);
        out[j * req.width + i] = z(lon, lat);
      }
    return { ...req, z: out };
  };
  it("re-lays the synthesized line down the bent channel, not the downhill", async () => {
    const s = S({ heightFt: 30, storageAcFt: 3900, areaAc: 330, lengthFt: 1275 });
    const rec = deriveRecord(s, await bendDem(nearRequest(LON, LAT)));
    assert.ok(rec.ok);
    if (!rec.ok) return;
    // The downhill at the toe point points north (the channel's first leg).
    assert.ok(rec.derived.bearingDeg < 20 || rec.derived.bearingDeg > 340, `downhill ${rec.derived.bearingDeg}`);
    const aimed = await aimAlongChannel(rec.dam, rec.derived.u, bendDem);
    assert.ok(aimed.aim, "re-aimed");
    assert.ok(aimed.aim!.bearingDeg > 25 && aimed.aim!.bearingDeg < 80, `aimed ${aimed.aim!.bearingDeg}`);
    assert.match(aimed.dam.sampleNote ?? "", /Re-aimed along the traced channel/);
    const { row } = await runStructure(s, { fetchDem: bendDem, fetchNd911: noNd911 });
    assert.equal(row.status, "done");
    assert.ok(row.bearingDeg! > 25 && row.bearingDeg! < 80, `row bearing ${row.bearingDeg}`);
  });
});
