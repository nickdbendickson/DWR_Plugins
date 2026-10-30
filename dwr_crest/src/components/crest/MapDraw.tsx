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
import { ND911_ATTRIBUTION_SHORT, nd911ElevLabel, type Nd911Point, type Nd911Row } from "@/lib/crest/nd911";
import { DEM_ATTRIBUTION_SHORT, OUTLINE_LABEL, type TerrainOutline } from "@/lib/crest/outline";
import { PLSS_ATTRIBUTION_SHORT, type PlssRow } from "@/lib/crest/plss";
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
  /** PLSS sections the strip touches (null: not looked up / not available, see `plssMessage`). */
  plss?: PlssRow[] | null;
  plssMessage?: string | null;
  /** 911 address points: inside the strip (listed) and the rest of the looked-up box (null: not looked up). */
  nd911?: { inside: Nd911Row[]; outside: Nd911Point[] } | null;
  nd911Message?: string | null;
  /** Terrain outline from 3DEP (null: not built, see `outlineMessage`). */
  outline?: TerrainOutline | null;
  outlineMessage?: string | null;
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
function FloodStripLayer({ strip, on, dashed }: { strip: FloodStrip | null | undefined; on: boolean; dashed: boolean }) {
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
      // Under a terrain outline the strip is only the fallback: a dashed edge and a faint fill.
      style: (f) =>
        dashed
          ? { stroke: true, color: "#1d5f8f", weight: 1.2, dashArray: "6 4", opacity: 0.8, fill: true, fillColor: "#2a7fbf", fillOpacity: 0.06 }
          : {
              stroke: false,
              fill: true,
              fillColor: (f?.properties as { color?: string } | undefined)?.color ?? "#2a7fbf",
              fillOpacity: 0.45,
            },
    }).addTo(map);
    return () => {
      layer.remove();
    };
  }, [map, strip, on, dashed]);
  return null;
}

const OUTLINE_PANE = "crest-terrain-outline";

/** The terrain outline: depth-band fills with a thin edge, above the strip and under the PLSS lines, not interactive. */
function OutlineLayer({ outline, on }: { outline: TerrainOutline | null | undefined; on: boolean }) {
  const map = useMap();
  useEffect(() => {
    if (!map.getPane(OUTLINE_PANE)) {
      const pane = map.createPane(OUTLINE_PANE);
      pane.style.zIndex = "355";
      pane.style.pointerEvents = "none";
    }
    if (!outline || !on) return;
    const layer = L.geoJSON(outline as unknown as GeoJSON.FeatureCollection, {
      pane: OUTLINE_PANE,
      interactive: false,
      attribution: DEM_ATTRIBUTION_SHORT,
      style: (f) => {
        const color = (f?.properties as { color?: string } | undefined)?.color ?? "#2a7fbf";
        return { stroke: true, color, weight: 0.8, opacity: 0.9, fill: true, fillColor: color, fillOpacity: 0.55 };
      },
    } as L.GeoJSONOptions).addTo(map);
    return () => {
      layer.remove();
    };
  }, [map, outline, on]);
  return null;
}

const PLSS_PANE = "crest-plss";

const escHtml = (t: string) => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/**
 * Touched PLSS sections: outlines and labels (section number; T/R once per township) in their own pane above the strip and
 * under the drawn shapes, pointer events off and nothing interactive, so drawing and editing are unaffected.
 */
function PlssLayer({ rows, on }: { rows: PlssRow[] | null | undefined; on: boolean }) {
  const map = useMap();
  useEffect(() => {
    if (!map.getPane(PLSS_PANE)) {
      const pane = map.createPane(PLSS_PANE);
      pane.style.zIndex = "360";
      pane.style.pointerEvents = "none";
    }
    if (!rows || rows.length === 0 || !on) return;
    const group = L.featureGroup([], { attribution: PLSS_ATTRIBUTION_SHORT } as L.LayerOptions);
    const townships = new Map<string, L.LatLngBounds>();
    const label = (at: L.LatLng, html: string, w: number, h: number) =>
      L.marker(at, {
        pane: PLSS_PANE,
        interactive: false,
        keyboard: false,
        icon: L.divIcon({ className: "crest-plss-label", iconSize: [w, h], iconAnchor: [w / 2, h / 2], html }),
      });
    for (const r of rows) {
      const outline = L.geoJSON(
        { type: "Feature", properties: {}, geometry: { type: "MultiPolygon", coordinates: r.coordinates } } as GeoJSON.Feature,
        { pane: PLSS_PANE, interactive: false, style: () => ({ color: "#7a3b12", weight: 1.6, dashArray: "5 3", fill: false }) },
      );
      group.addLayer(outline);
      const b = outline.getBounds();
      const key = `${r.township} ${r.range}`;
      townships.set(key, townships.has(key) ? townships.get(key)!.extend(b) : L.latLngBounds(b.getSouthWest(), b.getNorthEast()));
      group.addLayer(
        label(
          b.getCenter(),
          `<div data-testid="plss-label" style="text-align:center;font-weight:700;font-size:13px;line-height:20px;color:#5a2a0a;text-shadow:0 0 3px #fff,0 0 3px #fff,0 0 3px #fff">Sec ${escHtml(r.section)}</div>`,
          64,
          20,
        ),
      );
    }
    // Township/range once per township, on the north edge of the sections it has in this list.
    for (const [tr, b] of townships) {
      group.addLayer(
        label(
          L.latLng(b.getNorth(), b.getCenter().lng),
          `<div data-testid="plss-township-label" style="display:inline-block;padding:0 4px;border:1px solid #7a3b12;border-radius:2px;background:rgba(255,250,240,.92);font-size:11px;line-height:16px;font-style:italic;color:#5a2a0a;white-space:nowrap">${escHtml(tr)}</div>`,
          88,
          18,
        ),
      );
    }
    group.addTo(map);
    return () => {
      group.remove();
    };
  }, [map, rows, on]);
  return null;
}

