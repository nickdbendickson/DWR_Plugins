import { useEffect, useRef } from "react";
import { MapContainer, TileLayer, FeatureGroup, useMap } from "react-leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "leaflet-draw/dist/leaflet.draw.css";
import "leaflet-draw";
import iconRetinaUrl from "leaflet/dist/images/marker-icon-2x.png";
import iconUrl from "leaflet/dist/images/marker-icon.png";
import shadowUrl from "leaflet/dist/images/marker-shadow.png";
import { initialView, ND_CENTER } from "@/lib/crest/mapview";
import type { DamGeometry, GeoJSONFeature } from "@/lib/crest/types";

// Marker icons are bundled (no CDN), so the map and drawing tools work offline; only basemap tiles need the network.
delete (L.Icon.Default.prototype as unknown as { _getIconUrl?: unknown })._getIconUrl;
L.Icon.Default.mergeOptions({ iconRetinaUrl, iconUrl, shadowUrl });

type Props = {
  geometry?: DamGeometry;
  /** Record lat/lon, used to open the map on the dam when nothing is drawn yet. */
  lat?: number;
  lon?: number;
  onChange: (g: DamGeometry) => void;
};

type GeoLayer = L.Layer & { toGeoJSON: () => GeoJSONFeature };

function DrawControl({ geometry, lat, lon, onChange }: Props) {
  const map = useMap();
  const groupRef = useRef<L.FeatureGroup>(null);
  // The saved geometry seeds the layer once; later edits come from the map itself.
  // Re-running the effect on every save would re-add the same layers and controls.
  const initial = useRef({ geometry, lat, lon });
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

    // Load existing geometry, then open on the dam / breach point (not a fixed regional view).
    const { geometry: g, lat: lat0, lon: lon0 } = initial.current;
    if (g) {
      const feats = [g.reservoirPoint, g.breachPoint, g.damLine, g.aoi].filter(
        (f): f is GeoJSONFeature => Boolean(f),
      );
      for (const feat of feats) {
        L.geoJSON(feat as unknown as GeoJSON.Feature).eachLayer((l) => group.addLayer(l));
      }
    }
    const view = initialView(g, lat0, lon0);
    if ("fit" in view) {
      const bounds = L.geoJSON(view.fit as unknown as GeoJSON.Feature[]).getBounds();
      if (bounds.isValid()) map.fitBounds(bounds, { maxZoom: 15, padding: [24, 24] });
    } else {
      map.setView(view.center, view.zoom);
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

export function MapDraw({ geometry, lat, lon, onChange }: Props) {
  const view = initialView(geometry, lat, lon);
  const center: [number, number] = "center" in view ? view.center : ND_CENTER;
  return (
    <div style={{ height: 480, width: "100%", borderRadius: 8, overflow: "hidden", border: "1px solid var(--border)" }}>
      <MapContainer
        center={center}
        zoom={"zoom" in view ? view.zoom : 10}
        style={{ height: "100%", width: "100%" }}
      >
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>'
          url="https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png"
        />
        <DrawControl geometry={geometry} lat={lat} lon={lon} onChange={onChange} />
      </MapContainer>
    </div>
  );
}
