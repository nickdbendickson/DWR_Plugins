/** Basemap tile sources for the desk map. All are key-free; they are the only part of the desk that needs the network.
 *
 * Default: USGS National Map Topo (US public domain; contours, streams and roads, which suit dam work).
 * Override at build time with VITE_CREST_TILE_URL (an XYZ template with {z}/{x}/{y}) and VITE_CREST_TILE_ATTRIBUTION,
 * e.g. for a local tile server. The override becomes the first (default) layer; the built-in ones stay selectable.
 */
export type Basemap = { id: string; name: string; url: string; attribution: string; maxZoom: number };

const USGS = '<a href="https://www.usgs.gov/programs/national-geospatial-program/national-map">USGS The National Map</a>';

export const BASEMAPS: Basemap[] = [
  {
    id: "usgs-topo",
    name: "USGS Topo",
    url: "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
    attribution: `Basemap: ${USGS}`,
    maxZoom: 16,
  },
  {
    id: "usgs-imagery-topo",
    name: "USGS Imagery + Topo",
    url: "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryTopo/MapServer/tile/{z}/{y}/{x}",
    attribution: `Basemap: ${USGS}`,
    maxZoom: 16,
  },
  {
    id: "osm",
    name: "OpenStreetMap",
    url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19,
  },
];

type Env = { readonly [key: string]: unknown };

const str = (v: unknown) => (typeof v === "string" ? v.trim() : "");

/** The layers to offer, default first. `env` is import.meta.env in the app. */
export function basemaps(env: Env | undefined): Basemap[] {
  const url = str(env?.VITE_CREST_TILE_URL);
  if (!url || !/\{z\}/.test(url) || !/\{x\}/.test(url) || !/\{y\}/.test(url)) return BASEMAPS;
  const custom: Basemap = {
    id: "custom",
    name: "Configured basemap",
    url,
    attribution: str(env?.VITE_CREST_TILE_ATTRIBUTION) || "Basemap: configured tile server",
    maxZoom: 19,
  };
  return [custom, ...BASEMAPS];
}