const ND911_PANE = "crest-nd911";
const ND911_IN = "#b3261e";
const ND911_OUT = "#6b6b6b";

/**
 * 911 address points: small circles in their own pane above the drawn shapes' fill. Points inside the strip are red
 * and larger, the others grey. They are interactive only for the hover tooltip (address, ground elevation in
 * feet NAVD88, place type, placement).
 */
function Nd911Layer({ pts, on, against }: { pts: Props["nd911"]; on: boolean; against: string }) {
  const map = useMap();
  useEffect(() => {
    if (!map.getPane(ND911_PANE)) {
      const pane = map.createPane(ND911_PANE);
      pane.style.zIndex = "420";
    }
    if (!pts || !on) return;
    const group = L.featureGroup([], { attribution: ND911_ATTRIBUTION_SHORT } as L.LayerOptions);
    const tip = (address: string, placeType: string, placement: string, elev: number | null, extra: string) =>
      `<div data-testid="nd911-tip" style="font-size:12px;line-height:1.35"><b>${escHtml(address)}</b><br/>Ground: ${escHtml(nd911ElevLabel(elev))}<br/>Place type: ${escHtml(placeType || "not given")}; placement: ${escHtml(placement || "not given")}${extra}</div>`;
    for (const p of pts.outside) {
      group.addLayer(
        L.circleMarker([p.lat, p.lon], { pane: ND911_PANE, radius: 3.5, color: "#fff", weight: 1, fillColor: ND911_OUT, fillOpacity: 0.9 }).bindTooltip(
          tip(p.address, p.placeType, p.placement, p.elevNavdFt, `<br/>Outside the ${against}`),
        ),
      );
    }
    for (const r of pts.inside) {
      group.addLayer(
        L.circleMarker([r.lat, r.lon], { pane: ND911_PANE, radius: 6, color: "#fff", weight: 1.5, fillColor: ND911_IN, fillOpacity: 1 }).bindTooltip(
          tip(r.address, r.placeType, r.placement, r.elevNavdFt, `<br/>Inside: ${escHtml(r.bandLabel)}, ${r.downstreamKm.toFixed(2)} km downstream${r.added ? "<br/>Added for review" : ""}`),
        ),
      );
    }
    group.addTo(map);
    return () => {
      group.remove();
    };
  }, [map, pts, on, against]);
  return null;
}

