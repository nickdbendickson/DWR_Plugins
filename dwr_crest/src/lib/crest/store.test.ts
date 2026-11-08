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

describe("desk storage: a big inundation layer never overflows localStorage (2026-11-05 quota error)", () => {
  // Safari caps localStorage at about 5 MB a site and throws "The quota has been exceeded." past it. The routed
  // ND00199 / DWR-2801 layer alone is 5–6 MB of GeoJSON, and persist wrote the whole desk on every update(), so the
  // throw came out of update() and the guided setup showed it as the Terrain layer step's error.
  const QUOTA = 200_000;
  const fake = () => {
    const m = new Map<string, string>();
    return {
      m,
      getItem: (k: string) => m.get(k) ?? null,
      setItem: (k: string, v: string) => {
        if (v.length > QUOTA) {
          const e = new Error("The quota has been exceeded.");
          e.name = "QuotaExceededError";
          throw e;
        }
        m.set(k, v);
      },
      removeItem: (k: string) => void m.delete(k),
    };
  };
  const bigLayer = () => ({ fingerprint: "f", builtAt: "t", source: "s", attribution: "a", stats: {}, outline: { pad: "x".repeat(QUOTA * 2) } }) as unknown as DamRecord["outline"];

  it("update() with a layer bigger than the quota doesn't throw, and the saved desk leaves the layer out", async () => {
    const { useDesk } = await import("./store.ts");
    const ls = fake();
    const g = globalThis as { localStorage?: unknown };
    const had = g.localStorage;
    g.localStorage = ls;
    try {
      // The old save (the whole record, layer and all) is what overflowed.
      const dam = { ...useDesk.getState().dams[0], outline: bigLayer() };
      assert.throws(() => ls.setItem("probe", JSON.stringify({ state: { dams: [dam] } })), /quota/);
      assert.doesNotThrow(() => useDesk.getState().update({ outline: bigLayer() }));
      const saved = ls.m.get("crest-desk-v2");
      assert.ok(saved, "the desk was saved");
      assert.ok(saved.length < QUOTA, "the saved desk is small");
      assert.ok(!saved.includes('"outline"'), "no layer in localStorage");
      assert.ok(activeDam(useDesk.getState()).outline, "the layer stays on the dam in this tab");
    } finally {
      g.localStorage = had;
      useDesk.getState().restore();
    }
  });

  it("a full localStorage still never throws out of update()", async () => {
    const { useDesk } = await import("./store.ts");
    const g = globalThis as { localStorage?: unknown };
    const had = g.localStorage;
    g.localStorage = { ...fake(), setItem: () => { throw Object.assign(new Error("The quota has been exceeded."), { name: "QuotaExceededError" }); } };
    const warn = console.warn;
    console.warn = () => {};
    try {
      assert.doesNotThrow(() => useDesk.getState().update({ name: "renamed" }));
      assert.equal(activeDam(useDesk.getState()).name, "renamed");
    } finally {
      console.warn = warn;
      g.localStorage = had;
      useDesk.getState().restore();
    }
  });

  it("flags the desk as no longer saved when a write overflows, and clears it on the next good save (audit H16)", async () => {
    const { useDesk, useSaveStatus } = await import("./store.ts");
    const g = globalThis as { localStorage?: unknown };
    const had = g.localStorage;
    const warn = console.warn;
    console.warn = () => {};
    try {
      // a localStorage whose setItem always throws (over quota): the save is skipped and the desk is flagged.
      g.localStorage = { ...fake(), setItem: () => { throw Object.assign(new Error("The quota has been exceeded."), { name: "QuotaExceededError" }); } };
      useDesk.getState().update({ name: "over quota" });
      assert.equal(useSaveStatus.getState().blocked, true, "blocked after an overflow");
      assert.match(useSaveStatus.getState().detail, /quota/i);
      // a working localStorage: the next save succeeds and clears the flag, so the banner goes away.
      g.localStorage = fake();
      useDesk.getState().update({ name: "fits now" });
      assert.equal(useSaveStatus.getState().blocked, false, "cleared after a good save");
    } finally {
      console.warn = warn;
      g.localStorage = had;
      useSaveStatus.setState({ blocked: false, detail: "" });
      useDesk.getState().restore();
    }
  });
});

