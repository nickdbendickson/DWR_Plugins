/**
 * Batch screen: pick many structures (one by one, by county, by watershed, or statewide) and run each through the
 * desk's own build + triage (src/lib/crest/batch.ts). Results are a Runner worklist: needs-runner first.
 *
 * The builds run on a pool of inundation workers (batch.ts runBatchInBrowser), so the tab stays live. Only the small
 * result rows are kept, in IndexedDB in batches (batch-store.ts); the most recent finished records live in memory for
 * "Open in desk" WITHOUT their terrain layer (it is rebuilt on the desk in one click), and an older row's record is
 * rebuilt when asked for, so a statewide batch does not hold thousands of records.
 *
 * A batch of any size can be left running, stopped, or lost to a closed tab and picked up again (batch-job.ts): the
 * selection being run is saved, so the screen reopens on it with Resume; a run of terrain / network failures pauses
 * the batch instead of failing everything left, and it tries again by itself; the rows are written before the page
 * goes away.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  BATCH_DISCLAIMER,
  BROWSER_BATCH_WARN,
  DEFAULT_STATUSES,
  KIND_LABEL,
  batchCsv,
  batchCsvMeta,
  facets,
  insertRow,
  loadStructures,
  missingData,
  runBatchInBrowser,
  runStructure,
  runningEstimate,
  selectStructures,
  sortRows,
  type BatchRow,
  type Selection,
  type Structure,
  type StructureKind,
} from "@/lib/crest/batch";
import { applyDefaults } from "@/lib/crest/settings";
import { sharedNow } from "@/lib/crest/settings-store";
import {
  FailureWatch,
  RecentRecords,
  clearBatchJob,
  jobBanner,
  sameSelection,
  loadBatchJob,
  recoverJob,
  retryDelayMs,
  saveBatchJob,
  waitText,
  type BatchJob,
} from "@/lib/crest/batch-job";
import {
  BatchRowBuffer,
  clearBatchRows,
  clearBatchSummary,
  loadBatchRows,
  loadBatchSummary,
  saveBatchSummary,
} from "@/lib/crest/batch-store";
import { poolSize } from "@/lib/crest/inundation-client";
import { fold, type NdData } from "@/lib/crest/nd-dams";
import { saveText } from "@/lib/crest/save";
import { useDesk } from "@/lib/crest/store";
import { BatchTable } from "./BatchTable";
import { TerrainSource } from "./TerrainSource";

const btn =
  "inline-flex h-11 items-center rounded-sm border border-line bg-card px-4 text-sm text-ink disabled:opacity-50";
const primary =
  "inline-flex h-11 items-center rounded-sm border border-water bg-water px-4 text-sm text-card disabled:opacity-50";
const KINDS: StructureKind[] = ["nid", "dam", "dugout", "unknown"];
const BY: { id: Selection["by"]; label: string }[] = [
  { id: "pick", label: "Pick structures" },
  { id: "county", label: "County" },
  { id: "watershed", label: "Watershed (HUC8)" },
  { id: "state", label: "Statewide" },
];

/** Rows the previous version kept in localStorage (crest-batch-v1): read once and moved to IndexedDB. */
const OLD_ROWS_KEY = "crest-batch-v1";
function takeOldRows(): BatchRow[] {
  try {
    const raw = localStorage.getItem(OLD_ROWS_KEY);
    if (!raw) return [];
    localStorage.removeItem(OLD_ROWS_KEY);
    const v = JSON.parse(raw) as unknown;
    return Array.isArray(v) ? (v as BatchRow[]) : [];
  } catch {
    return [];
  }
}

const fmtTime = (s: number) =>
  s < 90
    ? `${Math.round(s)} s`
    : s < 5400
      ? `${Math.round(s / 60)} min`
      : `${(s / 3600).toFixed(1)} h`;

