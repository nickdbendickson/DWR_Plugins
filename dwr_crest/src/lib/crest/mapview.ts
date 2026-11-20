import type { DamGeometry, GeoJSONFeature } from "./types.ts";

/** Fallback view when a record has no geometry and no lat/lon: central North Dakota. */
export const ND_CENTER: [number, number] = [47.45, -100.47];

/**
 * Where the map opens: the drawn breach point, else the dam line, else the record lat/lon, else the reservoir point,
 * else the AOI. Returns the features to fit, or a single centre.
 */
export function initialView(
  geometry: DamGeometry | undefined,
  lat?: number,
  lon?: number,
): { fit: GeoJSONFeature[] } | { center: [number, number]; zoom: number } {
  const g = geometry ?? {};
  const focus = [g.breachPoint, g.damLine].filter((f): f is GeoJSONFeature => Boolean(f));
  if (focus.length > 0) return { fit: focus };
  if (lat != null && lon != null && Number.isFinite(lat) && Number.isFinite(lon) && !(lat === 0 && lon === 0)) {
    return { center: [lat, lon], zoom: 14 };
  }
  const rest = [g.reservoirPoint, g.aoi].filter((f): f is GeoJSONFeature => Boolean(f));
  if (rest.length > 0) return { fit: rest };
  return { center: ND_CENTER, zoom: 7 };
}
