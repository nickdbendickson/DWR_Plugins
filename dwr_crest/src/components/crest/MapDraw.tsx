import { useEffect, useRef } from "react";
import { MapContainer, TileLayer, FeatureGroup, useMap } from "react-leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "leaflet-draw/dist/leaflet.draw.css";
import "leaflet-draw";
import type { DamGeometry, GeoJSONFeature } from "@/lib/crest/types";

// Fix default icon paths for Vite
delete (L.Icon.Default.prototype as any)._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl: "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-icon-2x.png",
  iconUrl: "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-icon.png",
  shadowUrl: "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-shadow.png",
});

type Props = {
  geometry?: DamGeometry;
  onChange: (g: DamGeometry) => void;
};

function DrawControl({ geometry, onChange }: Props) {
  const map = useMap();
  const groupRef = useRef<L.FeatureGroup>(null);

  useEffect(() => {
    if (!groupRef.current) return;
    const group = groupRef.current;

    const drawControl = new L.Control.Draw({
      position: "topright",
      draw: {
        polyline: { shapeOptions: { color: "#8B5E3C", weight: 4 }, metric: true },
        polygon: { shapeOptions: { color: "#0E768A", fillOpacity: 0.15 }, metric: true },
        marker: true,
        circle: false,
        rectangle: false,
        circlemarker: false,
      },
      edit: { featureGroup: group, remove: true },
    });
    map.addControl(drawControl);

    const onCreated = (e: any) => {
      const layer = e.layer;
      group.addLayer(layer);
      save();
    };
    const onEdited = () => save();
    const onDeleted = () => save();

    map.on(L.Draw.Event.CREATED, onCreated);
    map.on(L.Draw.Event.EDITED, onEdited);
    map.on(L.Draw.Event.DELETED, onDeleted);

    function save() {
      const next: DamGeometry = {};
      group.eachLayer((layer: any) => {
        const geo = layer.toGeoJSON() as GeoJSONFeature;
        if (geo.geometry.type === "Point") {
          if (!next.reservoirPoint) next.reservoirPoint = geo;
          else next.breachPoint = geo;
        } else if (geo.geometry.type === "LineString") {
          next.damLine = geo;
        } else if (geo.geometry.type === "Polygon") {
          next.aoi = geo;
        }
      });
      onChange(next);
    }

    // Load existing geometry
    if (geometry) {
      [geometry.reservoirPoint, geometry.breachPoint, geometry.damLine, geometry.aoi]
        .filter(Boolean)
        .forEach((feat) => {
          const layer = L.geoJSON(feat as any);
          layer.eachLayer((l) => group.addLayer(l));
        });
    }

    return () => {
      map.removeControl(drawControl);
      map.off(L.Draw.Event.CREATED, onCreated);
      map.off(L.Draw.Event.EDITED, onEdited);
      map.off(L.Draw.Event.DELETED, onDeleted);
    };
  }, [map, geometry, onChange]);

  return <FeatureGroup ref={groupRef} />;
}

export function MapDraw({ geometry, onChange }: Props) {
  return (
    <div style={{ height: 480, width: "100%", borderRadius: 8, overflow: "hidden", border: "1px solid var(--border)" }}>
      <MapContainer
        center={[46.8, -100.8]} // ND-ish default
        zoom={10}
        style={{ height: "100%", width: "100%" }}
      >
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        />
        <DrawControl geometry={geometry} onChange={onChange} />
      </MapContainer>
    </div>
  );
}
