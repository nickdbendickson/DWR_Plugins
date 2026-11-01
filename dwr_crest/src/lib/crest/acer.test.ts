import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { rankReceptor, thresholds, zone } from "./acer.ts";
import { screenReceptors } from "./engine.ts";

function wet(name: string, kind: "house" | "mobile" | "barn" | "road", depthFt: number, velocityFps: number, extra = {}) {
  const r = rankReceptor({ kind, depthFt, velocityFps, ...extra });
  return {
    name,
    kind,
    depthFt,
    velocityFps,
    zone: r.zone,
    ndClass: r.class,
    rankReason: r.reason,
    needsJudgment: r.judgment,
  };
}

describe("ACER TM 11 zones (damsuite tm11_curves.zone)", () => {
  it("house chart boundaries at v = 0 are 2.94 / 5.968 ft", () => {
    assert.deepEqual(thresholds("house", 0), [2.94, 5.968]);
  });

  it("2.45 ft at 0.03 ft/s is in the low zone", () => {
    assert.equal(zone("house", 2.45, 0.03), "low");
  });

  it("a depth on a boundary takes the higher zone", () => {
    assert.equal(zone("house", 2.94, 0), "judgment");
    assert.equal(zone("house", 5.968, 0), "high");
  });

  it("has no velocity-past-axis rule: 30 ft/s reads the 25 ft/s column", () => {
    assert.equal(zone("house", 0.5, 30), "judgment");
    assert.equal(zone("house", 0.8, 30), "high");
  });

  it("a depth past the chart's depth axis is high", () => {
    assert.equal(zone("mobile", 3.2, 0), "high");
  });

  it("uses the mobile-home chart for mobile homes", () => {
    assert.equal(zone("mobile", 2.0, 0), "judgment");
    assert.equal(zone("house", 2.0, 0), "low");
  });
});

describe("ND ranking (damsuite classify_nd)", () => {
  it("CHECK: a house at 2.45 ft and low velocity screens Medium, not High", () => {
    const r = rankReceptor({ kind: "house", depthFt: 2.45, velocityFps: 0.03 });
    assert.equal(r.class, "Medium");
    const s = screenReceptors([wet("4069 Casselton", "house", 2.45, 0.03)]);
    assert.equal(s.level, "Medium");
    assert.equal(s.acerLevel, "low");
  });

  it("Nick 2026-10-30: a home in the judgment zone is Medium by rule; the old option still makes it High (5.3)", () => {
    const r = rankReceptor({ kind: "house", depthFt: 3.0, velocityFps: 0 });
    assert.equal(r.zone, "judgment");
    assert.equal(r.class, "Medium");
    assert.equal(r.judgment, false, "automatic, not an engineer's call");
    assert.match(r.reason, /Medium by rule/);
    assert.doesNotMatch(r.reason, /5\.3/);
    const old = rankReceptor({ kind: "house", depthFt: 3.0, velocityFps: 0, judgmentAsProbable: true });
    assert.equal(old.class, "High");
    assert.equal(old.judgment, true);
    assert.match(old.reason, /old option, 5\.3\)/);
  });

  it("CHECK: the DWR-2801 house 4010 (3.10 ft, 0.11 ft/s) and DWR-4031 house 9953 (2.111 ft, 6.95 ft/s; toe run, was 16775) screen Medium", () => {
    for (const [name, d, v] of [["4010", 3.1, 0.11], ["9953", 2.111, 6.95]] as const) {
      const r = rankReceptor({ kind: "house", depthFt: d, velocityFps: v });
      assert.equal(r.zone, "judgment", name);
      assert.equal(r.class, "Medium", name);
      const s = screenReceptors([wet(name, "house", d, v)], [{ kind: "house" }]);
      assert.equal(s.level, "Medium", name);
      assert.equal(s.label, "Medium", name);
      assert.equal(s.judgmentAsProbable, false);
      assert.equal(s.judgment.length, 0, name);
      assert.ok(s.reasons.some((x) => /Medium by rule/.test(x)), s.reasons.join("\n"));
      assert.ok(!s.reasons.some((x) => /5\.3\(f\)/.test(x)), "no 5.3(f) citation");
    }
  });

  it("labels a judgment-driven High under the old option; by default the judgment zone is Medium", () => {
    const j = rankReceptor({ kind: "house", depthFt: 3.5, velocityFps: 2 });
    assert.equal(j.zone, "judgment");
    const def = screenReceptors([wet("Farmstead", "house", 3.5, 2)], [{ kind: "house" }]);
    assert.equal(def.level, "Medium");
    assert.equal(def.label, "Medium");
    const on = screenReceptors([wet("Farmstead", "house", 3.5, 2, { judgmentAsProbable: true })], [{ kind: "house" }], {
      judgmentAsProbable: true,
    });
    assert.equal(on.level, "High");
    assert.equal(on.label, "High (flagged: judgment zone)");
    assert.ok(on.judgment.length === 1, "the old option is flagged for an engineer");
    assert.match(on.reasons[0], /old option, 5\.3/);
    const hz = screenReceptors([wet("House", "house", 8, 4)], [{ kind: "house" }]);
    assert.equal(hz.level, "High", "a TM 11 high-zone home is still High");
    assert.equal(hz.label, "High", "a TM 11 high-zone High is not relabelled");
  });

  it("barns and quonsets are not homes", () => {
    const r = rankReceptor({ kind: "barn", depthFt: 4, velocityFps: 3 });
    assert.equal(r.class, null);
    assert.equal(screenReceptors([wet("2134 quonset", "barn", 4, 3)]).level, "Low");
  });

  it("ranks roads by AADT and class", () => {
    assert.equal(rankReceptor({ kind: "road", depthFt: 0.5, velocityFps: 1, aadt: 3423 }).class, "High");
    assert.equal(rankReceptor({ kind: "road", depthFt: 0.5, velocityFps: 1, aadt: 800 }).class, "Medium");
    assert.equal(rankReceptor({ kind: "road", depthFt: 0.5, velocityFps: 1, aadt: 100 }).class, "Low");
    assert.equal(rankReceptor({ kind: "road", depthFt: 0.5, velocityFps: 1, roadClass: "interstate" }).class, "High");
    assert.equal(rankReceptor({ kind: "road", depthFt: 0.5, velocityFps: 1, roadClass: "us_state" }).class, "Medium");
    const unknown = rankReceptor({ kind: "road", depthFt: 2, velocityFps: 1 });
    assert.equal(unknown.class, null);
    assert.equal(unknown.judgment, true);
    assert.equal(rankReceptor({ kind: "road", depthFt: 0.05, velocityFps: 1, aadt: 5000 }).class, null);
  });

  it("DWR-2801 benchmark structures (ANUGA depths, harness run_crest_2801.mjs) screen Medium", () => {
    const s = screenReceptors([
      wet("2134 barn/quonset", "barn", 2.27, 2.0),
      wet("1096", "house", 1.53, 2.06),
      wet("435", "house", 1.94, 0.12),
      wet("4069 Casselton", "house", 2.45, 0.03),
    ]);
    assert.equal(s.level, "Medium");
  });
});
