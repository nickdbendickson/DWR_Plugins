import type { Analysis, DamRecord, SeriesPoint } from "./types";

/** Hydrograph CSV in FIMsim / Breach Runner format: time_hours,discharge_cms */
export function exportHydrographCsv(series: SeriesPoint[]): string {
  const lines = ["time_hours,discharge_cms"];
  for (const p of series) {
    const cms = p.qCfs / 35.3146667;
    lines.push(`${p.tHr.toFixed(4)},${cms.toFixed(4)}`);
  }
  return lines.join("\n") + "\n";
}

/** Minimal dam.json matching fields the Runner / Screening read. */
export function exportDamJson(dam: DamRecord, analysis: Analysis): string {
  const obj = {
    dam_id: dam.nid || dam.id,
    name: dam.name,
    height_ft: dam.heightFt,
    storage_acft: dam.storageAcFt,
    length_ft: dam.lengthFt,
    crest_ft: analysis.datum.crestNavdFt,
    method: dam.method,
    failure_mode: dam.failureMode,
    screened: {
      level: analysis.screen.level,
      level_label: analysis.screen.label,
      judgment_as_probable: analysis.screen.judgmentAsProbable,
      acer_level: analysis.screen.acerLevel,
      judgment: analysis.screen.judgment,
      notes: analysis.screen.reasons,
    },
    hydrograph_peak_cfs: analysis.breach.peakCfs,
    volume_acft: analysis.breach.volumeAcFt,
  };
  return JSON.stringify(obj, null, 2) + "\n";
}

/** LISFLOOD package: .par, .bci (point source just DS), .bdy (unit-width). */
export function generateLisfloodPackage(
  dam: DamRecord,
  series: SeriesPoint[],
  cellSizeM = 10,
  pointX = 0,
  pointY = 0,
): { par: string; bci: string; bdy: string } {
  const resroot = (dam.nid || "dam").toLowerCase().replace(/[^a-z0-9]/g, "_");
  const par = `resroot ${resroot}
dirroot .
solver 1
drycheck 1
start 0
end ${Math.ceil((series[series.length - 1]?.tHr ?? 1) * 3600)}
mass 1
hazard 1
mint_hk 1
qoutput 1
`;
  // Point source just downstream of the dam
  const bci = `P ${pointX} ${pointY} QVAR upstream1
`;
  // BDY: value-then-time, unit-width = Q_cms / cell_size
  const bdyLines = ["upstream1"];
  for (const p of series) {
    const cms = p.qCfs / 35.3146667;
    const unitWidth = cms / cellSizeM;
    const tSec = p.tHr * 3600;
    bdyLines.push(`${unitWidth.toFixed(6)} ${tSec.toFixed(1)}`);
  }
  const bdy = bdyLines.join("\n") + "\n";
  return { par, bci, bdy };
}
