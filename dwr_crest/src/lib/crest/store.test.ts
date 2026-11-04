import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { activeDam, applyPatch, migrateDesk } from "./store.ts";
import { analyze } from "./engine.ts";
import type { DamRecord } from "./types.ts";
import { SAMPLE_DAMS } from "./samples.ts";

describe("migrateDesk", () => {
  // A corrupted localStorage value must fall back to the samples, not be cast blindly to DeskState and crash
  // later when activeDam() reads state.dams.
  for (const [label, bad] of [
    ["a string", "corrupted"],
    ["a number", 42],
    ["null", null],
    ["an array", [1, 2, 3]],
    ["an object without a dams array", { dams: "nope", activeId: "x" }],
  ] as const) {
    it(`falls back to the samples for ${label}`, () => {
      const state = migrateDesk(bad, 2);
      assert.ok(Array.isArray(state.dams), "dams is an array");
      assert.equal(state.dams, SAMPLE_DAMS);
      assert.equal(state.activeId, SAMPLE_DAMS[0].id);
      // activeDam must not throw on the recovered state.
      assert.ok(activeDam(state));
    });
  }

  it("keeps a valid persisted record and migrates the ND00199 sample in place", () => {
    const persisted = {
      dams: [{ id: "nd00199", crestFt: 1072.24, streambedFt: 1044.74 }],
      activeId: "nd00199",
      step: "setup",
    };
    const state = migrateDesk(persisted, 0);
    const now = SAMPLE_DAMS.find((d) => d.id === "nd00199");
    assert.equal(state.dams[0].crestFt, now?.crestFt);
    assert.equal(state.activeId, "nd00199");
  });
});

describe("applyPatch (Docs & QA on #57: a stale NGVD29 note after the crest is retyped)", () => {
  const dam = { ...SAMPLE_DAMS[0], crestFt: 1630.46, statedDatum: "NAVD88", datumNote: "Crest converted from NGVD29 (1630.0 + 0.46 ft)." } as DamRecord;
  it("a typed crest clears the datum note, so datum_note and datum_rule stop carrying it", () => {
    const next = applyPatch(dam, { crestFt: 1631.2 });
    assert.equal(next.crestFt, 1631.2);
    assert.equal(next.datumNote, undefined);
    assert.equal(analyze(next).datum.rule, "Elevations treated as NAVD88.");
  });
  it("a typed streambed clears it the same way (Docs & QA on #64)", () => {
    const next = applyPatch(dam, { streambedFt: (dam.streambedFt ?? 0) + 1 });
    assert.equal(next.datumNote, undefined);
    assert.equal(next.crestFt, dam.crestFt);
    assert.equal(analyze(next).datum.rule, "Elevations treated as NAVD88.");
    assert.equal(applyPatch(dam, { streambedFt: dam.streambedFt }).datumNote, dam.datumNote);
  });
  it("other edits, the same crest, or a patch that sets the note itself keep it", () => {
    assert.equal(applyPatch(dam, { name: "x" }).datumNote, dam.datumNote);
    assert.equal(applyPatch(dam, { crestFt: 1630.46 }).datumNote, dam.datumNote);
    assert.equal(applyPatch(dam, { crestFt: 1600, datumNote: "n" }).datumNote, "n");
  });
});
