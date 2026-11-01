import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import type { analyze } from "@/lib/crest/engine";
import { DEPTH_BANDS, floodStrip } from "@/lib/crest/floodstrip";
import { CRS_DEFS, DIFF_BINS } from "@/lib/crest/raster-meta";
import { results } from "@/lib/crest/results-client";
import {
  deskRank,
  deskStats,
  disagreement,
  rankSample,
  receptorPoints,
  resultsContext,
  type ResultPoint,
  SOURCE_COLOR,
  SOURCE_LABEL,
  type SourceRank,
} from "@/lib/crest/results";
import { runKey, type RunState, useResults } from "@/lib/crest/results-store";
import { useDesk } from "@/lib/crest/store";
import type { DamRecord, ResultSource } from "@/lib/crest/types";
import type { Bounds, MapPoint, Overlay } from "./ResultsMap";
import { allChecks, dropRun, importRun, pickFiles, refFromRun } from "./results-flow";

type Analysis = ReturnType<typeof analyze>;
const ResultsMap = lazy(() => import("./ResultsMap").then((m) => ({ default: m.ResultsMap })));

const SOURCES: ResultSource[] = ["lisflood", "dsswise"];
type MapMode = "desk" | "terrain" | ResultSource | "compare";

const f1 = (n: number | null | undefined, d = 1) => (n == null || !Number.isFinite(n) ? "—" : n.toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: d }));
const f0 = (n: number | null | undefined) => (n == null || !Number.isFinite(n) ? "—" : Math.round(n).toLocaleString("en-US"));

function useRun(dam: DamRecord, source: ResultSource) {
  return useResults((s) => s.runs[runKey(dam.id, source)]);
}

function toUrl(px: Uint8ClampedArray, w: number, h: number) {
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  const g = c.getContext("2d");
  if (!g) return "";
  g.putImageData(new ImageData(new Uint8ClampedArray(px), w, h), 0, 0);
  return c.toDataURL("image/png");
}

export function ResultsStep({ dam, result }: { dam: DamRecord; result: Analysis }) {
  const lis = useRun(dam, "lisflood");
  const dss = useRun(dam, "dsswise");
  const runs: Partial<Record<ResultSource, RunState>> = { lisflood: lis, dsswise: dss };
  const ready = SOURCES.filter((s) => runs[s]?.status === "ready" && runs[s]?.summary?.stats);
  return (
    <section className="grid gap-4" data-testid="results-step">
      <div className="rounded-md border border-line bg-card p-4">
        <h2 className="text-xl">Results: imported model runs</h2>
        <p className="mt-1 text-sm text-muted">
          Load a LISFLOOD-FP run (ESRI ASCII or the Runner's GeoTIFFs, a folder or a zip) and a DSS-WISE Lite run (GeoTIFFs or the
          download zip) for this dam, compare them with the desk estimate, and pick which one the Screen, the PLSS list and the
          Report use. Rasters stay in this browser session; only the numbers and receptor samples of the source of record are saved
          with the dam.
        </p>
        <div className="mt-4 grid gap-4 lg:grid-cols-2">
          {SOURCES.map((s) => (
            <ImportCard key={s} dam={dam} result={result} source={s} run={runs[s]} />
          ))}
        </div>
      </div>
      <SourceOfRecord dam={dam} result={result} runs={runs} ready={ready} />
      <MapPanel dam={dam} result={result} runs={runs} ready={ready} />
      <CompareTable dam={dam} result={result} runs={runs} ready={ready} />
      <StructureTable dam={dam} result={result} runs={runs} ready={ready} />
    </section>
  );
}

