import { useEffect, useRef } from "react";
import { MapContainer, TileLayer, FeatureGroup, useMap } from "react-leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "leaflet-draw/dist/leaflet.draw.css";
import "leaflet-draw";
import type { DamGeometry, GeoJSONFeature } from "@/lib/crest/types";

// Fix default icon paths for Vite
delete (L.Icon.Default.prototype as unknown as { _getIconUrl?: unknown })._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl: "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-icon-2x.png",
  iconUrl: "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-icon.png",
  shadowUrl: "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/images/marker-shadow.png",
});

type Props = {
  geometry?: DamGeometry;
  onChange: (g: DamGeometry) => void;
};

type GeoLayer = L.Layer & { toGeoJSON: () => GeoJSONFeature };

function DrawControl({ geometry, onChange }: Props) {
  const map = useMap();
  const groupRef = useRef<L.FeatureGroup>(null);
  // The saved geometry seeds the layer once; later edits come from the map itself.
  // Re-running the effect on every save would re-add the same layers and controls.
  const initial = useRef(geometry);
  const changeRef = useRef(onChange);
  changeRef.current = onChange;

  useEffect(() => {
    if (!groupRef.current) return;
    const group = groupRef.current;

    const drawControl = new L.Control.Draw({
      position: "topright",
      draw: {
        polyline: { shapeOptions: { color: "#8B5E3C", weight: 4 }, metric: true },
        polygon: { shapeOptions: { color: "#0E768A", fillOpacity: 0.15 }, metric: true },
        marker: {},
        circle: false,
        rectangle: false,
        circlemarker: false,
      },
      edit: { featureGroup: group, remove: true },
    });
    map.addControl(drawControl);

    function save() {
      const next: DamGeometry = {};
      group.eachLayer((layer) => {
        const geo = (layer as GeoLayer).toGeoJSON();
        if (geo.geometry.type === "Point") {
          if (!next.reservoirPoint) next.reservoirPoint = geo;
          else next.breachPoint = geo;
        } else if (geo.geometry.type === "LineString") {
          next.damLine = geo;
        } else if (geo.geometry.type === "Polygon") {
          next.aoi = geo;
        }
      });
      changeRef.current(next);
    }

    const onCreated = (e: L.LeafletEvent) => {
      group.addLayer((e as L.DrawEvents.Created).layer);
      save();
    };
    const onEdited = () => save();
    const onDeleted = () => save();

    map.on(L.Draw.Event.CREATED, onCreated);
    map.on(L.Draw.Event.EDITED, onEdited);
    map.on(L.Draw.Event.DELETED, onDeleted);

    // Load existing geometry
    const g = initial.current;
    if (g) {
      const feats = [g.reservoirPoint, g.breachPoint, g.damLine, g.aoi].filter(
        (f): f is GeoJSONFeature => Boolean(f),
      );
      for (const feat of feats) {
        L.geoJSON(feat as unknown as GeoJSON.Feature).eachLayer((l) => group.addLayer(l));
      }
      if (feats.length > 0) {
        const bounds = group.getBounds();
        if (bounds.isValid()) map.fitBounds(bounds, { maxZoom: 15, padding: [24, 24] });
      }
    }

    return () => {
      map.removeControl(drawControl);
      map.off(L.Draw.Event.CREATED, onCreated);
      map.off(L.Draw.Event.EDITED, onEdited);
      map.off(L.Draw.Event.DELETED, onDeleted);
      group.clearLayers();
    };
  }, [map]);

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
