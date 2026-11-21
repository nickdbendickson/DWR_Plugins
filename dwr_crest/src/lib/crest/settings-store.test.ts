// Where Settings > Save keeps your values (settings-store.ts): the shared defaults file in the desktop window, even when
// the window's bridge comes up after the desk has started (pywebview's Qt backend on Linux, install test 2026-11-21).
import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";

import { loadSettings, resetSettings, saveSettings, useSettings } from "./settings-store.ts";

type Reply = { path?: string; text?: string | null; saved?: string; error?: string };
const g = globalThis as { window?: unknown };
const tick = () => new Promise((r) => setTimeout(r, 0));

function bridge(path = "/home/nick/DamSafety/defaults.json") {
  const written: string[] = [];
  const api = {
    get_defaults: async (): Promise<Reply> => ({ path, text: null }),
    save_defaults: async (text: string): Promise<Reply> => (written.push(text), { saved: path }),
  };
  return { api, written };
}

describe("Settings > Save in the desktop window", () => {
  afterEach(() => {
    delete g.window;
    resetSettings();
  });

  it("bridge there at start: your values go to the defaults file", async () => {
    const { api, written } = bridge();
    g.window = Object.assign(new EventTarget(), { pywebview: { api } });
    await loadSettings();
    assert.equal(useSettings.getState().where, "file");
    assert.equal(await saveSettings({ manning_n: 0.07 } as never), null);
    assert.equal(written.length, 1);
    assert.match(written[0], /"manning_n": 0\.07/);
  });

  it("bridge comes up after the desk started (Linux Qt): the desk switches to the file and Save writes it", async () => {
    const w = new EventTarget() as EventTarget & { pywebview?: unknown };
    g.window = w;
    await loadSettings();
    assert.equal(useSettings.getState().where, "browser", "no bridge yet");
    const { api, written } = bridge("/data/defaults.json");
    w.pywebview = { api };
    w.dispatchEvent(new Event("pywebviewready"));
    await tick();
    await loadSettings();
    assert.equal(useSettings.getState().where, "file");
    assert.equal(useSettings.getState().filePath, "/data/defaults.json");
    assert.equal(await saveSettings({ manning_n: 0.07 } as never), null);
    assert.equal(written.length, 1, "Save wrote the shared defaults file, not only this browser");
  });

  it("the web desk (no bridge ever): your values stay in this browser and nothing waits forever", async () => {
    g.window = new EventTarget();
    await loadSettings();
    assert.equal(useSettings.getState().where, "browser");
    (g.window as EventTarget).dispatchEvent(new Event("pywebviewready"));   // a stray event without a bridge
    await tick();
    assert.equal(useSettings.getState().where, "browser");
  });
});
