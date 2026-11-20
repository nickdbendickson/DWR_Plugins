// The shared defaults file (settings.ts), the starting pool (engine.ts startingPool) and per-record Manning's n.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { analyze } from "./engine.ts";
import { exportDamJson, generateLisfloodPackage } from "./export.ts";
import { dwr2801 } from "./fixtures.ts";
import { outlineInputs } from "./outline.ts";
import { DEFAULT_ROUGHNESS, roughnessOf } from "./routing.ts";
import { blankDam } from "./samples.ts";
import {
  SCHEMA,
  SHARED_KEYS,
  SUITE_VALUES,
  applyDefaults,
  assumptions,
  changedOnly,
  checkValue,
  cleanValues,
  defaultsFileText,
  differsText,
  mergeDefaults,
  readDefaultsFile,
  recordDefaults,
  usedValues,
} from "./settings.ts";

const file = (values: unknown, extra: Record<string, unknown> = {}) => JSON.stringify({ format: "dam-defaults", format_version: 1, values, ...extra });
const fp = (dam: ReturnType<typeof dwr2801>) => {
  const io = outlineInputs(dam, analyze(dam));
  assert.ok(io.ok);
  return io.inputs.fingerprint;
};

describe("shared defaults file", () => {
  it("the suite values are the suite's defaults.json, and every one is a valid value of its key", () => {
    assert.deepEqual(SUITE_VALUES, { sunny_day_start_pool: "max_pool", pmf_breach_volume: "storage_at_start_pool", failure_mode: "piping", breach_method: "froehlich2008", manning_n: 0.06 });
    assert.deepEqual(SHARED_KEYS, ["sunny_day_start_pool", "pmf_breach_volume", "failure_mode", "breach_method", "manning_n"]);
    for (const k of SHARED_KEYS) assert.deepEqual(checkValue(k, SUITE_VALUES[k]), { ok: true, value: SUITE_VALUES[k] });
    assert.equal(SUITE_VALUES.manning_n, DEFAULT_ROUGHNESS.nFloodplain, "the shared n is Crest's built-in floodplain n");
  });

  it("reads a file: good keys kept, unknown keys and bad values left out and named", () => {
    const r = readDefaultsFile(file({ sunny_day_start_pool: "normal_pool", manning_n: "0.05", breach_method: "magic", cfl: 0.7, failure_mode: "Overtopping" }), "mine.json");
    assert.ok(r.ok);
    assert.deepEqual(r.values, { sunny_day_start_pool: "normal_pool", manning_n: 0.05, failure_mode: "overtopping" });
    assert.equal(r.problems.length, 2);
    assert.match(r.problems.join("\n"), /mine\.json: 'magic' is not one of .*\(breach_method\)/);
    assert.match(r.problems.join("\n"), /unknown key 'cfl'/);
  });

  it("holds a number to its range and refuses a boolean or text", () => {
    assert.equal(checkValue("manning_n", 0.5).ok, false);
    assert.equal(checkValue("manning_n", 0.005).ok, false);
    assert.equal(checkValue("manning_n", true).ok, false);
    assert.equal(checkValue("manning_n", "rough").ok, false);
    assert.equal(checkValue("manning_n", "").ok, false);
    assert.equal(checkValue("__proto__", 1).ok, false);
    assert.equal(checkValue("toString", 1).ok, false);
  });

  it("refuses what is not a defaults file, with the reason", () => {
    for (const [text, why] of [
      ["{", /not valid JSON/],
      ["[1]", /no 'values' object/],
      [JSON.stringify({ format: "crest-desk", values: {} }), /not a defaults file/],
      [JSON.stringify({ format: "dam-defaults", values: [1] }), /no 'values' object/],
      [JSON.stringify({ name: "x" }), /not a defaults file/],
      [file({}, { format_version: 2 }), /newer tool/],
      ["x".repeat(300_000), /too large/],
    ] as const) {
      const r = readDefaultsFile(text, "f.json");
      assert.equal(r.ok, false, text.slice(0, 30));
      if (!r.ok) assert.match(r.reason, why);
    }
    // A byte-order mark (Notepad) and a file with no "format" but a values object are read, as the plugins do.
    assert.ok(readDefaultsFile("﻿" + file({ manning_n: 0.04 })).ok);
    assert.ok(readDefaultsFile(JSON.stringify({ values: { manning_n: 0.04 } })).ok);
  });

  it("merges yours over the suite's and says where each value came from", () => {
    const m = mergeDefaults({ manning_n: 0.05, failure_mode: "piping" });
    assert.equal(m.values.manning_n, 0.05);
    assert.equal(m.source.manning_n, "yours");
    assert.equal(m.source.failure_mode, "suite", "a value equal to the suite's is the suite's");
    assert.equal(m.values.breach_method, "froehlich2008");
  });

  it("your file holds only the keys that differ from the suite, and reads back the same", () => {
    const text = defaultsFileText({ ...SUITE_VALUES, manning_n: 0.045, sunny_day_start_pool: "normal_pool" }, new Date(2026, 10, 13, 9, 5, 7));
    const j = JSON.parse(text);
    assert.equal(j.format, "dam-defaults");
    assert.equal(j.format_version, 1);
    assert.equal(j.saved, "2026-11-13T09:05:07");
    assert.deepEqual(j.values, { sunny_day_start_pool: "normal_pool", manning_n: 0.045 });
    const back = readDefaultsFile(text);
    assert.ok(back.ok);
    assert.deepEqual(back.values, j.values);
    assert.deepEqual(changedOnly(SUITE_VALUES), {});
    assert.deepEqual(cleanValues({ manning_n: 9, failure_mode: "overtopping", junk: 1 }), { failure_mode: "overtopping" });
    assert.deepEqual(cleanValues(null), {});
  });

  it("every key says what Crest does with it", () => {
    for (const k of SHARED_KEYS) assert.ok(SCHEMA[k].crest.length > 10 && SCHEMA[k].label.length > 3, k);
  });
});

