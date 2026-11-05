/**
 * "I want to be able to pick the location of where the vrt is" (Nick, 2026-11-05): the desk's terrain from a local
 * DEM (e.g. the statewide 1 m mosaic ND_1m_EPSG26914.vrt) instead of USGS 3DEP.
 *
 * Desktop app only. The window's Python side remembers the chosen file (crest-settings.json "dem_path") and answers
 * GET /api/dem?bbox=w,s,e,n&w=&h=&cell= with a gdalwarp window of it, the same float32 grid 3DEP returns
 * (crest-desktop/crest/localdem.py). Once a DEM is chosen, outline.ts fetchDem reads from it everywhere: the
 * inundation layer, the batch screen, the breach point, the crest read at a new dam's point. A browser cannot read a
 * file on the user's drive by path, so the web desk stays on 3DEP (the batch command line takes --dem <vrt>).
 */
import { parseBsq, setDemOverride, type DemGrid, type DemRequest } from "./outline.ts";

type DemReply = { path?: string | null; gdalwarp?: string | null; cancelled?: boolean; error?: string };

type DemApi = {
  get_dem: () => Promise<DemReply>;
  pick_dem: () => Promise<DemReply>;
  clear_dem: () => Promise<DemReply>;
};

export type DemState =
  /** The web desk, or a desktop build from before the local DEM. */
  | { kind: "unavailable" }
  | { kind: "3dep"; gdalwarp: boolean }
  | { kind: "local"; path: string; gdalwarp: boolean };

function demApi(): DemApi | undefined {
  if (typeof window === "undefined") return undefined;
  const api = (window as unknown as { pywebview?: { api?: Partial<DemApi> } }).pywebview?.api;
  return api && typeof api.get_dem === "function" && typeof api.pick_dem === "function" && typeof api.clear_dem === "function" ? (api as DemApi) : undefined;
}

const baseName = (p: string) => p.split(/[\\/]/).pop() || p;

type Fetch = (url: string, init?: { headers?: Record<string, string> }) => Promise<{ ok: boolean; status: number; arrayBuffer: () => Promise<ArrayBuffer>; text: () => Promise<string> }>;

function token(): string {
  return typeof document === "undefined" ? "" : (document.querySelector('meta[name="crest-token"]')?.getAttribute("content") ?? "");
}

export function localDemUrl(req: DemRequest): string {
  const [w, s, e, n] = req.bbox;
  return `/api/dem?bbox=${w},${s},${e},${n}&w=${req.width}&h=${req.height}&cell=${req.cellM}`;
}

/** Read a window of the chosen DEM through the desktop server. */
export async function fetchLocalDem(req: DemRequest, name: string, fetchFn: Fetch = fetch as unknown as Fetch): Promise<DemGrid> {
  const res = await fetchFn(localDemUrl(req), { headers: { "X-Crest-Token": token() } });
  if (!res.ok) {
    let why = "";
    try {
      why = (JSON.parse(await res.text()) as { error?: string }).error ?? "";
    } catch {
      /* not JSON */
    }
    throw new Error(`The local DEM ${name} could not be read (${res.status}${why ? `: ${why}` : ""}). Choose it again, or switch back to 3DEP.`);
  }
  return { ...req, z: parseBsq(await res.arrayBuffer(), req.width, req.height) };
}

function install(reply: DemReply): DemState {
  if (reply.path) {
    const name = baseName(reply.path);
    setDemOverride({ fetch: (r) => fetchLocalDem(r, name), label: `local DEM ${name}` });
    return { kind: "local", path: reply.path, gdalwarp: !!reply.gdalwarp };
  }
  setDemOverride(null);
  return { kind: "3dep", gdalwarp: !!reply.gdalwarp };
}

/** The saved choice, installed as the desk's terrain. Call once when the desk opens. */
export async function loadDemSource(): Promise<DemState> {
  const api = demApi();
  if (!api) return { kind: "unavailable" };
  try {
    return install(await api.get_dem());
  } catch {
    return { kind: "unavailable" };
  }
}

/** Open the file dialog; the new choice is saved and installed. Cancel keeps the current one. */
export async function pickDemSource(): Promise<{ state: DemState | null; error?: string }> {
  const api = demApi();
  if (!api) return { state: { kind: "unavailable" } };
  const r = await api.pick_dem();
  if (r.error) return { state: null, error: r.error };
  if (r.cancelled) return { state: null };
  return { state: install(r) };
}

/** Back to USGS 3DEP. */
export async function clearDemSource(): Promise<DemState> {
  const api = demApi();
  if (!api) return { kind: "unavailable" };
  return install(await api.clear_dem());
}

// ---- the desk-wide choice (one state, so the Flood step and the Batch screen agree) ----------------------------

let state: DemState | null = null;
let loading: Promise<DemState> | null = null;
const listeners = new Set<(s: DemState) => void>();

export function currentDemSource(): DemState | null {
  return state;
}

export function publishDemSource(s: DemState): void {
  state = s;
  listeners.forEach((f) => f(s));
}

export function subscribeDemSource(f: (s: DemState) => void): () => void {
  listeners.add(f);
  return () => void listeners.delete(f);
}

/** Install the saved DEM choice once (the desk calls this when it opens, so every terrain read uses it). */
export function ensureDemSource(): Promise<DemState> {
  if (state) return Promise.resolve(state);
  loading ??= loadDemSource().then((s) => (publishDemSource(s), s));
  return loading;
}
