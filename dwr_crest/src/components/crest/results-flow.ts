/**
 * Results step plumbing: importing a run into the results worker, keeping its numbers and receptor samples in step
 * with the record (breach, direction, receptors), and the analysis / strip of the source of record that the Screen,
 * PLSS list, Report and exports use.
 */
import { useEffect, useMemo } from "react";
import type { analyze } from "@/lib/crest/engine";
import { floodStrip, type FloodStrip, type StripCommon } from "@/lib/crest/floodstrip";
import type { InFile } from "@/lib/crest/raster-engine";
import { wantFile } from "@/lib/crest/raster-meta";
import { results } from "@/lib/crest/results-client";
import { analysisOfRecord, type Check, receptorPoints, recordChecks, resultsContext } from "@/lib/crest/results";
import { runKey, type RunState, useResults } from "@/lib/crest/results-store";
import { useDesk } from "@/lib/crest/store";
import type { DamRecord, ResultsOfRecord, ResultSource } from "@/lib/crest/types";

type Analysis = ReturnType<typeof analyze>;

const pathOf = (f: File) => (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name;

/** The files of a pick that the engine reads (result grids, dam.json, zips); the rest are not even read. */
export function pickFiles(list: FileList | File[]) {
  const all = [...list];
  return { use: all.filter((f) => wantFile(pathOf(f))), total: all.length };
}

function pointsFor(dam: DamRecord, result: Analysis) {
  return receptorPoints(dam, result).map((p) => ({ id: p.id, lon: p.lon, lat: p.lat }));
}

export async function importRun(dam: DamRecord, result: Analysis, source: ResultSource, list: File[], crs: string) {
  const key = runKey(dam.id, source);
  const { patch } = useResults.getState();
  const inputs = list.map(pathOf);
  patch(key, { damId: dam.id, source, status: "loading", progress: `Reading ${list.length} file${list.length === 1 ? "" : "s"}…`, error: undefined, inputs, strip: undefined, stripKey: undefined });
  try {
    const files: InFile[] = [];
    for (const f of list) files.push({ name: pathOf(f), buf: await f.arrayBuffer() });
    const rc = resultsContext(dam, result);
    const summary = await results.import({ type: "import", key, source, files, crs, ctx: rc.ctx }, (progress) =>
      useResults.getState().patch(key, { damId: dam.id, source, progress }),
    );
    const points = pointsFor(dam, result);
    const samples = await results.sample({ type: "sample", key, points });
    patch(key, {
      damId: dam.id,
      source,
      status: "ready",
      progress: undefined,
      summary,
      samples,
      ctxKey: JSON.stringify(rc.ctx),
      pointsKey: JSON.stringify(points),
      importedAt: new Date().toISOString(),
    });
  } catch (e) {
    patch(key, { damId: dam.id, source, status: "error", progress: undefined, error: (e as Error).message });
  }
}

export function dropRun(dam: DamRecord, source: ResultSource) {
  const key = runKey(dam.id, source);
  useResults.getState().remove(key);
  // A saved source of record stays (its numbers and samples are on the record); it just cannot resample.
  void results.drop(key).catch(() => {});
}

/** Raster checks plus the record checks (dam match, coverage, edge). */
export function allChecks(dam: DamRecord, result: Analysis, run: RunState | undefined): Check[] {
  const s = run?.summary;
  if (!s) return [];
  const out = [...s.checks];
  if (s.stats) out.push(...recordChecks(dam, result, s.stats, s.runDam));
  else out.push({ level: "error", text: "No breach location on this record, so the run cannot be placed or compared." });
  return out;
}

export function refFromRun(dam: DamRecord, result: Analysis, run: RunState): ResultsOfRecord | null {
  const s = run.summary;
  if (!s?.stats || !run.samples) return null;
  return {
    source: run.source,
    label: s.label,
    files: s.used.map((n) => n.split(/[\\/]/).pop() ?? n),
    importedAt: run.importedAt ?? new Date().toISOString(),
    crs: s.crs,
    stats: s.stats,
    samples: run.samples,
    warnings: allChecks(dam, result, run)
      .filter((c) => c.level !== "ok")
      .map((c) => c.text),
  };
}

const sameRef = (a: ResultsOfRecord | undefined, b: ResultsOfRecord | null) =>
  !!a && !!b && JSON.stringify({ ...a, importedAt: "" }) === JSON.stringify({ ...b, importedAt: "" });

/** The strip a run gives for the PLSS list: its max-depth raster coarsened to ≥ 60 m, banded like the desk strip. */
function stripCommon(dam: DamRecord, result: Analysis, label: string): StripCommon {
  const desk = floodStrip(dam, result).strip;
  return {
    disclaimer: `Depth bands from the ${label} max-depth raster (cells merged to 60 m or more); a model result, not a mapped flood extent of record`,
    dam_id: dam.nid || dam.id,
    dam_name: dam.name,
    crest_navd88_ft: desk?.properties.crest_navd88_ft ?? Math.round(result.datum.crestNavdFt * 1000) / 1000,
    peak_cfs: Math.round(result.breach.peakCfs),
    engine_version: label,
  };
}

/**
 * Keeps every loaded run of the active dam in step with the record: rescans when the breach or direction moves,
 * resamples when receptors move, refreshes the saved source of record, and builds its PLSS strip.
 */
export function useResultsSync(dam: DamRecord, result: Analysis) {
  const runs = useResults((s) => s.runs);
  const update = useDesk((s) => s.update);
  const rc = useMemo(() => resultsContext(dam, result), [dam, result]);
  const ctxKey = JSON.stringify(rc.ctx);
  const points = useMemo(() => pointsFor(dam, result), [dam, result]);
  const pointsKey = JSON.stringify(points);
  useEffect(() => {
    const { patch } = useResults.getState();
    for (const run of Object.values(runs)) {
      if (run.damId !== dam.id || run.status !== "ready") continue;
      if (rc.ctx && run.ctxKey !== ctxKey) {
        patch(run.key, { damId: run.damId, source: run.source, ctxKey, status: "loading", progress: "Rescanning for the new breach location…" });
        void results
          .rescan({ type: "rescan", key: run.key, ctx: rc.ctx })
          .then((summary) => patch(run.key, { damId: run.damId, source: run.source, summary, status: "ready", progress: undefined, strip: undefined, stripKey: undefined }))
          .catch((e: Error) => patch(run.key, { damId: run.damId, source: run.source, status: "error", error: e.message }));
        continue;
      }
      if (run.pointsKey !== pointsKey) {
        patch(run.key, { damId: run.damId, source: run.source, pointsKey });
        void results
          .sample({ type: "sample", key: run.key, points })
          .then((samples) => patch(run.key, { damId: run.damId, source: run.source, samples }))
          .catch(() => {});
        continue;
      }
      const rec = dam.resultsOfRecord;
      if (rec?.source === run.source) {
        const ref = refFromRun(dam, result, run);
        if (ref && !sameRef(rec, ref) && !allChecks(dam, result, run).some((c) => c.level === "error"))
          update({ resultsOfRecord: { ...ref, importedAt: rec.importedAt } });
        if (rc.ctx && run.stripKey !== ctxKey) {
          patch(run.key, { damId: run.damId, source: run.source, stripKey: ctxKey });
          void results
            .strip({ type: "strip", key: run.key, ctx: rc.ctx, common: stripCommon(dam, result, run.summary!.label), direction: rc.direction ?? "" })
            .then((strip) => patch(run.key, { damId: run.damId, source: run.source, strip }))
            .catch(() => patch(run.key, { damId: run.damId, source: run.source, strip: null }));
        }
      }
    }
  }, [runs, dam, result, rc, ctxKey, points, pointsKey, update]);
}

/** The analysis the Screen, Report and dam.json use: the desk's, or re-ranked on the source of record. */
export function useAnalysisOfRecord(dam: DamRecord, result: Analysis) {
  return useMemo(() => analysisOfRecord(dam, result), [dam, result]);
}

/**
 * The strip behind the PLSS list and the strip exports: the source of record's raster strip when that run is loaded
 * in this session, else the desk strip (with a note when a run is the source of record but not loaded).
 */
export function useStripOfRecord(dam: DamRecord, result: Analysis): {
  strip: FloodStrip | null;
  reason: string | null;
  basis: string;
  pending: boolean;
} {
  const run = useResults((s) => (dam.resultsOfRecord ? s.runs[runKey(dam.id, dam.resultsOfRecord.source)] : undefined));
  return useMemo(() => {
    const desk = floodStrip(dam, result);
    const rec = dam.resultsOfRecord;
    if (!rec) return { strip: desk.strip, reason: desk.reason, basis: "the schematic desk strip", pending: false };
    if (run?.strip)
      return { strip: run.strip, reason: null, basis: `the ${rec.label} max-depth raster (source of record)`, pending: false };
    if (run?.status === "ready" && run.strip === undefined)
      return { strip: desk.strip, reason: desk.reason, basis: `the desk strip until the ${rec.label} strip is built`, pending: true };
    return {
      strip: desk.strip,
      reason: desk.reason,
      basis: `the desk strip: ${rec.label} is the source of record but its rasters are not loaded in this session (import them on Results to list its sections)`,
      pending: false,
    };
  }, [dam, result, run]);
}