describe("a new record starts from the shared defaults", () => {
  it("the suite values leave a blank record exactly as it was", () => {
    const base = blankDam();
    assert.deepEqual(applyDefaults(base, SUITE_VALUES), base);
  });

  it("takes the starting pool, failure mode, equation and floodplain n", () => {
    const d = applyDefaults(blankDam(), mergeDefaults({ sunny_day_start_pool: "normal_pool", failure_mode: "overtopping", breach_method: "mlm1984", manning_n: 0.045 }).values);
    assert.equal(d.startPool, "normal");
    assert.equal(d.failureMode, "overtopping");
    assert.equal(d.method, "macdonald");
    assert.deepEqual(d.roughness, { nFloodplain: 0.045 });
    // Back to the suite values: the record's own channel n is kept, the rest goes back.
    const back = applyDefaults({ ...d, roughness: { nChannel: 0.04, nFloodplain: 0.045 } }, SUITE_VALUES);
    assert.equal(back.startPool, undefined);
    assert.deepEqual(back.roughness, { nChannel: 0.04 });
    assert.equal(back.method, "froehlich");
  });

  it("an equation Crest does not have starts on Froehlich (2008) and says so", () => {
    for (const m of ["froehlich1995", "vtg1990"]) {
      const r = recordDefaults({ ...SUITE_VALUES, breach_method: m });
      assert.equal(r.method, "froehlich");
      assert.match(r.notes.join(" "), /Crest does not have/);
    }
    assert.equal(recordDefaults({ ...SUITE_VALUES, breach_method: "tr60_curvilinear" }).method, "tr60");
    assert.deepEqual(recordDefaults(SUITE_VALUES).notes, []);
  });
});

