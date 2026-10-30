import { useEffect, useRef, useState } from "react";
import { MapContainer, TileLayer, FeatureGroup, LayersControl, useMap } from "react-leaflet";
import L from "leaflet";
import "leaflet/dist/leaflet.css";
import "leaflet-draw/dist/leaflet.draw.css";
import "leaflet-draw";
import iconRetinaUrl from "leaflet/dist/images/marker-icon-2x.png";
import iconUrl from "leaflet/dist/images/marker-icon.png";
import shadowUrl from "leaflet/dist/images/marker-shadow.png";
import { basemaps } from "@/lib/crest/basemap";
import { DEPTH_BANDS, FLOOD_STRIP_DISCLAIMER, type FloodStrip } from "@/lib/crest/floodstrip";
import { initialView, ND_CENTER } from "@/lib/crest/mapview";
import type { DamGeometry, GeoJSONFeature } from "@/lib/crest/types";

const LAYERS = basemaps(import.meta.env);

// Marker icons are bundled (no CDN), so the map and drawing tools work offline; only basemap tiles need the network.
delete (L.Icon.Default.prototype as unknown as { _getIconUrl?: unknown })._getIconUrl;
L.Icon.Default.mergeOptions({ iconRetinaUrl, iconUrl, shadowUrl });

type Props = {
  geometry?: DamGeometry;
  /** Record lat/lon, used to open the map on the dam when nothing is drawn yet. */
  lat?: number;
  lon?: number;
  onChange: (g: DamGeometry) => void;
  /** Approximate flooded strip from the current result (null: none, see `stripMessage`). */
  strip?: FloodStrip | null;
  stripMessage?: string | null;
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

const STRIP_PANE = "crest-flood-strip";

/**
 * The flood strip as a read-only overlay: its own pane under the drawn shapes (overlayPane is 400) with pointer
 * events off, so it never catches a click meant for drawing or editing. Rebuilt whenever the result changes.
 */
function FloodStripLayer({ strip, on }: { strip: FloodStrip | null | undefined; on: boolean }) {
  const map = useMap();
  useEffect(() => {
    if (!map.getPane(STRIP_PANE)) {
      const pane = map.createPane(STRIP_PANE);
      pane.style.zIndex = "350";
      pane.style.pointerEvents = "none";
    }
    if (!strip || !on) return;
    const layer = L.geoJSON(strip as unknown as GeoJSON.FeatureCollection, {
      pane: STRIP_PANE,
      interactive: false,
      style: (f) => ({
        stroke: false,
        fill: true,
        fillColor: (f?.properties as { color?: string } | undefined)?.color ?? "#2a7fbf",
        fillOpacity: 0.45,
      }),
    }).addTo(map);
    return () => {
      layer.remove();
    };
  }, [map, strip, on]);
  return null;
}

function StripLegend({
  strip,
  message,
  on,
  setOn,
  onZoom,
}: {
  strip: FloodStrip | null | undefined;
  message?: string | null;
  on: boolean;
  setOn: (v: boolean) => void;
  onZoom: () => void;
}) {
  const used = new Set(strip?.features.map((f) => f.properties.band) ?? []);
  return (
    <div
      data-testid="flood-strip-legend"
      style={{
        position: "absolute",
        left: 8,
        bottom: 8,
        zIndex: 1000,
        maxWidth: 260,
        background: "rgba(255,255,255,0.92)",
        border: "1px solid #c9c2b4",
        borderRadius: 6,
        padding: "6px 8px",
        fontSize: 12,
        lineHeight: 1.35,
        color: "#1f2a30",
      }}
    >
      {strip ? (
        <>
          <label style={{ display: "flex", alignItems: "center", gap: 6, fontWeight: 600 }}>
            <input type="checkbox" checked={on} onChange={(e) => setOn(e.target.checked)} aria-label="Show the flood strip" />
            Flood strip
            <button
              type="button"
              onClick={onZoom}
              style={{ marginLeft: "auto", fontWeight: 400, textDecoration: "underline", background: "none", border: 0, cursor: "pointer" }}
            >
              zoom to
            </button>
          </label>
          <div style={{ marginTop: 2, color: "#5c5a52" }}>{FLOOD_STRIP_DISCLAIMER}.</div>
          {on ? (
            <div style={{ marginTop: 4 }}>
              {DEPTH_BANDS.filter((b) => used.has(b.id))
                .reverse()
                .map((b) => (
                  <div key={b.id} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <span style={{ width: 14, height: 10, background: b.color, opacity: 0.75, display: "inline-block" }} />
                    {b.label}
                  </div>
                ))}
              <div style={{ color: "#5c5a52" }}>Schematic desk depth; not a model run.</div>
            </div>
          ) : null}
        </>
      ) : (
        <div style={{ color: "#5c5a52" }}>{message ?? "No flood strip on the map."}</div>
      )}
    </div>
  );
}

export function MapDraw({ geometry, lat, lon, onChange, strip, stripMessage }: Props) {
  const view = initialView(geometry, lat, lon);
  const center: [number, number] = "center" in view ? view.center : ND_CENTER;
  const [on, setOn] = useState(true);
  const mapRef = useRef<L.Map | null>(null);
  const zoom = () => {
    const map = mapRef.current;
    if (!map || !strip) return;
    const b = L.geoJSON(strip as unknown as GeoJSON.FeatureCollection).getBounds();
    if (b.isValid()) map.fitBounds(b, { padding: [24, 24] });
  };
  return (
    <div
      style={{
        position: "relative",
        height: 480,
        width: "100%",
        borderRadius: 8,
        overflow: "hidden",
        border: "1px solid var(--border)",
      }}
    >
      <StripLegend strip={strip} message={stripMessage} on={on} setOn={setOn} onZoom={zoom} />
      <MapContainer
        ref={mapRef}
        center={center}
        zoom={"zoom" in view ? view.zoom : 10}
        style={{ height: "100%", width: "100%" }}
      >
        <LayersControl position="topright">
          {LAYERS.map((b, i) => (
            <LayersControl.BaseLayer key={b.id} name={b.name} checked={i === 0}>
              <TileLayer url={b.url} attribution={b.attribution} maxZoom={19} maxNativeZoom={b.maxZoom} />
            </LayersControl.BaseLayer>
          ))}
        </LayersControl>
        <FloodStripLayer strip={strip} on={on} />
        <DrawControl geometry={geometry} lat={lat} lon={lon} onChange={onChange} />
      </MapContainer>
    </div>
  );
}
