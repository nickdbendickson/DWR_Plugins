/**
 * The results engine: holds imported runs (one per dam and source) and answers import / sample / render / strip
 * requests. Runs inside raster.worker.ts; the client falls back to running it on the main thread.
 */
import { unzipSync } from "fflate";
import { wantFile } from "./raster-meta.ts";
import {
  asciiRows,
  CRS_DEFS,
  classifyFile,
  DEFAULT_ASCII_CRS,
  frame,
  type FileRole,
  type GridGeo,
  type Layer,
  lonLatBounds,
  openGeoTiff,
  parseAsciiGrid,
  projector,
  type Projector,
  renderCompare,
  renderDepth,
  renderOutline,
  type Retained,
  type RowSource,
  sameGrid,
  sameShape,
  samplePoint,
  type ScanContext,
  scanRun,
  type ScanStats,
  tiffRows,
  VAR_LABEL,
  type VarId,
  viewFor,
  type CompareStats,
} from "./raster.ts";
import { type Check, stripFromGrid } from "./results.ts";
import type { FloodStrip, StripCommon } from "./floodstrip.ts";
import type { ReceptorSample, ResultSource } from "./types.ts";

export type InFile = { name: string; buf: ArrayBuffer };

export type RunDam = { lon: number; lat: number; id?: string; nid?: string; crs?: string; name?: string };

export type RunSummary = {
  key: string;
  source: ResultSource;
  label: string;
  files: { name: string; use: string }[];
  used: string[];
  checks: Check[];
  crs: string;
  crsLabel: string;
  width: number;
  height: number;
  cellM: number;
  retainedM: number;
  vars: VarId[];
  stats: ScanStats | null;
  bounds: { west: number; south: number; east: number; north: number };
  runDam: RunDam | null;
  /** From dam.json: final model and cut-off note (Runner). */
  runNotes: string[];
};

export type Req =
  | { type: "import"; key: string; source: ResultSource; files: InFile[]; crs: string; ctx: ScanContext | null }
  | { type: "rescan"; key: string; ctx: ScanContext }
  | { type: "sample"; key: string; points: { id: string; lon: number; lat: number }[] }
  | {
      type: "render";
      mode: "depth" | "compare";
      keys: string[];
      colors?: [string, string];
      maxPx?: number;
      downstream?: { lon: number; lat: number; u: [number, number] } | null;
      extra?: { lon: number; lat: number }[];
    }
  | { type: "strip"; key: string; ctx: ScanContext; common: StripCommon; direction: string; minCellM?: number }
  | { type: "drop"; key: string };

export type RenderOut = {
  width: number;
  height: number;
  bounds: { west: number; south: number; east: number; north: number };
  images: Uint8ClampedArray[];
  compare?: CompareStats;
};

type Run = {
  summary: RunSummary;
  proj: Projector;
  vars: { depth: RowSource; velocity?: RowSource; arrival?: RowSource };
  retained: Retained;
};

export { wantFile };

const isJunk = (n: string) => /(^|[\\/])(__MACOSX[\\/]|\._)/.test(n);
const base = (n: string) => n.split(/[\\/]/).pop() ?? n;

function expand(files: InFile[], notes: { name: string; use: string }[]): InFile[] {
  const out: InFile[] = [];
  for (const f of files) {
    if (isJunk(f.name)) continue;
    if (/\.zip$/i.test(f.name)) {
      const z = unzipSync(new Uint8Array(f.buf), {
        filter: (e) => {
          if (isJunk(e.name) || e.name.endsWith("/")) return false;
          const r = classifyFile(e.name);
          if (r.kind === "skip") notes.push({ name: `${base(f.name)}/${base(e.name)}`, use: `skipped: ${r.reason}` });
          return r.kind !== "skip";
        },
      });
      for (const [n, u8] of Object.entries(z))
        out.push({ name: `${base(f.name)}/${n}`, buf: u8.buffer.slice(u8.byteOffset, u8.byteOffset + u8.byteLength) as ArrayBuffer });
    } else out.push(f);
  }
  return out;
}