describe("starting pool", () => {
  const top = analyze(dwr2801());

  it("is the top of dam by default, with the numbers unchanged", () => {
    assert.equal(top.breach.startPool, "top");
    assert.equal(top.breach.startPoolNavdFt, 1071.09);
    assert.ok(Math.abs(top.breach.widthFt - 100) < 1);
    assert.ok(Math.abs(top.breach.headFt - 27.5) < 1e-9);
    assert.deepEqual(analyze(dwr2801({ startPool: "top" })).breach, top.breach);
  });

  it("normal pool: a lower head, the storage at that pool in the equations, a smaller peak and release", () => {
    const a = analyze(dwr2801({ startPool: "normal", normalPoolFt: 1061.09 }));
    assert.equal(a.breach.startPool, "normal");
    assert.equal(a.breach.startPoolNavdFt, 1061.09);
    assert.ok(Math.abs(a.breach.headFt - 17.5) < 1e-9, `head ${a.breach.headFt}`);
    // m = 2 without a surface area: storage at 17.5 of 27.5 ft is (17.5/27.5)^2 of the maximum.
    const vw = 1612.59 * (17.5 / 27.5) ** 2;
    assert.ok(Math.abs(a.breach.volumeAcFt - vw) < 0.01 * vw, `released ${a.breach.volumeAcFt} vs ${vw}`);
    assert.ok(a.breach.peakCfs < 0.6 * top.breach.peakCfs, `${a.breach.peakCfs} vs ${top.breach.peakCfs}`);
    assert.ok(a.breach.widthFt < top.breach.widthFt, "Froehlich width on the smaller Vw");
    assert.match(a.breach.notes.join("\n"), /Starting pool 1,061\.1 ft NAVD88 \(normal pool\), 10\.0 ft below the top of dam/);
    assert.equal(a.datum.warnings.filter((w) => w.startsWith("Starting pool")).length, 0);
  });

  it("an entered elevation is tied like the other stated elevations", () => {
    const a = analyze(dwr2801({ startPool: "elevation", startPoolFt: 1066.09 }));
    assert.equal(a.breach.startPool, "elevation");
    assert.ok(Math.abs(a.breach.headFt - 22.5) < 1e-9);
    // A local (0-300 ft) record: the typed value moves with the record's offset.
    const local = { ...blankDam(), startPool: "elevation" as const, startPoolFt: 95 };
    const b = analyze(local);
    assert.equal(b.breach.startPool, "elevation");
    assert.ok(Math.abs(b.breach.startPoolNavdFt - (95 + 1550 - 100)) < 1e-9, String(b.breach.startPoolNavdFt));
    assert.ok(Math.abs(b.breach.headFt - 13) < 1e-9);
  });

  it("a pool that cannot be used falls back to the top of dam and says why", () => {
    for (const [over, why] of [
      [{ startPool: "normal", normalPoolFt: Number.NaN }, /no normal pool elevation/],
      [{ startPool: "elevation" }, /no elevation is entered/],
      [{ startPool: "elevation", startPoolFt: 1080 }, /above the top of dam/],
      [{ startPool: "elevation", startPoolFt: 1043.7 }, /not above the breach bottom/],
      [{ startPool: "normal", normalPoolFt: 120 }, /could not be tied to NAVD88/],
    ] as const) {
      const a = analyze(dwr2801(over));
      assert.equal(a.breach.startPool, "top", JSON.stringify(over));
      assert.equal(a.breach.peakCfs, top.breach.peakCfs);
      const w = a.datum.warnings.filter((x) => x.startsWith("Starting pool:"));
      assert.equal(w.length, 1, JSON.stringify(over));
      assert.match(w[0], why);
      assert.match(w[0], /starts at the top of dam instead/);
      assert.ok(a.datum.flags?.some((f) => f.code === "W-V1" && f.text === w[0]));
    }
    // A pool typed at the top of dam is the default case, not a warning.
    const same = analyze(dwr2801({ startPool: "elevation", startPoolFt: 1071.09 }));
    assert.equal(same.breach.startPool, "top");
    assert.equal(same.datum.warnings.filter((x) => x.startsWith("Starting pool")).length, 0);
  });
});

