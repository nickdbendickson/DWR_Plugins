import { useEffect } from "react";
import { LayersControl, MapContainer, TileLayer, useMap } from "react-leaflet";
import L from "leaflet";
import { extentLine } from "./extent-layer";
import "leaflet/dist/leaflet.css";
import { basemaps } from "@/lib/crest/basemap";
import { HILLSHADE_URL } from "@/lib/crest/dashboard";
import type { FloodStrip } from "@/lib/crest/floodstrip";
import { ND_CENTER } from "@/lib/crest/mapview";
import type { Bounds, Overlay } from "./ResultsMap";

const LAYERS = basemaps(import.meta.env);
const PANE = "crest-dash";

export type DashPoint = {
  id: string;
  name: string;
  lon: number;
  lat: number;
  color: string;
  label?: string;
  ring?: boolean;
};

type Props = {
  depth: FloodStrip | null;
  points: DashPoint[];
  arrival: { id: string; lon: number; lat: number; text: string }[];
  hillshade: boolean;
  overlays: Overlay[];
  fit: Bounds | null;
};

const ll = (b: Bounds) => L.latLngBounds([b.south, b.west], [b.north, b.east]);

function Fit({ fit }: { fit: Bounds | null }) {
  const map = useMap();
  const key = fit
    ? `${fit.west.toFixed(4)},${fit.south.toFixed(4)},${fit.east.toFixed(4)},${fit.north.toFixed(4)}`
    : "";
  useEffect(() => {
    if (fit) map.fitBounds(ll(fit), { padding: [16, 16], maxZoom: 15 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, key]);
  return null;
}

/** Each toggle is its own Leaflet group, so turning one off never redraws another (acceptance B1). */
function Group({ build, deps }: { build: (pane: string) => L.Layer | null; deps: unknown[] }) {
  const map = useMap();
  useEffect(() => {
    if (!map.getPane(PANE)) {
      const pane = map.createPane(PANE);
      pane.style.zIndex = "350";
      pane.style.pointerEvents = "none";
    }
    const layer = build(PANE);
    layer?.addTo(map);
    return () => {
      layer?.remove();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, ...deps]);
  return null;
}

export function DashboardMap({ depth, points, arrival, hillshade, overlays, fit }: Props) {
  return (
    <MapContainer center={ND_CENTER} zoom={12} className="dash-leaflet" scrollWheelZoom>
      <LayersControl position="topright">
        {LAYERS.map((b, i) => (
          <LayersControl.BaseLayer key={b.id} name={b.name} checked={i === 0}>
            <TileLayer url={b.url} attribution={b.attribution} maxZoom={b.maxZoom} />
          </LayersControl.BaseLayer>
        ))}
      </LayersControl>
      {hillshade ? (
        <TileLayer
          url={HILLSHADE_URL}
          opacity={0.45}
          zIndex={300}
          attribution="Hillshade: USGS The National Map"
        />
      ) : null}
      <Group
        deps={[overlays]}
        build={(pane) => {
          if (!overlays.length) return null;
          const g = L.featureGroup();
          for (const o of overlays)
            g.addLayer(
              L.imageOverlay(o.url, ll(o.bounds), {
                pane,
                opacity: o.opacity ?? 0.6,
                interactive: false,
              }),
            );
          return g;
        }}
      />
      <Group
        deps={[depth]}
        build={(pane) =>
          depth
            ? L.featureGroup(
                [
                  L.geoJSON(depth as unknown as GeoJSON.FeatureCollection, {
                    pane,
                    interactive: false,
                    style: (f) => ({
                      stroke: false,
                      fill: true,
                      fillColor: (f?.properties as { color?: string } | undefined)?.color ?? "#2a7fbf",
                      fillOpacity: 0.6,
                    }),
                  }),
                  extentLine(depth, pane),
                ].filter((l): l is L.Layer => l != null),
              )
            : null
        }
      />
      <Group
        deps={[points]}
        build={() => {
          if (!points.length) return null;
          const g = L.featureGroup();
          for (const p of points) {
            const m = L.circleMarker([p.lat, p.lon], {
              radius: 6,
              color: p.ring ? p.color : "#111",
              weight: 2,
              fillColor: p.color,
              fillOpacity: p.ring ? 0.15 : 0.95,
            });
            m.bindTooltip(p.label ?? p.name, { direction: "top" });
            g.addLayer(m);
          }
          return g;
        }}
      />
      <Group
        deps={[arrival]}
        build={() => {
          if (!arrival.length) return null;
          const g = L.featureGroup();
          for (const a of arrival)
            g.addLayer(
              L.marker([a.lat, a.lon], {
                interactive: false,
                icon: L.divIcon({
                  className: "dash-arrival",
                  html: a.text,
                  iconSize: undefined as unknown as L.PointExpression,
                  iconAnchor: [-8, 22],
                }),
              }),
            );
          return g;
        }}
      />
      <Fit fit={fit} />
    </MapContainer>
  );
}
