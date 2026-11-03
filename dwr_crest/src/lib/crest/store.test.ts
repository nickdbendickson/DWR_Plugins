import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { activeDam, migrateDesk } from "./store.ts";
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
