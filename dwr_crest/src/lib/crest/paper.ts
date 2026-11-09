/**
 * Style 3 "paper report" (Crest build pack, 2026-11-02): the numbers, flags and SVG the Report step's paper sheet
 * shows. The same sheet is printed (browser print / Save as PDF), so what is on screen is what is on paper. Pure
 * functions, no DOM: the component is src/components/crest/PaperReport.tsx.
 */
import { csvCell } from "./csv.ts";
import { num } from "./format.ts";
import type { Analysis, SeriesPoint } from "./types.ts";

/** On every result surface and every printed sheet (brief rule 6). User-tested wording: keep it as written. */
export const PAPER_DISCLAIMER = "Screening estimate — not an ND Dam Safety or ACER classification.";

/** The notice box (verbatim from the Style 3 template). */
export const PAPER_NOTICE =
  "This is a screening-level estimate produced by Crest. It is not an ND Dam Safety or ACER hazard classification. " +
  "Decisions affecting life safety must use the full hydraulic study (Runner / LISFLOOD-FP) results.";

/** Printed receptor rows are capped; the CSV always has every one. */
export const PAPER_ROW_CAP = 25;

export type PaperRow = {
  name: string;
  kind: string;
  depth: string;
  vel: string;
  /** Front arrival (h): the desk's travel time to the front, or the imported run's arrival raster. */
  front: string;
  /** Routed peak (h): not computed per structure yet; kept as its own column (brief rule 7: never merged). */
  peak: string;
  dem: string;
  rating: string;
};

type Receptor = Analysis["flood"]["receptors"][number];

function row(r: Receptor): PaperRow {
  return {
    name: r.name,
    kind: r.kind,
    depth: r.unresolved ? "unresolved" : r.flooded ? num(r.depthFt, 2) : "dry",
    vel: r.flooded ? num(r.velocityFps, 2) : "—",
    front: r.arrivalHr == null ? "—" : num(r.arrivalHr, 2),
    peak: "—",
    dem: "—",
    rating: r.ndClass ?? "—",
  };
}

/** Affected structures (wet, or unresolved so a possible High is never hidden), deepest first. */
export function affected(result: Analysis): Receptor[] {
  return result.flood.receptors
    .filter((r) => r.flooded || r.unresolved)
    .sort((a, b) => b.depthFt - a.depthFt);
}

export function paperRows(
  result: Analysis,
  cap = PAPER_ROW_CAP,
): { rows: PaperRow[]; shown: number; total: number } {
  const all = affected(result);
  const rows = all.slice(0, cap).map(row);
  return { rows, shown: rows.length, total: all.length };
}

/** Every receptor (dry ones too), as the CSV export from the paper toolbar. */
export function receptorCsv(result: Analysis): string {
  const head = [
    "Structure",
    "Kind",
    "Distance km",
    "Depth ft",
    "Vel ft/s",
    "Front arrival hr",
    "Routed peak hr",
    "DEM",
    "Class",
    "Reason",
  ];
  const lines = [head.join(",")];
  for (const r of [...result.flood.receptors].sort((a, b) => b.depthFt - a.depthFt)) {
    const p = row(r);
    lines.push(
      [
        p.name,
        p.kind,
        num(r.distanceKm, 2),
        p.depth,
        p.vel,
        p.front,
        p.peak,
        p.dem,
        p.rating,
        r.rankReason,
      ]
        .map((v) => csvCell(String(v)))
        .join(","),
    );
  }
  lines.push(csvCell(PAPER_DISCLAIMER));
  return lines.join("\n") + "\n";
}

/** Model flags carried into the report: the record's datum checks, the flood step's flags, the imported run's warnings. */
export function paperFlags(result: Analysis, runWarnings: string[] = []): string[] {
  return [...result.datum.warnings, ...result.flood.warnings, ...runWarnings];
}

/** "Medium" -> the stamp's class suffix; the stamp also carries the word, so it reads in grayscale. */
export function stampClass(level: string): "high" | "medium" | "low" | "none" {
  const l = level.toLowerCase();
  return l.startsWith("high")
    ? "high"
    : l.startsWith("medium")
      ? "medium"
      : l.startsWith("low")
        ? "low"
        : "none";
}

/** The hydrograph as plain SVG (no script), so it prints the same as on screen; the peak is labelled (value + time). */
export function hydrographSvg(series: SeriesPoint[]): string {
  const W = 400,
    H = 130,
    PAD = 24;
  if (!series.length)
    return `<svg viewBox="0 0 ${W} ${H}" width="100%" role="img" aria-label="No hydrograph"></svg>`;
  const tMax = Math.max(...series.map((p) => p.tHr)) || 1;
  const qMax = Math.max(...series.map((p) => p.qCfs)) || 1;
  const x = (t: number) => PAD + (t / tMax) * (W - PAD - 10);
  const y = (q: number) => H - 8 - (q / qMax) * (H - 24);
  const d = series
    .map((p, i) => `${i === 0 ? "M" : "L"}${x(p.tHr).toFixed(1)},${y(p.qCfs).toFixed(1)}`)
    .join(" ");
  const ip = series.findIndex((p) => p.qCfs === qMax);
  const px = x(series[ip].tHr),
    py = y(qMax);
  const label = `peak ${num(qMax, 0)} cfs @ ${num(series[ip].tHr, 2)} hr`;
  const right = px > W - 150;
  return `<svg viewBox="0 0 ${W} ${H + 6}" width="100%" role="img" aria-label="Breach hydrograph, ${label}">
<line x1="${PAD}" y1="${H - 8}" x2="${W - 10}" y2="${H - 8}" stroke="#2B2A26"/>
<line x1="${PAD}" y1="8" x2="${PAD}" y2="${H - 8}" stroke="#2B2A26"/>
<path d="${d}" fill="none" stroke="#2B2A26" stroke-width="2"/>
<circle cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="3.5" fill="#2B2A26"/>
<text x="${(right ? px - 7 : px + 7).toFixed(1)}" y="${(py + 3).toFixed(1)}" font-size="9" font-family="Arial" text-anchor="${right ? "end" : "start"}">${label}</text>
<text x="${W - 10}" y="${H + 5}" font-size="8.5" font-family="Arial" fill="#6B6255" text-anchor="end">hours after breach (0–${num(tMax, 1)})</text>
<text x="6" y="60" font-size="8.5" font-family="Arial" fill="#6B6255" transform="rotate(-90 8,60)">cfs</text>
</svg>`;
}
