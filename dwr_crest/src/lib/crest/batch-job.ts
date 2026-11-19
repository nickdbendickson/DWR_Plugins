/**
 * What makes a browser batch of hundreds or thousands of structures safe to leave running (2026-11-13). Before this
 * the batch screen kept its rows but not what was being run: after a closed tab or a crash the engineer had to
 * rebuild the same selection by hand before Resume lit up, and a dropped connection turned every remaining structure
 * into a "failed" row. Above 300 structures the screen sent you to the command line instead.
 *
 *   BatchJob        the selection being run and how far it got, in localStorage (a few hundred bytes), so the screen
 *                   reopens on the same selection with one Resume button.
 *   FailureWatch    counts terrain / network failures in a row; at the limit the batch pauses instead of failing
 *                   everything left. Rows that failed that way are retried on resume, never kept as results.
 *   retryDelayMs    the waits before a paused batch tries again by itself (1, 2, 5, 10, 15, 30 min), so an unattended
 *                   run rides out a service outage; after the last wait it stays paused until Resume is pressed.
 *   RecentRecords   the finished records kept for "Open in desk", capped, so a statewide run does not hold thousands
 *                   of records in memory; an older row's record is rebuilt on demand.
 *
 * Pure (localStorage aside), so all of it is tested without a browser.
 */
import type { BatchRow, Selection } from "./batch.ts";
import type { DamRecord } from "./types.ts";

const JOB_KEY = "crest-batch-job-v1";

export type BatchJobState = "running" | "stopped" | "paused" | "done";

export type BatchJob = {
  selection: Selection;
  /** Structures in the selection when the batch started. */
  total: number;
  /** Rows finished and kept (done or needs-data), as of the last save. */
  finished: number;
  state: BatchJobState;
  /** Why it paused, in a sentence. */
  pauseReason?: string;
  startedAt: string;
  updatedAt: string;
};

const BY = ["pick", "county", "watershed", "state"];
const strings = (v: unknown, max: number): string[] | null =>
  Array.isArray(v) && v.length <= max && v.every((x) => typeof x === "string" && x.length <= 200)
    ? (v as string[])
    : null;

/** A job read back from storage, or null when it is missing or not a job (storage is outside data: checked, not cast). */
export function parseBatchJob(raw: string | null | undefined): BatchJob | null {
  if (!raw) return null;
  try {
    const j = JSON.parse(raw) as Record<string, unknown>;
    const s = j?.selection as Record<string, unknown> | undefined;
    if (!s || typeof s !== "object" || !BY.includes(s.by as string)) return null;
    const kinds = strings(s.kinds, 10);
    const statuses = strings(s.statuses, 50);
    // A statewide pick list can name every structure; anything larger than the inventories is not ours.
    const ids = s.ids === undefined ? [] : strings(s.ids, 50_000);
    const counties = s.counties === undefined ? [] : strings(s.counties, 200);
    const watersheds = s.watersheds === undefined ? [] : strings(s.watersheds, 500);
    if (!kinds || !statuses || !ids || !counties || !watersheds) return null;
    if (!["running", "stopped", "paused", "done"].includes(j.state as string)) return null;
    const n = (v: unknown) =>
      typeof v === "number" && Number.isFinite(v) && v >= 0 ? Math.floor(v) : 0;
    return {
      selection: {
        by: s.by as Selection["by"],
        ids,
        counties,
        watersheds,
        kinds: kinds as Selection["kinds"],
        statuses,
      },
      total: n(j.total),
      finished: n(j.finished),
      state: j.state as BatchJobState,
      pauseReason: typeof j.pauseReason === "string" ? j.pauseReason.slice(0, 400) : undefined,
      startedAt: typeof j.startedAt === "string" ? j.startedAt : "",
      updatedAt: typeof j.updatedAt === "string" ? j.updatedAt : "",
    };
  } catch {
    return null;
  }
}

export function loadBatchJob(): BatchJob | null {
  try {
    return parseBatchJob(globalThis.localStorage?.getItem(JOB_KEY));
  } catch {
    return null;
  }
}

export function saveBatchJob(job: BatchJob): void {
  try {
    globalThis.localStorage?.setItem(JOB_KEY, JSON.stringify(job));
  } catch {
    // Storage full or blocked: the rows are still kept (IndexedDB); only the one-click resume is lost.
  }
}

export function clearBatchJob(): void {
  try {
    globalThis.localStorage?.removeItem(JOB_KEY);
  } catch {
    /* nothing to remove */
  }
}

/**
 * A job as found when the page opens. One left "running" was cut off (the tab closed, crashed or reloaded mid-run);
 * one left "paused" had a retry timer that died with the page. Both are stopped jobs with a reason, waiting for
 * Resume. Anything else is returned as it is.
 */
