/**
 * The depth-band polygons on a Leaflet map, drawn cheaply (MapDraw.tsx, DashboardMap.tsx).
 *
 * A 10 m routed layer is six MultiPolygons, ~80,000 vertices in 641 rings. Drawn with L.geoJSON on the SVG renderer
 * every vertex was a path node redrawn on each pan and zoom, and the layer was rebuilt whenever the React prop was a
 * new object (every render that re-derived it). This draws it on a Canvas renderer, simplified to half a pixel at the
 * current zoom (geo-simplify.ts; the exact rings stay in the data for exports and lookups), and re-simplifies only
 * when the zoom level changes, with the simplified copies cached per (layer fingerprint, zoom level).
 */
import L from "leaflet";
import type { FloodStrip } from "@/lib/crest/floodstrip";
import { displayZoomKey, simplifyForDisplay } from "@/lib/crest/geo-simplify";
import type { TerrainOutline } from "@/lib/crest/outline";

type Collection = FloodStrip | TerrainOutline;

/**
 * A stable key for a collection, so an equal layer re-derived as a new object is not redrawn: the trace version and
 * the dam id, areas and vertex count of its bands (the terrain outline carries trace_version; the strip carries its
 * reach and area). Cheap: no coordinates are read beyond their count.
 */
export function layerKey(fc: Collection): string {
  const p = fc.properties as Record<string, unknown>;
  const bands = fc.features.map((f) => {
    const g = f.geometry;
    const n = g.type === "Polygon" ? g.coordinates.reduce((s, r) => s + r.length, 0) : g.coordinates.reduce((s, poly) => s + poly.reduce((t, r) => t + r.length, 0), 0);
    return `${f.properties.band}:${f.properties.area_ac}:${n}`;
  });
  return [p.kind, p.dam_id, p.trace_version ?? "", p.strip_area_ac ?? "", p.wet_reach_km ?? "", p.outline_area_ac ?? "", ...bands].join("|");
}

const renderers = new WeakMap<L.Map, Map<string, L.Canvas>>();
/**
 * One Canvas renderer per map and pane, shared by the flood layers drawn in that pane (a canvas per layer would stack
 * a bitmap each). A path drawn by a shared renderer lives in the RENDERER's pane, not the layer's, so the renderer is
 * made for the pane the caller wants: the strip's and the outline's own panes keep their place under the PLSS lines
 * and the drawn shapes.
 */
export function canvasRenderer(map: L.Map, pane: string): L.Canvas {
  let byPane = renderers.get(map);
  if (!byPane) {
    byPane = new Map();
    renderers.set(map, byPane);
  }
  let r = byPane.get(pane);
  if (!r) {
    r = L.canvas({ padding: 0.5, pane });
    byPane.set(pane, r);
  }
  return r;
}

export type FloodLayerStyle = (f: GeoJSON.Feature | undefined) => L.PathOptions;

/**
 * Keep `fc` drawn on `map` in `pane` while the returned handle lives: the simplified copy for the current zoom level,
 * redrawn when the zoom level changes (a pan redraws nothing but Leaflet's own transform). `remove()` takes it off.
 */
export function mountFloodLayer(map: L.Map, fc: Collection, opts: { pane: string; style: FloodLayerStyle; attribution?: string }): { remove: () => void } {
  const cache = new Map<number, Collection>();
  let layer: L.GeoJSON | null = null;
  let shown = Number.NaN;
  const renderer = canvasRenderer(map, opts.pane);
  const draw = () => {
    const zk = displayZoomKey(map.getZoom());
    if (zk === shown) return;
    let copy = cache.get(zk);
    if (!copy) {
      copy = simplifyForDisplay(fc, zk);
      cache.set(zk, copy);
    }
    const next = L.geoJSON(copy as unknown as GeoJSON.FeatureCollection, {
      pane: opts.pane,
      renderer,
      interactive: false,
      ...(opts.attribution ? { attribution: opts.attribution } : {}),
      style: opts.style,
    } as L.GeoJSONOptions);
    next.addTo(map);
    layer?.remove();
    layer = next;
    shown = zk;
  };
  draw();
  map.on("zoomend", draw);
  return {
    remove: () => {
      map.off("zoomend", draw);
      layer?.remove();
      layer = null;
    },
  };
}
