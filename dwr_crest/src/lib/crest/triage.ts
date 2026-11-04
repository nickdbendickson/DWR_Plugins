/**
 * Per-dam triage recommendation (Nick, 2026-11-04, "Do all", step 3 of landing Crest as the suite's fast first-pass
 * screener). Crest is a SCREENING stand-in, never the layer of record: its job in front of the Screening plugin is to
 * sort a dam list into "clearly trivial", "uncertain: run it" and "needs a Runner run". This module turns what Crest
 * already computed for a dam (the saved inundation layer and its extent check, the reach summary, the flood-size gate
 * and the desk screen) into that call, with a terrain confidence and the reasons, so the role is built into the tool
 * rather than left to whoever reads the footprint.
 *
 * WHY EDGE SHIFT IS THE CONFIDENCE SIGNAL
 * The four-dam baseline (crest-inundation/csi-summary-2026-11-03, reach-2026-11-03) sorts on terrain: Crest matches the
 * Runner on a confined valley (DWR-3034, CSI 0.70) and not on the flat or thin-sheet floods (DWR-2801 Absaraka 0.23,
 * DWR-2865 0.30, DWR-4031 0.35), where even two refined 2-D models of 2801 agree only at CSI 0.35–0.46. What makes a
 * footprint fragile is how much ground sits within a small rise of the flood surface: in a confined valley a foot of
 * extra (or missing) water moves the edge a cell or two up a steep bank; on a flat plain it moves the edge hundreds of
 * metres. Crest's depths are good to about a foot against the 2-D models (depth bias where both are wet: +1.0 ft on
 * 2801 after the cap, −0.9 ft on 2865), so a ±1 ft band around Crest's own surface is an honest measure of how far its
 * edge can be trusted. extent.ts measures it on the routed spread's depth grid and the DEM it was built on:
 *
 *   outer fringe = dry ground connected to the flood, within 500 m of it, that a surface 1 ft higher would wet
 *   edge shift   = outer fringe area ÷ wet-edge length: how far (m) the edge moves outward per foot of depth error
 *
 * Edge shift is a distance, so it does not grow just because a flood is small (an area ratio does: a 27 ac sliver has
 * a long edge for its area). On the baseline, built exactly as the desk builds them (scripts/triage-dams.mjs; 2801 on
 * its routed layer, the three small dams on the routed-spread probe beside their terrain outline):
 *
 *   DWR-3034  26 m   confined valley / basin   CSI 0.70 (1 ft 0.63)   -> high
 *   DWR-2801 127 m   flat multi-channel plain  CSI 0.23 (1 ft 0.18)   -> medium, then low: incomplete downstream
 *   DWR-4031 149 m   tiny pool, thin sheet     CSI 0.35 (1 ft 0.10)   -> medium (extent only; its depths are noise)
 *   DWR-2865 158 m   incised, moderate release CSI 0.30 (1 ft 0.31)   -> medium
 *
 * The confidence words match the published accuracy envelope (crest-inundation/accuracy-envelope-2026-11-04, the
 * trust map): High = confined valley or closed basin, Medium = open or thin-sheet terrain where the footprint is a
 * minimum, Low = the routed layer could not finish the flood downstream (its "one hard rule"), the flat sheet-flow
 * plain. Edge shift alone never says Low: four dams cannot place a second threshold, and the dams it would catch
 * already carry the incomplete-downstream alert or the large-flood gate.
 *
 * WHAT THIS DOES NOT CLAIM
 * - The threshold sits in a wide gap (26 m vs 127–158 m) between four dams. It is provisional, like the flood-size
 *   gate, and should be re-fit as more Runner runs come in.
 * - "Clearly trivial" means Crest's own screen shows nothing at stake on a footprint that is complete and holds up to
 *   a foot of depth error. It orders the Runner worklist; it is not a hazard classification, and the class of record
 *   still rests on a model run or an engineer.
 * - High confidence is not a Runner result. It means Crest is a fair screen there, not that it is right.
 */
import type { ExtentSensitivity } from "./extent.ts";
import { GATE_VOLUME_ACFT, VOLUME_RATIO_BAND } from "./inundation.ts";
import type { OutlineState } from "./outline.ts";
import type { Analysis, DamRecord } from "./types.ts";

/**
 * Edge shift (m per foot of depth error) at or below which the terrain is confined enough for a high-confidence screen;
 * above it, medium. Provisional, fit in the gap between the four baseline dams (26 m vs 127–158 m).
 */
export const CONFIDENCE_SHIFT_M = 60;

/** Released volume (ac-ft) at or below which the flood is a thin sheet whose depths are noise (trust map, DWR-4031). */
export const THIN_SHEET_RELEASE_ACFT = 10;

