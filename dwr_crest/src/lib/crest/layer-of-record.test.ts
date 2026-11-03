import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { dashboardFlags, dashMethod } from "./dashboard.ts";
import { analyze } from "./engine.ts";
import { GATE_VOLUME_ACFT } from "./inundation.ts";
import { adoptRunAsRecord, isLargeFlood, layerOfRecord, runnerWorklist, worklistCsv } from "./layer-of-record.ts";
import type { OutlineState, RoutedSummary } from "./outline.ts";
import { SAMPLE_DAMS } from "./samples.ts";
import type { DamRecord, ResultsOfRecord, ResultSource } from "./types.ts";

const big = () => structuredClone(SAMPLE_DAMS.find((d) => d.nid === "ND00199")!); // Swan Buffalo 12, ~1,600 ac-ft released
const small = () => structuredClone(SAMPLE_DAMS.find((d) => d.id === "prairie")!); // ~45 ac-ft released

const ref = (source: ResultSource = "lisflood", edge = 0): ResultsOfRecord => ({
  source,
  label: source === "lisflood" ? "LISFLOOD-FP (DWR-2801, ND Dam Breach Runner 3.26.2)" : "DSS-WISE (test)",
  files: ["res.max"],
  importedAt: "2026-11-03T00:00:00.000Z",
  crs: "EPSG:26914",
  stats: { peakDepthFt: 9, peakDepthAllFt: 12, wetAc: 6144, wetAcAll: 6200, farthestKm: 20, maxVelocityFps: 4, arrivalMaxHr: 10, nearestWetM: 5, edgeWetCells: edge, coverKm: 30, cellM: 20, cells: 1, breachLon: 0, breachLat: 0 },
  samples: [],
  warnings: [],
});

const routedSummary: RoutedSummary = {
  floodedAc: 9415,
  peakInCfs: 20000,
  peakOutCfs: 15000,
  routedPeakFellBy: 0.25,
  channelKm: 20,
  sections: 300,
  cappedSections: 0,
  volumeFlag: true,
  reachedEdge: true,
  notes: [],
};
const ready = (layer: "outline" | "routed"): OutlineState =>
  ({ state: "ready", outline: { type: "FeatureCollection", features: [] }, stats: { hitsCorridor: false }, builtAt: "", message: null, layer, routed: layer === "routed" ? routedSummary : undefined }) as unknown as OutlineState;
const none: OutlineState = { state: "build", message: "not built" };

describe("layer of record", () => {
  it("the sample dams straddle the flood-size gate", () => {
    assert.ok(analyze(big()).breach.volumeAcFt >= GATE_VOLUME_ACFT);
    assert.ok(isLargeFlood(analyze(big())));
    assert.ok(!isLargeFlood(analyze(small())));
  });

  it("an imported Runner run is the layer of record whatever the gate picks", () => {
    for (const d of [big(), small()])
      for (const o of [ready("routed"), ready("outline"), none]) {
        d.resultsOfRecord = ref();
        const r = layerOfRecord(d, analyze(d), o);
        assert.equal(r.kind, "runner");
        assert.equal(r.needsRunner, false);
        assert.match(r.note, /^Layer of record: LISFLOOD-FP/);
        assert.match(r.note, /comparison only/);
      }
    const d = big();
    d.resultsOfRecord = ref("dsswise");
    assert.equal(layerOfRecord(d, analyze(d), ready("routed")).kind, "model");
  });

  it("without a run, a large dam is flagged as needing the Runner; a small dam is not", () => {
    const b = layerOfRecord(big(), analyze(big()), ready("routed"));
    assert.equal(b.kind, "routed");
    assert.ok(b.needsRunner);
    assert.match(b.note, /Run this dam in the ND Breach Runner/);
    const s = layerOfRecord(small(), analyze(small()), ready("outline"));
    assert.equal(s.kind, "outline");
    assert.ok(!s.needsRunner);
    assert.match(s.note, /layer of record until a Runner run is imported/);
    assert.equal(layerOfRecord(small(), analyze(small()), none).kind, "strip");
  });

  it("adopts only a clean Runner run, never over an existing record or a deliberate desk choice", () => {
    const d = big();
    assert.ok(adoptRunAsRecord(d, { source: "lisflood", blocked: false }));
    assert.ok(!adoptRunAsRecord(d, { source: "lisflood", blocked: true }));
    assert.ok(!adoptRunAsRecord(d, { source: "dsswise", blocked: false }));
    assert.ok(!adoptRunAsRecord({ ...d, sourceOfRecordChoice: "desk" }, { source: "lisflood", blocked: false }));
    assert.ok(!adoptRunAsRecord({ ...d, resultsOfRecord: ref("dsswise") }, { source: "lisflood", blocked: false }));
  });
});

