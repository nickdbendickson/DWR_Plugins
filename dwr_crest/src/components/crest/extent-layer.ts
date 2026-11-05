import L from "leaflet";
import type { ExtentFeature } from "@/lib/crest/outline";

/** The flood extent line around a terrain layer (option 3 of the 2026-11-05 outline mock-up). */
export const EXTENT_COLOR = "#0b3d6b";

/**
 * The terrain layer's generalized extent line (outline.ts extentOf), when the layer carries one: a dark line around the
 * depth bands. Null for the strip, an imported run, or a layer saved before trace version 8.
 */
export function extentLine(area: unknown, pane: string): L.Layer | null {
  const extent = (area as { extent?: ExtentFeature } | null | undefined)?.extent;
  if (!extent) return null;
  return L.geoJSON(extent as unknown as GeoJSON.Feature, {
    pane,
    interactive: false,
    style: () => ({ color: EXTENT_COLOR, weight: 2, opacity: 0.95, fill: false }),
  } as L.GeoJSONOptions);
}
