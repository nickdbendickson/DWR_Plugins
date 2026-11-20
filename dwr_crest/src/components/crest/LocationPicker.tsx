/**
 * Record step: click the map to set the dam's latitude and longitude (a new dam with no inventory point). Opens on the
 * record's point when it has one, else on North Dakota. Client-only (Leaflet), loaded lazily by the desk.
 */
import { useEffect, useState } from "react";
import { LayersControl, MapContainer, Marker, TileLayer, useMap, useMapEvents } from "react-leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import iconRetinaUrl from "leaflet/dist/images/marker-icon-2x.png";
import iconUrl from "leaflet/dist/images/marker-icon.png";
import shadowUrl from "leaflet/dist/images/marker-shadow.png";
import { basemaps } from "@/lib/crest/basemap";
import { ND_CENTER } from "@/lib/crest/mapview";

const LAYERS = basemaps(import.meta.env);
delete (L.Icon.Default.prototype as unknown as { _getIconUrl?: unknown })._getIconUrl;
L.Icon.Default.mergeOptions({ iconRetinaUrl, iconUrl, shadowUrl });

const r5 = (v: number) => Math.round(v * 1e5) / 1e5;

function Clicks({ onPick }: { onPick: (lat: number, lon: number) => void }) {
  useMapEvents({ click: (e) => onPick(r5(e.latlng.lat), r5(e.latlng.lng)) });
  return null;
}

/** Keeps the map on the point when it is typed in the fields. */
function Follow({ at }: { at: [number, number] | null }) {
  const map = useMap();
  useEffect(() => {
    if (at && !map.getBounds().contains(at)) map.setView(at, Math.max(map.getZoom(), 14));
  }, [map, at]);
  return null;
}

export function LocationPicker({ lat, lon, onPick }: { lat?: number; lon?: number; onPick: (lat: number, lon: number) => void }) {
  const has = Number.isFinite(lat) && Number.isFinite(lon);
  const at: [number, number] | null = has ? [lat!, lon!] : null;
  const [start] = useState(() => ({ center: at ?? ND_CENTER, zoom: at ? 15 : 7 }));
  return (
    <div style={{ height: 320, width: "100%", borderRadius: 8, overflow: "hidden", border: "1px solid var(--border)", cursor: "crosshair" }} data-testid="location-picker">
      <MapContainer center={start.center} zoom={start.zoom} style={{ height: "100%", width: "100%" }}>
        <LayersControl position="topright">
          {LAYERS.map((b, i) => (
            <LayersControl.BaseLayer key={b.id} name={b.name} checked={i === 0}>
              <TileLayer url={b.url} attribution={b.attribution} maxZoom={19} maxNativeZoom={b.maxZoom} />
            </LayersControl.BaseLayer>
          ))}
        </LayersControl>
        <Clicks onPick={onPick} />
        <Follow at={at} />
        {at ? <Marker position={at} /> : null}
      </MapContainer>
    </div>
  );
}
