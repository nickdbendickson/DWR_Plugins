import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { dashboardFlags } from "./dashboard.ts";
import { analyze } from "./engine.ts";
import type { ExtentSensitivity } from "./extent.ts";
import type { OutlineState, ReachSummary, RoutedSummary } from "./outline.ts";
import { SAMPLE_DAMS } from "./samples.ts";
import { CONFIDENCE_SHIFT_M, confidenceOf, triageCsv, triageDam } from "./triage.ts";
import type { DamRecord, ResultsOfRecord } from "./types.ts";

const big = () => structuredClone(SAMPLE_DAMS.find((d) => d.nid === "ND00199")!); // ~1,600 ac-ft released, 4 receptors flooded
const small = () => structuredClone(SAMPLE_DAMS.find((d) => d.id === "prairie")!); // ~45 ac-ft released, nothing flooded

const extent = (over: Partial<ExtentSensitivity> = {}): ExtentSensitivity => ({
  wetAc: 40,
  outerAc: 8,
  innerAc: 4,
  sensitivity: 0.3,
  edgeM: 4000,
  outShiftM: 20,
  inShiftM: 10,
  basis: "routed-probe",
  stoppedInBasin: false,
  reachedEdge: false,
  volumeRatio: 3.5,
  truncated: false,
  ...over,
});

const routedSummary = (reach?: Partial<ReachSummary>): RoutedSummary => ({
  floodedAc: 4800,
  peakInCfs: 20000,
  peakOutCfs: 15000,
  routedPeakFellBy: 0.25,
  channelKm: 24,
  sections: 300,
  cappedSections: 0,
  volumeFlag: false,
  reachedEdge: true,
  fillAcFt: 6000,
  releasedAcFt: 1600,
  volumeRatio: 3.8,
  method: "spread",
  ...(reach
    ? { reach: { rounds: 4, extendedKm: 13.5, stoppedBecause: "round-cap", incomplete: true, strandedAcFt: 558, demCellM: 10, demCells: 1, ...reach } }
    : {}),
  notes: [],
});

const ready = (ext?: ExtentSensitivity, routed?: RoutedSummary, hitsCorridor = false): OutlineState =>
  ({
    state: "ready",
    outline: { type: "FeatureCollection", features: [] },
    stats: { hitsCorridor, ...(ext ? { extent: ext } : {}) },
    builtAt: "",
    message: null,
    layer: routed ? "routed" : "outline",
    routed,
  }) as unknown as OutlineState;

const runRef: ResultsOfRecord = {
  source: "lisflood",
  label: "LISFLOOD-FP (test)",
  files: [],
  importedAt: "2026-11-04T00:00:00.000Z",
  crs: "EPSG:26914",
  stats: { peakDepthFt: 1, peakDepthAllFt: 1, wetAc: 1, wetAcAll: 1, farthestKm: 1, maxVelocityFps: 1, arrivalMaxHr: 1, nearestWetM: 1, edgeWetCells: 0, coverKm: 1, cellM: 20, cells: 1, breachLon: 0, breachLat: 0 },
  samples: [],
  warnings: [],
};

const t = (d: DamRecord, o: OutlineState) => triageDam(d, analyze(d), o);

describe("triage confidence", () => {
  it("reads the edge shift against the provisional thresholds", () => {
    assert.equal(confidenceOf(26), "high"); // DWR-3034, confined
    assert.equal(confidenceOf(CONFIDENCE_SHIFT_M), "high");
    for (const m of [80, 127, 149, 158]) assert.equal(confidenceOf(m), "medium"); // edge shift alone never says low
  });
});