describe("Manning's n on a record", () => {
  it("nothing set is the built-in roughness itself; a set value is held to 0.01 - 0.3", () => {
    assert.equal(roughnessOf(undefined), DEFAULT_ROUGHNESS);
    assert.equal(roughnessOf({}), DEFAULT_ROUGHNESS);
    assert.equal(roughnessOf({ nChannel: 0.035, nFloodplain: 0.06 }), DEFAULT_ROUGHNESS);
    assert.equal(roughnessOf({ nFloodplain: Number.NaN, nChannel: 0 }), DEFAULT_ROUGHNESS);
    assert.deepEqual(roughnessOf({ nFloodplain: 0.08 }), { nChannel: 0.035, nFloodplain: 0.08, bankfullM: 1.5 });
    assert.deepEqual(roughnessOf({ nChannel: 5, nFloodplain: 0.001 }), { nChannel: 0.3, nFloodplain: 0.01, bankfullM: 1.5 });
  });

  it("the terrain layer's fingerprint moves only when the record leaves the defaults", () => {
    const base = fp(dwr2801());
    assert.equal(fp(dwr2801({ startPool: "top", roughness: {} })), base, "defaults spelled out: the saved layer still matches");
    assert.equal(fp(dwr2801({ roughness: { nFloodplain: 0.06 } })), base);
    assert.notEqual(fp(dwr2801({ roughness: { nFloodplain: 0.08 } })), base);
    assert.notEqual(fp(dwr2801({ roughness: { nChannel: 0.05 } })), base);
    assert.notEqual(fp(dwr2801({ startPool: "normal", normalPoolFt: 1061.09 })), base);
    assert.equal(fp(dwr2801({ startPool: "normal" })), base, "a pool that fell back to the top of dam is the default layer");
  });

  it("the LISFLOOD export carries the record's floodplain n", () => {
    const series = analyze(dwr2801()).breach.series;
    assert.match(generateLisfloodPackage(dwr2801({ lisflood: { pointX: 1, pointY: 2 } }), series).par, /^fpfric 0\.0600$/m);
    assert.match(generateLisfloodPackage(dwr2801({ lisflood: { pointX: 1, pointY: 2 }, roughness: { nFloodplain: 0.045 } }), series).par, /^fpfric 0\.0450$/m);
  });
});

describe("assumptions entry in dam.json", () => {
  const when = new Date("2026-11-13T18:00:00Z");

  it("a default record on the suite values differs in nothing", () => {
    const dam = dwr2801();
    const j = JSON.parse(exportDamJson(dam, analyze(dam), mergeDefaults({}), when));
    assert.deepEqual(j.assumptions.used, { sunny_day_start_pool: "max_pool", pmf_breach_volume: null, failure_mode: "piping", breach_method: "froehlich2008", manning_n: 0.06 });
    assert.deepEqual(j.assumptions.differs, {});
    assert.deepEqual(j.assumptions.shared_values, SUITE_VALUES);
    assert.equal(j.assumptions.tool, "Crest");
    assert.equal(j.assumptions.when, "2026-11-13T18:00:00");
    assert.equal(j.assumptions.version, j.crest_version);
    assert.equal(j.starting_pool_ft, 1071.09);
    assert.equal(j.starting_pool_rule, "top of dam");
    assert.equal(j.manning_n_floodplain, 0.06);
    assert.equal(j.manning_n_channel, 0.035);
  });

  it("says where the record differs from the shared values, in the shared file's own ids", () => {
    const dam = dwr2801({ startPool: "normal", normalPoolFt: 1061.09, method: "macdonald", roughness: { nFloodplain: 0.08, nChannel: 0.04 } });
    const a = analyze(dam);
    const shared = mergeDefaults({ manning_n: 0.05 });
    const j = JSON.parse(exportDamJson(dam, a, shared, when));
    assert.deepEqual(j.assumptions.differs, {
      sunny_day_start_pool: { used: "normal_pool", shared: "max_pool" },
      breach_method: { used: "mlm1984", shared: "froehlich2008" },
      manning_n: { used: 0.08, shared: 0.05 },
    });
    assert.equal(j.assumptions.used.failure_mode, null, "MacDonald has no failure-mode term");
    assert.equal(j.assumptions.shared_source.manning_n, "yours");
    assert.match(j.assumptions.note, /Channel Manning's n 0\.04/);
    assert.equal(j.starting_pool_ft, 1061.09);
    const text = differsText(j.assumptions);
    assert.equal(text.length, 3);
    assert.match(text.join("\n"), /Sunny-day breach: starting pool: Normal pool on this record; the shared defaults say Maximum pool/);
  });

  it("records the pool the engine really used when the record's choice fell back", () => {
    const dam = dwr2801({ startPool: "normal" });
    const a = analyze(dam);
    assert.equal(assumptions(dam, mergeDefaults({}), "v", a.breach.startPool).used.sunny_day_start_pool, "max_pool");
    assert.equal(usedValues(dwr2801({ startPool: "elevation", startPoolFt: 1060 })).sunny_day_start_pool, "entered_elevation");
  });
});
