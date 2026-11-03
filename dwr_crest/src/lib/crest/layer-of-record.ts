/**
 * Which inundation layer is the layer of record for a dam, and which large dams still need a Runner run (Nick,
 * 2026-11-03, "Do 1-3": item 1, the Runner-run substitution).
 *
 * The fill review (crest-inundation/gate-review-2026-11-03/FINDINGS.md) showed that neither terrain layer can be
 * trusted on a large flood: on DWR-2801 the bathtub outline covers only about a tenth (0.10×) of the area the ND
 * Breach Runner floods, and the routed layer's better score there is largely its downstream leak. A dam that already
 * has an imported Runner run must therefore show that run as the layer of record, whatever the flood-size gate in
 * inundation.ts would pick, and the large dams that have no run yet are a worklist for the Runner.
 *
 * Pure module: the desk, the Flood step's terrain panel, the Dashboard and the Results step read it.
 */
import { GATE_VOLUME_ACFT } from "./inundation.ts";
import type { OutlineState } from "./outline.ts";
import { SOURCE_LABEL } from "./results.ts";
import type { Analysis, DamRecord, ResultsOfRecord, ResultSource } from "./types.ts";

/** The source the ND Dam Breach Runner writes (LISFLOOD-FP rasters). */
export const RUNNER_SOURCE: ResultSource = "lisflood";

export type LayerKind = "runner" | "model" | "routed" | "outline" | "strip";

export type LayerOfRecord = {
  kind: LayerKind;
  /** Plain-words name of the layer of record. */
  label: string;
  /** The imported run, when the layer of record is one. */
  ref: ResultsOfRecord | null;
  /** The released breach volume reaches the flood-size gate: a flood the terrain layers cannot be trusted on. */
  large: boolean;
  releasedAcFt: number;
  /** Large flood with no imported model run as the record: the screen rests on a terrain layer that can under-predict. */
  needsRunner: boolean;
  /** One line for the panels: what the record is and why. */
  note: string;
};

/** A released breach volume at or above the gate's threshold: the dams the terrain layers are least safe on. */
export function isLargeFlood(result: Analysis): boolean {
  const v = result.breach.volumeAcFt;
  return Number.isFinite(v) && v >= GATE_VOLUME_ACFT;
}

/** The warning shown wherever a large dam's screen rests on a terrain layer. */
export function runnerNeededText(releasedAcFt: number): string {
  return (
    `Large flood (${Math.round(releasedAcFt).toLocaleString("en-US")} ac-ft released): the terrain layers can badly under-predict a flood this size ` +
    "(on DWR-2801 the outline covered about a tenth of the area the ND Breach Runner floods). Run this dam in the ND Breach Runner " +
    "and import the run on Results; it then becomes the layer of record."
  );
}

/**
 * The layer of record. An imported model run that is the source of record always wins (a Runner run first), whatever
 * the gate picks; otherwise the built terrain layer (routed or outline); otherwise the dated strip.
 */
export function layerOfRecord(dam: DamRecord, result: Analysis, outline: OutlineState): LayerOfRecord {
  const ref = dam.resultsOfRecord ?? null;
  const large = isLargeFlood(result);
  const releasedAcFt = result.breach.volumeAcFt;
  if (ref) {
    const runner = ref.source === RUNNER_SOURCE;
    return {
      kind: runner ? "runner" : "model",
      label: ref.label,
      ref,
      large,
      releasedAcFt,
      needsRunner: false,
      note:
        `Layer of record: ${ref.label}, an imported ${runner ? "ND Breach Runner" : SOURCE_LABEL[ref.source]} run. ` +
        "The terrain layer is shown for comparison only and does not drive the screen, the lists or the report.",
    };
  }
  const kind: LayerKind = outline.state === "ready" ? (outline.layer === "routed" ? "routed" : "outline") : "strip";
  const label = kind === "routed" ? "Routed terrain flood" : kind === "outline" ? "Terrain outline" : "Schematic flood strip (dated last resort)";
  return {
    kind,
    label,
    ref: null,
    large,
    releasedAcFt,
    needsRunner: large,
    note: large
      ? runnerNeededText(releasedAcFt)
      : "This terrain layer is Crest's inundation layer of record until a Runner run is imported. The schematic flood strip is a dated last resort, kept only for offline use or until this layer is built; build this once and it is saved with the dam.",
  };
}

/** What adoptRunAsRecord needs to know about a loaded run. */
export type LoadedRun = { source: ResultSource; blocked: boolean };

/**
 * Should a loaded run become the source of record on its own? Only a Runner (LISFLOOD-FP) run, only when the import
 * checks raise no error, only when no run is the record already, and not when the user deliberately chose the desk
 * estimate for this dam.
 */
export function adoptRunAsRecord(dam: DamRecord, run: LoadedRun): boolean {
  return run.source === RUNNER_SOURCE && !run.blocked && !dam.resultsOfRecord && dam.sourceOfRecordChoice !== "desk";
}

export type WorklistRow = {
  id: string;
  name: string;
  nid: string;
  county: string;
  heightFt: number;
  storageAcFt: number;
  releasedAcFt: number;
  peakCfs: number;
  /** Why it is on the list. */
  status: "no run" | "run loaded, not the record" | "desk chosen over a run";
};

/**
 * Large dams (released volume at or above the gate) whose layer of record is not an imported model run, largest
 * release first. `loaded` holds the ids of dams with a Runner run loaded in this session.
 */
export function runnerWorklist(dams: DamRecord[], analyzeFn: (d: DamRecord) => Analysis, loaded: ReadonlySet<string> = new Set()): WorklistRow[] {
  const rows: WorklistRow[] = [];
  for (const d of dams) {
    if (d.resultsOfRecord) continue;
    let a: Analysis;
    try {
      a = analyzeFn(d);
    } catch {
      continue;
    }
    if (!isLargeFlood(a)) continue;
    rows.push({
      id: d.id,
      name: d.name,
      nid: d.nid,
      county: d.county,
      heightFt: d.heightFt,
      storageAcFt: d.storageAcFt,
      releasedAcFt: a.breach.volumeAcFt,
      peakCfs: a.breach.peakCfs,
      status: d.sourceOfRecordChoice === "desk" ? "desk chosen over a run" : loaded.has(d.id) ? "run loaded, not the record" : "no run",
    });
  }
  return rows.sort((x, y) => y.releasedAcFt - x.releasedAcFt);
}

const csvCell = (v: string | number) => {
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

/** The worklist as CSV, for handing to whoever runs the Runner. */
export function worklistCsv(rows: WorklistRow[]): string {
  const head = ["dam_id", "nid", "name", "county", "height_ft", "storage_acft", "released_acft", "breach_peak_cfs", "status"];
  const body = rows.map((r) =>
    [r.id, r.nid, r.name, r.county, r.heightFt, r.storageAcFt, Math.round(r.releasedAcFt), Math.round(r.peakCfs), r.status].map(csvCell).join(","),
  );
  return [head.join(","), ...body].join("\n") + "\n";
}
