import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  DEFAULT_LAYERS,
  DISCLAIMER,
  dashboardFlags,
  dashMethod,
  flagSeverity,
  LAYER_DEFS,
  paginate,
  PER_PAGE,
  peakOf,
  routedVolumes,
  type Receptor,
  searchReceptors,
  sortReceptors,
} from "./dashboard.ts";
import { readFileSync } from "node:fs";
import { analyze } from "./engine.ts";
import type { OutlineState, RoutedSummary } from "./outline.ts";
import { SAMPLE_DAMS } from "./samples.ts";

const dam = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;
const result = analyze(dam);
const clean = {
  ...result,
  datum: { ...result.datum, warnings: [] },
  flood: { ...result.flood, warnings: [] },
};

const routed = (over: Partial<RoutedSummary> = {}): RoutedSummary => ({
  floodedAc: 100,
  peakInCfs: 1000,
  peakOutCfs: 800,
  routedPeakFellBy: 0.2,
  channelKm: 5,
  sections: 20,
  cappedSections: 0,
  volumeFlag: false,
  fillAcFt: 0,
  releasedAcFt: 0,
  volumeRatio: 0,
  reachedEdge: false,
  notes: [],
  ...over,
});
const ready = (r?: RoutedSummary, hitsCorridor = false): OutlineState =>
  ({
    state: "ready",
    outline: { type: "FeatureCollection", features: [] },
    stats: { hitsCorridor },
    builtAt: "2026-11-03",
    message: null,
    layer: r ? "routed" : "outline",
    routed: r,
  }) as unknown as OutlineState;

function rec(i: number, over: Partial<Receptor> = {}): Receptor {
  return {
    ...result.flood.receptors[0],
    id: `r${i}`,
    name: `S${String(i).padStart(2, "0")}`,
    depthFt: i,
    flooded: true,
    unresolved: false,
    ...over,
  };
}