function StripLegend({
  strip,
  message,
  on,
  setOn,
  onZoom,
  plss,
  plssMessage,
  plssOn,
  setPlssOn,
  nd911,
  nd911Message,
  nd911On,
  setNd911On,
  outline,
  outlineMessage,
  outlineOn,
  setOutlineOn,
}: {
  strip: FloodStrip | null | undefined;
  message?: string | null;
  on: boolean;
  setOn: (v: boolean) => void;
  onZoom: () => void;
  plss?: PlssRow[] | null;
  plssMessage?: string | null;
  plssOn: boolean;
  setPlssOn: (v: boolean) => void;
  nd911?: Props["nd911"];
  nd911Message?: string | null;
  nd911On: boolean;
  setNd911On: (v: boolean) => void;
  outline?: TerrainOutline | null;
  outlineMessage?: string | null;
  outlineOn: boolean;
  setOutlineOn: (v: boolean) => void;
}) {
  const showOutline = !!outline && outlineOn;
  const used = new Set((showOutline ? outline : strip)?.features.map((f) => f.properties.band) ?? []);
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
          <div style={{ marginTop: 2, color: "#5c5a52" }}>
            {FLOOD_STRIP_DISCLAIMER}.{showOutline && on ? " Dashed: the fallback under the terrain outline." : ""}
          </div>
          {outline ? (
            <>
              <label style={{ display: "flex", alignItems: "center", gap: 6, fontWeight: 600, marginTop: 4 }}>
                <input
                  type="checkbox"
                  checked={outlineOn}
                  onChange={(e) => setOutlineOn(e.target.checked)}
                  aria-label="Show the terrain outline"
                />
                Terrain outline (approximate)
              </label>
              <div data-testid="outline-legend-note" style={{ marginTop: 2, color: "#5c5a52" }}>
                {OUTLINE_LABEL}; {outline.properties.outline_area_ac.toFixed(0)} ac.
              </div>
            </>
          ) : (
            <div data-testid="outline-legend-note" style={{ marginTop: 4, color: "#5c5a52" }}>
              {outlineMessage ?? "Terrain outline: not built."}
            </div>
          )}
          {on || showOutline ? (
            <div style={{ marginTop: 4 }}>
              {DEPTH_BANDS.filter((b) => used.has(b.id))
                .reverse()
                .map((b) => (
                  <div key={b.id} style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <span style={{ width: 14, height: 10, background: b.color, opacity: 0.75, display: "inline-block" }} />
                    {b.label}
                  </div>
                ))}
              <div style={{ color: "#5c5a52" }}>
                {showOutline ? "Desk depth over the terrain; not a model run." : "Schematic desk depth; not a model run."}
              </div>
            </div>
          ) : null}
          <div style={{ marginTop: 6, borderTop: "1px solid #e2dccf", paddingTop: 4 }}>
            {plss && plss.length > 0 ? (
              <label style={{ display: "flex", alignItems: "center", gap: 6, fontWeight: 600 }}>
                <input
                  type="checkbox"
                  checked={plssOn}
                  onChange={(e) => setPlssOn(e.target.checked)}
                  aria-label="Show the PLSS sections"
                />
                <span style={{ width: 14, height: 0, borderTop: "2px dashed #7a3b12", display: "inline-block" }} />
                PLSS sections ({plss.length})
              </label>
            ) : (
              <div data-testid="plss-legend-note" style={{ color: "#5c5a52" }}>
                {plssMessage ?? "PLSS sections: not looked up."}
              </div>
            )}
            {nd911 ? (
              <label style={{ display: "flex", alignItems: "center", gap: 6, fontWeight: 600, marginTop: 2 }}>
                <input
                  type="checkbox"
                  checked={nd911On}
                  onChange={(e) => setNd911On(e.target.checked)}
                  aria-label="Show the 911 address points"
                />
                <span style={{ width: 9, height: 9, borderRadius: 9, background: ND911_IN, display: "inline-block" }} />
                911 address points ({nd911.inside.length} inside of {nd911.inside.length + nd911.outside.length})
              </label>
            ) : (
              <div data-testid="nd911-legend-note" style={{ color: "#5c5a52" }}>
                {nd911Message ?? "911 address points: not looked up."}
              </div>
            )}
          </div>
        </>
      ) : (
        <div style={{ color: "#5c5a52" }}>{message ?? "No flood strip on the map."}</div>
      )}
    </div>
  );
}

export function MapDraw({
  geometry,
  lat,
  lon,
  onChange,
  strip,
  stripMessage,
  plss,
  plssMessage,
  nd911,
  nd911Message,
  outline,
  outlineMessage,
}: Props) {
  const view = initialView(geometry, lat, lon);
  const center: [number, number] = "center" in view ? view.center : ND_CENTER;
  const [on, setOn] = useState(true);
  const [plssOn, setPlssOn] = useState(true);
  const [nd911On, setNd911On] = useState(true);
  const [outlineOn, setOutlineOn] = useState(true);
  const mapRef = useRef<L.Map | null>(null);
  const zoom = () => {
    const map = mapRef.current;
    if (!map || !strip) return;
    const b = L.geoJSON(strip as unknown as GeoJSON.FeatureCollection).getBounds();
    if (outline) b.extend(L.geoJSON(outline as unknown as GeoJSON.FeatureCollection).getBounds());
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
      <StripLegend
        strip={strip}
        message={stripMessage}
        on={on}
        setOn={setOn}
        onZoom={zoom}
        plss={plss}
        plssMessage={plssMessage}
        plssOn={plssOn}
        setPlssOn={setPlssOn}
        nd911={nd911}
        nd911Message={nd911Message}
        nd911On={nd911On}
        setNd911On={setNd911On}
        outline={outline}
        outlineMessage={outlineMessage}
        outlineOn={outlineOn}
        setOutlineOn={setOutlineOn}
      />
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
        <FloodStripLayer strip={strip} on={on} dashed={!!outline && outlineOn} />
        <OutlineLayer outline={outline} on={outlineOn} />
        <PlssLayer rows={plss} on={plssOn} />
        <Nd911Layer pts={nd911} on={nd911On} against={outline ? "terrain outline" : "strip"} />
        <DrawControl geometry={geometry} lat={lat} lon={lon} onChange={onChange} />
      </MapContainer>
    </div>
  );
}
