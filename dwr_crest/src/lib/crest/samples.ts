import type { DamGeometry, DamRecord, GeoJSONFeature, ReceptorSeed } from "./types";

const point = (lon: number, lat: number): GeoJSONFeature => ({
  type: "Feature",
  geometry: { type: "Point", coordinates: [lon, lat] },
  properties: {},
});
const line = (coords: [number, number][]): GeoJSONFeature => ({
  type: "Feature",
  geometry: { type: "LineString", coordinates: coords },
  properties: {},
});

// The samples carry a dam line and a reservoir point so a drawn area has a downstream direction
// (without them a corridor AOI fell back to "nearest edge in any direction": 100 m / 2.06 ac).
//
// ND00199: from the DWR-2801 DSS-WISE input shapes (117417_Input_Shapes, UTM 14N, read-only).
// The embankment (117417_structures.shp) is a 6-vertex L; its straight 131 m segment through the
// breach (vertices 3-4) is used as the dam line, because the direction is the normal to the line's
// first-to-last chord and the whole L's chord points WNW, upstream. Reservoir point from
// 117417_reservoirs.shp. Result: downstream ~130 deg (SE), matching the DSS-WISE flood extent
// (centroid bearing ~123 deg from the breach).
const ND00199_GEOMETRY: DamGeometry = {
  damLine: line([
    [-97.39039149, 46.98383234],
    [-97.39151266, 46.98293598],
  ]),
  reservoirPoint: point(-97.39171369, 46.98463491),
};

// The other two samples are fictional. Their placement is illustrative, not a surveyed dam: a straight
// east-west embankment of the record's crest length on open ND prairie, pool to the north, so the
// valley runs south; breach point at the middle of the dam line.
const LOCAL_SAMPLE_GEOMETRY: DamGeometry = {
  damLine: line([
    [-98.751971, 47.1],
    [-98.748029, 47.1],
  ]),
  reservoirPoint: point(-98.75, 47.102713),
  breachPoint: point(-98.75, 47.1),
};
const PRAIRIE_GEOMETRY: DamGeometry = {
  damLine: line([
    [-100.401284, 46.95],
    [-100.398716, 46.95],
  ]),
  reservoirPoint: point(-100.4, 46.951357),
  breachPoint: point(-100.4, 46.95),
};
const ILLUSTRATIVE_NOTE =
  "Fictional dam. Its map placement (an east-west dam line with the pool to the north, so the valley runs south) is illustrative, not a surveyed site.";

function valley(prefix: string): ReceptorSeed[] {
  return [
    {
      id: `${prefix}-road-1`,
      name: "Township road",
      kind: "road",
      station: 0.22,
      lateral: 0.15,
      padFt: 1.2,
      aadt: 40,
    },
    {
      id: `${prefix}-farm`,
      name: "Farmstead",
      kind: "house",
      station: 0.4,
      lateral: -0.35,
      padFt: 3.4,
    },
    {
      id: `${prefix}-barn`,
      name: "Machine shed",
      kind: "barn",
      station: 0.46,
      lateral: -0.55,
      padFt: 2.2,
      habitable: false,
    },
    {
      id: `${prefix}-road-2`,
      name: "County road",
      kind: "road",
      station: 0.64,
      lateral: 0.05,
      padFt: 1.6,
      aadt: 320,
    },
    {
      id: `${prefix}-house`,
      name: "House on the bench",
      kind: "house",
      station: 0.8,
      lateral: 0.42,
      padFt: 6.5,
    },
  ];
}

