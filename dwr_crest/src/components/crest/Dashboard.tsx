import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { DEPTH_BANDS, floodStrip, type FloodStrip } from "@/lib/crest/floodstrip";
import {
  crest,
  dashboardFlags,
  dashMethod,
  DEFAULT_LAYERS,
  DISCLAIMER,
  LAYER_DEFS,
  type LayerKey,
  METHOD_LABEL,
  paginate,
  peakOf,
  type Receptor,
  searchReceptors,
  type SortKey,
  sortReceptors,
  type DashFlag,
  routedVolumes,
} from "@/lib/crest/dashboard";
import { num } from "@/lib/crest/format";
import { outlineState } from "@/lib/crest/outline";
import { results } from "@/lib/crest/results-client";
import { receptorPoints, SOURCE_LABEL } from "@/lib/crest/results";
import { runKey, useResults } from "@/lib/crest/results-store";
import type { Analysis, DamRecord, ResultSource } from "@/lib/crest/types";
import type { DashPoint } from "./DashboardMap";
import type { Bounds, Overlay } from "./ResultsMap";
import { useAnalysisOfRecord } from "./results-flow";

const DashboardMap = lazy(() =>
  import("./DashboardMap").then((m) => ({ default: m.DashboardMap })),
);

const CLASS_COLOR: Record<string, string> = {
  High: crest.red,
  Medium: crest.amber,
  Low: crest.green,
};
const cls = (r: string | null) =>
  r === "High" ? "high" : r === "Medium" ? "sig" : r === "Low" ? "low" : "none";

