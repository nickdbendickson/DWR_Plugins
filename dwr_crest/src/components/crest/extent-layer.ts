import L from "leaflet";
import type { ExtentFeature, OpenEdgeFeature } from "@/lib/crest/outline";

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

/** The dashed edge where a routed flood goes on beyond the terrain shown (outline.ts openEdgeOf). */
export const OPEN_EDGE_COLOR = "#d9480f";

/**
 * Where the routed layer stops short of the water (outline.ts OpenEdgeFeature): a dashed orange line with a tooltip
 * giving the water held back. Null when the layer has none (the flood stops inside the terrain, or an older layer).
 */
export function openEdgeLine(area: unknown, pane: string): L.Layer | null {
  const edge = (area as { openEdge?: OpenEdgeFeature } | null | undefined)?.openEdge;
  if (!edge) return null;
  const held = edge.properties.held_ac_ft;
  const tip = `The flood goes on beyond the terrain shown${held > 0 ? ` (about ${held.toLocaleString("en-US")} ac-ft held back here)` : ""}.`;
  return L.geoJSON(edge as unknown as GeoJSON.Feature, {
    pane,
    style: () => ({ color: OPEN_EDGE_COLOR, weight: 3, opacity: 0.95, dashArray: "6 5", fill: false }),
    onEachFeature: (_f, layer) => layer.bindTooltip(tip, { sticky: true }),
  } as L.GeoJSONOptions);
}
