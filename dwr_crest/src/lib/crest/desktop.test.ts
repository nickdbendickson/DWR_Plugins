import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";
import { initialView, ND_CENTER } from "./mapview.ts";
import { isDesktop, saveText } from "./save.ts";
import type { GeoJSONFeature } from "./types.ts";

const pt = (lon: number, lat: number): GeoJSONFeature =>
  ({ type: "Feature", properties: {}, geometry: { type: "Point", coordinates: [lon, lat] } }) as GeoJSONFeature;

describe("map opens on the dam, not a fixed regional view", () => {
  it("breach point and dam line first", () => {
    const v = initialView({ breachPoint: pt(-97.39, 46.98), aoi: pt(-97, 46) }, 1, 2);
    assert.ok("fit" in v);
    assert.equal(v.fit.length, 1);
  });
  it("record lat/lon when nothing is drawn", () => {
    assert.deepEqual(initialView(undefined, 46.98364, -97.39064), { center: [46.98364, -97.39064], zoom: 14 });
  });
  it("falls back to ND only with no geometry and no lat/lon", () => {
    assert.deepEqual(initialView({}, undefined, undefined), { center: ND_CENTER, zoom: 7 });
    assert.deepEqual(initialView({}, 0, 0), { center: ND_CENTER, zoom: 7 });
  });
});

describe("exports save through the desktop bridge when it is there", () => {
  const g = globalThis as { window?: unknown };
  afterEach(() => {
    delete g.window;
  });

  it("no window / no bridge: not desktop", () => {
    assert.equal(isDesktop(), false);
    g.window = {};
    assert.equal(isDesktop(), false);
  });

  it("calls pywebview.api.save_file and reports the path, cancel and errors", async () => {
    const calls: [string, string][] = [];
    let reply: object = { saved: "/tmp/nd00199.par" };
    g.window = { pywebview: { api: { save_file: async (n: string, t: string) => (calls.push([n, t]), reply) } } };
    assert.equal(isDesktop(), true);
    assert.deepEqual(await saveText("nd00199.par", "DEMfile x"), { status: "saved", path: "/tmp/nd00199.par" });
    assert.deepEqual(calls, [["nd00199.par", "DEMfile x"]]);
    reply = { cancelled: true };
    assert.deepEqual(await saveText("a.csv", "t"), { status: "cancelled" });
    reply = { error: "disk full" };
    assert.deepEqual(await saveText("a.csv", "t"), { status: "error", message: "disk full" });
  });
});