export function BatchScreen({ onOpen }: { onOpen: (note?: string) => void }) {
  const addDam = useDesk((s) => s.addDam);
  const deskDams = useDesk((s) => s.dams);
  const selectDam = useDesk((s) => s.select);
  const [data, setData] = useState<{
    nd: NdData;
    structures: Structure[];
    source: Record<string, unknown>;
  } | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  // A batch that was stopped, paused or cut off by a closed tab: the screen reopens on the selection it was running.
  const [job, setJob] = useState<BatchJob | null>(() => recoverJob(loadBatchJob()));
  const was = job && job.state !== "done" ? job.selection : null;
  const [by, setBy] = useState<Selection["by"]>(was?.by ?? "county");
  const [kinds, setKinds] = useState<StructureKind[]>(was?.kinds ?? KINDS);
  const [statuses, setStatuses] = useState<string[]>(was?.statuses ?? [...DEFAULT_STATUSES]);
  const [ids, setIds] = useState<string[]>(was?.ids ?? []);
  const [counties, setCounties] = useState<string[]>(was?.counties ?? []);
  const [watersheds, setWatersheds] = useState<string[]>(was?.watersheds ?? []);
  const [query, setQuery] = useState("");
  /** The rows in worklist order (sortRows); a new row is inserted in place, not re-sorted. */
  const [sorted, setSorted] = useState<BatchRow[]>([]);
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<{ done: number; total: number; status: string } | null>(
    null,
  );
  const [saved, setSaved] = useState<string | null>(null);
  const [summary] = useState(() => loadBatchSummary());
  const ctl = useRef<AbortController | null>(null);
  /** The latest finished records for "Open in desk", without their inundation layer (runBatchInBrowser drops it). */
  const dams = useRef(new RecentRecords());
  /** The running batch's row buffer and rows so far, for the write when the page is hidden or closed. */
  const live = useRef<{ buf: BatchRowBuffer; rows: () => BatchRow[]; job: BatchJob } | null>(null);
  /** Automatic retries used since the batch last made progress, and the pending retry. */
  const retries = useRef(0);
  const retryTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const startRef = useRef<(resume: boolean) => Promise<void>>(async () => {});
  const [opening, setOpening] = useState<string | null>(null);
  const lanes = useMemo(() => poolSize(), []);

  useEffect(() => {
    let live = true;
    void (async () => {
      const old = takeOldRows();
      const kept = await loadBatchRows();
      if (!live) return;
      const all = sortRows([...kept, ...old.filter((r) => !kept.some((k) => k.id === r.id))]);
      setSorted(all);
      if (old.length) {
        const buf = new BatchRowBuffer();
        for (const r of old) buf.add(r);
        await buf.flush();
      }
    })();
    loadStructures()
      .then((d) => setData(d))
      .catch((e) => setLoadErr((e as Error).message));
    return () => {
      live = false;
      ctl.current?.abort();
      if (retryTimer.current) clearTimeout(retryTimer.current);
    };
  }, []);

  // Nothing is lost to a closed or backgrounded tab: when the page is hidden, write the rows still in the buffer and
  // how far the batch got. While a batch runs, closing the tab asks first, and a paused batch resumes when the
  // connection comes back.
  useEffect(() => {
    const save = () => {
      const l = live.current;
      if (!l) return;
      void l.buf.flush();
      const rows = l.rows();
      saveBatchSummary(rows);
      saveBatchJob({
        ...l.job,
        finished: rows.filter((r) => r.status !== "failed").length,
        updatedAt: new Date().toISOString(),
      });
    };
    const hidden = () => {
      if (document.visibilityState === "hidden") save();
    };
    const leaving = (e: BeforeUnloadEvent) => {
      if (live.current) e.preventDefault();
    };
    const online = () => {
      if (!live.current && retryTimer.current) {
        clearTimeout(retryTimer.current);
        retryTimer.current = null;
        void startRef.current(true);
      }
    };
    document.addEventListener("visibilitychange", hidden);
    window.addEventListener("pagehide", save);
    window.addEventListener("beforeunload", leaving);
    window.addEventListener("online", online);
    return () => {
      document.removeEventListener("visibilitychange", hidden);
      window.removeEventListener("pagehide", save);
      window.removeEventListener("beforeunload", leaving);
      window.removeEventListener("online", online);
    };
  }, []);
  const rows = sorted;

  const all = useMemo(() => data?.structures ?? [], [data]);
  const byId = useMemo(() => new Map(all.map((s) => [s.id, s])), [all]);
  const sel: Selection = { by, ids, counties, watersheds, kinds, statuses };
  const list = useMemo(
    () => (data ? selectStructures(all, sel) : []),
    [data, by, ids, counties, watersheds, kinds, statuses],
  ); // eslint-disable-line react-hooks/exhaustive-deps
  const ready = list.filter((s) => !missingData(s)).length;
  // The time estimate: the baseline prior (8 s small / 25 s large) until three rows of this run are timed, then the
  // running mean of row.seconds, over what is still to run, `lanes` at a time (the worker pool). It updates with every finished row.
  const est = useMemo(
    () =>
      runningEstimate(
        rows,
        list.filter((s) => !rows.some((r) => r.id === s.id && r.status !== "failed")),
        lanes,
      ),
    [rows, list, lanes],
  );
  const scoped = useMemo(() => all.filter((s) => kinds.includes(s.kind)), [all, kinds]);
  const countyF = useMemo(
    () =>
      facets(
        scoped.filter((s) => statuses.includes(s.status)),
        "county",
      ),
    [scoped, statuses],
  );
  const hucF = useMemo(
    () =>
      facets(
        scoped.filter((s) => statuses.includes(s.status)),
        "huc8",
      ),
    [scoped, statuses],
  );
  const statusF = useMemo(() => facets(scoped, "status"), [scoped]);
  const matches = useMemo(() => {
    const q = fold(query);
    if (!q) return [];
    return all
      .filter((s) => fold(`${s.id} ${s.label} ${s.nid ?? ""} ${s.county ?? ""}`).includes(q))
      .slice(0, 40);
  }, [all, query]);
  const doneIds = useMemo(
    () => new Set(rows.filter((r) => r.status !== "failed").map((r) => r.id)),
    [rows],
  );
  // Only when the selection on screen is still the one the job was running (changing it starts a different batch).
  const banner =
    job != null && sameSelection(job.selection, sel)
      ? jobBanner(job, list.filter((s) => doneIds.has(s.id)).length)
      : null;

  const toggle = (arr: string[], v: string, set: (a: string[]) => void) =>
    set(arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]);

  const start = async (resume: boolean) => {
    if (!data || live.current) return;
    if (retryTimer.current) clearTimeout(retryTimer.current);
    retryTimer.current = null;
    const c = new AbortController();
    ctl.current = c;
    setRunning(true);
    // Resume keeps finished rows only; a "failed" row is retried, not treated as done.
    let acc = resume
      ? rows.filter((r) => r.status !== "failed" && list.some((s) => s.id === r.id))
      : [];
    if (!resume) {
      dams.current.clear();
      retries.current = 0;
      await clearBatchRows();
    } else if (acc.length !== rows.length) {
      await clearBatchRows();
      const keep = new BatchRowBuffer();
      for (const r of acc) keep.add(r);
      await keep.flush();
    }
    setSorted(sortRows(acc));
    const skip = new Set(acc.map((r) => r.id));
    setProgress({ done: 0, total: list.length - skip.size, status: "Starting…" });
    const now = new Date().toISOString();
    const running: BatchJob = {
      selection: sel,
      total: list.length,
      finished: acc.length,
      state: "running",
      startedAt: resume && job?.startedAt ? job.startedAt : now,
      updatedAt: now,
    };
    saveBatchJob(running);
    setJob(running);
    // Rows go to IndexedDB in batches (every 10 rows or 2 s), never one JSON.stringify of the whole list per row.
    const buf = new BatchRowBuffer();
    live.current = { buf, rows: () => acc, job: running };
    // Keep the screen awake while it runs, where the browser allows (a sleeping laptop stops the batch).
    const lock = await (
      navigator as {
        wakeLock?: { request: (t: "screen") => Promise<{ release: () => Promise<void> }> };
      }
    ).wakeLock
      ?.request("screen")
      .catch(() => null);
    const watch = new FailureWatch();
    let paused: string | null = null;
    try {
      await runBatchInBrowser(list, {
        nd: data.nd,
        signal: c.signal,
        skip,
        yieldFn: () => new Promise((r) => setTimeout(r, 0)),
        onStatus: (t) => setProgress((p) => (p ? { ...p, status: t } : p)),
        onRow: (row, dam, done, total) => {
          if (dam) dams.current.set(row.id, dam);
          acc = [...acc, row];
          setSorted((prev) => insertRow(prev, row));
          buf.add(row);
          setProgress({
            done,
            total,
            status: `${row.id}: ${row.status === "done" ? (row.triage?.label ?? "done") : row.reason}`,
          });
          if (row.status === "done") retries.current = 0;
          // The terrain or 911 service stopped answering: pause here instead of failing every structure left.
          if (!paused && watch.add(row)) {
            paused = `The last ${watch.failedIds.length} structures could not reach the terrain service (${row.reason ?? "no answer"}).`;
            c.abort();
          }
        },
      });
    } finally {
      await buf.flush();
      live.current = null;
      void lock?.release().catch(() => {});
    }
    saveBatchSummary(acc);
    const finished = acc.filter((r) => r.status !== "failed").length;
    const left = list.length - finished;
    let state: BatchJob["state"] = paused
      ? "paused"
      : c.signal.aborted
        ? "stopped"
        : left > 0
          ? "stopped"
          : "done";
    let status = paused
      ? "Paused."
      : c.signal.aborted
        ? "Stopped. Resume picks up where it left off."
        : "Done.";
    let pauseReason: string | undefined = paused ?? undefined;
    if (paused) {
      const wait = retryDelayMs(retries.current);
      if (wait == null) {
        state = "stopped";
        pauseReason = `${paused} It was retried ${retries.current} times without success. Press Resume when the connection is back.`;
        status = "Paused: the terrain service is not answering. Press Resume to try again.";
      } else {
        retries.current++;
        pauseReason = `${paused} Trying again by itself in ${waitText(wait)}.`;
        status = `Paused: the terrain service is not answering. Trying again in ${waitText(wait)} (the failed structures are retried).`;
        retryTimer.current = setTimeout(() => {
          retryTimer.current = null;
          void startRef.current(true);
        }, wait);
      }
    }
    const end: BatchJob = {
      ...running,
      finished,
      state,
      pauseReason,
      updatedAt: new Date().toISOString(),
    };
    if (state === "done") clearBatchJob();
    else saveBatchJob(end);
    setJob(state === "done" ? null : end);
    setRunning(false);
    setProgress((p) => (p ? { ...p, status } : p));
  };
  startRef.current = start;

  // Any finished row can be opened: its record is one of the recent ones kept in memory, or is rebuilt from the
  // inventories and the terrain (a few seconds) when it is older than those or from before a reload.
  const canOpen = useCallback(
    (id: string) =>
      dams.current.has(id) ||
      (byId.has(id) && rows.some((r) => r.id === id && r.status === "done")),
    [byId, rows],
  );
  const open = useCallback(
    async (id: string) => {
      // A record for this structure may already be on the desk (an earlier Open in desk, or one made from the NID /
      // DWR pickers): select it instead of adding a copy on every click. The batch record's id ("batch-nd00199") and
      // the structure id in its `nid` / `nidId` fields are what identify it.
      const onDesk = (recId?: string) =>
        deskDams.find(
          (x) =>
            x.nid === id ||
            x.nidId === id ||
            (recId && (x.id === recId || x.id.startsWith(`${recId}-`))),
        );
      let d = dams.current.get(id);
      let existing = onDesk(d?.id);
      if (!d && !existing) {
        const s = byId.get(id);
        if (!s || !data) return;
        setOpening(id);
        try {
          // The same shared defaults the batch starts each record from (batch.ts runBatchInBrowser).
          const shared = sharedNow().values;
          const again = await runStructure(s, { nd: data.nd, prepare: (r) => applyDefaults(r, shared) });
          d = again.dam ?? undefined;
          if (d) dams.current.set(id, d);
          else
            return onOpen(
              `The record for ${id} could not be rebuilt (${again.row.reason ?? "no record"}). Run it again from the batch, or make it on the desk.`,
            );
        } finally {
          setOpening(null);
        }
        existing = onDesk(d.id);
      }
      if (existing) {
        selectDam(existing.id);
        onOpen(
          `Opened the record already on the desk for ${id} (${existing.name}); the batch run was not copied onto it. Its terrain layer is rebuilt on the Flood step in one click.`,
        );
        return;
      }
      if (!d) return;
      // Without the terrain layer (runBatchInBrowser already drops it): it is rebuilt on the desk in one click.
      const { outline: _outline, ...rest } = d;
      void _outline;
      addDam({ ...rest, id: `${d.id}-${Date.now().toString(36)}` });
      onOpen(
        `New record from the batch for ${id}. Its terrain layer is rebuilt on the Flood step in one click.`,
      );
    },
    [deskDams, selectDam, addDam, onOpen, byId, data],
  );

  const exportCsv = async () => {
    const r = await saveText(
      `crest-batch-${new Date().toISOString().slice(0, 10)}.csv`,
      batchCsv(rows, data ? batchCsvMeta(data) : {}),
      "text/csv",
    );
    setSaved(
      r.status === "saved"
        ? r.path
          ? `Saved to ${r.path}`
          : "Downloaded."
        : r.status === "cancelled"
          ? null
          : `Not saved: ${r.message}`,
    );
  };

  if (loadErr)
    return (
      <p className="text-sm text-red-700" role="alert">
        Could not load the structure lists: {loadErr}
      </p>
    );
  if (!data)
    return <p className="text-sm text-muted">Loading the NID, DWR dam and dugout lists…</p>;

  const pane = "min-w-0 rounded-md border border-line bg-card p-4";
  const chip = (on: boolean) =>
    `inline-flex min-h-9 items-center gap-1 rounded-sm border px-2 py-1 text-xs ${on ? "border-water bg-water text-card" : "border-line bg-paper text-ink"}`;
  const facetList = (
    f: { value: string; count: number }[],
    chosen: string[],
    set: (a: string[]) => void,
    label: string,
  ) => (
    <div>
      <p className="mb-2 text-sm text-muted">
        {chosen.length ? `${chosen.length} chosen. ` : ""}Tick one or more {label}.
        {chosen.length ? (
          <button type="button" className="ml-2 text-water underline" onClick={() => set([])}>
            Clear
          </button>
        ) : null}
      </p>
      <div className="flex max-h-64 flex-wrap gap-1 overflow-y-auto">
        {f.map((x) => (
          <button
            key={x.value}
            type="button"
            className={chip(chosen.includes(x.value))}
            onClick={() => toggle(chosen, x.value, set)}
          >
            {x.value} <span className="opacity-70">{x.count}</span>
          </button>
        ))}
      </div>
    </div>
  );

  return (
    <section className="grid min-w-0 gap-4" data-testid="batch-screen">
      <div className={pane}>
        <h2 className="text-xl">Batch screen</h2>
        <p className="mt-1 text-sm text-muted">
          Run many structures through the same build and triage as one dam on the desk. Each record
          is auto-derived: height and storage from the inventory, crest from DWR NAVD 88 where
          offered or else from 3DEP at the point, and the downstream direction from the 3DEP
          downhill (or from the local DEM chosen below). Use the results to order the Runner
          worklist.
        </p>
        <TerrainSource />

        <div className="mt-4 flex flex-wrap gap-2" role="group" aria-label="Select by">
          {BY.map((b) => (
            <button
              key={b.id}
              type="button"
              className={by === b.id ? primary : btn}
              onClick={() => setBy(b.id)}
              aria-pressed={by === b.id}
            >
              {b.label}
            </button>
          ))}
        </div>

        <div className="mt-4 grid gap-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="text-muted">Include:</span>
            {KINDS.map((k) => (
              <label key={k} className="inline-flex min-h-9 items-center gap-1">
                <input
                  type="checkbox"
                  checked={kinds.includes(k)}
                  onChange={() =>
                    setKinds(kinds.includes(k) ? kinds.filter((x) => x !== k) : [...kinds, k])
                  }
                />
                {KIND_LABEL[k]}s
              </label>
            ))}
          </div>
          {by !== "pick" ? (
            <div className="flex flex-wrap items-center gap-1 text-sm">
              <span className="mr-1 text-muted">Status:</span>
              {statusF.map((x) => (
                <button
                  key={x.value}
                  type="button"
                  className={chip(statuses.includes(x.value))}
                  onClick={() => toggle(statuses, x.value, setStatuses)}
                >
                  {x.value === "Location Incon" ? "Location inconclusive" : x.value}{" "}
                  <span className="opacity-70">{x.count}</span>
                </button>
              ))}
            </div>
          ) : null}

          {by === "pick" ? (
            <div>
              <label className="block text-sm">
                <span className="text-muted">
                  Find a structure (name, NID ID, DWR-/DUG- number, county)
                </span>
                <input
                  className="mt-1 h-11 w-full rounded-sm border border-line bg-paper px-3"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="e.g. Absaraka, ND00199, DWR-4031, DUG-130"
                />
              </label>
              {matches.length ? (
                <ul className="mt-2 max-h-64 overflow-y-auto rounded-sm border border-line">
                  {matches.map((s) => (
                    <li key={s.id} className="border-b border-line last:border-b-0">
                      <label className="flex min-h-11 cursor-pointer items-center gap-2 px-3 py-1 text-sm">
                        <input
                          type="checkbox"
                          checked={ids.includes(s.id)}
                          onChange={() => toggle(ids, s.id, setIds)}
                        />
                        <span className="min-w-0 flex-1">
                          <span className="font-medium">{s.label}</span>{" "}
                          <span className="text-muted">
                            · {KIND_LABEL[s.kind]} · {s.county ?? "—"} · {s.status}
                            {missingData(s) ? " · needs data" : ""}
                          </span>
                        </span>
                      </label>
                    </li>
                  ))}
                </ul>
              ) : null}
              {ids.length ? (
                <div className="mt-2 flex flex-wrap gap-1">
                  {ids.map((id) => (
                    <button
                      key={id}
                      type="button"
                      className={chip(true)}
                      onClick={() => toggle(ids, id, setIds)}
                      aria-label={`Remove ${id}`}
                    >
                      {byId.get(id)?.label ?? id} ✕
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}
          {by === "county" ? facetList(countyF, counties, setCounties, "counties") : null}
          {by === "watershed"
            ? facetList(hucF, watersheds, setWatersheds, "HUC8 watersheds")
            : null}
        </div>

        <div className="mt-4 rounded-sm bg-sand p-3 text-sm" data-testid="batch-summary">
          <p>
            <strong>{list.length.toLocaleString("en-US")}</strong> selected ·{" "}
            {ready.toLocaleString("en-US")} ready to run ·{" "}
            {(list.length - ready).toLocaleString("en-US")} need data (no height, storage or
            location) · about {fmtTime(est.seconds)}
            {running || rows.length ? " left" : ""} at{" "}
            {lanes === 1 ? "one" : lanes === 2 ? "two" : lanes === 3 ? "three" : "four"} at a time
            {est.measured
              ? ` (from this run: ${Math.round(est.perSmallS)} s a small structure, ${Math.round(est.perLargeS)} s a large one)`
              : ""}
            .
          </p>
          {summary && !rows.length ? (
            <p className="mt-1 text-muted" data-testid="batch-resume-note">
              A previous batch left {summary.rows.toLocaleString("en-US")} rows (
              {summary.done.toLocaleString("en-US")} done); they are loading from this browser's
              storage.
            </p>
          ) : null}
          {banner && !running ? (
            <p className="mt-1 font-medium" role="status" data-testid="batch-job-banner">
              {banner}
            </p>
          ) : null}
          {ready > BROWSER_BATCH_WARN ? (
            <p className="mt-1 text-muted" data-testid="batch-large-note">
              A batch this size runs here for a long time. It can be left alone: rows are saved as
              they finish, Stop or a closed tab loses nothing and Resume carries on, and if the
              terrain service stops answering the batch pauses and tries again by itself. Keep this
              tab open and the computer awake. The command line does the same run without a browser
              and can read a local DEM mosaic instead of 3DEP:{" "}
              <code className="break-all">
                node --experimental-strip-types scripts/batch-dams.mjs --out batch-out{" "}
                {by === "state"
                  ? "--state"
                  : by === "county"
                    ? `--county "${counties.join(",")}"`
                    : by === "watershed"
                      ? `--watershed "${watersheds.join(",")}"`
                      : `--ids ${ids.join(",")}`}
              </code>
            </p>
          ) : null}
        </div>

        <div className="mt-3 flex flex-wrap gap-2">
          {running ? (
            <button type="button" className={btn} onClick={() => ctl.current?.abort()}>
              Stop
            </button>
          ) : (
            <>
              <button
                type="button"
                className={primary}
                disabled={!list.length}
                onClick={() => void start(false)}
              >
                Run {list.length ? list.length.toLocaleString("en-US") : ""}
              </button>
              <button
                type="button"
                className={btn}
                disabled={!list.some((s) => !doneIds.has(s.id)) || !rows.length}
                onClick={() => void start(true)}
                data-testid="batch-resume"
              >
                Resume
              </button>
              {job?.state === "paused" ? (
                <button
                  type="button"
                  className={btn}
                  data-testid="batch-stop-retrying"
                  onClick={() => {
                    if (retryTimer.current) clearTimeout(retryTimer.current);
                    retryTimer.current = null;
                    const stopped: BatchJob = {
                      ...job,
                      state: "stopped",
                      pauseReason: "Automatic retries were stopped.",
                    };
                    saveBatchJob(stopped);
                    setJob(stopped);
                    setProgress((p) =>
                      p ? { ...p, status: "Stopped. Resume picks up where it left off." } : p,
                    );
                  }}
                >
                  Stop retrying
                </button>
              ) : null}
            </>
          )}
          <button
            type="button"
            className={btn}
            disabled={!rows.length}
            onClick={() => void exportCsv()}
          >
            Download CSV
          </button>
          <button
            type="button"
            className={btn}
            disabled={running || !rows.length}
            onClick={() => {
              if (retryTimer.current) clearTimeout(retryTimer.current);
              retryTimer.current = null;
              setSorted([]);
              void clearBatchRows();
              clearBatchSummary();
              clearBatchJob();
              setJob(null);
              dams.current.clear();
              setProgress(null);
            }}
          >
            Clear results
          </button>
        </div>
        {saved ? <p className="mt-2 text-sm text-muted">{saved}</p> : null}
        {progress ? (
          <div className="mt-3" aria-live="polite">
            <div className="h-2 w-full overflow-hidden rounded-sm bg-sand">
              <div
                className="h-2 bg-water"
                style={{ width: `${progress.total ? (100 * progress.done) / progress.total : 0}%` }}
              />
            </div>
            <p className="mt-1 text-sm text-muted">
              {progress.done} / {progress.total} · {progress.status}
            </p>
          </div>
        ) : null}
      </div>

      {sorted.length ? (
        <div className={pane}>
          <h3 className="text-lg">Results: Runner worklist first</h3>
          <p className="mt-1 text-xs text-muted">{BATCH_DISCLAIMER}</p>
          {opening ? (
            <p className="mt-1 text-sm text-muted" role="status">
              Rebuilding the record for {opening} from the inventories and the terrain…
            </p>
          ) : null}
          <BatchTable rows={sorted} canOpen={canOpen} onOpen={(id) => void open(id)} />
        </div>
      ) : null}
    </section>
  );
}
