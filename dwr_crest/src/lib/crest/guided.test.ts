import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { analyze } from "./engine.ts";
import {
  buildProgress,
  FLAG_STACK_MAX,
  guidedFlags,
  guidedSteps,
  recordProblems,
  stackFlags,
  type StepState,
} from "./guided.ts";
import { fieldTag, revertPatch } from "./nd-dams.ts";
import type { OutlineState, RoutedSummary } from "./outline.ts";
import { SAMPLE_DAMS } from "./samples.ts";
import { activeDam, useDesk } from "./store.ts";
import type { DamRecord } from "./types.ts";

const dam = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;
const result = analyze(dam);
const build: OutlineState = {
  state: "build",
  message: "The terrain outline is not built yet (one 3DEP terrain request).",
};
const routed: RoutedSummary = {
  floodedAc: 1234,
  peakInCfs: 1000,
  peakOutCfs: 800,
  routedPeakFellBy: 0.2,
  channelKm: 5,
  sections: 20,
  cappedSections: 0,
  volumeFlag: false,
  reachedEdge: false,
  notes: [],
};
const ready = (r: RoutedSummary = routed): OutlineState =>
  ({
    state: "ready",
    outline: { type: "FeatureCollection", features: [] },
    stats: { hitsCorridor: false, outlineAc: 1 },
    builtAt: "x",
    message: null,
    layer: "routed",
    routed: r,
  }) as unknown as OutlineState;
const by = (s: StepState[], k: string) => s.find((x) => x.key === k)!;

describe("Style C guided setup", () => {
  it("rail: record and breach done, terrain waiting, the open step active", () => {
    const s = guidedSteps(dam, result, build, "terrain");
    assert.deepEqual(
      s.map((x) => [x.key, x.state]),
      [
        ["record", "done"],
        ["breach", "done"],
        ["terrain", "active"],
        ["review", "pending"],
      ],
    );
    assert.equal(by(s, "terrain").detail, build.message);
    assert.match(by(s, "breach").detail, /^Peak [\d,]+ cfs · [\d,]+ ac-ft released$/);
  });

  it("C1: the terrain step turns done with the layer's area once the build saves it", () => {
    const s = guidedSteps(dam, result, ready(), "record");
    assert.equal(by(s, "terrain").state, "done");
    assert.equal(by(s, "terrain").detail, "Routed flood · 1,234 ac");
    const busy = guidedSteps(dam, result, build, "record", {
      busy: "Fetching the 3DEP terrain (10 × 10 cells at 10 m)…",
      error: null,
    });
    assert.equal(by(busy, "terrain").progress, 30);
    assert.equal(buildProgress("Building the inundation layer on the terrain…"), 70);
    assert.equal(buildProgress(null), undefined);
  });

  it("C2: a broken step carries its error on its own card", () => {
    const bad: DamRecord = { ...dam, heightFt: Number.NaN, lat: 123 };
    const s = guidedSteps(bad, analyze(bad), build, "record");
    const rec = by(s, "record");
    assert.equal(rec.state, "broken");
    assert.match(rec.error!, /^Missing: height/);
    assert.match(rec.error!, /Location out of range/);
    assert.equal(by(s, "breach").state, "pending", "only the step at fault is broken");
    const err = guidedSteps(dam, result, build, "record", {
      busy: null,
      error: "3DEP request failed (HTTP 504)",
    });
    assert.equal(by(err, "terrain").state, "broken");
    assert.equal(by(err, "terrain").error, "3DEP request failed (HTTP 504)");
    assert.deepEqual(recordProblems({ ...dam, crestFt: 1000, streambedFt: 1010 }), [
      "Crest is not above the breach bottom",
    ]);
    assert.deepEqual(recordProblems(dam), []);
  });

  it("C2: no modal in the guided setup (no window.alert/confirm, no dialog)", () => {
    const src = readFileSync(
      new URL("../../components/crest/HybridSetup.tsx", import.meta.url),
      "utf8",
    );
    assert.doesNotMatch(src, /\b(alert|confirm|prompt)\(|<dialog|role="dialog"|AlertDialog/);
  });

  it("C3: at most three pills, then +N more", () => {
    assert.equal(FLAG_STACK_MAX, 3);
    const six = Array.from({ length: 6 }, (_, i) => `f${i}`);
    assert.deepEqual(stackFlags(six), { shown: ["f0", "f1", "f2"], more: 3, moreText: "+3 more" });
    assert.deepEqual(stackFlags(six.slice(0, 3)), {
      shown: ["f0", "f1", "f2"],
      more: 0,
      moreText: null,
    });
    assert.deepEqual(stackFlags([]), { shown: [], more: 0, moreText: null });
  });

  it("flags from all steps, worst first, one critical at a time", () => {
    const note = "The fill lays down 9000 ac-ft, more than the breach releases (100 ac-ft): wider.";
    const s = guidedSteps(
      dam,
      result,
      ready({ ...routed, volumeFlag: true, reachedEdge: true, notes: [note] }),
      "record",
    );
    const f = guidedFlags(s);
    assert.equal(f[0].sev, "critical");
    assert.equal(f.filter((x) => x.sev === "critical").length, 1);
    assert.ok(f.some((x) => x.key === "edge" && x.sev === "info"));
    const twice = guidedFlags([{ ...s[0], flags: [f[0], { ...f[0], key: "v2" }] }]);
    assert.deepEqual(
      twice.map((x) => x.sev),
      ["critical", "warning"],
    );
  });

  it("C4: nothing locked — an auto-filled field takes an edit, is tagged edited, and can be reverted", () => {
    const linked: DamRecord = {
      ...dam,
      id: "c4",
      nidId: "ND00199",
      provenance: {
        heightFt: { src: "NID", field: "NID Height (Ft)", value: dam.heightFt, asOf: "2026-10-01" },
      },
    };
    assert.equal(fieldTag(linked, "heightFt")?.kind, "from");
    useDesk.setState({ dams: [linked], activeId: "c4" });
    useDesk.getState().update({ heightFt: 40 });
    const edited = activeDam(useDesk.getState());
    assert.equal(edited.heightFt, 40);
    assert.equal(fieldTag(edited, "heightFt")?.kind, "edited");
    assert.notEqual(
      analyze(edited).breach.peakCfs,
      analyze(linked).breach.peakCfs,
      "the analysis follows the edit",
    );
    assert.equal({ ...edited, ...revertPatch(edited, "heightFt") }.heightFt, dam.heightFt);
    // Every step stays open whatever its state: the rail has no locked state, and the desk's inputs are never read-only.
    const states: StepState["state"][] = ["pending", "active", "done", "broken"];
    for (const s of guidedSteps(edited, analyze(edited), build, "record"))
      assert.ok(states.includes(s.state));
    const desk = readFileSync(new URL("../../components/crest/desk.tsx", import.meta.url), "utf8");
    const fields = desk.slice(desk.indexOf("function TextField("));
    assert.doesNotMatch(
      fields,
      /readOnly|disabled=/,
      "TextField / NumField / OptNumField / SelectField never lock",
    );
  });
});
