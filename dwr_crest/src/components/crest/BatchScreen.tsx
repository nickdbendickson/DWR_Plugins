/**
 * Batch screen: pick many structures (one by one, by county, by watershed, or statewide) and run each through the
 * desk's own build + triage (src/lib/crest/batch.ts). Results are a Runner worklist: needs-runner first.
 *
 * Only the small result rows are kept in browser storage (crest-batch-v1); the finished records, with their terrain
 * layers, live in memory for "Open in desk" and are not saved, so a large batch cannot fill the storage quota.
 */
import { useEffect, useMemo, useRef, useState } from "react";

import {
  BATCH_DISCLAIMER,
  BROWSER_BATCH_WARN,
  DEFAULT_STATUSES,
  KIND_LABEL,
  batchCsv,
  estimateSeconds,
  facets,
  loadStructures,
  missingData,
  runBatch,
  selectStructures,
  sortRows,
  type BatchRow,
  type Selection,
  type Structure,
  type StructureKind,
} from "@/lib/crest/batch";
import { fold, type NdData } from "@/lib/crest/nd-dams";
import { saveText } from "@/lib/crest/save";
import { useDesk } from "@/lib/crest/store";
import type { DamRecord } from "@/lib/crest/types";
import { TerrainSource } from "./TerrainSource";

const ROWS_KEY = "crest-batch-v1";
const btn = "inline-flex h-11 items-center rounded-sm border border-line bg-card px-4 text-sm text-ink disabled:opacity-50";
const primary = "inline-flex h-11 items-center rounded-sm border border-water bg-water px-4 text-sm text-card disabled:opacity-50";
const KINDS: StructureKind[] = ["nid", "dam", "dugout"];
const BY: { id: Selection["by"]; label: string }[] = [
  { id: "pick", label: "Pick structures" },
  { id: "county", label: "County" },
  { id: "watershed", label: "Watershed (HUC8)" },
  { id: "state", label: "Statewide" },
];

function loadRows(): BatchRow[] {
  try {
    const raw = localStorage.getItem(ROWS_KEY);
    const v = raw ? (JSON.parse(raw) as unknown) : null;
    return Array.isArray(v) ? (v as BatchRow[]) : [];
  } catch {
    return [];
  }
}
function saveRows(rows: BatchRow[]) {
  try {
    localStorage.setItem(ROWS_KEY, JSON.stringify(rows));
  } catch {
    // Storage full or blocked: the rows stay on screen and in the CSV; nothing else depends on them being saved.
  }
}

const fmtTime = (s: number) => (s < 90 ? `${Math.round(s)} s` : s < 5400 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);
const num = (v: number | null | undefined, d = 0) => (v == null ? "—" : v.toLocaleString("en-US", { maximumFractionDigits: d }));

