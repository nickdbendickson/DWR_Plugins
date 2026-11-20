/**
 * Terrain source: USGS 3DEP (default) or a local DEM file the user picks (desktop app). See dem-source.ts.
 * The choice is shared by the whole desk: one module-level state, so the Flood step and the Batch screen agree.
 */
import { useEffect, useState } from "react";

import { clearDemSource, currentDemSource, ensureDemSource, pickDemSource, publishDemSource as publish, subscribeDemSource, type DemState } from "@/lib/crest/dem-source";

function useDemSource(): DemState | null {
  const [s, setS] = useState<DemState | null>(currentDemSource);
  useEffect(() => {
    const off = subscribeDemSource(setS);
    void ensureDemSource().then(setS);
    return off;
  }, []);
  return s;
}

export function TerrainSource({ compact = false }: { compact?: boolean }) {
  const s = useDemSource();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const btn = "inline-flex h-11 items-center rounded-sm border border-line bg-paper px-3 text-sm text-ink disabled:opacity-50";
  const pick = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await pickDemSource();
      if (r.error) setErr(r.error);
      if (r.state) publish(r.state);
    } catch (e) {
      setErr((e as Error).message);
    }
    setBusy(false);
  };
  const clear = async () => {
    setBusy(true);
    setErr(null);
    publish(await clearDemSource());
    setBusy(false);
  };
  if (!s) return null;
  return (
    <div className={`rounded-md border border-line p-3 text-sm ${compact ? "" : "mt-3"}`} data-testid="terrain-source">
      <p>
        <span className="text-muted">Terrain: </span>
        {s.kind === "local" ? (
          <>
            <span className="font-medium">local DEM</span> <span className="break-all">{s.path}</span>
          </>
        ) : (
          <span className="font-medium">USGS 3DEP (online)</span>
        )}
      </p>
      {s.kind === "unavailable" ? (
        <p className="mt-1 text-xs text-muted">
          A local DEM (e.g. the statewide 1 m VRT) can be chosen in the Crest desktop app; a web page cannot open a file on your drive by path. For a
          batch from the command line: <code>npm run batch:dams -- --dem /path/to/ND_1m_EPSG26914.vrt</code>.
        </p>
      ) : (
        <>
          <div className="mt-2 flex flex-wrap gap-2">
            <button type="button" className={btn} disabled={busy} onClick={() => void pick()}>
              {s.kind === "local" ? "Choose another DEM file…" : "Use a local DEM file (.vrt, .tif)…"}
            </button>
            {s.kind === "local" ? (
              <button type="button" className={btn} disabled={busy} onClick={() => void clear()}>
                Back to USGS 3DEP
              </button>
            ) : null}
          </div>
          {!s.gdalwarp ? (
            <p className="mt-1 text-xs text-amber">gdalwarp was not found: install QGIS (it ships gdalwarp) or put GDAL on PATH before using a local DEM.</p>
          ) : null}
          <p className="mt-1 text-xs text-muted">
            Elevations must be metres (NAVD 88). Only the window around each dam is read from the file. Cells with no data in the mosaic are left
            blank, as 3DEP gaps are.
          </p>
        </>
      )}
      {err ? <p className="mt-1 text-xs text-red-700">{err}</p> : null}
    </div>
  );
}
