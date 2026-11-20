// 3DHP waterbody connectors: the query, parsing a page, paging, and the cells burned for the trace.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { burnMask, fetchFlowlines, FLOWLINE_SERVICE, flowlineUrl, parseFlowlines, WATERBODY_CONNECTOR } from "./flowlines.ts";

describe("flowlineUrl and parseFlowlines", () => {
  it("asks layer 50 for waterbody connectors in the box, as GeoJSON in WGS 84", () => {
    const u = new URL(flowlineUrl([-84.4, 43.6, -84.2, 43.8], 2000));
    assert.equal(`${u.origin}${u.pathname}`, `${FLOWLINE_SERVICE}/query`);
    assert.equal(u.searchParams.get("where"), `featuretype = ${WATERBODY_CONNECTOR}`);
    assert.equal(u.searchParams.get("geometry"), "-84.4,43.6,-84.2,43.8");
    assert.equal(u.searchParams.get("outSR"), "4326");
    assert.equal(u.searchParams.get("resultOffset"), "2000");
    assert.equal(u.searchParams.get("f"), "geojson");
  });
  it("reads LineStrings and MultiLineStrings, drops junk, and reads the transfer flag either way", () => {
    const page = {
      features: [
        { geometry: { type: "LineString", coordinates: [[0, 0], [1, 1]] } },
        { geometry: { type: "MultiLineString", coordinates: [[[2, 2], [3, 3]], [[4, 4]]] } },
        { geometry: { type: "Point", coordinates: [5, 5] } },
        { geometry: null },
        { geometry: { type: "LineString", coordinates: [[0, 0], ["x", 1], [2, 0]] } },
      ],
      exceededTransferLimit: true,
    };
    const r = parseFlowlines(page);
    assert.deepEqual(r.lines, [[[0, 0], [1, 1]], [[2, 2], [3, 3]], [[0, 0], [2, 0]]]);
    assert.ok(r.more);
    assert.ok(parseFlowlines({ features: [], properties: { exceededTransferLimit: true } }).more);
    assert.deepEqual(parseFlowlines(null), { lines: [], more: false });
  });
});

describe("fetchFlowlines", () => {
  it("reads page after page while the service says more remain, and throws on a failed read", async () => {
    const urls: string[] = [];
    const pages = [{ features: [{ geometry: { type: "LineString", coordinates: [[0, 0], [1, 1]] } }], exceededTransferLimit: true }, { features: [{ geometry: { type: "LineString", coordinates: [[1, 1], [2, 2]] } }] }];
    const lines = await fetchFlowlines([0, 0, 1, 1], async (url) => {
      urls.push(url);
      return { ok: true, status: 200, json: async () => pages[urls.length - 1] };
    });
    assert.equal(lines.length, 2);
    assert.equal(urls.length, 2);
    assert.equal(new URL(urls[1]).searchParams.get("resultOffset"), "2000");
    await assert.rejects(fetchFlowlines([0, 0, 1, 1], async () => ({ ok: false, status: 429, json: async () => ({}) })), /answered 429/);
  });
});

describe("burnMask", () => {
  // 20 × 10 cells of 0.001°: a flat lake (z = 100) in columns 5..14, rising ground either side.
  const W = 20;
  const H = 10;
  const z = new Float32Array(W * H);
  for (let j = 0; j < H; j++) for (let i = 0; i < W; i++) z[j * W + i] = i >= 5 && i <= 14 ? 100 : 100 + Math.abs(i < 5 ? 5 - i : i - 14) * 0.5;
  const g = { bbox: [0, 0, 0.02, 0.01] as [number, number, number, number], width: W, height: H, z };
  it("marks a connected run of flat water cells along the line, never the banks", () => {
    const m = burnMask(g, [[[0.0005, 0.0055], [0.0195, 0.0055]]]);
    assert.ok(m);
    if (!m) return;
    const row = 4; // lat 0.0055 is the fifth row from the north
    const marked = [...m].map((v, k) => (v ? k : -1)).filter((k) => k >= 0);
    // Flat cells: those whose every neighbour is also flat water, so columns 6..13 on that row.
    assert.deepEqual(marked, Array.from({ length: 8 }, (_, d) => row * W + 6 + d));
  });
  it("a diagonal line marks an 8-connected run; nothing flat crossed is null", () => {
    const m = burnMask(g, [[[0.0065, 0.0085], [0.0135, 0.0015]]]);
    assert.ok(m);
    if (!m) return;
    const cells = [...m].map((v, k) => (v ? k : -1)).filter((k) => k >= 0);
    for (let a = 1; a < cells.length; a++) {
      const [i0, j0] = [cells[a - 1] % W, Math.floor(cells[a - 1] / W)];
      const near = cells.some((c) => c !== cells[a - 1] && Math.abs((c % W) - i0) <= 1 && Math.abs(Math.floor(c / W) - j0) <= 1);
      assert.ok(near, `cell ${cells[a - 1]} has a marked neighbour`);
    }
    assert.equal(burnMask(g, [[[0.0005, 0.0005], [0.0035, 0.0095]]]), null);
  });
});