describe("desk storage: a stale tab never clobbers a newer tab's save (audit H17)", () => {
  const fake = () => {
    const m = new Map<string, string>();
    return { m, getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), removeItem: (k: string) => void m.delete(k) };
  };

  it("a desk save bumps the shared cross-tab counter", async () => {
    const { useDesk, resetDeskSync } = await import("./store.ts");
    const g = globalThis as { localStorage?: unknown };
    const had = g.localStorage;
    const ls = fake();
    g.localStorage = ls;
    resetDeskSync();
    try {
      useDesk.getState().update({ name: "a" });
      const s1 = Number(ls.m.get("crest-desk-seq"));
      assert.ok(s1 >= 1, "counter bumped on a save");
      useDesk.getState().update({ name: "b" });
      assert.equal(Number(ls.m.get("crest-desk-seq")), s1 + 1, "counter bumps again on the next save");
    } finally {
      g.localStorage = had;
      resetDeskSync();
      useDesk.getState().restore();
    }
  });

  it("stops saving (and flags the tab) when another tab saved a newer desk, leaving that save intact", async () => {
    const { useDesk, useSaveStatus, markOtherTabWrite, resetDeskSync } = await import("./store.ts");
    const g = globalThis as { localStorage?: unknown };
    const had = g.localStorage;
    const ls = fake();
    g.localStorage = ls;
    resetDeskSync();
    try {
      // the other tab's newer desk is already in localStorage
      ls.m.set("crest-desk-v2", JSON.stringify({ state: { dams: [{ id: "other-tab-dam" }] } }));
      ls.m.set("crest-desk-seq", "5");
      markOtherTabWrite(5); // this tab (counter 0) sees the newer save
      assert.equal(useSaveStatus.getState().stale, true, "flagged stale");
      useDesk.getState().update({ name: "edit in the stale tab" }); // must not throw, must not clobber
      assert.ok(useDesk.getState().dams.some((d) => d.name === "edit in the stale tab"), "the edit stands in memory");
      assert.ok(ls.m.get("crest-desk-v2")!.includes("other-tab-dam"), "the newer tab's save is NOT overwritten");
    } finally {
      g.localStorage = had;
      resetDeskSync();
      useDesk.getState().restore();
    }
  });

  it("an older or equal counter from another tab does not make the tab stale", async () => {
    const { markOtherTabWrite, useSaveStatus, resetDeskSync } = await import("./store.ts");
    resetDeskSync();
    try {
      markOtherTabWrite(0);
      assert.equal(useSaveStatus.getState().stale, false);
    } finally {
      resetDeskSync();
    }
  });
});

describe("outlineSync: the layers kept in IndexedDB follow the desk", () => {
  const layer = (n: number) => ({ fingerprint: String(n) }) as unknown as DamRecord["outline"];
  const rec = (id: string, outline?: DamRecord["outline"]) => ({ id, outline }) as unknown as DamRecord;

  it("writes a built layer once, rewrites a rebuilt one, deletes a removed one and a deleted dam's", async () => {
    const { outlineSync } = await import("./outline-store.ts");
    const log: string[] = [];
    const sync = outlineSync({ save: async (id, c) => void log.push(`save ${id} ${c.fingerprint}`), remove: async (id) => void log.push(`del ${id}`) });
    const a1 = layer(1);
    sync([rec("a", a1), rec("b")]);
    sync([rec("a", a1), rec("b")]); // an unrelated edit: nothing written
    const a2 = layer(2);
    const b3 = layer(3);
    sync([rec("a", a2), rec("b", b3)]);
    sync([rec("a"), rec("b", b3)]);
    assert.deepEqual(log, ["save a 1", "save a 2", "save b 3", "del a"]);
    log.length = 0;
    const b = layer(4);
    sync([rec("b", b)]);
    sync([]);
    assert.deepEqual(log, ["save b 4", "del b"]);
  });

  it("doesn't write back the layers IndexedDB already holds", async () => {
    const { outlineSync } = await import("./outline-store.ts");
    const log: string[] = [];
    const kept = layer(7);
    const sync = outlineSync({ save: async (id) => void log.push(`save ${id}`), remove: async (id) => void log.push(`del ${id}`) }, new Map([["a", kept!]]));
    sync([rec("a", kept)]);
    assert.deepEqual(log, []);
  });
});
