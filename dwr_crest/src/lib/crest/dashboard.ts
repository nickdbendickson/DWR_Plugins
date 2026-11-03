/**
 * Style B (Crest build pack, 2026-11-02): the map-first results dashboard. Pure parts here (tokens, flag severity,
 * the dashboard's flags, sorting and paging the structure table, the hydrograph peak); the view is
 * src/components/crest/Dashboard.tsx.
 */
import type { OutlineState } from "./outline.ts";
import { PAPER_DISCLAIMER } from "./paper.ts";
import type { Analysis } from "./types.ts";

/** Design tokens (the same hex values as the :root variables in src/styles.css). */
export const crest = {
  teal: "#0E768A",
  tealDark: "#0B5C6C",
  tealLight: "#E0F2F5",
  bg: "#F5F5F0",
  panel: "#FFFFFF",
  ink: "#1F2933",
  muted: "#6B7280",
  border: "#E5E7EB",
  red: "#B91C1C",
  redBg: "#FEE2E2",
  amber: "#B45309",
  amberBg: "#FEF3C7",
  green: "#15803D",
  greenBg: "#DCFCE7",
} as const;

export const DISCLAIMER = PAPER_DISCLAIMER;

export type Severity = "ok" | "warning" | "critical";

/** Single source of truth for volume-flag severity (brief rule 4). Critical still permits going on, after acknowledgement. */
export function flagSeverity(flag: {
  wet_volume?: number;
  release_volume?: number;
  reservoir_volume?: number;
}): Severity {
  if (flag.wet_volume == null || flag.release_volume == null) return "ok";
  if (flag.reservoir_volume != null && flag.wet_volume > flag.reservoir_volume) return "critical";
  if (flag.wet_volume > flag.release_volume) return "warning";
  return "ok";
}

export type DashFlag = { key: string; text: string; sev: Exclude<Severity, "ok"> | "info" };

/** The method behind the depth layer the dashboard shows. */
export type DashMethod = "routed" | "outline" | "strip-legacy";

export function dashMethod(outline: OutlineState): DashMethod {
  return outline.state === "ready"
    ? outline.layer === "routed"
      ? "routed"
      : "outline"
    : "strip-legacy";
}

export const METHOD_LABEL: Record<DashMethod, string> = {
  routed: "Fast layer (terrain method): routed flood",
  outline: "Fast layer (terrain method): terrain outline",
  "strip-legacy": "Legacy strip — dated fallback",
};

/**
 * Flags for the status bar, worst first. At most one critical is shown at a time (brief rule 4); lesser flags are
 * listed after it. Volume flag: the fast layer's wet volume above what the breach releases (warning); the fast layer
 * does not compare against the reservoir yet, so it is never critical from that layer alone.
 */
export function dashboardFlags(
  result: Analysis,
  outline: OutlineState,
  opts: { volume?: { wet: number; release: number; reservoir?: number } } = {},
): DashFlag[] {
  const out: DashFlag[] = [];
  if (opts.volume) {
    const sev = flagSeverity({
      wet_volume: opts.volume.wet,
      release_volume: opts.volume.release,
      reservoir_volume: opts.volume.reservoir,
    });
    if (sev !== "ok") {
      const ratio = (opts.volume.wet / opts.volume.release).toFixed(1);
      out.push({
        key: "volume",
        sev,
        text: `Volume flag: wet ${ratio}× release${sev === "critical" ? ", above reservoir volume" : ""} — review`,
      });
    }
  } else if (outline.state === "ready" && outline.routed?.volumeFlag) {
    out.push({
      key: "volume",
      sev: "warning",
      text: "Volume flag: wet volume above release volume — review",
    });
  }
  if (outline.state === "ready" && (outline.routed?.reachedEdge || outline.stats.hitsCorridor))
    out.push({
      key: "edge",
      sev: "info",
      text: "Flood edge touched domain — extent may be larger",
    });
  if (outline.state !== "ready")
    out.push({
      key: "legacy",
      sev: "warning",
      text: `Showing the legacy strip: ${outline.message}`,
    });
  for (const [i, w] of result.datum.warnings.entries())
    out.push({ key: `datum-${i}`, sev: "warning", text: w });
  for (const [i, w] of result.flood.warnings.entries())
    out.push({ key: `flood-${i}`, sev: "warning", text: w });
  const rank = { critical: 0, warning: 1, info: 2 } as const;
  const sorted = out.sort((a, b) => rank[a.sev] - rank[b.sev]);
  // Never more than one critical at a time: later criticals wait as warnings until the first is acknowledged.
  let seen = false;
  return sorted.map((f) => {
    if (f.sev !== "critical") return f;
    if (seen) return { ...f, sev: "warning" };
    seen = true;
    return f;
  });
}