describe("Style B dashboard", () => {
  it("uses the brief's disclaimer", () => {
    assert.equal(DISCLAIMER, "Screening estimate — not an ND Dam Safety or ACER classification.");
  });

  it("flagSeverity: ok / warning / critical", () => {
    assert.equal(flagSeverity({}), "ok");
    assert.equal(flagSeverity({ wet_volume: 5, release_volume: 10 }), "ok");
    assert.equal(flagSeverity({ wet_volume: 15, release_volume: 10 }), "warning");
    assert.equal(
      flagSeverity({ wet_volume: 15, release_volume: 10, reservoir_volume: 20 }),
      "warning",
    );
    assert.equal(
      flagSeverity({ wet_volume: 25, release_volume: 10, reservoir_volume: 20 }),
      "critical",
    );
  });

  it("names the method behind the depth layer", () => {
    assert.equal(dashMethod(ready(routed())), "routed");
    assert.equal(dashMethod(ready()), "outline");
    assert.equal(dashMethod({ state: "none", message: "x" }), "strip-legacy");
  });

  it("status-bar flags: volume, edge, legacy; worst first", () => {
    assert.deepEqual(dashboardFlags(clean, ready(routed())), []);
    const f = dashboardFlags(clean, ready(routed({ volumeFlag: true, reachedEdge: true })));
    assert.deepEqual(
      f.map((x) => [x.key, x.sev]),
      [
        ["volume", "warning"],
        ["edge", "info"],
      ],
    );
    assert.equal(f[1].text, "Flood edge touched domain — extent may be larger");
    assert.equal(dashboardFlags(clean, ready(undefined, true))[0].key, "edge");
    const legacy = dashboardFlags(clean, {
      state: "build",
      message: "Build the fast layer on the Flood step.",
    });
    assert.equal(legacy[0].sev, "warning");
    assert.match(legacy[0].text, /^Showing the legacy strip: /);
  });

  it("a critical volume flag uses the spec's wording and sorts first", () => {
    const f = dashboardFlags(clean, ready(routed({ reachedEdge: true })), {
      volume: { wet: 30, release: 10, reservoir: 20 },
    });
    assert.equal(f[0].sev, "critical");
    assert.equal(f[0].text, "Volume flag: wet 3.0× release, above reservoir volume — review");
    const w = dashboardFlags(clean, ready(), { volume: { wet: 15, release: 10 } });
    assert.equal(w[0].text, "Volume flag: wet 1.5× release — review");
  });

  it("never shows more than one critical at a time", () => {
    const f = dashboardFlags(clean, ready(), { volume: { wet: 30, release: 10, reservoir: 20 } });
    assert.equal(f.filter((x) => x.sev === "critical").length, 1);
  });

  it("table: sorts by any column, both ways", () => {
    const rows = [
      rec(3, { ndClass: "Low" }),
      rec(1, { ndClass: "High" }),
      rec(2, { ndClass: null, flooded: false }),
    ];
    assert.deepEqual(
      sortReceptors(rows, "depthFt", true).map((r) => r.id),
      ["r3", "r1", "r2"],
    );
    assert.deepEqual(
      sortReceptors(rows, "ndClass", true).map((r) => r.id),
      ["r1", "r3", "r2"],
    );
    assert.deepEqual(
      sortReceptors(rows, "name", false).map((r) => r.name),
      ["S01", "S02", "S03"],
    );
    assert.deepEqual(
      sortReceptors(rows, "name", true).map((r) => r.name),
      ["S03", "S02", "S01"],
    );
    assert.deepEqual(
      rows.map((r) => r.id),
      ["r3", "r1", "r2"],
      "sorts a copy",
    );
  });

  it("table: pages at 10 rows, every page reachable, page clamped", () => {
    assert.equal(PER_PAGE, 10);
    const rows = Array.from({ length: 23 }, (_, i) => i);
    const p0 = paginate(rows, 0);
    assert.equal(p0.view.length, 10);
    assert.equal(p0.pages, 3);
    assert.deepEqual(paginate(rows, 2).view, [20, 21, 22]);
    assert.equal(paginate(rows, 9).page, 2);
    assert.deepEqual(paginate([], 0), { view: [], pages: 1, page: 0 });
  });

  it("search filters by name or kind", () => {
    const rows = [
      rec(1, { name: "Farmstead A", kind: "house" }),
      rec(2, { name: "Bridge B", kind: "road" }),
    ];
    assert.deepEqual(
      searchReceptors(rows, "farm").map((r) => r.id),
      ["r1"],
    );
    assert.deepEqual(
      searchReceptors(rows, "ROAD").map((r) => r.id),
      ["r2"],
    );
    assert.equal(searchReceptors(rows, "  ").length, 2);
  });

  it("hydrograph peak: value and time", () => {
    const pk = peakOf(result.breach.series)!;
    assert.equal(pk.qCfs, Math.max(...result.breach.series.map((p) => p.qCfs)));
    assert.equal(peakOf([]), null);
  });

  it("four independent map toggles, depth and structures on by default", () => {
    assert.deepEqual(
      LAYER_DEFS.map(([k]) => k),
      ["depth", "arrival", "structures", "hillshade"],
    );
    assert.equal(DEFAULT_LAYERS.depth && DEFAULT_LAYERS.structures, true);
    assert.equal(DEFAULT_LAYERS.runnerOverlay, false);
  });

  it("reads the volumes from the routed layer's note; critical when wet is above the reservoir", () => {
    const note =
      "The fill lays down 2400 ac-ft, more than the breach releases (800 ac-ft): the water-surface envelope is wider than the released volume supports.";
    assert.equal(
      routedVolumes(ready(routed({ notes: [note] })), 1000),
      null,
      "no flag, no volumes",
    );
    const v = routedVolumes(ready(routed({ volumeFlag: true, notes: [note] })), 1000)!;
    assert.deepEqual(v, { wet: 2400, release: 800, reservoir: 1000 });
    assert.equal(
      dashboardFlags(clean, ready(routed({ volumeFlag: true, notes: [note] })), { volume: v })[0]
        .sev,
      "critical",
    );
    assert.equal(
      routedVolumes(ready(routed({ volumeFlag: true })), 1000),
      null,
      "older layer: no note",
    );
    // The note is written by routed-flood.ts; keep the two in step.
    const src = readFileSync(new URL("./routed-flood.ts", import.meta.url), "utf8");
    assert.match(
      src,
      /`The fill lays down \$\{.*\} ac-ft, more than the breach releases \(\$\{.*\} ac-ft\)/,
    );
  });

  it("reads the volume provenance fields in preference to the note", () => {
    // A current layer carries fillAcFt/releasedAcFt, so routedVolumes reads them straight, no note needed.
    const v = routedVolumes(
      ready(routed({ volumeFlag: true, fillAcFt: 2400, releasedAcFt: 800, volumeRatio: 3 })),
      1000,
    )!;
    assert.deepEqual(v, { wet: 2400, release: 800, reservoir: 1000 });
    // The fields win even when an older note disagrees (the fields are authoritative).
    const v2 = routedVolumes(
      ready(routed({ volumeFlag: true, fillAcFt: 2400, releasedAcFt: 800, notes: ["The fill lays down 999 ac-ft, more than the breach releases (111 ac-ft):"] })),
      1000,
    )!;
    assert.deepEqual(v2, { wet: 2400, release: 800, reservoir: 1000 });
  });
});
