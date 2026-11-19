/**
 * "Pick the breach point automatically" (Nick, 2026-11-05): where the old channel crosses the embankment, read off the
 * terrain. Along the dam line, the breach is put where the ground at the downstream toe is lowest: that is where the
 * stream ran before the dam, where the embankment is highest and where a piping or overtopping breach cuts down to.
 *
 *   dam line drawn      sample the line every SAMPLE_M; at each sample, the toe ground = the lowest DEM cell TOE_M
 *                       downstream of it (across the dam, away from the reservoir); the breach is the sample with the
 *                       lowest toe
 *   no dam line         the line is synthesized as the batch screen does it (batch.ts): across the 3DEP downhill at
 *                       the record's point, the record's length long, then the same search
 *   no reservoir point  one is added 150 m upstream of the breach (the side the ground rises to)
 *
 * The placed point says how it was found (feature property "source"); it is a starting point the user can drag.
 */
import { deriveFromDem, nearRequest, synthGeometry } from "./batch.ts";
import { downstreamDirection } from "./aoi.ts";
import { damLocation } from "./engine.ts";
import { fetchDem, type DemGrid, type DemRequest } from "./outline.ts";
import type { DamGeometry, DamRecord, GeoJSONFeature } from "./types.ts";

export const SAMPLE_M = 5;
/** Distances downstream of the line (m) the toe ground is read at. */
export const TOE_M = [15, 25, 35, 50];
const M_PER_DEG_LAT = 110574;
const kxOf = (lat: number) => 111320 * Math.cos((lat * Math.PI) / 180);

function groundAt(g: DemGrid, lon: number, lat: number): number | null {
  const fx = ((lon - g.bbox[0]) / (g.bbox[2] - g.bbox[0])) * g.width;
  const fy = ((g.bbox[3] - lat) / (g.bbox[3] - g.bbox[1])) * g.height;
  if (fx < 0 || fy < 0 || fx >= g.width || fy >= g.height) return null;
  const z = g.z[Math.floor(fy) * g.width + Math.floor(fx)];
  return Number.isFinite(z) && z > -9000 ? z : null;
}

export type AutoBreach =
  | { ok: true; geometry: DamGeometry; note: string; toeFt: number }
  | { ok: false; reason: string };

const pointFeature = (c: number[], role: "breach" | "reservoir", source: string): GeoJSONFeature => ({
  type: "Feature",
  properties: { role, source },
  geometry: { type: "Point", coordinates: c },
});