export function BatchScreen({ onOpen }: { onOpen: () => void }) {
  const addDam = useDesk((s) => s.addDam);
  const [data, setData] = useState<{ nd: NdData; structures: Structure[] } | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [by, setBy] = useState<Selection["by"]>("county");
  const [kinds, setKinds] = useState<StructureKind[]>(KINDS);
  const [statuses, setStatuses] = useState<string[]>([...DEFAULT_STATUSES]);
  const [ids, setIds] = useState<string[]>([]);
  const [counties, setCounties] = useState<string[]>([]);
  const [watersheds, setWatersheds] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [rows, setRows] = useState<BatchRow[]>([]);
  const [running, setRunning] = useState(false);
  const [progress, setProgress] = useState<{ done: number; total: number; status: string } | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const ctl = useRef<AbortController | null>(null);
  const dams = useRef(new Map<string, DamRecord>());

  useEffect(() => {
    setRows(loadRows());
    loadStructures()
      .then((d) => setData(d))
      .catch((e) => setLoadErr((e as Error).message));
    return () => ctl.current?.abort();
  }, []);

  const all = useMemo(() => data?.structures ?? [], [data]);
  const byId = useMemo(() => new Map(all.map((s) => [s.id, s])), [all]);
  const sel: Selection = { by, ids, counties, watersheds, kinds, statuses };
  const list = useMemo(() => (data ? selectStructures(all, sel) : []), [data, by, ids, counties, watersheds, kinds, statuses]); // eslint-disable-line react-hooks/exhaustive-deps
  const ready = list.filter((s) => !missingData(s)).length;
  const est = estimateSeconds(list) / 2;
  const scoped = useMemo(() => all.filter((s) => kinds.includes(s.kind)), [all, kinds]);
  const countyF = useMemo(() => facets(scoped.filter((s) => statuses.includes(s.status)), "county"), [scoped, statuses]);
  const hucF = useMemo(() => facets(scoped.filter((s) => statuses.includes(s.status)), "huc8"), [scoped, statuses]);
  const statusF = useMemo(() => facets(scoped, "status"), [scoped]);
  const matches = useMemo(() => {
    const q = fold(query);
    if (!q) return [];
    return all.filter((s) => fold(`${s.id} ${s.label} ${s.nid ?? ""} ${s.county ?? ""}`).includes(q)).slice(0, 40);
  }, [all, query]);
  const sorted = useMemo(() => sortRows(rows), [rows]);
  const doneIds = useMemo(() => new Set(rows.map((r) => r.id)), [rows]);

  const toggle = (arr: string[], v: string, set: (a: string[]) => void) => set(arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v]);

  const start = async (resume: boolean) => {
    if (!data) return;
    const c = new AbortController();
    ctl.current = c;
    setRunning(true);
    let acc = resume ? rows.filter((r) => list.some((s) => s.id === r.id)) : [];
    if (!resume) dams.current.clear();
    setRows(acc);
    const skip = new Set(acc.map((r) => r.id));
    setProgress({ done: 0, total: list.length - skip.size, status: "Starting…" });
    await runBatch(list, {
      nd: data.nd,
      signal: c.signal,
      skip,
      concurrency: 2,
      yieldFn: () => new Promise((r) => setTimeout(r, 0)),
      onStatus: (t) => setProgress((p) => (p ? { ...p, status: t } : p)),
      onRow: (row, dam, done, total) => {
        if (dam) dams.current.set(row.id, dam);
        acc = [...acc, row];
        setRows(acc);
        saveRows(acc);
        setProgress({ done, total, status: `${row.id}: ${row.status === "done" ? row.triage?.label ?? "done" : row.reason}` });
      },
    });
    setRunning(false);
    setProgress((p) => (p ? { ...p, status: c.signal.aborted ? "Stopped. Resume picks up where it left off." : "Done." } : p));
  };

  const open = (id: string) => {
    const d = dams.current.get(id);
    if (!d) return;
    // Without the terrain layer: it is rebuilt on the desk in one click, and saving it would fill the browser storage.
    const { outline: _outline, ...rest } = d;
    void _outline;
    addDam({ ...rest, id: `${d.id}-${Date.now().toString(36)}` });
    onOpen();
  };

  const exportCsv = async () => {
    const r = await saveText(`crest-batch-${new Date().toISOString().slice(0, 10)}.csv`, batchCsv(rows), "text/csv");
    setSaved(r.status === "saved" ? (r.path ? `Saved to ${r.path}` : "Downloaded.") : r.status === "cancelled" ? null : `Not saved: ${r.message}`);
  };

  if (loadErr) return <p className="text-sm text-red-700">Could not load the structure lists: {loadErr}</p>;
  if (!data) return <p className="text-sm text-muted">Loading the NID, DWR dam and dugout lists…</p>;

  const pane = "min-w-0 rounded-md border border-line bg-card p-4";
  const chip = (on: boolean) => `inline-flex min-h-9 items-center gap-1 rounded-sm border px-2 py-1 text-xs ${on ? "border-water bg-water text-card" : "border-line bg-paper text-ink"}`;
  const facetList = (f: { value: string; count: number }[], chosen: string[], set: (a: string[]) => void, label: string) => (
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
          <button key={x.value} type="button" className={chip(chosen.includes(x.value))} onClick={() => toggle(chosen, x.value, set)}>
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
          Run many structures through the same build and triage as one dam on the desk. Each record is auto-derived: height and
          storage from the inventory, crest from DWR NAVD 88 where offered or else from 3DEP at the point, and the downstream
          direction from the 3DEP downhill (or from the local DEM chosen below). Use the results to order the Runner worklist.
        </p>
        <TerrainSource />

        <div className="mt-4 flex flex-wrap gap-2" role="group" aria-label="Select by">
          {BY.map((b) => (
            <button key={b.id} type="button" className={by === b.id ? primary : btn} onClick={() => setBy(b.id)} aria-pressed={by === b.id}>
              {b.label}
            </button>
          ))}
        </div>

        <div className="mt-4 grid gap-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="text-muted">Include:</span>
            {KINDS.map((k) => (
              <label key={k} className="inline-flex min-h-9 items-center gap-1">
                <input type="checkbox" checked={kinds.includes(k)} onChange={() => setKinds(kinds.includes(k) ? kinds.filter((x) => x !== k) : [...kinds, k])} />
                {KIND_LABEL[k]}s
              </label>
            ))}
          </div>
          {by !== "pick" ? (
            <div className="flex flex-wrap items-center gap-1 text-sm">
              <span className="mr-1 text-muted">Status:</span>
              {statusF.map((x) => (
                <button key={x.value} type="button" className={chip(statuses.includes(x.value))} onClick={() => toggle(statuses, x.value, setStatuses)}>
                  {x.value === "Location Incon" ? "Location inconclusive" : x.value} <span className="opacity-70">{x.count}</span>
                </button>
              ))}
            </div>
          ) : null}

          {by === "pick" ? (
            <div>
              <label className="block text-sm">
                <span className="text-muted">Find a structure (name, NID ID, DWR-/DUG- number, county)</span>
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
                        <input type="checkbox" checked={ids.includes(s.id)} onChange={() => toggle(ids, s.id, setIds)} />
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
                    <button key={id} type="button" className={chip(true)} onClick={() => toggle(ids, id, setIds)} aria-label={`Remove ${id}`}>
                      {byId.get(id)?.label ?? id} ✕
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}
          {by === "county" ? facetList(countyF, counties, setCounties, "counties") : null}
          {by === "watershed" ? facetList(hucF, watersheds, setWatersheds, "HUC8 watersheds") : null}
        </div>

        <div className="mt-4 rounded-sm bg-sand p-3 text-sm" data-testid="batch-summary">
          <p>
            <strong>{list.length.toLocaleString("en-US")}</strong> selected · {ready.toLocaleString("en-US")} ready to run ·{" "}
            {(list.length - ready).toLocaleString("en-US")} need data (no height, storage or location) · about {fmtTime(est)} at two
            at a time.
          </p>
          {ready > BROWSER_BATCH_WARN ? (
            <p className="mt-1 text-amber">
              Large batch for a browser tab: keep the tab open, or run it from the command line, which resumes after a stop and can
              read a local DEM mosaic instead of 3DEP:{" "}
              <code className="break-all">node --experimental-strip-types scripts/batch-dams.mjs --out batch-out {by === "state" ? "--state" : by === "county" ? `--county "${counties.join(",")}"` : by === "watershed" ? `--watershed "${watersheds.join(",")}"` : `--ids ${ids.join(",")}`}</code>
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
              <button type="button" className={primary} disabled={!list.length} onClick={() => void start(false)}>
                Run {list.length ? list.length.toLocaleString("en-US") : ""}
              </button>
              <button type="button" className={btn} disabled={!list.some((s) => !doneIds.has(s.id)) || !rows.length} onClick={() => void start(true)}>
                Resume
              </button>
            </>
          )}
          <button type="button" className={btn} disabled={!rows.length} onClick={() => void exportCsv()}>
            Download CSV
          </button>
          <button
            type="button"
            className={btn}
            disabled={running || !rows.length}
            onClick={() => {
              setRows([]);
              saveRows([]);
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
              <div className="h-2 bg-water" style={{ width: `${progress.total ? (100 * progress.done) / progress.total : 0}%` }} />
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
          <div className="mt-3 max-w-full overflow-x-auto">
            <table className="w-full min-w-[760px] text-left text-sm" data-testid="batch-results">
              <thead className="text-xs text-muted">
                <tr className="border-b border-line">
                  <th className="py-2 pr-2">Structure</th>
                  <th className="py-2 pr-2">County</th>
                  <th className="py-2 pr-2">Triage</th>
                  <th className="py-2 pr-2">Screen</th>
                  <th className="py-2 pr-2 text-right">Released ac-ft</th>
                  <th className="py-2 pr-2 text-right">Flooded ac</th>
                  <th className="py-2 pr-2 text-right">Homes wet / in reach</th>
                  <th className="py-2" />
                </tr>
              </thead>
              <tbody>
                {sorted.map((r) => (
                  <tr key={r.id} className="border-b border-line align-top">
                    <td className="py-2 pr-2">
                      <span className="font-medium">{r.label}</span>
                      <span className="block text-xs text-muted">
                        {KIND_LABEL[r.kind]}
                        {r.crestFrom ? ` · crest from ${r.crestFrom}` : ""}
                      </span>
                    </td>
                    <td className="py-2 pr-2">{r.county ?? "—"}</td>
                    <td className="py-2 pr-2">
                      {r.triage ? (
                        <>
                          <span className={r.triage.recommendation === "needs-runner" ? "font-semibold text-amber" : ""}>{r.triage.label}</span>
                          <span className="block text-xs text-muted">{r.triage.confidence} confidence</span>
                        </>
                      ) : (
                        <span className="text-muted">{r.status === "needs-data" ? "Needs data" : "Failed"}</span>
                      )}
                      {r.reason ? <span className="block text-xs text-muted">{r.reason}</span> : null}
                    </td>
                    <td className="py-2 pr-2">{r.screenLabel ?? "—"}</td>
                    <td className="py-2 pr-2 text-right">{num(r.releasedAcFt)}</td>
                    <td className="py-2 pr-2 text-right">{num(r.floodedAc)}</td>
                    <td className="py-2 pr-2 text-right">
                      {r.structuresFlooded == null ? "—" : `${r.structuresFlooded} / ${r.structuresInside ?? "?"}`}
                    </td>
                    <td className="py-2">
                      {dams.current.has(r.id) ? (
                        <button type="button" className="inline-flex h-9 items-center rounded-sm border border-line bg-paper px-2 text-xs" onClick={() => open(r.id)}>
                          Open in desk
                        </button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </section>
  );
}