describe("Runner worklist", () => {
  it("lists large dams without a run of record, largest release first, and leaves out small and run dams", () => {
    const a = big();
    const b: DamRecord = { ...big(), id: "bigger", name: "Bigger", storageAcFt: big().storageAcFt * 2 };
    const c: DamRecord = { ...big(), id: "has-run", name: "Has run", resultsOfRecord: ref() };
    const rows = runnerWorklist([small(), a, b, c], analyze, new Set(["bigger"]));
    assert.deepEqual(
      rows.map((r) => r.id),
      ["bigger", "nd00199"],
    );
    assert.equal(rows[0].status, "run loaded, not the record");
    assert.equal(rows[1].status, "no run");
    assert.equal(runnerWorklist([{ ...a, sourceOfRecordChoice: "desk" }], analyze)[0].status, "desk chosen over a run");
  });

  it("writes a CSV with one row per dam and quotes commas", () => {
    const rows = runnerWorklist([{ ...big(), name: "Swan Buffalo, No. 12" }], analyze);
    const lines = worklistCsv(rows).trim().split("\n");
    assert.equal(lines.length, 2);
    assert.match(lines[0], /^dam_id,nid,name,county,height_ft,storage_acft,released_acft,breach_peak_cfs,status$/);
    assert.match(lines[1], /"Swan Buffalo, No\. 12"/);
  });
});

describe("dashboard with a layer of record", () => {
  const clean = (d: DamRecord) => {
    const r = analyze(d);
    return { ...r, datum: { ...r.datum, warnings: [] }, flood: { ...r.flood, warnings: [] } };
  };

  it("names the Runner run as the method, whatever fast layer is built", () => {
    assert.equal(dashMethod(ready("routed"), ref()), "runner");
    assert.equal(dashMethod(ready("outline"), ref("dsswise")), "model");
    assert.equal(dashMethod(ready("routed"), null), "routed");
  });

  it("raises one critical for a large dam with no run, and none for a small one", () => {
    const b = big();
    const fb = dashboardFlags(clean(b), ready("outline"), { record: layerOfRecord(b, analyze(b), ready("outline")) });
    assert.equal(fb[0].key, "runner-needed");
    assert.equal(fb[0].sev, "critical");
    const s = small();
    const fs = dashboardFlags(clean(s), ready("outline"), { record: layerOfRecord(s, analyze(s), ready("outline")) });
    assert.ok(!fs.some((f) => f.key === "runner-needed"));
  });

  it("drops the fast layer's own flags when a run is the record, and says when its rasters are not loaded", () => {
    const b = { ...big(), resultsOfRecord: ref("lisflood", 3) };
    const rec = layerOfRecord(b, analyze(b), ready("routed"));
    const f = dashboardFlags(clean(b), ready("routed"), { record: rec, recordMapped: false, volume: { wet: 50000, release: 1600, reservoir: 2000 } });
    assert.deepEqual(
      f.map((x) => x.key),
      ["record-unmapped", "edge"],
    );
    assert.ok(f.every((x) => x.sev === "info"));
    const mapped = dashboardFlags(clean(b), ready("routed"), { record: rec, recordMapped: true });
    assert.ok(!mapped.some((x) => x.key === "record-unmapped"));
  });
});
