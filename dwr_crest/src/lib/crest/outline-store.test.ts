// outline-store.ts: an IndexedDB that refuses a layer is reported, not swallowed (the desk said "saved with this dam").
// A small fake of the IndexedDB open/transaction/request surface stands in for the browser.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { OutlineCache } from "./outline.ts";

type Handler = (() => void) | null;

/** A fake IDBFactory whose readwrite transactions fail with `failWith` (or succeed when null). */
function fakeIndexedDb(failWith: string | null) {
  const stored = new Map<string, unknown>();
  const later = (fn: () => void) => void setTimeout(fn, 0);
  const request = (result: unknown) => {
    const r = { result, error: null as { message: string } | null };
    return r;
  };
  const db = {
    objectStoreNames: { contains: () => true },
    createObjectStore() {},
    transaction(_name: string, mode: string) {
      const t = { oncomplete: null as Handler, onerror: null as Handler, onabort: null as Handler, error: null as { message: string } | null };
      const store = {
        put(v: unknown, k: string) {
          const r = request(k);
          later(() => {
            if (failWith && mode === "readwrite") {
              t.error = { message: failWith };
              r.error = t.error;
              t.onabort?.();
            } else {
              stored.set(k, v);
              t.oncomplete?.();
            }
          });
          return r;
        },
        delete(k: string) {
          const r = request(undefined);
          later(() => {
            stored.delete(k);
            t.oncomplete?.();
          });
          return r;
        },
        getAllKeys() {
          const r = request([...stored.keys()]);
          later(() => t.oncomplete?.());
          return r;
        },
        getAll() {
          const r = request([...stored.values()]);
          later(() => t.oncomplete?.());
          return r;
        },
      };
      return Object.assign(t, { objectStore: () => store });
    },
  };
  return {
    stored,
    factory: {
      open() {
        const req = { result: db, onupgradeneeded: null as Handler, onsuccess: null as Handler, onerror: null as Handler, onblocked: null as Handler };
        later(() => req.onsuccess?.());
        return req;
      },
    },
  };
}

const layer = (n: number) => ({ fingerprint: String(n) }) as unknown as OutlineCache;

/** Each case loads a fresh copy of the module (it caches the open database) against its own fake. */
async function fresh(failWith: string | null) {
  const fake = fakeIndexedDb(failWith);
  (globalThis as { indexedDB?: unknown }).indexedDB = fake.factory;
  const m = await import(`./outline-store.ts?case=${failWith ?? "ok"}-${Math.random()}`);
  return { m: m as typeof import("./outline-store.ts"), fake };
}

describe("saveOutline reports what did not happen", () => {
  it("saves and resolves true when IndexedDB works; no error is recorded", async () => {
    const { m, fake } = await fresh(null);
    assert.equal(await m.saveOutline("a", layer(1)), true);
    assert.ok(fake.stored.has("a"));
    assert.equal(m.outlineSaveError("a"), null);
    const back = await m.loadOutlines();
    assert.equal(back.get("a")?.fingerprint, "1");
  });

  it("resolves false, warns and records the error when the transaction aborts (quota, private window)", async () => {
    const { m, fake } = await fresh("QuotaExceededError: the quota has been exceeded");
    const warned: string[] = [];
    const orig = console.warn;
    console.warn = (...a: unknown[]) => void warned.push(a.join(" "));
    let fired = 0;
    const off = m.onOutlineSaveChange(() => fired++);
    try {
      assert.equal(await m.saveOutline("a", layer(1)), false);
    } finally {
      console.warn = orig;
      off();
    }
    assert.equal(fake.stored.has("a"), false);
    assert.match(warned[0], /inundation layer of a was not saved \(QuotaExceededError/);
    assert.match(m.outlineSaveError("a") ?? "", /QuotaExceededError/);
    assert.equal(fired, 1, "the desk is told once");
    assert.equal(m.outlineSaveError("b"), null);
  });

  it("resolves false with a reason when there is no IndexedDB at all (Node, a locked-down browser)", async () => {
    const m = (await import(`./outline-store.ts?case=none-${Math.random()}`)) as typeof import("./outline-store.ts");
    delete (globalThis as { indexedDB?: unknown }).indexedDB;
    const orig = console.warn;
    console.warn = () => {};
    try {
      assert.equal(await m.saveOutline("a", layer(1)), false);
      assert.match(m.outlineSaveError("a") ?? "", /not available/);
      assert.equal((await m.loadOutlines()).size, 0);
    } finally {
      console.warn = orig;
    }
  });
});