/** The breach point (and any missing dam line / reservoir point) for a record, on terrain `g` around the dam. */
export function autoBreachOn(dam: DamRecord, g: DemGrid): AutoBreach {
  const loc = damLocation(dam);
  if (!loc) return { ok: false, reason: "No location: fill the record's latitude and longitude or draw the dam line first." };
  let geometry: DamGeometry = { ...(dam.geometry ?? {}) };
  const notes: string[] = [];
  const lineOk = geometry.damLine?.geometry.type === "LineString" && (geometry.damLine.geometry.coordinates as number[][]).length >= 2;
  if (!lineOk) {
    const d = deriveFromDem(g, loc.lon, loc.lat);
    if (!d) return { ok: false, reason: "No terrain at the dam's point." };
    const len = Number.isFinite(dam.lengthFt) && dam.lengthFt > 0 ? dam.lengthFt : 600;
    const s = synthGeometry(loc.lon, loc.lat, d.u, len);
    geometry = { ...geometry, damLine: s.damLine, ...(geometry.reservoirPoint ? {} : { reservoirPoint: { ...s.reservoirPoint, properties: { role: "reservoir", source: "auto: 150 m up the terrain slope" } } }) };
    notes.push("No dam line was drawn, so one was laid across the downhill slope at the record's point, as long as the record's dam length.");
  }
  const line = geometry.damLine!.geometry.coordinates as number[][];
  const lat0 = line[0][1];
  const kx = kxOf(lat0);
  // Which side is downstream: from the reservoir point when there is one, else the side the ground falls to.
  let u: [number, number] | null = null;
  if (geometry.reservoirPoint) {
    const mid = line[Math.floor(line.length / 2)];
    const dir = downstreamDirection(geometry, { lon: mid[0], lat: mid[1] });
    u = dir.u ? [dir.u[0], dir.u[1]] : null;
  }
  if (!u) {
    const a = line[0];
    const b = line[line.length - 1];
    const ex = (b[0] - a[0]) * kx;
    const ny = (b[1] - a[1]) * M_PER_DEG_LAT;
    const L = Math.hypot(ex, ny) || 1;
    const n: [number, number] = [-ny / L, ex / L];
    const mid = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
    const side = (s: number) => groundAt(g, mid[0] + (s * n[0] * 60) / kx, mid[1] + (s * n[1] * 60) / M_PER_DEG_LAT) ?? Infinity;
    u = side(1) <= side(-1) ? n : [-n[0], -n[1]];
  }

  let best: { lon: number; lat: number; toe: number } | null = null;
  for (let k = 0; k + 1 < line.length; k++) {
    const [ax, ay] = line[k];
    const [bx, by] = line[k + 1];
    const segM = Math.hypot((bx - ax) * kx, (by - ay) * M_PER_DEG_LAT);
    const steps = Math.max(1, Math.round(segM / SAMPLE_M));
    for (let i = 0; i <= steps; i++) {
      const t = i / steps;
      const lon = ax + t * (bx - ax);
      const lat = ay + t * (by - ay);
      let toe = Infinity;
      for (const dM of TOE_M) {
        const z = groundAt(g, lon + (u[0] * dM) / kx, lat + (u[1] * dM) / M_PER_DEG_LAT);
        if (z != null && z < toe) toe = z;
      }
      if (Number.isFinite(toe) && (!best || toe < best.toe - 1e-6)) best = { lon, lat, toe };
    }
  }
  if (!best) return { ok: false, reason: "No terrain along the dam line: is the line inside the DEM?" };
  const toeFt = Math.round((best.toe / 0.3048) * 10) / 10;
  const src = `auto: lowest downstream toe along the dam line (${toeFt.toFixed(1)} ft NAVD 88)`;
  geometry.breachPoint = pointFeature([Math.round(best.lon * 1e7) / 1e7, Math.round(best.lat * 1e7) / 1e7], "breach", src);
  if (!geometry.reservoirPoint) {
    geometry.reservoirPoint = pointFeature([best.lon - (u[0] * 150) / kx, best.lat - (u[1] * 150) / M_PER_DEG_LAT], "reservoir", "auto: 150 m upstream of the breach");
    notes.push("No reservoir point was drawn, so one was put 150 m upstream of the breach.");
  }
  return {
    ok: true,
    geometry,
    toeFt,
    note: [`Breach point placed where the ground at the downstream toe is lowest along the dam line (${toeFt.toFixed(1)} ft): the old channel crossing. Drag it if you know better.`, ...notes].join(" "),
  };
}

/** Fetch the terrain around the dam (5 m cells) and place the breach point. */
export async function autoBreachPoint(dam: DamRecord, fetch: (req: DemRequest) => Promise<DemGrid> = (r) => fetchDem(r)): Promise<AutoBreach> {
  const line = dam.geometry?.damLine?.geometry.type === "LineString" ? (dam.geometry.damLine.geometry.coordinates as number[][]) : null;
  const loc = damLocation(dam);
  const c = line && line.length >= 2 ? [(line[0][0] + line[line.length - 1][0]) / 2, (line[0][1] + line[line.length - 1][1]) / 2] : loc ? [loc.lon, loc.lat] : null;
  if (!c) return { ok: false, reason: "No location: fill the record's latitude and longitude or draw the dam line first." };
  const halfKm = Math.min(2, Math.max(0.6, ((Number.isFinite(dam.lengthFt) ? dam.lengthFt : 600) * 0.3048) / 1000));
  try {
    return autoBreachOn(dam, await fetch(nearRequest(c[0], c[1], halfKm, 5)));
  } catch (e) {
    return { ok: false, reason: (e as Error).message };
  }
}