describe("per-dam triage", () => {
  it("a small dam with nothing flooded on confined, complete terrain is clearly trivial", () => {
    const r = t(small(), ready(extent()));
    assert.equal(r.recommendation, "trivial");
    assert.equal(r.confidence, "high");
    assert.equal(r.edgeShiftM, 20);
    assert.match(r.summary, /^Clearly trivial: screen only \(high confidence\)/);
    assert.ok(r.reasons.some((x) => /routed-spread probe/.test(x)));
  });

  it("the same dam on open terrain is uncertain, medium confidence (the footprint is a minimum)", () => {
    const r = t(small(), ready(extent({ outShiftM: 149 })));
    assert.equal(r.recommendation, "uncertain");
    assert.equal(r.confidence, "medium");
    assert.ok(r.reasons.some((x) => /read the footprint as a minimum/.test(x)));
  });

  it("a confined flood that ends in a closed basin keeps high confidence but is never called trivial", () => {
    // Trust map: "confined valley or closed basin" is High. But a culvert the DEM can't see would send the water on.
    const r = t(small(), ready(extent({ stoppedInBasin: true, volumeRatio: 1.9 })));
    assert.deepEqual([r.recommendation, r.confidence], ["uncertain", "high"]);
    assert.ok(r.reasons.some((x) => /culvert or bridge/.test(x)));
    assert.ok(!r.reasons.some((x) => /released volume/.test(x)), "the volume band does not apply to water parked in a basin");
  });

  it("a tiny release says its depths are noise", () => {
    const d = small();
    const a = analyze(d);
    a.breach.volumeAcFt = 6.9; // DWR-4031
    const r = triageDam(d, a, ready(extent({ outShiftM: 149 })));
    assert.ok(r.reasons.some((x) => /thin sheet/.test(x)));
  });

  it("a volume outside the 2–6× band, or a probe that runs off its terrain, is downgraded", () => {
    assert.equal(t(small(), ready(extent({ volumeRatio: 9 }))).confidence, "medium");
    assert.equal(t(small(), ready(extent({ volumeRatio: 1.2 }))).confidence, "medium");
    assert.equal(t(small(), ready(extent({ reachedEdge: true }))).confidence, "medium");
    // A routed layer's own reachedEdge is handled by its reach summary, not this probe flag.
    assert.equal(t(small(), ready(extent({ basis: "routed-layer", reachedEdge: true }))).confidence, "high");
  });

  it("no layer, or a layer saved before the extent check, is uncertain and says how to fix it", () => {
    const none = t(small(), { state: "build", message: "not built" });
    assert.deepEqual([none.recommendation, none.confidence, none.edgeShiftM], ["uncertain", "low", null]);
    assert.ok(none.reasons.some((x) => /build the inundation layer/.test(x)));
    const old = t(small(), ready(undefined));
    assert.equal(old.recommendation, "uncertain");
    assert.ok(old.reasons.some((x) => /build it again/.test(x)));
  });

  it("a large flood needs a Runner run, whatever the terrain", () => {
    const d = big();
    d.receptors = [];
    const r = t(d, ready(extent({ basis: "routed-layer" }), routedSummary()));
    assert.equal(r.recommendation, "needs-runner");
    assert.match(r.reasons[0], /^Large flood: 1,6\d\d ac-ft released/);
    assert.equal(r.confidence, "high", "terrain confidence is reported separately from the call");
  });

  it("an incomplete routed reach needs a Runner run and drops confidence to low", () => {
    const d = big();
    d.receptors = [];
    const r = t(d, ready(extent({ basis: "routed-layer", outShiftM: 127 }), routedSummary({}))); // DWR-2801
    assert.equal(r.recommendation, "needs-runner");
    assert.equal(r.confidence, "low");
    assert.ok(r.reasons.some((x) => /558 ac-ft still travelling/.test(x)));
  });

  it("anything flooded in the screen needs a Runner run, even on confined terrain", () => {
    const d = small();
    const a = analyze(d);
    a.flood.receptors = [{ ...a.flood.receptors[0], flooded: true }];
    const r = triageDam(d, a, ready(extent()));
    assert.equal(r.recommendation, "needs-runner");
    assert.ok(r.reasons.some((x) => /1 structure or receptor is flooded/.test(x)));
  });

  it("a receptor past the desk reach keeps a confined dam from being trivial", () => {
    const d = small();
    const a = analyze(d);
    a.flood.receptors = [{ ...a.flood.receptors[0], flooded: false, unresolved: true, outsideAoi: false }];
    assert.equal(triageDam(d, a, ready(extent())).recommendation, "uncertain");
  });

  it("an imported run of record settles it", () => {
    const d = big();
    d.resultsOfRecord = runRef;
    const r = t(d, ready(extent({ outShiftM: 150 })));
    assert.equal(r.recommendation, "trivial");
    assert.match(r.reasons[0], /layer of record/);
  });

  it("a trivial call never comes from the outline's corridor flag alone being clear", () => {
    // hitsCorridor on a terrain outline is a lateral bathtub limit, not a downstream reach: it is not a Runner trigger.
    assert.equal(t(small(), ready(extent(), undefined, true)).recommendation, "trivial");
  });
});

describe("triage CSV and the dashboard flag", () => {
  it("lists Runner jobs first, then uncertain, then trivial", () => {
    const s = small();
    const b = big();
    const rows = [
      { dam: s, result: analyze(s), triage: t(s, ready(extent())) },
      { dam: b, result: analyze(b), triage: t(b, ready(extent({ basis: "routed-layer" }), routedSummary())) },
      { dam: { ...s, id: "flat" }, result: analyze(s), triage: t(s, ready(extent({ outShiftM: 140 }))) },
    ];
    const lines = triageCsv(rows).trim().split("\n");
    assert.equal(lines[0], "dam_id,nid,name,county,released_acft,recommendation,confidence,edge_shift_m,reasons");
    assert.deepEqual(
      lines.slice(1).map((l) => l.split(",")[0]),
      [b.id, "flat", s.id],
    );
    assert.match(lines[1], /,needs-runner,/);
    assert.match(lines[3], /,trivial,high,20,/);
  });

  it("shows one triage flag when given, and nothing new when not", () => {
    const s = small();
    const a = analyze(s);
    const clean = { ...a, datum: { ...a.datum, warnings: [] }, flood: { ...a.flood, warnings: [] } };
    const o = ready(extent());
    assert.equal(dashboardFlags(clean, o).filter((f) => f.key === "triage").length, 0);
    const f = dashboardFlags(clean, o, { triage: triageDam(s, a, o) }).find((x) => x.key === "triage")!;
    assert.equal(f.sev, "info");
    assert.equal(f.text, "Triage: Clearly trivial: screen only (high confidence)");
    const b = big();
    const fb = dashboardFlags(clean, o, { triage: t(b, ready(extent(), routedSummary())) }).find((x) => x.key === "triage")!;
    assert.equal(fb.sev, "warning");
  });
});
