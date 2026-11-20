// The data bundles on demand (data-bundle.ts): the JSON modules here, the packed copies in the one-file build.
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { gzipSync } from "node:zlib";

import { inflateBase64Gzip, loadBundle } from "./data-bundle.ts";

describe("data-bundle", () => {
  it("inflates what the single-file build packs (gzip + base64) back to the same text", async () => {
    const text = JSON.stringify({ cols: ["id"], rows: [["DWR-1"], ["DUG-2"]], source: { note: "é" } });
    const b64 = gzipSync(Buffer.from(text, "utf8"), { level: 9 }).toString("base64");
    assert.equal(await inflateBase64Gzip(b64), text);
  });

  it("throws on a corrupt payload rather than handing back nothing", async () => {
    await assert.rejects(inflateBase64Gzip("not base64 at all!!"));
  });

  it("loads the committed structures bundle once, from its JSON module (no packed table in Node)", async () => {
    const a = await loadBundle<{ cols: string[]; rows: unknown[][] }>("nd_structures");
    const b = await loadBundle<{ cols: string[]; rows: unknown[][] }>("nd_structures");
    assert.equal(a, b, "cached");
    assert.ok(a.cols.includes("id") && a.rows.length > 10000);
    const d = await loadBundle<{ rows: unknown[][] }>("nd_dams");
    assert.ok(d.rows.length > 800);
  });
});