export const SAMPLE_DAMS: DamRecord[] = [
  {
    id: "nd00199",
    name: "Swan Buffalo Detention 12",
    nid: "ND00199",
    county: "Cass",
    year: 1960,
    // DWR-2801 benchmark (Nick's ruling, 2026-10-29): crest 1072.24 ft NAVD88 = the NGVD29 record crest
    // 1071.15 + VERTCON 1.09 ft, already converted, so the datum is NAVD88 and nothing shifts it again.
    // Breach bottom 1044.74 NAVD88, height 27.50 ft, piping. Storage 1,612.59 ac-ft (the review's value,
    // = the r3 hydrograph volume), surface area 154.5 ac. DEM crest given in NAVD88 too (1071.15 is NGVD29).
    // Generic receptor seeds, kept on purpose (Nick): the Farmstead lands in the TM 11 judgment zone, so
    // the sample screens High (flagged); the class of record is ND MEDIUM / TM11 Significant.
    heightFt: 27.5,
    lengthFt: 1700,
    crestFt: 1072.24,
    streambedFt: 1044.74,
    spillwayFt: NaN,
    normalPoolFt: NaN,
    storageAcFt: 1612.59,
    drainageMi2: 13.7,
    statedDatum: "NAVD88",
    datumNote:
      "Crest converted from NGVD29 1071.15 ft (+1.09 ft VERTCON at the site); do not shift again.",
    demCrestFt: 1072.24,
    classOfRecord: "ND MEDIUM / TM11 Significant (DWR-2801 benchmark)",
    sampleNote:
      "Sample with generic receptor seeds, not the DWR-2801 benchmark structures. With the judgment zone counted as High (the default), the generic Farmstead falls in the ACER TM 11 judgment zone, so this desk screen reads High (flagged). That is a flag for an engineer, not the class of record (ND MEDIUM / TM11 Significant).",
    lat: 46.98364,
    lon: -97.39064,
    geometry: ND00199_GEOMETRY,
    surfaceAreaAc: 154.5,
    failureMode: "piping",
    method: "froehlich",
    engine: "lisflood",
    receptors: valley("abs"),
  },
  {
    id: "local-sample",
    name: "Local-datum sample",
    nid: "SAMPLE-4031",
    county: "Worked example",
    year: 1958,
    heightFt: 22,
    lengthFt: 980,
    crestFt: 120.7,
    streambedFt: 100,
    spillwayFt: 117.7,
    normalPoolFt: 117.7,
    storageAcFt: 420,
    drainageMi2: 6.2,
    statedDatum: "Local",
    demCrestFt: 1629,
    sampleNote: ILLUSTRATIVE_NOTE,
    geometry: LOCAL_SAMPLE_GEOMETRY,
    failureMode: "piping",
    method: "froehlich",
    engine: "anuga",
    receptors: valley("loc"),
  },
  {
    id: "prairie",
    name: "Prairie holding pond",
    nid: "DESK-014",
    county: "Burleigh",
    year: 1984,
    heightFt: 16,
    lengthFt: 640,
    crestFt: 42,
    streambedFt: 30,
    spillwayFt: 39,
    normalPoolFt: 36,
    storageAcFt: 48,
    drainageMi2: 1.1,
    statedDatum: "Local",
    demCrestFt: 1710,
    sampleNote: ILLUSTRATIVE_NOTE,
    geometry: PRAIRIE_GEOMETRY,
    failureMode: "overtopping",
    method: "tr60",
    engine: "routing",
    receptors: [
      {
        id: "pr-drive",
        name: "Field approach",
        kind: "road",
        station: 0.3,
        lateral: 0.1,
        padFt: 2,
      },
      {
        id: "pr-house",
        name: "Section house",
        kind: "house",
        station: 0.72,
        lateral: -0.4,
        padFt: 8,
      },
    ],
  },
];

export function blankDam(): DamRecord {
  return {
    id: `desk-${Math.random().toString(36).slice(2, 8)}`,
    name: "Untitled embankment",
    nid: "NEW",
    county: "",
    year: new Date().getFullYear(),
    heightFt: 20,
    lengthFt: 800,
    crestFt: 100,
    streambedFt: 82,
    spillwayFt: 96,
    normalPoolFt: 92,
    storageAcFt: 200,
    drainageMi2: 4,
    statedDatum: "Local",
    demCrestFt: 1550,
    failureMode: "overtopping",
    method: "froehlich",
    engine: "lisflood",
    receptors: valley("new"),
  };
}