function readRunDam(text: string): { dam: RunDam | null; notes: string[] } {
  try {
    const j = JSON.parse(text) as {
      format?: string;
      fields?: Record<string, unknown>;
      status?: { step?: string; tool?: string; version?: string; result?: Record<string, unknown> }[];
    };
    const f = j.fields ?? (j as Record<string, unknown>);
    const lon = Number(f.lon);
    const lat = Number(f.lat);
    const notes: string[] = [];
    let finalModel: string | undefined;
    let version: string | undefined;
    for (const s of j.status ?? []) {
      if (s.step === "modelled" && s.result) {
        finalModel = String(s.result.final_model ?? "");
        version = `${s.tool ?? "Runner"} ${s.version ?? ""}`.trim();
        const cut = s.result.cut_off as { reason?: string; detail?: string } | undefined;
        if (cut?.detail) notes.push(`Run cut-off (${cut.reason ?? "?"}): ${cut.detail}`);
        if (finalModel) notes.push(`Runner final model: ${finalModel}; Crest imports the LISFLOOD-FP rasters for this column.`);
      }
      if (s.step === "classified" && s.result?.nd_class)
        notes.push(
          `The Runner's own hazard class for this run (${s.tool ?? "ndhazard"} ${s.version ?? ""}, on its ${finalModel || "final"} rasters): ND ${String(s.result.nd_class)}, TM 11 ${String(s.result.tm11_class ?? "?")}.`,
        );
    }
    return {
      dam: Number.isFinite(lon) && Number.isFinite(lat)
        ? {
            lon,
            lat,
            id: f.dam_id ? String(f.dam_id) : undefined,
            nid: f.nid_id ? String(f.nid_id) : undefined,
            crs: f.crs ? String(f.crs) : undefined,
            name: f.name ? String(f.name) : undefined,
          }
        : null,
      notes: [finalModel ? `final:${finalModel}` : "", version ? `version:${version}` : "", ...notes].filter(Boolean),
    };
  } catch {
    return { dam: null, notes: [] };
  }
}

type Cand = { name: string; role: Extract<FileRole, { kind: "grid" }>; src: RowSource; crs: string | null; geo: GridGeo; georef: boolean; rawMax?: number };

