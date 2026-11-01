import { useEffect } from "react";
import { MapContainer, TileLayer, LayersControl, useMap } from "react-leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import { basemaps } from "@/lib/crest/basemap";
import type { FloodStrip } from "@/lib/crest/floodstrip";
import { ND_CENTER } from "@/lib/crest/mapview";

const LAYERS = basemaps(import.meta.env);

export type Bounds = { west: number; south: number; east: number; north: number };
export type Overlay = { url: string; bounds: Bounds; opacity?: number };
export type MapPoint = { id: string; name: string; lon: number; lat: number; color: string; kind: "receptor" | "breach" | "run-dam"; note?: string };

type Props = {
  overlays: Overlay[];
  strip?: FloodStrip | null;
  points: MapPoint[];
  fit: Bounds | null;
};

const ll = (b: Bounds) => L.latLngBounds([b.south, b.west], [b.north, b.east]);

function Fit({ fit }: { fit: Bounds | null }) {
  const map = useMap();
  const key = fit ? `${fit.west.toFixed(4)},${fit.south.toFixed(4)},${fit.east.toFixed(4)},${fit.north.toFixed(4)}` : "";
  useEffect(() => {
    if (fit) map.fitBounds(ll(fit), { padding: [16, 16], maxZoom: 15 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, key]);
  return null;
}

const PANE = "crest-results";

/** Raster overlays (data-URL images in Web Mercator rows, so they line up with the tiles), the desk strip, and markers. */
function Layers({ overlays, strip, points }: Omit<Props, "fit">) {
  const map = useMap();
  useEffect(() => {
    if (!map.getPane(PANE)) {
      const pane = map.createPane(PANE);
      pane.style.zIndex = "350";
      pane.style.pointerEvents = "none";
    }
    const group = L.featureGroup();
    for (const o of overlays)
      group.addLayer(L.imageOverlay(o.url, ll(o.bounds), { pane: PANE, opacity: o.opacity ?? 1, interactive: false, className: "crest-results-raster" }));
    if (strip)
      group.addLayer(
        L.geoJSON(strip as unknown as GeoJSON.FeatureCollection, {
          pane: PANE,
          interactive: false,
          style: (f) => ({
            stroke: false,
            fill: true,
            fillColor: (f?.properties as { color?: string } | undefined)?.color ?? "#2a7fbf",
            fillOpacity: 0.55,
          }),
        }),
      );
    for (const p of points) {
      const m = L.circleMarker([p.lat, p.lon], {
        radius: p.kind === "receptor" ? 6 : 7,
        color: p.kind === "receptor" ? "#111" : p.color,
        weight: 2,
        fillColor: p.color,
        fillOpacity: p.kind === "run-dam" ? 0.2 : 0.95,
      });
      m.bindTooltip(`${p.name}${p.note ? ` (${p.note})` : ""}`, { direction: "top", permanent: false });
      group.addLayer(m);
    }
    group.addTo(map);
    return () => {
      group.remove();
    };
  }, [map, overlays, strip, points]);
  return null;
}

export function ResultsMap({ overlays, strip, points, fit }: Props) {
  return (
    <MapContainer center={ND_CENTER} zoom={12} className="h-[460px] w-full rounded-md border border-line" scrollWheelZoom>
      <LayersControl position="topright">
        {LAYERS.map((b, i) => (
          <LayersControl.BaseLayer key={b.id} name={b.name} checked={i === 0}>
            <TileLayer url={b.url} attribution={b.attribution} maxZoom={b.maxZoom} />
          </LayersControl.BaseLayer>
        ))}
      </LayersControl>
      <Layers overlays={overlays} strip={strip} points={points} />
      <Fit fit={fit} />
    </MapContainer>
  );
}
