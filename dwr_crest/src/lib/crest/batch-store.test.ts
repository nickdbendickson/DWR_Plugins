// The batch screen's row store (batch-store.ts): rows are written in batches, never one write per row.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { type BatchRow, insertRow, sortRows } from "./batch.ts";
import { BatchRowBuffer, FLUSH_MS, FLUSH_ROWS, loadBatchRows, loadBatchSummary, putBatchRows } from "./batch-store.ts";

const row = (id: string, over: Partial<BatchRow> = {}): BatchRow =>
  ({
    id,
    kind: "dam",
    label: id,
    county: "Cass",
    huc8: null,
    status: "done",
    reason: null,
    heightFt: 10,
    storageAcFt: 50,
    crestFt: 1000,
    crestFrom: "3DEP",
    bearingDeg: 180,
    releasedAcFt: 50,
    peakCfs: 1000,
    layer: "outline",
    floodedAc: 10,
    screen: "Low",
    screenLabel: "Low",
    structuresInside: 0,
    structuresFlooded: 0,
    triage: { recommendation: "trivial", label: "Clearly trivial: screen only", confidence: "high", edgeShiftM: 20, summary: "s" },
    seconds: 1,
    ...over,
  }) as BatchRow;

/** A fake clock: timers fire when `tick` is called. */
function clock() {
  const due: { at: number; fn: () => void; id: number }[] = [];
  let now = 0;
  let n = 0;
  return {
    timers: {
      set: ((fn: () => void, ms: number) => {
        due.push({ at: now + ms, fn, id: ++n });
        return n as unknown as ReturnType<typeof setTimeout>;
      }) as typeof setTimeout,
      clear: ((id: unknown) => {
        const i = due.findIndex((d) => d.id === (id as number));
        if (i >= 0) due.splice(i, 1);
      }) as typeof clearTimeout,
    },
    tick(ms: number) {
      now += ms;
      for (const d of due.splice(0).filter((d) => (d.at <= now ? (d.fn(), false) : true))) due.push(d);
    },
  };
}

describe("BatchRowBuffer", () => {
  it("writes once after FLUSH_ROWS rows, not once per row", async () => {
    const writes: BatchRow[][] = [];
    const c = clock();
    const buf = new BatchRowBuffer({ write: async (r) => void writes.push(r), timers: c.timers });
    for (let i = 0; i < FLUSH_ROWS - 1; i++) buf.add(row(`R${i}`));
    assert.equal(writes.length, 0);
    assert.equal(buf.unwritten, FLUSH_ROWS - 1);
    buf.add(row("last"));
    await Promise.resolve();
    assert.equal(writes.length, 1);
    assert.equal(writes[0].length, FLUSH_ROWS);
    assert.equal(buf.unwritten, 0);
  });

  it("writes a trickle of rows after FLUSH_MS, and flush() writes what is left at the end", async () => {
    const writes: BatchRow[][] = [];
    const c = clock();
    const buf = new BatchRowBuffer({ write: async (r) => void writes.push(r), timers: c.timers });
    buf.add(row("A"));
    buf.add(row("B"));
    c.tick(FLUSH_MS - 1);
    assert.equal(writes.length, 0);
    c.tick(1);
    await Promise.resolve();
    assert.deepEqual(writes.map((w) => w.map((r) => r.id)), [["A", "B"]]);
    buf.add(row("C"));
    await buf.flush();
    assert.deepEqual(writes[1].map((r) => r.id), ["C"]);
    await buf.flush();
    assert.equal(writes.length, 2, "an empty flush writes nothing");
    assert.equal(buf.writes, 2);
  });
});

describe("insertRow keeps the worklist order without a re-sort", () => {
  it("places each new row where sortRows would, and replaces a retried dam's row", () => {
    const rows = [
      row("T"),
      row("N", { status: "needs-data", triage: null, screen: null, reason: "No height in the inventory." }),
      row("R", { triage: { recommendation: "needs-runner", label: "Needs a Runner run", confidence: "low", edgeShiftM: 120, summary: "s" }, releasedAcFt: 900 }),
      row("U", { triage: { recommendation: "uncertain", label: "Uncertain: run it", confidence: "medium", edgeShiftM: 90, summary: "s" } }),
      row("F", { status: "failed", triage: null, screen: null, reason: "3DEP down" }),
    ];
    let acc: BatchRow[] = [];
    for (const r of rows) acc = insertRow(acc, r);
    assert.deepEqual(acc.map((r) => r.id), sortRows(rows).map((r) => r.id));
    const again = insertRow(acc, row("F", { releasedAcFt: 10 }));
    assert.equal(again.length, acc.length);
    assert.deepEqual(again.map((r) => r.id), sortRows([...rows.filter((r) => r.id !== "F"), row("F", { releasedAcFt: 10 })]).map((r) => r.id));
  });
});

describe("without IndexedDB or localStorage (Node)", () => {
  it("the store is empty and writes are no-ops, so the batch still runs", async () => {
    assert.deepEqual(await loadBatchRows(), []);
    await putBatchRows([row("A")]);
    assert.deepEqual(await loadBatchRows(), []);
    assert.equal(loadBatchSummary(), null);
  });
});