export type Confidence = "high" | "medium" | "low";
export type Recommendation = "trivial" | "uncertain" | "needs-runner";

/** Labels shared with the published accuracy envelope (trust map): confined = high, flat sheet-flow plain = low. */
export const RECOMMENDATION_LABEL: Record<Recommendation, string> = {
  trivial: "Clearly trivial: screen only",
  uncertain: "Uncertain: run it",
  "needs-runner": "Needs a Runner run",
};

export const CONFIDENCE_LABEL: Record<Confidence, string> = {
  high: "High confidence (confined valley or closed basin)",
  medium: "Medium confidence (open terrain: read the footprint as a minimum)",
  low: "Low confidence (incomplete or unchecked footprint)",
};

export type Triage = {
  recommendation: Recommendation;
  label: string;
  confidence: Confidence;
  /** Edge shift (m per foot of depth error) the confidence rests on; null when no extent check is saved. */
  edgeShiftM: number | null;
  /** Plain-words reasons, most decisive first. */
  reasons: string[];
  /** One line for the panels and the CSV. */
  summary: string;
};

/** The terrain confidence an edge shift supports on its own (never low: see the module note). */
export function confidenceOf(edgeShiftM: number): Exclude<Confidence, "low"> {
  return edgeShiftM <= CONFIDENCE_SHIFT_M ? "high" : "medium";
}

const worse = (a: Confidence, b: Confidence): Confidence => (a === "low" || b === "low" ? "low" : a === "medium" || b === "medium" ? "medium" : "high");
const ac = (v: number) => Math.round(v).toLocaleString("en-US");
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

/** One line on what the extent check found, for the reasons list. */
export function extentLine(e: ExtentSensitivity): string {
  const conf = confidenceOf(e.outShiftM);
  const on = e.basis === "routed-probe" ? " (measured on a routed-spread probe on the same terrain)" : "";
  const base = `A foot of depth error moves the flood's edge about ${Math.round(e.outShiftM)} m${on}; a surface 1 ft higher would add ${ac(e.outerAc)} ac to the ${ac(e.wetAc)} ac it floods`;
  return conf === "high"
    ? `${base}. Confined terrain: the footprint holds up.`
    : `${base}. Open terrain: the edge is fragile, so read the footprint as a minimum; a receptor just outside it is not cleared.`;
}

/**
 * The triage call for one dam from the analysis of record and its saved inundation layer. An imported model run that
 * is the record settles it. Otherwise, in order:
 *   needs-runner  a large flood (the flood-size gate), an extent the routed layer could not finish (the reach
 *                 stopped with water still at the terrain's edge), or anything flooded in Crest's screen
 *                 (something is at stake, and a class with something at stake rests on a model run, not a screen)
 *   uncertain     nothing flooded, but the call is not safe to make: medium or low confidence, a volume out of band, a
 *                 flood that ends in a closed basin (the footprint is trusted, but a culvert the DEM can't see would
 *                 send the water on), a routed probe that runs off its terrain, no extent check, or a receptor the
 *                 screen could not resolve
 *   trivial       nothing flooded on a complete, high-confidence footprint
 */
