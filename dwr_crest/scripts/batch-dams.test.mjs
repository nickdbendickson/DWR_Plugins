// The batch command line's resume file (scripts/batch-dams.mjs): a batch.jsonl cut short by a kill mid-append is
// resumed, a corrupted middle line stops the run with its line number, and every appended row is a whole fsynced line.
//
// batch-dams.mjs imports the TypeScript engine, which plain `node --test` cannot load, so each check runs the script's
// exports in a child node with --experimental-strip-types (the flag `npm run batch:dams` itself uses).
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import { tempDir } from "./test-tmp.mjs";

const SCRIPT = join(dirname(fileURLToPath(import.meta.url)), "batch-dams.mjs");

/** Run `body` (JS source) in a child node that has `m` = the script's exports and `argv` = JSON args; returns stdout JSON. */
function inChild(body, ...args) {
  const src = `
    import * as m from ${JSON.stringify(SCRIPT)};
    const argv = ${JSON.stringify(args)};
    const out = await (async () => { ${body} })();
    process.stdout.write(JSON.stringify(out ?? null));
  `;
  const r = spawnSync(process.execPath, ["--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", src], { encoding: "utf8" });
  if (r.status !== 0) throw new Error(`child failed: ${r.stderr}`);
  return JSON.parse(r.stdout);
}

const row = (id, status = "done") => ({ id, status, reason: null, seconds: 1 });

describe("batch.jsonl resume parsing", () => {
  it("drops a cut-short last line with a warning and keeps every whole row", () => {
    const whole = [row("A"), row("B", "failed")];
    const text = whole.map((r) => JSON.stringify(r) + "\n").join("") + JSON.stringify(row("C")).slice(0, 17);
    const out = inChild(
      `const warned = []; const rows = m.parseJsonl(argv[0], (s) => warned.push(s)); return { rows, warned };`,
      text,
    );
    assert.deepEqual(out.rows, whole);
    assert.equal(out.warned.length, 1);
    assert.match(out.warned[0], /last line \(3\) is cut short/);
    assert.match(out.warned[0], /runs again/);
  });

  it("reads a clean file, with or without the final newline, and skips blank lines", () => {
    const whole = [row("A"), row("B")];
    const clean = whole.map((r) => JSON.stringify(r)).join("\n");
    assert.deepEqual(inChild(`return m.parseJsonl(argv[0]);`, clean + "\n"), whole);
    assert.deepEqual(inChild(`return m.parseJsonl(argv[0]);`, clean), whole);
    assert.deepEqual(inChild(`return m.parseJsonl(argv[0]);`, clean.replace("\n", "\n\n") + "\n"), whole);
    assert.deepEqual(inChild(`return m.parseJsonl(argv[0]);`, ""), []);
  });

  it("stops on a corrupt MIDDLE line and names it (never silently reruns or skips rows)", () => {
    const text = [JSON.stringify(row("A")), '{"id":"B","status":', JSON.stringify(row("C"))].join("\n") + "\n";
    const out = inChild(
      `try { m.parseJsonl(argv[0], () => {}); return { threw: false }; } catch (e) { return { threw: true, message: e.message }; }`,
      text,
    );
    assert.equal(out.threw, true);
    assert.match(out.message, /line 2 is not JSON/);
    assert.match(out.message, /edited or corrupted/);
  });
});

describe("appendRow", () => {
  it("appends one whole JSON line per row (fsynced), which parseJsonl reads back", () => {
    const dir = tempDir("batch-dams-");
    const file = join(dir, "batch.jsonl");
    writeFileSync(file, "");
    const rows = [row("A"), row("B", "needs-data")];
    const back = inChild(
      `const { readFileSync } = await import("node:fs"); for (const r of argv[1]) m.appendRow(argv[0], r); return m.parseJsonl(readFileSync(argv[0], "utf8"));`,
      file,
      rows,
    );
    assert.deepEqual(back, rows);
    const text = readFileSync(file, "utf8");
    assert.ok(text.endsWith("\n"));
    assert.equal(text.split("\n").length, rows.length + 1);
  });
});

describe("resume skip-set from a truncated file (the fix end to end)", () => {
  it("a done row is skipped, a failed row and the torn one are rerun", () => {
    const text = JSON.stringify(row("A")) + "\n" + JSON.stringify(row("B", "failed")) + "\n" + '{"id":"C","sta';
    const out = inChild(
      `const rows = m.parseJsonl(argv[0], () => {});
       const skip = [...new Set(rows.filter((r) => r.status !== "failed").map((r) => r.id))];
       return { skip, retry: rows.filter((r) => r.status === "failed").map((r) => r.id) };`,
      text,
    );
    assert.deepEqual(out, { skip: ["A"], retry: ["B"] });
  });
});
