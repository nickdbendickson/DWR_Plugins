import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";
import { initialView, ND_CENTER } from "./mapview.ts";
import { isDesktop, saveBytes, saveText, toBase64 } from "./save.ts";
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

  it("view PDF/PNG: sends the bytes as base64 through save_file_b64", async () => {
    const bytes = new Uint8Array(70_000).map((_, i) => (i * 37) & 255); // binary, and bigger than one btoa chunk
    const calls: [string, string][] = [];
    let reply: object = { saved: "/tmp/nd00199_flood_view.pdf" };
    const save_file = async () => ({ error: "text path used for a binary file" });
    g.window = { pywebview: { api: { save_file, save_file_b64: async (n: string, b: string) => (calls.push([n, b]), reply) } } };
    assert.deepEqual(await saveBytes("nd00199_flood_view.pdf", bytes, "application/pdf"), { status: "saved", path: "/tmp/nd00199_flood_view.pdf" });
    assert.equal(calls[0][0], "nd00199_flood_view.pdf");
    assert.deepEqual(Uint8Array.from(Buffer.from(calls[0][1], "base64")), bytes);
    assert.equal(calls[0][1], Buffer.from(bytes).toString("base64"));
    reply = { cancelled: true };
    assert.deepEqual(await saveBytes("a.png", bytes, "image/png"), { status: "cancelled" });
    reply = { error: "disk full" };
    assert.deepEqual(await saveBytes("a.png", bytes, "image/png"), { status: "error", message: "disk full" });
  });

  it("an older desktop app without save_file_b64 says so instead of doing nothing", async () => {
    g.window = { pywebview: { api: { save_file: async () => ({ saved: "/x" }) } } };
    const r = await saveBytes("a.pdf", new Uint8Array([37, 80]), "application/pdf");
    assert.equal(r.status, "error");
    assert.match((r as { message: string }).message, /older than the PDF\/image export/);
  });

  it("toBase64 matches Node's encoder", () => {
    for (const n of [0, 1, 2, 3, 32_768, 32_769]) {
      const b = new Uint8Array(n).map((_, i) => (i * 251) & 255);
      assert.equal(toBase64(b), Buffer.from(b).toString("base64"));
    }
  });
});