export function createEngine() {
  const runs = new Map<string, Run>();

  async function doImport(req: Extract<Req, { type: "import" }>, progress: (t: string) => void): Promise<RunSummary> {
    const notes: { name: string; use: string }[] = [];
    const files = expand(req.files, notes);
    const checks: Check[] = [];
    let runDam: RunDam | null = null;
    let runNotes: string[] = [];
    let finalModel: string | undefined;
    let version: string | undefined;
    for (const f of files)
      if (base(f.name).toLowerCase() === "dam.json") {
        const r = readRunDam(new TextDecoder().decode(f.buf));
        runDam = r.dam;
        finalModel = r.notes.find((n) => n.startsWith("final:"))?.slice(6);
        version = r.notes.find((n) => n.startsWith("version:"))?.slice(8);
        runNotes = r.notes.filter((n) => !n.startsWith("final:") && !n.startsWith("version:"));
        notes.push({ name: f.name, use: `run record: ${runDam?.id ?? "?"} / ${runDam?.nid ?? "?"}, ${runDam?.crs ?? "no CRS"}` });
      }
    const asciiCrs = req.crs === "auto" ? (runDam?.crs && CRS_DEFS[runDam.crs] ? runDam.crs : DEFAULT_ASCII_CRS) : req.crs;

    const cands: Cand[] = [];
    const asciiPending: { name: string; role: Extract<FileRole, { kind: "grid" }>; grid: ReturnType<typeof parseAsciiGrid> }[] = [];
    for (const f of files) {
      const role = classifyFile(f.name);
      if (role.kind !== "grid") {
        if (role.kind === "skip") notes.push({ name: f.name, use: `skipped: ${role.reason}` });
        continue;
      }
      if (req.source === "dsswise" && (role.final || /^lisflood_/i.test(base(f.name)) || role.format === "ascii")) {
        notes.push({ name: f.name, use: "skipped: a Breach Runner / LISFLOOD file in the DSS-WISE import" });
        continue;
      }
      if (req.source === "lisflood" && /hmax_ft_upto|vmax_ftps|arrival_time_hr/i.test(base(f.name))) {
        notes.push({ name: f.name, use: "skipped: a DSS-WISE file in the LISFLOOD import" });
        continue;
      }
      if (role.final && req.source === "lisflood" && finalModel && !/lisflood/i.test(finalModel)) {
        notes.push({ name: f.name, use: `skipped: Runner final raster from ${finalModel} (dam.json), not LISFLOOD-FP` });
        continue;
      }
      progress(`Reading ${base(f.name)}…`);
      if (role.format === "geotiff") {
        try {
          const { info, image } = await openGeoTiff(f.buf);
          let crs = info.epsg ? `EPSG:${info.epsg}` : null;
          if (crs && !CRS_DEFS[crs]) {
            notes.push({ name: f.name, use: `skipped: ${crs} is not supported (${Object.keys(CRS_DEFS).join(", ")})` });
            continue;
          }
          if (!crs) {
            crs = asciiCrs;
            checks.push({ level: "warn", text: `${base(f.name)} has no EPSG code; read as ${asciiCrs}.` });
          }
          cands.push({ name: f.name, role, src: tiffRows(f.name, role.variable, role.factor, info, image), crs, geo: info, georef: true });
        } catch (e) {
          notes.push({ name: f.name, use: `skipped: could not read the GeoTIFF (${(e as Error).message})` });
        }
      } else {
        try {
          const grid = parseAsciiGrid(new TextDecoder().decode(f.buf));
          asciiPending.push({ name: f.name, role, grid });
        } catch (e) {
          notes.push({ name: f.name, use: `skipped: ${(e as Error).message}` });
        }
      }
    }
    for (const a of asciiPending) {
      let geo: GridGeo = a.grid;
      let crs: string | null = asciiCrs;
      let georef = a.grid.georeferenced;
      if (!georef) {
        const donor = cands.find((c) => c.georef && c.role.format === "geotiff" && sameShape(c.geo, a.grid));
        if (donor) {
          geo = { ...a.grid, x0: donor.geo.x0, y0: donor.geo.y0 };
          crs = donor.crs;
          georef = true;
          notes.push({ name: a.name, use: `georeference borrowed from ${base(donor.name)} (same ${a.grid.width}×${a.grid.height} grid, xll/yll 0 in the ASCII)` });
        }
      }
      let rawMax = 0;
      for (let i = 0; i < a.grid.data.length; i++) {
        const v = a.grid.data[i];
        if (v > rawMax && v !== a.grid.nodata) rawMax = v;
      }
      cands.push({ name: a.name, role: a.role, src: asciiRows(a.name, a.role.variable, a.role.factor, a.grid, geo), crs, geo, georef, rawMax });
    }

    // One file per variable: lowest rank (Runner GeoTIFF over ASCII over final raster).
    const pick = new Map<VarId, Cand>();
    for (const c of cands) {
      const cur = pick.get(c.role.variable);
      if (!cur || c.role.rank < cur.role.rank || (c.role.rank === cur.role.rank && c.georef && !cur.georef)) pick.set(c.role.variable, c);
    }
    const depth = pick.get("depth");
    if (!depth) throw new Error(`No max-depth raster found (${req.source === "dsswise" ? "…Hmax_ft….tif" : "lisflood_hmax_ft.tif or <run>.max"}).`);
    if (!depth.georef)
      throw new Error(
        `${base(depth.name)} is not georeferenced (xllcorner/yllcorner 0). Add the Runner's lisflood_hmax_ft.tif (same grid) or a georeferenced ASCII grid.`,
      );
    const crs = depth.crs ?? asciiCrs;
    const proj = projector(crs);
    const vars: Run["vars"] = { depth: depth.src };
    const used: string[] = [];
    for (const [v, c] of pick) {
      if (v !== "depth" && v !== "velocity" && v !== "arrival") {
        notes.push({ name: c.name, use: `read: ${VAR_LABEL[v]} (${c.role.srcUnit}), not used in the comparison` });
        continue;
      }
      if (c !== depth && (!sameGrid(c.geo, depth.geo) || c.crs !== depth.crs)) {
        notes.push({ name: c.name, use: `skipped: grid differs from ${base(depth.name)}` });
        checks.push({ level: "warn", text: `${base(c.name)} is not on the depth grid, so ${VAR_LABEL[v]} is left out.` });
        continue;
      }
      if (v !== "depth") vars[v] = c.src;
      used.push(c.name);
      const unit = v === "depth" ? "ft" : v === "velocity" ? "ft/s" : "h";
      notes.push({ name: c.name, use: `${VAR_LABEL[v]}: ${c.role.srcUnit}${c.role.srcUnit === unit ? "" : ` → ${unit}`}${c.role.guessed ? " (unit guessed from no name)" : ""}` });
      if (c.role.guessed) checks.push({ level: "warn", text: `${base(c.name)}: no unit in the name; read as ${c.role.srcUnit}.` });
    }
    for (const c of cands)
      if (![...pick.values()].includes(c)) {
        const p = pick.get(c.role.variable)!;
        notes.push({ name: c.name, use: `not used: ${VAR_LABEL[c.role.variable]} taken from ${base(p.name)}` });
      }
    // Cross-check an ASCII .max against the Runner GeoTIFF it duplicates.
    const asciiDepth = cands.find((c) => c.role.variable === "depth" && c.role.format === "ascii");
    if (asciiDepth && asciiDepth !== depth && asciiDepth.rawMax != null) {
      const asc = asciiDepth.rawMax * asciiDepth.role.factor;
      checks.push({ level: "ok", text: `${base(asciiDepth.name)} max ${asciiDepth.rawMax.toFixed(3)} m = ${asc.toFixed(2)} ft; cross-check against ${base(depth.name)} below.` });
    }
    const g = depth.geo;
    checks.push({
      level: "ok",
      text: `CRS ${CRS_DEFS[crs].label}${/NAD83/.test(CRS_DEFS[crs].label) ? "; NAD 83 shown on the WGS 84 map without a datum shift (about 1 m here)" : ""}.`,
    });
    checks.push({
      level: g.dx > 30.5 ? "warn" : "ok",
      text: `Cell ${g.dx.toFixed(g.dx < 10 ? 4 : 1)} m (${(g.dx * 3.28084).toFixed(1)} ft), ${g.width.toLocaleString("en-US")} × ${g.height.toLocaleString("en-US")}${g.dx > 30.5 ? ": coarse for structures" : ""}.`,
    });
    if (Math.abs(g.dx - g.dy) > 1e-6 * g.dx) checks.push({ level: "warn", text: `Cells are not square (${g.dx} × ${g.dy} m).` });
    checks.push({
      level: vars.velocity ? "ok" : "warn",
      text: vars.velocity ? "Velocity raster found." : "No velocity raster: TM 11 zones fall back to depth alone and are flagged.",
    });
    if (!vars.arrival) checks.push({ level: "ok", text: "No arrival-time raster; arrival shows —." });

    const ctx: ScanContext = req.ctx ?? { breach: { lon: runDam?.lon ?? 0, lat: runDam?.lat ?? 0 }, u: null, reachKm: 0 };
    progress("Scanning at full resolution…");
    const s = await scanRun(vars, ctx, proj, (fr) => progress(`Scanning at full resolution… ${Math.round(fr * 100)}%`));
    const retained: Retained = s.retained;
    const stats: ScanStats | null = req.ctx ? s.stats : null;
    if (asciiDepth && asciiDepth !== depth && asciiDepth.rawMax != null && stats) {
      const asc = asciiDepth.rawMax * asciiDepth.role.factor;
      const d = Math.abs(asc - stats.peakDepthAllFt);
      checks.push({
        level: d > 0.05 ? "warn" : "ok",
        text: `${base(asciiDepth.name)} (m → ft) peak ${asc.toFixed(2)} ft vs ${base(depth.name)} ${stats.peakDepthAllFt.toFixed(2)} ft: ${d > 0.05 ? "they differ — check units" : "units agree"}.`,
      });
    }
    if (stats && stats.peakDepthAllFt > 150) checks.push({ level: "warn", text: `Peak depth ${stats.peakDepthAllFt.toFixed(0)} ft is implausible: check the units.` });
    if (stats && stats.maxVelocityFps != null && stats.maxVelocityFps > 60)
      checks.push({ level: "warn", text: `Max velocity ${stats.maxVelocityFps.toFixed(0)} ft/s is implausible: check the units.` });
    const dssRun = req.source === "dsswise" ? /(^|\/)(\d{4,})_/.exec(depth.name)?.[2] : null;
    const label =
      req.source === "lisflood"
        ? `LISFLOOD-FP${runDam?.id ? ` (${runDam.id}${version ? `, ${version}` : ""})` : ""}`
        : `DSS-WISE Lite${dssRun ? ` (run ${dssRun})` : ""}`;
    const merged = new Map<string, string[]>();
    for (const n of notes) merged.set(n.name, [...(merged.get(n.name) ?? []), n.use]);
    const summary: RunSummary = {
      key: req.key,
      source: req.source,
      label,
      files: [...merged].map(([name, uses]) => ({ name, use: uses.join("; ") })),
      used,
      checks,
      crs,
      crsLabel: CRS_DEFS[crs].label,
      width: g.width,
      height: g.height,
      cellM: g.dx,
      retainedM: retained.dx,
      vars: (["depth", "velocity", "arrival"] as VarId[]).filter((v) => vars[v as "depth"]),
      stats,
      bounds: lonLatBounds(g, proj),
      runDam,
      runNotes,
    };
    runs.set(req.key, { summary, proj, vars, retained });
    return summary;
  }

  function layer(key: string): Layer & { run: Run } {
    const run = runs.get(key);
    if (!run) throw new Error("That run is no longer loaded; import it again.");
    return { grid: run.retained, proj: run.proj, run };
  }

  async function handle(req: Req, progress: (t: string) => void = () => {}): Promise<unknown> {
    switch (req.type) {
      case "import":
        return doImport(req, progress);
      case "rescan": {
        const l = layer(req.key);
        progress("Rescanning…");
        const s = await scanRun(l.run.vars, req.ctx, l.proj);
        l.run.summary = { ...l.run.summary, stats: s.stats };
        return l.run.summary;
      }
      case "sample": {
        const l = layer(req.key);
        return req.points.map((p): ReceptorSample => ({ id: p.id, lon: p.lon, lat: p.lat, ...samplePoint(l.grid, l.proj, p.lon, p.lat) }));
      }
      case "render": {
        const ls = req.keys.map(layer);
        const b = { west: Infinity, south: Infinity, east: -Infinity, north: -Infinity };
        for (const l of ls) {
          const x = l.run.summary.bounds;
          b.west = Math.min(b.west, x.west);
          b.south = Math.min(b.south, x.south);
          b.east = Math.max(b.east, x.east);
          b.north = Math.max(b.north, x.north);
        }
        const v = viewFor(b, req.maxPx ?? 2048);
        if (req.mode === "compare") {
          if (ls.length !== 2) throw new Error("Compare needs two runs.");
          const c = renderCompare(ls[0], ls[1], v, req.colors ?? ["#c2185b", "#2e7d32"], req.downstream);
          return { width: v.width, height: v.height, bounds: b, images: [c.diff, c.outline], compare: c.stats } satisfies RenderOut;
        }
        const imgs = ls.map((l) => renderDepth(l, v));
        if (req.colors) ls.forEach((l, i) => imgs.push(renderOutline(l, v, req.colors![i] ?? "#000000")));
        return { width: v.width, height: v.height, bounds: b, images: imgs } satisfies RenderOut;
      }
      case "strip": {
        const l = layer(req.key);
        const fr = frame(req.ctx, l.proj);
        const g = l.grid;
        const k = Math.max(1, Math.ceil((req.minCellM ?? 60) / g.dx));
        const w = Math.ceil(g.width / k);
        const h = Math.ceil(g.height / k);
        const d = new Float32Array(w * h);
        for (let r = 0; r < g.height; r++)
          for (let c = 0; c < g.width; c++) {
            const v = g.depth[r * g.width + c];
            const i = Math.floor(r / k) * w + Math.floor(c / k);
            if (v > d[i]) d[i] = v;
          }
        const strip: FloodStrip | null = stripFromGrid(
          { width: w, height: h, x0: g.x0, y0: g.y0, dx: g.dx * k, dy: g.dy * k, depth: d },
          (x, y) => l.proj.inv(x, y),
          (x, y) => (fr.known ? (x - fr.bx) * fr.ux + (y - fr.by) * fr.uy : 0),
          req.common,
          { direction: req.direction, breach: req.ctx.breach },
        );
        return strip;
      }
      case "drop":
        runs.delete(req.key);
        return true;
    }
  }
  return { handle };
}
