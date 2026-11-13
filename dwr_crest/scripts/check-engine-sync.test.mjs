// The cross-repo engine sync check (scripts/check-engine-sync.mjs): allow-listed line pairs pass, any other
// difference fails with its line number, and an allow entry whose line has gone is reported so the list cannot rot.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { compareFile, loadAllowList, selfRepo } from "./check-engine-sync.mjs";

const allow = [
  {
    file: "engine.ts",
    crest: 'import { a } from "./vertcon_nd.ts";',
    damit: 'import { a } from "./vertcon.ts";',
  },
];

describe("engine sync compare", () => {
  it("passes identical files and allow-listed lines", () => {
    assert.equal(compareFile("spread.ts", "x\ny\n", "x\ny\n", allow).ok, true);
    const c = 'import { a } from "./vertcon_nd.ts";\nconst k = 1;\n';
    const d = 'import { a } from "./vertcon.ts";\nconst k = 1;\n';
    assert.deepEqual(compareFile("engine.ts", c, d, allow), { ok: true, problems: [] });
  });

  it("fails a difference that is not allowed, naming the line", () => {
    const c = 'import { a } from "./vertcon_nd.ts";\nconst k = 1;\n';
    const d = 'import { a } from "./vertcon.ts";\nconst k = 2;\n';
    const r = compareFile("engine.ts", c, d, allow);
    assert.equal(r.ok, false);
    assert.match(r.problems[0], /engine\.ts:2 differs/);
  });

  it("fails a line added at the end of one copy", () => {
    const r = compareFile("routing.ts", "x", "x\ny", []);
    assert.equal(r.ok, false);
    assert.match(r.problems[0], /\(end of file\)/);
  });

  it("reports a stale allow entry", () => {
    const r = compareFile("engine.ts", "same\n", "same\n", allow);
    assert.equal(r.ok, false);
    assert.match(r.problems.join("\n"), /stale allow entry/);
  });

  it("only allows a pair in the file it names", () => {
    const c = 'import { a } from "./vertcon_nd.ts";\n';
    const d = 'import { a } from "./vertcon.ts";\n';
    assert.equal(compareFile("spread.ts", c, d, allow).ok, false);
  });

  it("covers the four engine files and knows which repo it is in", () => {
    const { files, allow: list } = loadAllowList();
    for (const f of ["engine.ts", "spread.ts", "outline.ts", "routing.ts"])
      assert.ok(files.includes(f), f);
    for (const a of list) assert.ok(a.why, `allow entry in ${a.file} has no reason`);
    assert.equal(selfRepo("/x/crest/scripts"), "crest");
    assert.equal(selfRepo("/x/damit/damit_crest/scripts"), "damit");
  });
});