export function triageDam(dam: DamRecord, result: Analysis, outline: OutlineState): Triage {
  const done = (recommendation: Recommendation, confidence: Confidence, edgeShiftM: number | null, reasons: string[]): Triage => ({
    recommendation,
    label: RECOMMENDATION_LABEL[recommendation],
    confidence,
    edgeShiftM,
    reasons,
    summary: `${RECOMMENDATION_LABEL[recommendation]} (${confidence} confidence). ${reasons[0] ?? ""}`.trim(),
  });

  if (dam.resultsOfRecord)
    return done("trivial", "high", null, [
      `An imported ${dam.resultsOfRecord.label} run is the layer of record; there is nothing left for Crest to triage on this dam.`,
    ]);

  const releasedAcFt = result.breach.volumeAcFt;
  const large = Number.isFinite(releasedAcFt) && releasedAcFt >= GATE_VOLUME_ACFT;
  const receptors = result.flood.receptors;
  const flooded = receptors.filter((r) => r.flooded).length;
  const unresolved = receptors.filter((r) => r.unresolved && !r.outsideAoi).length;
  const ready = outline.state === "ready" ? outline : null;
  const ext = ready?.stats.extent;
  const shift = ext ? ext.outShiftM : null;

  let confidence: Confidence = shift == null ? "low" : confidenceOf(shift);
  const hard: string[] = [];
  const soft: string[] = [];
  if (!ready) soft.push("No terrain layer is built yet, so there is no footprint to judge: build the inundation layer to triage this dam.");
  else if (!ext) soft.push("The saved terrain layer has no extent check (built before it, or the routed probe could not be built): build it again for a confidence.");
  else soft.push(extentLine(ext));

  if (ready?.routed?.reach?.incomplete)
    hard.push(`The flood runs past the terrain Crest could build (${ac(ready.routed.reach.strandedAcFt)} ac-ft still travelling at the edge): the footprint is incomplete downstream.`);
  if (ext) {
    if (ext.basis === "routed-probe" && ext.reachedEdge) {
      confidence = worse(confidence, "medium");
      soft.push("The routed probe reaches the edge of the terrain with water still moving: the flood may run further than the layer shows.");
    }
    const ratio = ext.volumeRatio;
    // The band is for a moving flood's envelope; water parked in a closed basin lays down less by design.
    if (!ext.stoppedInBasin && ratio != null && Number.isFinite(ratio) && ratio > 0 && (ratio < VOLUME_RATIO_BAND.lo || ratio > VOLUME_RATIO_BAND.hi)) {
      confidence = worse(confidence, "medium");
      soft.push(`The routed spread lays down ${ratio.toFixed(1)}× the released volume, outside the ${VOLUME_RATIO_BAND.lo}–${VOLUME_RATIO_BAND.hi}× a moving flood's envelope usually holds.`);
    }
  }
  const basin = !!ext?.stoppedInBasin;
  if (basin)
    soft.push("The flood ends in a closed basin: if a road or rail fill closes it, a culvert or bridge the DEM doesn't show would let the water on (both Crest and the Runner treat the fill as a dam).");
  if (Number.isFinite(releasedAcFt) && releasedAcFt <= THIN_SHEET_RELEASE_ACFT)
    soft.push(`Tiny release (${releasedAcFt.toFixed(1)} ac-ft): the flood is a thin sheet, so use the footprint for "is anything in reach" and not its depths.`);
  if (hard.length) confidence = "low";

  const decisive: string[] = [];
  if (large) decisive.push(`Large flood: ${ac(releasedAcFt)} ac-ft released (≥ ${GATE_VOLUME_ACFT}), where the fast layers can badly under-predict.`);
  decisive.push(...hard);
  if (flooded) decisive.push(`${plural(flooded, "structure or receptor is", "structures or receptors are")} flooded in Crest's screen: something is at stake, so the class should rest on a model run.`);
  if (decisive.length) return done("needs-runner", confidence, shift, [...decisive, ...soft]);

  if (unresolved) soft.push(`${plural(unresolved, "receptor lies", "receptors lie")} past the desk reach and could not be screened.`);
  if (confidence !== "high" || unresolved || !ext || basin)
    return done("uncertain", confidence, shift, [
      confidence === "high" && basin && !unresolved
        ? "Nothing is flooded in Crest's screen and the footprint holds up, but it ends at a closed basin a hidden culvert could drain: check it before calling the dam trivial."
        : "Nothing is flooded in Crest's screen, but the footprint is not robust enough to call the dam trivial.",
      ...soft,
    ]);
  return done("trivial", confidence, shift, ["Nothing is flooded in Crest's screen, on a complete footprint that holds up to a foot of depth error.", ...soft]);
}

const csvCell = (v: string | number) => {
  const s = String(v);
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};

/** Order for sorting a list: what needs a Runner first, then what is uncertain, then what is trivial. */
export const RECOMMENDATION_ORDER: Record<Recommendation, number> = { "needs-runner": 0, uncertain: 1, trivial: 2 };

/** A triaged dam list as CSV, Runner jobs first (largest release first within a group). */
export function triageCsv(rows: { dam: DamRecord; result: Analysis; triage: Triage }[]): string {
  const head = ["dam_id", "nid", "name", "county", "released_acft", "recommendation", "confidence", "edge_shift_m", "reasons"];
  const sorted = [...rows].sort(
    (a, b) =>
      RECOMMENDATION_ORDER[a.triage.recommendation] - RECOMMENDATION_ORDER[b.triage.recommendation] ||
      b.result.breach.volumeAcFt - a.result.breach.volumeAcFt,
  );
  const body = sorted.map(({ dam, result, triage: t }) =>
    [
      dam.id,
      dam.nid,
      dam.name,
      dam.county,
      Math.round(result.breach.volumeAcFt),
      t.recommendation,
      t.confidence,
      t.edgeShiftM == null ? "" : Math.round(t.edgeShiftM),
      t.reasons.join(" | "),
    ]
      .map(csvCell)
      .join(","),
  );
  return [head.join(","), ...body].join("\n") + "\n";
}