function boundsOf(fc: FloodStrip | null): Bounds | null {
  if (!fc) return null;
  let w = Infinity,
    s = Infinity,
    e = -Infinity,
    n = -Infinity;
  const walk = (c: unknown): void => {
    if (Array.isArray(c) && typeof c[0] === "number") {
      w = Math.min(w, c[0] as number);
      e = Math.max(e, c[0] as number);
      s = Math.min(s, c[1] as number);
      n = Math.max(n, c[1] as number);
    } else if (Array.isArray(c)) c.forEach(walk);
  };
  for (const f of fc.features) walk((f.geometry as { coordinates: unknown }).coordinates);
  return Number.isFinite(w) ? { west: w, south: s, east: e, north: n } : null;
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

/**
 * Style B: map-first results dashboard. Header (brand | scenario | method | search), map with a layer rail, a bottom
 * grid (hydrograph | KPIs | structure impacts) and a status bar that carries the flags. Reads the same analysis of
 * record and saved fast layer as the Flood and Report steps; it changes nothing.
 */
export function Dashboard({ dam, result: deskResult }: { dam: DamRecord; result: Analysis }) {
  const { result, label: source } = useAnalysisOfRecord(dam, deskResult);
  const online = typeof navigator === "undefined" ? true : navigator.onLine;
  const outline = useMemo(() => outlineState(dam, result, online), [dam, result, online]);
  const method = dashMethod(outline);
  const depth = useMemo<FloodStrip | null>(
    () => (outline.state === "ready" ? outline.outline : floodStrip(dam, result).strip),
    [outline, dam, result],
  );
  const fit = useMemo(() => boundsOf(depth), [depth]);
  const flags = useMemo(
    () =>
      dashboardFlags(result, outline, {
        volume: routedVolumes(outline, dam.storageAcFt) ?? undefined,
      }),
    [result, outline, dam.storageAcFt],
  );
  const [layers, setLayers] = useState<Record<LayerKey, boolean>>(DEFAULT_LAYERS);
  const [acked, setAcked] = useState(false);
  const [q, setQ] = useState("");
  const critical = flags.find((f) => f.sev === "critical");
  const gated = !!critical && !acked;

  const placed = useMemo(() => {
    const byId = new Map(result.flood.receptors.map((r) => [r.id, r]));
    return receptorPoints(dam, result).map((p) => ({ p, r: byId.get(p.id) }));
  }, [dam, result]);
  const points = useMemo<DashPoint[]>(
    () =>
      placed.map(({ p, r }) => ({
        id: p.id,
        name: p.name,
        lon: p.lon,
        lat: p.lat,
        color: r?.ndClass ? CLASS_COLOR[r.ndClass] : r?.flooded ? crest.teal : "#9CA3AF",
        label: `${p.name}: ${r ? (r.flooded ? `${num(r.depthFt, 2)} ft` : r.unresolved ? "unresolved" : "dry") : "—"}${r?.ndClass ? ` · ${r.ndClass}` : ""}`,
      })),
    [placed],
  );
  const arrival = useMemo(
    () =>
      placed
        .filter(({ r }) => r?.flooded && r.arrivalHr != null)
        .map(({ p, r }) => ({
          id: p.id,
          lon: p.lon,
          lat: p.lat,
          text: `${num(r!.arrivalHr!, 1)} h`,
        })),
    [placed],
  );

  // Compare with a Runner run: the imported LISFLOOD-FP (else DSS-WISE) max depth, when loaded in this session.
  const runs = useResults((s) => s.runs);
  const runner =
    (["lisflood", "dsswise"] as ResultSource[]).find(
      (s) => runs[runKey(dam.id, s)]?.status === "ready",
    ) ?? null;
  const [overlay, setOverlay] = useState<{ key: string; overlays: Overlay[]; error?: string }>({
    key: "",
    overlays: [],
  });
  const want = layers.runnerOverlay && runner ? runKey(dam.id, runner) : "";
  useEffect(() => {
    if (!want || overlay.key === want) return;
    let live = true;
    results
      .render({ type: "render", mode: "depth", keys: [want], maxPx: 1600, extra: [] })
      .then(
        (r) =>
          live &&
          setOverlay({
            key: want,
            overlays: r.images.map((px) => ({
              url: toUrl(px, r.width, r.height),
              bounds: r.bounds,
              opacity: 0.55,
            })),
          }),
      )
      .catch((e: Error) => live && setOverlay({ key: want, overlays: [], error: e.message }));
    return () => {
      live = false;
    };
  }, [want, overlay.key]);

  return (
    <section className="dash-root" data-testid="dashboard">
      <header className="dash-header">
        <span className="dash-brand">▲ Crest</span>
        <span className="chip">
          <b>Scenario:</b> Sunny-day breach · {dam.name}
        </span>
        <span
          className={"chip " + (method === "strip-legacy" ? "legacy" : "")}
          data-testid="dash-method"
          title={source}
        >
          {METHOD_LABEL[method]}
        </span>
        <input
          className="dash-search"
          placeholder="Search structures…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          aria-label="Search structures"
        />
      </header>

      <div className="dash-body">
        <div className="dash-map" data-testid="dash-map">
          <Suspense fallback={<p className="p-4 text-sm text-muted">Loading the map…</p>}>
            <DashboardMap
              depth={layers.depth ? depth : null}
              points={layers.structures ? points : []}
              arrival={layers.arrival ? arrival : []}
              hillshade={layers.hillshade}
              overlays={layers.runnerOverlay ? overlay.overlays : []}
              fit={fit}
            />
          </Suspense>
        </div>
        <LayerRail
          layers={layers}
          setLayers={setLayers}
          runner={runner}
          overlayError={overlay.error}
          method={method}
        />
      </div>

      <div className="dash-bottom">
        <HydrographPane series={result.breach.series} />
        <KpiStack result={result} />
        <ImpactsTable receptors={searchReceptors(result.flood.receptors, q)} gated={gated} />
      </div>

      <StatusBar flags={flags} critical={critical} acked={acked} onAck={() => setAcked(true)} />
      <p className="disclaimer">{DISCLAIMER}</p>
    </section>
  );
}

function LayerRail({
  layers,
  setLayers,
  runner,
  overlayError,
  method,
}: {
  layers: Record<LayerKey, boolean>;
  setLayers: (l: Record<LayerKey, boolean>) => void;
  runner: ResultSource | null;
  overlayError?: string;
  method: string;
}) {
  return (
    <aside className="dash-layers" aria-label="Layers">
      <h5>LAYERS</h5>
      {LAYER_DEFS.map(([k, label]) => (
        <label key={k} className={"layer-row " + (layers[k] ? "" : "off")}>
          <input
            type="checkbox"
            checked={layers[k]}
            onChange={() => setLayers({ ...layers, [k]: !layers[k] })}
            data-testid={`layer-${k}`}
          />
          {label}
        </label>
      ))}
      <h5>COMPARE</h5>
      <label className={"layer-row " + (layers.runnerOverlay ? "" : "off")}>
        <input
          type="checkbox"
          checked={layers.runnerOverlay}
          onChange={() => setLayers({ ...layers, runnerOverlay: !layers.runnerOverlay })}
          data-testid="layer-runnerOverlay"
        />
        ↔ Overlay Runner run
      </label>
      {layers.runnerOverlay ? (
        <p className="mini">
          {overlayError
            ? overlayError
            : runner
              ? `${SOURCE_LABEL[runner]} max depth, 55% opacity.`
              : "No Runner run loaded: import one on the Results step."}
        </p>
      ) : null}
      {layers.depth ? (
        <>
          <h5>MAX DEPTH (FT)</h5>
          {[...DEPTH_BANDS].reverse().map((b) => (
            <p key={b.id} className="legend-row">
              <span style={{ background: b.color }} /> {b.label}
            </p>
          ))}
          <p className="mini">
            {method === "strip-legacy"
              ? "Schematic strip, not a mapped extent."
              : "Fast terrain layer, not a 2-D model."}
          </p>
        </>
      ) : null}
      {layers.arrival ? (
        <p className="mini">Arrival: the desk's front arrival (h) at each wet structure.</p>
      ) : null}
    </aside>
  );
}

function HydrographPane({ series }: { series: Analysis["breach"]["series"] }) {
  const W = 420,
    H = 170,
    L = 40,
    B = 22,
    T = 12,
    R = 12;
  const pk = peakOf(series);
  if (!pk || series.length < 2)
    return (
      <div className="pane">
        <h4>Breach hydrograph</h4>
        <p className="mini">No hydrograph.</p>
      </div>
    );
  const tMax = Math.max(...series.map((p) => p.tHr)) || 1;
  const x = (t: number) => L + (t / tMax) * (W - L - R);
  const y = (q: number) => T + (1 - q / pk.qCfs) * (H - T - B);
  const d = series
    .map((p, i) => `${i ? "L" : "M"}${x(p.tHr).toFixed(1)},${y(p.qCfs).toFixed(1)}`)
    .join(" ");
  const right = x(pk.tHr) > W - 160;
  const text = `Peak ${num(pk.qCfs, 0)} cfs @ ${num(pk.tHr, 2)} hr`;
  return (
    <div className="pane" data-testid="dash-hydrograph">
      <h4>Breach hydrograph</h4>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        className="w-full"
        role="img"
        aria-label={`Breach hydrograph. ${text}`}
      >
        <line x1={L} y1={y(0)} x2={W - R} y2={y(0)} stroke={crest.border} />
        <line x1={L} y1={T} x2={L} y2={y(0)} stroke={crest.border} />
        <path
          d={`${d} L${x(series[series.length - 1].tHr)},${y(0)} L${x(0)},${y(0)} Z`}
          fill={crest.teal}
          opacity="0.12"
        />
        <path d={d} fill="none" stroke={crest.teal} strokeWidth="2" />
        <circle cx={x(pk.tHr)} cy={y(pk.qCfs)} r="4.5" fill={crest.red} />
        <text
          x={x(pk.tHr) + (right ? -8 : 8)}
          y={y(pk.qCfs) + 4}
          fontSize="11"
          fill={crest.ink}
          textAnchor={right ? "end" : "start"}
          data-testid="dash-peak"
        >
          {text}
        </text>
        <text x={W - R} y={H - 4} fontSize="10" fill={crest.muted} textAnchor="end">
          hours after breach (0–{num(tMax, 1)})
        </text>
        <text x={4} y={T + 8} fontSize="10" fill={crest.muted}>
          cfs
        </text>
      </svg>
    </div>
  );
}

function KpiStack({ result }: { result: Analysis }) {
  const wet = result.flood.receptors.filter((r) => r.flooded).length;
  const cards = [
    {
      l: "Peak discharge",
      v: `${num(result.breach.peakCfs, 0)} cfs`,
      warn: result.screen.level === "High",
    },
    { l: "Max depth", v: `${num(result.flood.maxDepthFt, 1)} ft`, warn: false },
    { l: "Structures flooded", v: `${wet} of ${result.flood.receptors.length}`, warn: false },
    { l: "Inundated area", v: `${num(result.flood.acres, 0)} ac`, warn: false },
  ];
  return (
    <div className="kpi-stack" data-testid="dash-kpis">
      {cards.map((c) => (
        <div key={c.l} className={"kpi-card " + (c.warn ? "warn" : "")}>
          <div className="l">{c.l}</div>
          <div className="v">{c.v}</div>
        </div>
      ))}
      <div className="kpi-card">
        <div className="l">Desk screen</div>
        <div className="v">{result.screen.label}</div>
      </div>
    </div>
  );
}

const COLS: { key: SortKey | null; label: string; title?: string }[] = [
  { key: "name", label: "Structure" },
  { key: "depthFt", label: "Depth ft" },
  { key: "velocityFps", label: "Vel ft/s" },
  {
    key: "arrivalHr",
    label: "Front arrival hr",
    title:
      "Front arrival: the desk's travel time to the flood front (or the imported run's arrival raster)",
  },
  {
    key: null,
    label: "Routed peak hr",
    title: "Routed peak: Muskingum-Cunge travel time per structure, not computed yet",
  },
  {
    key: null,
    label: "DEM",
    title: "Terrain source for this receptor's depth read, not recorded yet",
  },
  { key: "ndClass", label: "Class" },
];

function ImpactsTable({ receptors, gated }: { receptors: Receptor[]; gated: boolean }) {
  const [sortKey, setSortKey] = useState<SortKey>("depthFt");
  const [desc, setDesc] = useState(true);
  const [page, setPage] = useState(0);
  const sorted = useMemo(() => sortReceptors(receptors, sortKey, desc), [receptors, sortKey, desc]);
  const pg = paginate(sorted, page);
  const sortBy = (k: SortKey) => {
    if (k === sortKey) setDesc(!desc);
    else {
      setSortKey(k);
      setDesc(k !== "name");
    }
    setPage(0);
  };
  return (
    <div className="pane dash-impacts" data-testid="dash-impacts">
      <h4>Structure impacts</h4>
      <div className="overflow-x-auto">
        <table className="dash-table">
          <thead>
            <tr>
              {COLS.map((c) => (
                <th
                  key={c.label}
                  scope="col"
                  title={c.title}
                  aria-sort={c.key === sortKey ? (desc ? "descending" : "ascending") : undefined}
                >
                  {c.key ? (
                    <button type="button" onClick={() => sortBy(c.key!)}>
                      {c.label}
                      {c.key === sortKey ? (desc ? " ▼" : " ▲") : ""}
                    </button>
                  ) : (
                    c.label
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {gated ? (
              <tr>
                <td colSpan={7} className="gate-msg">
                  ⚠ Volume flag critical — acknowledge in the status bar to continue.
                </td>
              </tr>
            ) : (
              pg.view.map((r) => (
                <tr key={r.id} className={"hl-" + cls(r.ndClass)}>
                  <th scope="row">{r.name}</th>
                  <td>{r.unresolved ? "unresolved" : r.flooded ? num(r.depthFt, 2) : "dry"}</td>
                  <td>{r.flooded ? num(r.velocityFps, 2) : "—"}</td>
                  <td>{r.arrivalHr == null ? "—" : num(r.arrivalHr, 2)}</td>
                  <td>—</td>
                  <td>—</td>
                  <td>
                    {r.ndClass ? (
                      <span className={"badge " + cls(r.ndClass)}>{r.ndClass}</span>
                    ) : (
                      "—"
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
      <div className="tbl-foot">
        <span>
          Showing {gated ? 0 : pg.view.length} of {sorted.length} · sorted by{" "}
          {COLS.find((c) => c.key === sortKey)?.label ?? sortKey}
        </span>
        <div className="pg">
          <button
            type="button"
            disabled={gated || pg.page === 0}
            onClick={() => setPage(pg.page - 1)}
            aria-label="Previous page"
          >
            ‹
          </button>
          {pageWindow(pg.page, pg.pages).map((i) => (
            <button
              key={i}
              type="button"
              disabled={gated}
              className={pg.page === i ? "cur" : ""}
              onClick={() => setPage(i)}
              aria-label={`Page ${i + 1}`}
              aria-current={pg.page === i ? "page" : undefined}
            >
              {i + 1}
            </button>
          ))}
          <button
            type="button"
            disabled={gated || pg.page >= pg.pages - 1}
            onClick={() => setPage(pg.page + 1)}
            aria-label="Next page"
          >
            ›
          </button>
        </div>
      </div>
    </div>
  );
}

/** Up to five page buttons around the current page, so every page stays reachable. */
function pageWindow(page: number, pages: number): number[] {
  const start = Math.max(0, Math.min(page - 2, pages - 5));
  return Array.from({ length: Math.min(5, pages) }, (_, i) => start + i);
}

const SEV_WORD = { critical: "Critical", warning: "Warning", info: "Note" } as const;

function StatusBar({
  flags,
  critical,
  acked,
  onAck,
}: {
  flags: DashFlag[];
  critical?: DashFlag;
  acked: boolean;
  onAck: () => void;
}) {
  return (
    <footer className="status-bar" data-testid="dash-status">
      <span>
        <span className="dot">●</span> Model completed
      </span>
      {flags.map((f) => (
        <span key={f.key} className={"flag-pill " + f.sev} data-testid={`flag-${f.sev}`}>
          <span aria-hidden="true">{f.sev === "info" ? "ℹ" : "⚑"}</span>{" "}
          <span className="sr-only">{SEV_WORD[f.sev]}: </span>
          {f.text}
        </span>
      ))}
      {critical && !acked ? (
        <button type="button" className="ack-btn" onClick={onAck}>
          Acknowledge and continue
        </button>
      ) : null}
    </footer>
  );
}
