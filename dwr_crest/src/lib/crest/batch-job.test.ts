// What lets a large browser batch be left running (batch-job.ts): the saved job, the pause on a run of network
// failures, the automatic retries, and the cap on records kept in memory.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  FailureWatch,
  PAUSE_AFTER_FAILURES,
  RETRY_AFTER_S,
  RecentRecords,
  isNetworkFailure,
  jobBanner,
  parseBatchJob,
  recoverJob,
  retryDelayMs,
  sameSelection,
  waitText,
  type BatchJob,
} from "./batch-job.ts";
import type { Selection } from "./batch.ts";
import { blankDam } from "./samples.ts";

const SEL: Selection = {
  by: "county",
  ids: [],
  counties: ["Cass", "Barnes"],
  watersheds: [],
  kinds: ["nid", "dam"],
  statuses: ["NID", "Intact"],
};
const JOB: BatchJob = {
  selection: SEL,
  total: 412,
  finished: 130,
  state: "stopped",
  startedAt: "2026-11-13T10:00:00Z",
  updatedAt: "2026-11-13T11:00:00Z",
};
const failed = (id: string, reason: string) => ({ id, status: "failed" as const, reason });
const done = (id: string) => ({ id, status: "done" as const, reason: null });

describe("the saved batch job", () => {
  it("round-trips through storage", () => {
    assert.deepEqual(parseBatchJob(JSON.stringify(JOB)), { ...JOB, pauseReason: undefined });
  });

  it("is refused when it is not a job (storage is outside data)", () => {
    for (const bad of [
      null,
      "",
      "x",
      "[]",
      "{}",
      '{"selection":{"by":"all"}}',
      JSON.stringify({ ...JOB, state: "exploding" }),
      JSON.stringify({ ...JOB, selection: { ...SEL, kinds: [1, 2] } }),
      JSON.stringify({ ...JOB, selection: { ...SEL, counties: "Cass" } }),
    ])
      assert.equal(parseBatchJob(bad), null, String(bad));
    const odd = parseBatchJob(JSON.stringify({ ...JOB, total: -5, finished: "many" }));
    assert.deepEqual([odd?.total, odd?.finished], [0, 0]);
  });

  it("a job cut off mid-run, or mid-pause, comes back stopped with the reason", () => {
    assert.equal(recoverJob(null), null);
    assert.equal(recoverJob(JOB), JOB);
    const cut = recoverJob({ ...JOB, state: "running" })!;
    assert.equal(cut.state, "stopped");
    assert.match(cut.pauseReason!, /closed or reloaded/);
    const paused = recoverJob({
      ...JOB,
      state: "paused",
      pauseReason: "… Trying again by itself in 2 min.",
    })!;
    assert.equal(paused.state, "stopped");
    assert.doesNotMatch(paused.pauseReason!, /Trying again/);
  });

  it("the banner counts what is left, and is silent when nothing is", () => {
    assert.match(
      jobBanner(JOB, 130)!,
      /batch of 412 structures stopped with 130 finished and 282 to go.*Resume/,
    );
    assert.match(
      jobBanner({ ...JOB, state: "paused", pauseReason: "No answer." }, 5_000 - 4_600)!,
      /paused/,
    );
    assert.match(
      jobBanner({ ...JOB, total: 9_435 }, 1_200)!,
      /9,435 structures.*1,200 finished and 8,235 to go/,
    );
    assert.equal(jobBanner(JOB, 412), null);
    assert.equal(jobBanner({ ...JOB, state: "done" }, 0), null);
    assert.equal(jobBanner(null, 0), null);
  });

  it("the same selection is the same whatever order it was ticked in, and only the mode's own list counts", () => {
    assert.ok(sameSelection(SEL, { ...SEL, counties: ["Barnes", "Cass"], kinds: ["dam", "nid"] }));
    assert.ok(!sameSelection(SEL, { ...SEL, counties: ["Cass"] }));
    assert.ok(!sameSelection(SEL, { ...SEL, by: "state" }));
    assert.ok(sameSelection({ ...SEL, by: "state" }, { ...SEL, by: "state", counties: [] }));
    assert.ok(!sameSelection(SEL, { ...SEL, statuses: ["NID"] }));
  });
});

describe("pausing on network failures", () => {
  it("tells a service failure from a failure of the structure itself", () => {
    for (const r of [
      "Could not reach the 3DEP terrain service (Failed to fetch). Connect and build it again.",
      "The 3DEP terrain service answered 502.",
      "The 3DEP terrain service answered 429.",
      "TypeError: Failed to fetch",
      "NetworkError when attempting to fetch resource.",
      "Load failed",
    ])
      assert.ok(isNetworkFailure(failed("a", r)), r);
    for (const r of [
      "The 3DEP terrain service answered 400.",
      "No terrain outline: no breach location.",
      "The 3DEP terrain service returned 12 bytes; expected 400 for 10 × 10.",
      "the channel network could not be traced",
    ])
      assert.ok(!isNetworkFailure(failed("a", r)), r);
    assert.ok(!isNetworkFailure({ status: "done", reason: null }));
    assert.ok(!isNetworkFailure({ status: "needs-data", reason: "no height" }));
  });

  it("pauses after that many in a row, and a structure that got through resets the count", () => {
    const w = new FailureWatch();
    const down = "Could not reach the 3DEP terrain service (Failed to fetch).";
    for (let i = 0; i < PAUSE_AFTER_FAILURES - 1; i++)
      assert.equal(w.add(failed(`a${i}`, down)), false);
    assert.equal(w.add(done("ok")), false);
    assert.deepEqual(w.failedIds, []);
    for (let i = 0; i < PAUSE_AFTER_FAILURES - 1; i++)
      assert.equal(w.add(failed(`b${i}`, down)), false);
    // A structure with no height never called the service: it neither counts nor resets.
    assert.equal(w.add({ id: "nodata", status: "needs-data", reason: "no height" }), false);
    // A failure of the structure itself is not a service failure: it resets.
    assert.equal(w.add(failed("own", "No terrain outline: no breach location.")), false);
    for (let i = 0; i < PAUSE_AFTER_FAILURES - 1; i++)
      assert.equal(w.add(failed(`c${i}`, down)), false);
    assert.equal(w.add(failed("last", down)), true);
    assert.ok(w.tripped);
    assert.equal(w.failedIds.length, PAUSE_AFTER_FAILURES);
  });

  it("retries by itself with growing waits, then stays paused", () => {
    assert.deepEqual(
      RETRY_AFTER_S.map((_, i) => retryDelayMs(i)),
      RETRY_AFTER_S.map((s) => s * 1000),
    );
    assert.ok(RETRY_AFTER_S.every((s, i) => i === 0 || s > RETRY_AFTER_S[i - 1]));
    assert.equal(retryDelayMs(RETRY_AFTER_S.length), null);
    assert.equal(retryDelayMs(-1), null);
    assert.equal(waitText(60_000), "60 s");
    assert.equal(waitText(300_000), "5 min");
  });
});

describe("records kept for Open in desk", () => {
  it("keeps the most recent, drops the oldest past the cap", () => {
    const r = new RecentRecords(3);
    for (const id of ["a", "b", "c"]) r.set(id, { ...blankDam(), id });
    r.set("a", { ...blankDam(), id: "a" }); // touched again: now the newest
    r.set("d", { ...blankDam(), id: "d" });
    assert.equal(r.size, 3);
    assert.ok(!r.has("b") && r.has("a") && r.has("c") && r.has("d"));
    r.clear();
    assert.equal(r.size, 0);
  });
});
