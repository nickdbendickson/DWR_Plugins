/**
 * Mapped flowlines through lakes, to steer the drainage trace across them (Nick, 2026-11-18).
 *
 * 3DEP lidar shows a lake as one flat surface. The drainage trace (outline.ts) crosses a flat by the order the
 * priority-flood reaches its cells, which is the shortest way across, not where the river runs: it can hug a shore or
 * cut a bay, and the sections cut square to it then sit on the bank. The USGS 3D Hydrography Program (3DHP) maps the
 * line a river takes through each lake ("waterbody connector" flowlines). Those lines are burned a few centimetres
 * into the flat water cells for the trace only, so across a lake it follows the mapped line; on land, and wherever the
 * service has no line or cannot be reached, the trace is the terrain's own, exactly as before.
 *
 * The service is the national 3DHP map service at hydro.nationalmap.gov; a failed read is never an error, only a note.
 */

import type { DemGrid } from "./outline.ts";

export const FLOWLINE_SERVICE = "https://hydro.nationalmap.gov/arcgis/rest/services/3DHP_all/MapServer/50";
/** 3DHP flowline feature type for the line a river takes through a waterbody. */
export const WATERBODY_CONNECTOR = 5;
/** How far (m) a burned cell is lowered for the trace. Only flat water is burned, so this only orders a flat. */
export const BURN_M = 0.05;
/** A cell is flat water when no 8-neighbour differs from it by more than this (m). Hydro-flattened lidar is flat to mm. */
export const FLAT_WATER_TOL_M = 0.02;
/** Most features read for one terrain box (the service pages at 2000). */
export const FLOWLINE_MAX_FEATURES = 6000;

export type FlowLine = [number, number][];
type Fetch = (url: string, init?: { signal?: AbortSignal }) => Promise<{ ok: boolean; status: number; json: () => Promise<unknown> }>;

/** The query URL for the waterbody connectors crossing a box (WGS 84), one page. */
export function flowlineUrl(bbox: readonly [number, number, number, number], offset = 0, pageSize = 2000): string {
  const q = new URLSearchParams({
    where: `featuretype = ${WATERBODY_CONNECTOR}`,
    geometry: bbox.join(","),
    geometryType: "esriGeometryEnvelope",
    inSR: "4326",
    outSR: "4326",
    spatialRel: "esriSpatialRelIntersects",
    outFields: "featuretype",
    returnGeometry: "true",
    resultOffset: String(offset),
    resultRecordCount: String(pageSize),
    f: "geojson",
  });
  return `${FLOWLINE_SERVICE}/query?${q.toString()}`;
}

/** The lines out of a GeoJSON page (LineString and MultiLineString), and whether the service says more remain. */
export function parseFlowlines(j: unknown): { lines: FlowLine[]; more: boolean } {
  const o = j as { features?: { geometry?: { type?: string; coordinates?: unknown } }[]; exceededTransferLimit?: boolean; properties?: { exceededTransferLimit?: boolean } };
  const lines: FlowLine[] = [];
  for (const f of o?.features ?? []) {
    const g = f?.geometry;
    const ok = (p: unknown): p is [number, number] => Array.isArray(p) && Number.isFinite(p[0]) && Number.isFinite(p[1]);
    if (g?.type === "LineString" && Array.isArray(g.coordinates)) lines.push((g.coordinates as unknown[]).filter(ok).map((p) => [p[0], p[1]]));
    else if (g?.type === "MultiLineString" && Array.isArray(g.coordinates))
      for (const l of g.coordinates as unknown[][]) if (Array.isArray(l)) lines.push(l.filter(ok).map((p) => [p[0], p[1]]));
  }
  return { lines: lines.filter((l) => l.length >= 2), more: !!(o?.exceededTransferLimit ?? o?.properties?.exceededTransferLimit) };
}

/** Read the waterbody connectors crossing a box from 3DHP, page by page. Throws on a failed read. */
export async function fetchFlowlines(bbox: readonly [number, number, number, number], fetchFn?: Fetch): Promise<FlowLine[]> {
  const f = fetchFn ?? (fetch as unknown as Fetch);
  const out: FlowLine[] = [];
  for (let offset = 0; offset < FLOWLINE_MAX_FEATURES; offset += 2000) {
    const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
    const timer = ctl ? setTimeout(() => ctl.abort(), 60_000) : null;
    try {
      const res = await f(flowlineUrl(bbox, offset), ctl ? { signal: ctl.signal } : undefined);
      if (!res.ok) throw new Error(`The 3DHP flowline service answered ${res.status}.`);
      const page = parseFlowlines(await res.json());
      out.push(...page.lines);
      if (!page.more) break;
    } finally {
      if (timer) clearTimeout(timer);
    }
  }
  return out;
}

/**
 * The cells to burn: flat water cells (FLAT_WATER_TOL_M) that a line passes through. Lines are walked in steps of a
 * third of a cell, so a diagonal line marks a connected (8-neighbour) run. Returns null when nothing is burned.
 */
export function burnMask(g: Pick<DemGrid, "bbox" | "width" | "height" | "z">, lines: FlowLine[]): Uint8Array | null {
  const W = g.width;
  const H = g.height;
  const dlon = (g.bbox[2] - g.bbox[0]) / W;
  const dlat = (g.bbox[3] - g.bbox[1]) / H;
  const valid = (k: number) => Number.isFinite(g.z[k]) && g.z[k] > -9998 && g.z[k] < 9000;
  const flat = (k: number) => {
    if (!valid(k)) return false;
    const i = k % W;
    const j = (k - i) / W;
    for (let dj = -1; dj <= 1; dj++)
      for (let di = -1; di <= 1; di++) {
        if (!di && !dj) continue;
        const ii = i + di;
        const jj = j + dj;
        if (ii < 0 || jj < 0 || ii >= W || jj >= H) continue;
        const m = jj * W + ii;
        if (!valid(m) || Math.abs(g.z[m] - g.z[k]) > FLAT_WATER_TOL_M) return false;
      }
    return true;
  };
  const mask = new Uint8Array(W * H);
  let n = 0;
  const mark = (lon: number, lat: number) => {
    const i = Math.floor((lon - g.bbox[0]) / dlon);
    const j = Math.floor((g.bbox[3] - lat) / dlat);
    if (i < 0 || j < 0 || i >= W || j >= H) return;
    const k = j * W + i;
    if (mask[k] || !flat(k)) return;
    mask[k] = 1;
    n++;
  };
  for (const l of lines)
    for (let p = 1; p < l.length; p++) {
      const [x0, y0] = l[p - 1];
      const [x1, y1] = l[p];
      const steps = Math.max(1, Math.ceil(Math.max(Math.abs(x1 - x0) / dlon, Math.abs(y1 - y0) / dlat) * 3));
      for (let s = 0; s <= steps; s++) mark(x0 + ((x1 - x0) * s) / steps, y0 + ((y1 - y0) * s) / steps);
    }
  return n ? mask : null;
}