export function recoverJob(job: BatchJob | null): BatchJob | null {
  if (!job) return job;
  if (job.state === "running")
    return {
      ...job,
      state: "stopped",
      pauseReason: "The page was closed or reloaded while this batch was running.",
    };
  if (job.state === "paused")
    return {
      ...job,
      state: "stopped",
      pauseReason: "It had paused because the terrain service was not answering.",
    };
  return job;
}

/** True when two selections pick the same structures (order of the ticked values does not matter). */
export function sameSelection(a: Selection, b: Selection): boolean {
  const eq = (x: readonly string[] = [], y: readonly string[] = []) =>
    x.length === y.length && [...x].sort().join("\n") === [...y].sort().join("\n");
  if (a.by !== b.by || !eq(a.kinds, b.kinds) || !eq(a.statuses, b.statuses)) return false;
  // Only the list the mode uses matters: counties ticked earlier do not change a statewide batch.
  return a.by === "pick"
    ? eq(a.ids, b.ids)
    : a.by === "county"
      ? eq(a.counties, b.counties)
      : a.by === "watershed"
        ? eq(a.watersheds, b.watersheds)
        : true;
}

/** The line over the batch screen for a job that can be picked up; null when there is nothing to resume. */
export function jobBanner(job: BatchJob | null, keptRows: number): string | null {
  if (!job || job.state === "done" || job.total <= 0) return null;
  const left = Math.max(0, job.total - keptRows);
  if (left === 0) return null;
  const n = (v: number) => v.toLocaleString("en-US");
  const why = job.state === "paused" ? "paused" : "stopped";
  return `A batch of ${n(job.total)} structures ${why} with ${n(keptRows)} finished and ${n(left)} to go.${job.pauseReason ? ` ${job.pauseReason}` : ""} The selection below is the one it was running; Resume carries on from where it stopped.`;
}

/**
 * True when a row failed because a service could not be reached (3DEP down or answering 5xx, no connection), not
 * because of the structure itself. Such a failure says nothing about the dam, so it counts toward a pause and the row
 * is retried.
 */
export function isNetworkFailure(row: Pick<BatchRow, "status" | "reason">): boolean {
  if (row.status !== "failed") return false;
  return /Could not reach|service answered (5\d\d|429)|Failed to fetch|NetworkError|Load failed|ERR_INTERNET_DISCONNECTED|\boffline\b/i.test(
    row.reason ?? "",
  );
}

/** Network failures in a row before the batch pauses. One structure in flight per lane can fail at once, hence not 2. */
export const PAUSE_AFTER_FAILURES = 6;

/** Counts network failures in a row; any row that reached the services resets it. */
export class FailureWatch {
  private run = 0;
  private readonly limit: number;
  /** Ids of the rows in the current run of failures: retried on resume, not kept. */
  readonly failedIds: string[] = [];

  constructor(limit = PAUSE_AFTER_FAILURES) {
    this.limit = limit;
  }

  /** Record a finished row; true when the batch should pause now. */
  add(row: Pick<BatchRow, "id" | "status" | "reason">): boolean {
    if (isNetworkFailure(row)) {
      this.run++;
      this.failedIds.push(row.id);
    } else if (row.status !== "needs-data") {
      // A needs-data row never called a service, so it says nothing either way.
      this.run = 0;
      this.failedIds.length = 0;
    }
    return this.run >= this.limit;
  }

  get tripped(): boolean {
    return this.run >= this.limit;
  }
}

/** Waits before a paused batch tries again by itself. */
export const RETRY_AFTER_S = [60, 120, 300, 600, 900, 1800];

/** Milliseconds before automatic retry number `attempt` (0-based); null when the retries are used up. */
export function retryDelayMs(attempt: number): number | null {
  return attempt >= 0 && attempt < RETRY_AFTER_S.length ? RETRY_AFTER_S[attempt] * 1000 : null;
}

/** "about 5 min" for the pause line. */
export function waitText(ms: number): string {
  const s = Math.round(ms / 1000);
  return s < 90 ? `${s} s` : `${Math.round(s / 60)} min`;
}

/** How many finished records are kept in memory for "Open in desk". */
export const RECENT_RECORDS = 300;

/** The most recent finished records, oldest dropped first once past the cap. */
export class RecentRecords {
  private readonly map = new Map<string, DamRecord>();
  private readonly cap: number;

  constructor(cap = RECENT_RECORDS) {
    this.cap = cap;
  }

  set(id: string, dam: DamRecord): void {
    this.map.delete(id);
    this.map.set(id, dam);
    while (this.map.size > this.cap) this.map.delete(this.map.keys().next().value as string);
  }

  get(id: string): DamRecord | undefined {
    return this.map.get(id);
  }

  has(id: string): boolean {
    return this.map.has(id);
  }

  clear(): void {
    this.map.clear();
  }

  get size(): number {
    return this.map.size;
  }
}
