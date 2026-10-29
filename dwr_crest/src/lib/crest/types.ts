export type DatumName = "Local" | "NAVD88" | "NGVD29";
export type FailureMode = "overtopping" | "piping";
export type BreachMethod = "froehlich" | "macdonald" | "tr60";
export type EngineName = "lisflood" | "anuga" | "routing";
export type ReceptorKind = "house" | "road" | "barn";
export type StepId = "record" | "breach" | "flood" | "screen" | "report";
export type ScreenLevel = "High" | "Significant" | "Low" | "Review";

export type ReceptorSeed = {
  id: string;
  name: string;
  kind: ReceptorKind;
  /** 0 at the dam, 1 at the downstream end of the desk valley. */
  station: number;
  /** -1 left bank to +1 right bank. */
  lateral: number;
  /** Feet the pad sits above the local channel bed. */
  padFt: number;
};

/** Optional drawn geometry for DSS-WISE style layout (GeoJSON Feature or Geometry) */
export type DrawnGeometry = {
  reservoirPoint?: GeoJSON.Point | null;
  damLine?: GeoJSON.LineString | null;
  breachPoint?: GeoJSON.Point | null;
  aoi?: GeoJSON.Polygon | null;
};

export type DamRecord = {
  id: string;
  name: string;
  nid: string;
  county: string;
  year: number;
  heightFt: number;
  lengthFt: number;
  crestFt: number;
  streambedFt: number;
  spillwayFt: number;
  normalPoolFt: number;
  storageAcFt: number;
  drainageMi2: number;
  statedDatum: DatumName;
  demCrestFt: number;
  failureMode: FailureMode;
  method: BreachMethod;
  engine: EngineName;
  receptors: ReceptorSeed[];
  /** Optional interactive map geometry (Leaflet prototype) */
  geometry?: DrawnGeometry;
};

export type SeriesPoint = { tHr: number; qCfs: number };

export type Analysis = {
  datum: {
    rule: string;
    offsetFt: number | null;
    crestNavdFt: number;
    streambedNavdFt: number | null;
    spillwayNavdFt: number | null;
    poolNavdFt: number | null;
    warnings: string[];
  };
  breach: {
    label: string;
    widthFt: number;
    timeHr: number;
    capped: boolean;
    empiricalPeakCfs: number;
    fittedPeakCfs: number;
    volumeAcFt: number;
    series: SeriesPoint[];
    notes: string[];
  };
  flood: {
    engineLabel: string;
    maxDepthFt: number;
    maxVelocityFps: number;
    acres: number;
    lengthM: number;
    stations: { s: number; halfM: number; depthFt: number }[];
    receptors: {
      id: string;
      name: string;
      kind: ReceptorKind;
      station: number;
      lateral: number;
      depthFt: number;
      velocityFps: number;
      arrivalHr: number | null;
      flooded: boolean;
    }[];
  };
  screen: {
    level: ScreenLevel;
    reasons: string[];
  };
};
