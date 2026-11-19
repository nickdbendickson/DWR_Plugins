import assert from "node:assert/strict";
import { test } from "node:test";
import { BASEMAPS, basemaps } from "./basemap.ts";

test("default basemap is key-free USGS Topo, with OSM as an alternate", () => {
  const [first] = basemaps(undefined);
  assert.equal(first.id, "usgs-topo");
  assert.match(first.url, /^https:\/\/basemap\.nationalmap\.gov\/.+\{z\}\/\{y\}\/\{x\}$/);
  assert.ok(BASEMAPS.some((b) => b.url.startsWith("https://tile.openstreetmap.org/")));
  for (const b of BASEMAPS) {
    assert.ok(b.attribution.length > 0, b.id);
    assert.doesNotMatch(b.url, /cartocdn|api[_-]?key|token/i, b.id);
  }
});

test("VITE_CREST_TILE_URL puts a configured layer first; a malformed one is ignored", () => {
  const list = basemaps({ VITE_CREST_TILE_URL: "http://tiles.local/{z}/{x}/{y}.png", VITE_CREST_TILE_ATTRIBUTION: "Local" });
  assert.equal(list[0].url, "http://tiles.local/{z}/{x}/{y}.png");
  assert.equal(list[0].attribution, "Local");
  assert.equal(list.length, BASEMAPS.length + 1);
  assert.deepEqual(basemaps({ VITE_CREST_TILE_URL: "http://tiles.local/{z}.png" }), BASEMAPS);
});