function ImportCard({ dam, result, source, run }: { dam: DamRecord; result: Analysis; source: ResultSource; run?: RunState }) {
  const [crs, setCrs] = useState("auto");
  const [skipped, setSkipped] = useState<string | null>(null);
  const busy = run?.status === "loading";
  const onPick = (list: FileList | null) => {
    if (!list || list.length === 0) return;
    const { use, total } = pickFiles(list);
    setSkipped(total > use.length ? `${total - use.length} of ${total} files are not result grids, dam.json or zips, so they were not read.` : null);
    if (use.length === 0) return;
    void importRun(dam, result, source, use, crs);
  };
  const checks = allChecks(dam, result, run);
  const s = run?.summary;
  const tid = `import-${source}`;
  const input = (label: string, extra: Record<string, unknown>, suffix: string) => (
    <label className={"inline-flex h-10 cursor-pointer items-center rounded-sm border border-line bg-paper px-3 text-sm " + (busy ? "pointer-events-none opacity-50" : "")}>
      {label}
      <input
        type="file"
        className="sr-only"
        data-testid={`${tid}-${suffix}`}
        disabled={busy}
        {...extra}
        onChange={(e) => {
          onPick(e.target.files);
          e.target.value = "";
        }}
      />
    </label>
  );
  return (
    <div className="rounded-md border border-line p-3" data-testid={tid} style={{ borderLeft: `4px solid ${SOURCE_COLOR[source]}` }}>
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="font-medium">{SOURCE_LABEL[source]}</h3>
        {run ? (
          <button type="button" className="text-xs text-muted underline" onClick={() => dropRun(dam, source)}>
            Unload
          </button>
        ) : null}
      </div>
      <p className="mt-1 text-xs text-muted">
        {source === "lisflood"
          ? "Max depth (.max or *hmax*), velocity (.Vmax/.maxVc or *vmax*), arrival (.maxtm or *arr*); m, m/s and h are converted to ft, ft/s and h. A Runner dam.json in the folder or zip is used for the dam check."
          : "Hmax_ft, Vmax_ftps and Arrival_Time_hr GeoTIFFs (ft, ft/s, h; nodata and zero depth are dry). The other DSS-WISE grids are listed but not used."}
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        {input("Files…", { multiple: true, accept: ".asc,.max,.mxe,.maxVc,.Vmax,.maxtm,.inittm,.tif,.tiff,.zip,.json" }, "files")}
        {input("Folder…", { webkitdirectory: "", directory: "" }, "folder")}
        {input("Zip…", { accept: ".zip" }, "zip")}
      </div>
      <label className="mt-2 block text-xs">
        <span className="text-muted">CRS for ASCII grids (GeoTIFFs carry their own)</span>
        <select value={crs} onChange={(e) => setCrs(e.target.value)} className="mt-1 h-9 w-full rounded-sm border border-line bg-paper px-2 text-sm" data-testid={`${tid}-crs`}>
          <option value="auto">Auto: the run's dam.json CRS, else EPSG:32614</option>
          {Object.entries(CRS_DEFS).map(([code, d]) => (
            <option key={code} value={code}>
              {code} {d.label}
            </option>
          ))}
        </select>
      </label>
      {skipped ? <p className="mt-2 text-xs text-muted">{skipped}</p> : null}
      {busy ? (
        <p className="mt-2 text-sm" data-testid={`${tid}-progress`}>
          {run?.progress ?? "Working…"}
        </p>
      ) : null}
      {run?.status === "error" ? (
        <p className="mt-2 text-sm text-red-700" data-testid={`${tid}-error`}>
          Import failed: {run.error}
        </p>
      ) : null}
      {s ? (
        <div className="mt-2 text-sm" data-testid={`${tid}-summary`}>
          <p className="font-medium">{s.label}</p>
          <p className="text-xs text-muted">
            {s.crsLabel}; {s.width.toLocaleString("en-US")} × {s.height.toLocaleString("en-US")} cells of {f1(s.cellM, 2)} m
            {s.retainedM > s.cellM + 1e-6 ? ` (map and samples from the full grid; display grid ${f1(s.retainedM, 2)} m)` : ""}; variables: {s.vars.join(", ")}
          </p>
          <ul className="mt-2 space-y-1" data-testid={`${tid}-checks`}>
            {checks.map((c, i) => (
              <li key={i} className={c.level === "error" ? "text-red-700" : c.level === "warn" ? "text-amber" : "text-muted"}>
                {c.level === "ok" ? "✓" : c.level === "warn" ? "⚠" : "✗"} {c.text}
              </li>
            ))}
          </ul>
          {s.runNotes.length ? (
            <ul className="mt-2 list-disc pl-5 text-xs text-muted">
              {s.runNotes.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          ) : null}
          <details className="mt-2 text-xs">
            <summary className="cursor-pointer text-muted">Files ({s.files.length})</summary>
            <ul className="mt-1 space-y-0.5">
              {s.files.map((f) => (
                <li key={f.name}>
                  <span className="font-mono">{f.name}</span> <span className="text-muted">— {f.use}</span>
                </li>
              ))}
            </ul>
          </details>
        </div>
      ) : null}
    </div>
  );
}

function SourceOfRecord({ dam, result, runs, ready }: { dam: DamRecord; result: Analysis; runs: Partial<Record<ResultSource, RunState>>; ready: ResultSource[] }) {
  const update = useDesk((s) => s.update);
  const rec = dam.resultsOfRecord;
  const value = rec?.source ?? "desk";
  const blocked = (s: ResultSource) => allChecks(dam, result, runs[s]).some((c) => c.level === "error");
  const options: { id: string; text: string; disabled: boolean }[] = [{ id: "desk", text: SOURCE_LABEL.desk, disabled: false }];
  for (const s of SOURCES) {
    const r = runs[s];
    if (r?.summary?.stats) options.push({ id: s, text: r.summary.label + (blocked(s) ? " (blocked: import check error)" : ""), disabled: blocked(s) });
    else if (rec?.source === s) options.push({ id: s, text: `${rec.label} (saved; rasters not loaded)`, disabled: false });
  }
  const choose = (id: string) => {
    if (id === "desk") return update({ resultsOfRecord: undefined });
    const run = runs[id as ResultSource];
    const ref = run ? refFromRun(dam, result, run) : null;
    if (ref) update({ resultsOfRecord: ref });
  };
  return (
    <div className="rounded-md border border-line bg-card p-4" data-testid="source-of-record">
      <div className="flex flex-wrap items-end gap-4">
        <label className="block text-sm">
          <span className="text-muted">Source of record</span>
          <select value={value} onChange={(e) => choose(e.target.value)} className="mt-1 h-11 min-w-72 rounded-sm border border-line bg-paper px-3 text-ink" data-testid="source-of-record-select">
            {options.map((o) => (
              <option key={o.id} value={o.id} disabled={o.disabled}>
                {o.text}
              </option>
            ))}
          </select>
        </label>
        <p className="max-w-3xl text-sm text-muted">
          Drives the Screen (receptor depths and velocities from this source, ranked with TM 11), the PLSS section list and the Report,
          and is named in the Report and in dam.json. Breach and hydrograph stay the desk's.
          {ready.length === 0 && !rec ? " Import a run to choose it." : ""}
        </p>
      </div>
      {rec?.warnings.length ? (
        <ul className="mt-2 list-disc pl-5 text-xs text-amber">
          {rec.warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

type Rendered = { overlays: Overlay[]; fit: Bounds | null; compare?: import("@/lib/crest/raster").CompareStats; error?: string; busy?: boolean };

function MapPanel({ dam, result, runs, ready }: { dam: DamRecord; result: Analysis; runs: Partial<Record<ResultSource, RunState>>; ready: ResultSource[] }) {
  const [mode, setMode] = useState<MapMode>("desk");
  const [flip, setFlip] = useState(false);
  const desk = useMemo(() => floodStrip(dam, result).strip, [dam, result]);
  const rc = useMemo(() => resultsContext(dam, result), [dam, result]);
  const pair: [ResultSource, ResultSource] = flip ? ["dsswise", "lisflood"] : ["lisflood", "dsswise"];
  const [out, setOut] = useState<Rendered>({ overlays: [], fit: null });
  const lisSum = runs.lisflood?.summary;
  const dssSum = runs.dsswise?.summary;
  useEffect(() => {
    let live = true;
    const want = mode === "compare" ? (ready.length === 2 ? pair : null) : mode === "lisflood" || mode === "dsswise" ? (ready.includes(mode) ? [mode] : null) : null;
    if (!want) {
      setOut({ overlays: [], fit: null });
      return;
    }
    setOut((o) => ({ ...o, busy: true }));
    const keys = want.map((s) => runKey(dam.id, s));
    const extra = rc.ctx ? [rc.ctx.breach] : [];
    const downstream = rc.ctx?.u ? { lon: rc.ctx.breach.lon, lat: rc.ctx.breach.lat, u: rc.ctx.u } : null;
    results
      .render(
        mode === "compare"
          ? { type: "render", mode: "compare", keys, colors: [SOURCE_COLOR[pair[0]], SOURCE_COLOR[pair[1]]], maxPx: 1600, downstream, extra }
          : { type: "render", mode: "depth", keys, maxPx: 1600, extra },
      )
      .then((r) => {
        if (!live) return;
        const overlays = r.images.map((px, i) => ({ url: toUrl(px, r.width, r.height), bounds: r.bounds, opacity: mode === "compare" && i === 0 ? 0.85 : 1 }));
        setOut({ overlays, fit: r.bounds, compare: r.compare });
      })
      .catch((e: Error) => live && setOut({ overlays: [], fit: null, error: e.message }));
    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, flip, ready.join(","), lisSum, dssSum, dam.id, rc]);

  const deskFit = useMemo<Bounds | null>(() => {
    if (!desk) return null;
    let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
    const walk = (c: unknown): void => {
      if (Array.isArray(c) && typeof c[0] === "number") {
        w = Math.min(w, c[0] as number); e = Math.max(e, c[0] as number); s = Math.min(s, c[1] as number); n = Math.max(n, c[1] as number);
      } else if (Array.isArray(c)) c.forEach(walk);
    };
    for (const f of desk.features) walk((f.geometry as { coordinates: unknown }).coordinates);
    return Number.isFinite(w) ? { west: w, south: s, east: e, north: n } : null;
  }, [desk]);

  const points = useMemo<MapPoint[]>(() => {
    const pts: MapPoint[] = receptorPoints(dam, result).map((p: ResultPoint) => ({ id: p.id, name: p.name, lon: p.lon, lat: p.lat, color: "#facc15", kind: "receptor", note: p.placed }));
    if (rc.ctx) pts.push({ id: "breach", name: "Breach (record)", lon: rc.ctx.breach.lon, lat: rc.ctx.breach.lat, color: "#b45309", kind: "breach" });
    for (const s of SOURCES) {
      const d = runs[s]?.summary?.runDam;
      if (d) pts.push({ id: `dam-${s}`, name: `${SOURCE_LABEL[s]} run's dam`, lon: d.lon, lat: d.lat, color: SOURCE_COLOR[s], kind: "run-dam" });
    }
    return pts;
  }, [dam, result, rc, runs]);

  const modes: { id: MapMode; label: string; disabled: boolean; title?: string }[] = [
    { id: "desk", label: "Desk strip", disabled: false },
    { id: "terrain", label: "Terrain outline", disabled: true, title: "The terrain-following outline is on the swe/flood-outline branch; it shows here once both are merged." },
    { id: "lisflood", label: "LISFLOOD", disabled: !ready.includes("lisflood") },
    { id: "dsswise", label: "DSS-WISE", disabled: !ready.includes("dsswise") },
    { id: "compare", label: "Compare A−B", disabled: ready.length < 2 },
  ];
  const showDesk = mode === "desk";
  return (
    <div className="rounded-md border border-line bg-card p-4" data-testid="results-map">
      <div className="flex flex-wrap items-center gap-2">
        {modes.map((m) => (
          <button
            key={m.id}
            type="button"
            disabled={m.disabled}
            title={m.title}
            onClick={() => setMode(m.id)}
            data-testid={`map-mode-${m.id}`}
            className={"h-9 rounded-sm border px-3 text-sm disabled:opacity-40 " + (mode === m.id ? "border-water bg-water text-card" : "border-line bg-paper text-ink")}
          >
            {m.label}
          </button>
        ))}
        {mode === "compare" ? (
          <button type="button" className="h-9 rounded-sm border border-line px-3 text-sm" onClick={() => setFlip(!flip)}>
            A = {SOURCE_LABEL[pair[0]]}, B = {SOURCE_LABEL[pair[1]]} (swap)
          </button>
        ) : null}
        {out.busy ? <span className="text-sm text-muted">Rendering…</span> : null}
      </div>
      <div className="mt-3 grid gap-3 lg:grid-cols-[1fr_220px]">
        <Suspense fallback={<p className="text-sm text-muted">Loading the map…</p>}>
          <ResultsMap overlays={showDesk ? [] : out.overlays} strip={showDesk ? desk : null} points={points} fit={showDesk ? deskFit : out.fit ?? deskFit} />
        </Suspense>
        <div className="text-xs" data-testid="results-legend">
          {mode === "compare" ? (
            <>
              <p className="font-medium">Depth A − B (ft)</p>
              <ul className="mt-1 space-y-0.5">
                {DIFF_BINS.map((b) => (
                  <li key={b.label} className="flex items-center gap-2">
                    <span className="inline-block h-3 w-5" style={{ background: b.color }} /> {b.label}
                  </li>
                ))}
              </ul>
              <p className="mt-2 font-medium">Wet outlines (≥ 0.1 ft)</p>
              {pair.map((s) => (
                <p key={s} className="flex items-center gap-2">
                  <span className="inline-block h-0.5 w-5" style={{ background: SOURCE_COLOR[s] }} /> {s === pair[0] ? "A" : "B"}: {SOURCE_LABEL[s]}
                </p>
              ))}
              {out.compare ? (
                <dl className="mt-2 space-y-0.5" data-testid="compare-stats">
                  <div>Wet in both: {f0(out.compare.bothAc)} ac</div>
                  <div>A only: {f0(out.compare.aOnlyAc)} ac</div>
                  <div>B only: {f0(out.compare.bOnlyAc)} ac</div>
                  <div>Mean |A−B|: {f1(out.compare.meanAbsFt)} ft</div>
                  <div>A deeper by up to {f1(out.compare.maxAOverFt)} ft; B by up to {f1(out.compare.maxBOverFt)} ft</div>
                  <div className="text-muted">
                    Downstream of the dam line, where both grids have cells, on a {f0(out.compare.pixelM)} m display grid.
                  </div>
                </dl>
              ) : null}
            </>
          ) : (
            <>
              <p className="font-medium">Max depth (ft)</p>
              <ul className="mt-1 space-y-0.5">
                {[...DEPTH_BANDS].reverse().map((b) => (
                  <li key={b.id} className="flex items-center gap-2">
                    <span className="inline-block h-3 w-5" style={{ background: b.color }} /> {b.label}
                  </li>
                ))}
              </ul>
              <p className="mt-2 text-muted">
                {mode === "desk"
                  ? "Desk strip: the schematic valley, not a mapped extent."
                  : `${SOURCE_LABEL[mode as ResultSource] ?? ""} max depth, reprojected to the map for display (numbers use the full grid). Datum shifts NAD83→WGS84 (about 1 m) are not applied.`}
              </p>
            </>
          )}
          <p className="mt-2 text-muted">Yellow dots: record receptors (desk valley placement unless mapped). Hollow circles: the run's own dam location.</p>
          {out.error ? <p className="mt-2 text-red-700">{out.error}</p> : null}
        </div>
      </div>
    </div>
  );
}

function CompareTable({ result, runs, ready }: { dam: DamRecord; result: Analysis; runs: Partial<Record<ResultSource, RunState>>; ready: ResultSource[] }) {
  const d = deskStats(result);
  const cols = ready.map((s) => ({ s, st: runs[s]!.summary!.stats!, cell: runs[s]!.summary!.cellM }));
  const rows: { k: string; desk: string; run: (st: (typeof cols)[number]) => string }[] = [
    { k: "Peak depth downstream (ft)", desk: f1(d.peakDepthFt), run: (c) => f1(c.st.peakDepthFt) },
    { k: "Peak depth incl. reservoir (ft)", desk: "—", run: (c) => f1(c.st.peakDepthAllFt) },
    { k: "Wet area ≥ 0.1 ft downstream (ac)", desk: f0(d.wetAc), run: (c) => f0(c.st.wetAc) },
    { k: "Farthest wet downstream (km)", desk: f1(d.farthestKm, 2), run: (c) => f1(c.st.farthestKm, 2) },
    { k: "Max velocity (ft/s)", desk: f1(d.maxVelocityFps), run: (c) => f1(c.st.maxVelocityFps) },
    { k: "Latest arrival, wet cells (h)", desk: d.arrivalMaxHr == null ? "—" : `${f1(d.arrivalMaxHr)} (receptors)`, run: (c) => f1(c.st.arrivalMaxHr) },
    { k: "Grid coverage downstream (km)", desk: f1(result.flood.lengthM / 1000, 2) + " (reach)", run: (c) => f1(c.st.coverKm, 2) },
    { k: "Nearest wet cell to breach (m)", desk: "—", run: (c) => f0(c.st.nearestWetM) },
    { k: "Cell size (m)", desk: "—", run: (c) => f1(c.cell, 2) },
  ];
  return (
    <div className="overflow-x-auto rounded-md border border-line bg-card p-4" data-testid="compare-table">
      <h3 className="font-medium">Comparison</h3>
      <table className="mt-2 w-full text-left text-sm">
        <thead className="text-xs tracking-wide text-muted uppercase">
          <tr>
            <th className="px-2 py-1 font-medium" />
            <th className="px-2 py-1 font-medium" style={{ color: SOURCE_COLOR.desk }}>Desk</th>
            {cols.map((c) => (
              <th key={c.s} className="px-2 py-1 font-medium" style={{ color: SOURCE_COLOR[c.s] }}>
                {SOURCE_LABEL[c.s]}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="tabular-nums">
          {rows.map((r) => (
            <tr key={r.k} className="border-t border-line">
              <td className="px-2 py-1 text-muted">{r.k}</td>
              <td className="px-2 py-1">{r.desk}</td>
              {cols.map((c) => (
                <td key={c.s} className="px-2 py-1">
                  {r.run(c)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-muted">
        Downstream = past the dam line along the record's downstream direction (reservoir cells left out). Farthest is a straight
        line along that direction, not along the channel. Desk numbers are the schematic valley's.
      </p>
    </div>
  );
}

const ZONE: Record<string, string> = { low: "Low", judgment: "Judgment", high: "High" };

function rankCell(r: SourceRank | undefined) {
  if (!r) return { d: "—", v: "—", z: "—", title: "" };
  if (!r.covered) return { d: "n/c", v: "", z: "", title: r.reason };
  if (!r.flooded) return { d: "dry", v: "", z: "", title: r.reason };
  return { d: f1(r.depthFt), v: f1(r.velocityFps), z: r.zone ? ZONE[r.zone] : "—", title: r.reason };
}

/**
 * One row per point. Points are the record's receptors today; 911 address points (ResultPoint source "911") slot in as
 * more rows with the same per-source columns once that layer is merged.
 */
function StructureTable({ dam, result, runs, ready }: { dam: DamRecord; result: Analysis; runs: Partial<Record<ResultSource, RunState>>; ready: ResultSource[] }) {
  const points = receptorPoints(dam, result);
  const byId = new Map(points.map((p) => [p.id, p]));
  const samples = Object.fromEntries(ready.map((s) => [s, new Map((runs[s]!.samples ?? []).map((x) => [x.id, x]))])) as Record<ResultSource, Map<string, import("@/lib/crest/types").ReceptorSample>>;
  const rows = dam.receptors.map((seed, i) => {
    const desk = result.flood.receptors[i] ? deskRank(result.flood.receptors[i]) : undefined;
    const ranks: Partial<Record<ResultSource | "desk", SourceRank>> = { desk };
    for (const s of ready) ranks[s] = rankSample(dam, i, samples[s].get(seed.id), runs[s]!.summary!.label);
    return { seed, point: byId.get(seed.id), ranks, flags: disagreement(ranks) };
  });
  const cols: (ResultSource | "desk")[] = ["desk", ...ready];
  return (
    <div className="overflow-x-auto rounded-md border border-line bg-card p-4" data-testid="structure-table">
      <h3 className="font-medium">Per structure</h3>
      <table className="mt-2 w-full text-left text-sm">
        <thead className="text-xs tracking-wide text-muted uppercase">
          <tr>
            <th className="px-2 py-1 font-medium" rowSpan={2}>Point</th>
            {cols.map((c) => (
              <th key={c} colSpan={3} className="border-l border-line px-2 py-1 font-medium" style={{ color: SOURCE_COLOR[c] }}>
                {SOURCE_LABEL[c]}
              </th>
            ))}
            <th className="px-2 py-1 font-medium" rowSpan={2}>Disagreement</th>
          </tr>
          <tr>
            {cols.map((c) => [
              <th key={c + "d"} className="border-l border-line px-2 py-1 font-medium">ft</th>,
              <th key={c + "v"} className="px-2 py-1 font-medium">ft/s</th>,
              <th key={c + "z"} className="px-2 py-1 font-medium">TM 11</th>,
            ])}
          </tr>
        </thead>
        <tbody className="tabular-nums">
          {rows.map((r) => (
            <tr key={r.seed.id} className={"border-t border-line " + (r.flags.length ? "bg-[var(--amber-bg)]" : "")} data-testid="structure-row">
              <td className="px-2 py-1">
                {r.seed.name}
                <span className="block text-xs text-muted">
                  {r.seed.kind}
                  {r.point ? `, ${r.point.placed}` : ", not placed"}
                </span>
              </td>
              {cols.map((c) => {
                const k = rankCell(r.ranks[c]);
                return [
                  <td key={c + "d"} className="border-l border-line px-2 py-1" title={k.title}>{k.d}</td>,
                  <td key={c + "v"} className="px-2 py-1">{k.v}</td>,
                  <td key={c + "z"} className="px-2 py-1">{k.z}</td>,
                ];
              })}
              <td className="px-2 py-1 text-xs">{r.flags.length ? <span className="text-amber">⚑ {r.flags.join("; ")}</span> : <span className="text-muted">—</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-muted">
        Raster values are the cell under each point (full-resolution grid), with no pad elevation subtracted. n/c = outside that
        run's grid. Flags: wet in one source and dry in another, a different TM 11 zone or ND class, or a depth spread over 1 ft
        and half the deepest.
      </p>
    </div>
  );
}