/**
 * The fast layer's wet and released volumes, for the volume flag's severity. The saved routed summary keeps only the
 * yes/no flag, so the numbers come from the note src/lib/crest/routed-flood.ts writes when the flag is raised
 * ("The fill lays down N ac-ft, more than the breach releases (M ac-ft) ..."). The reservoir volume is the record's
 * storage. Null when there is no such note (no flag, or an older saved layer).
 */
export function routedVolumes(
  outline: OutlineState,
  storageAcFt: number,
): { wet: number; release: number; reservoir?: number } | null {
  if (outline.state !== "ready" || !outline.routed?.volumeFlag) return null;
  for (const n of outline.routed.notes) {
    const m =
      /The fill lays down ([\d.]+) ac-ft, more than the breach releases \(([\d.]+) ac-ft\)/.exec(n);
    if (m && Number(m[2]) > 0)
      return {
        wet: Number(m[1]),
        release: Number(m[2]),
        reservoir: storageAcFt > 0 ? storageAcFt : undefined,
      };
  }
  return null;
}

export type Receptor = Analysis["flood"]["receptors"][number];
export type SortKey =
  "name" | "kind" | "depthFt" | "velocityFps" | "arrivalHr" | "distanceKm" | "ndClass";

const CLASS_RANK: Record<string, number> = { High: 3, Medium: 2, Low: 1 };

function sortValue(r: Receptor, k: SortKey): number | string {
  switch (k) {
    case "name":
    case "kind":
      return r[k].toLowerCase();
    case "ndClass":
      return r.ndClass ? CLASS_RANK[r.ndClass] : 0;
    case "depthFt":
    case "velocityFps":
      return r.flooded ? r[k] : -1;
    case "arrivalHr":
      return r.arrivalHr ?? -1;
    case "distanceKm":
      return r.distanceKm;
  }
}

/** Sort a copy; numbers and missing values (as -1) sort together, text by locale. */
export function sortReceptors(rows: Receptor[], key: SortKey, desc: boolean): Receptor[] {
  return [...rows].sort((a, b) => {
    const av = sortValue(a, key),
      bv = sortValue(b, key);
    const c =
      typeof av === "string" && typeof bv === "string"
        ? av.localeCompare(bv)
        : (av as number) - (bv as number);
    return desc ? -c : c;
  });
}

export const PER_PAGE = 10;

export function paginate<T>(
  rows: T[],
  page: number,
  per = PER_PAGE,
): { view: T[]; pages: number; page: number } {
  const pages = Math.max(1, Math.ceil(rows.length / per));
  const p = Math.min(Math.max(0, page), pages - 1);
  return { view: rows.slice(p * per, p * per + per), pages, page: p };
}

/** Filter by the header search box (name or kind). */
export function searchReceptors(rows: Receptor[], q: string): Receptor[] {
  const s = q.trim().toLowerCase();
  return s
    ? rows.filter((r) => r.name.toLowerCase().includes(s) || r.kind.toLowerCase().includes(s))
    : rows;
}

/** Peak of a hydrograph: value and time, for the labelled marker (acceptance B2). */
export function peakOf(series: Analysis["breach"]["series"]): { tHr: number; qCfs: number } | null {
  if (!series.length) return null;
  return series.reduce((m, p) => (p.qCfs > m.qCfs ? p : m), series[0]);
}

export type LayerKey = "depth" | "arrival" | "structures" | "hillshade" | "runnerOverlay";

export const LAYER_DEFS: [Exclude<LayerKey, "runnerOverlay">, string][] = [
  ["depth", "Max depth"],
  ["arrival", "Arrival time"],
  ["structures", "Structures"],
  ["hillshade", "Hillshade"],
];

export const DEFAULT_LAYERS: Record<LayerKey, boolean> = {
  depth: true,
  arrival: false,
  structures: true,
  hillshade: false,
  runnerOverlay: false,
};

/** USGS shaded relief (key-free, the same National Map server as the basemaps). */
export const HILLSHADE_URL =
  "https://basemap.nationalmap.gov/arcgis/rest/services/USGSShadedReliefOnly/MapServer/tile/{z}/{y}/{x}";
