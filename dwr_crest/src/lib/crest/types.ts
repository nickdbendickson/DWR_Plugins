export type DatumName = "Local" | "NAVD88" | "NGVD29";
export type FailureMode = "overtopping" | "piping";
export type BreachMethod = "froehlich" | "macdonald" | "tr60";
export type EngineName = "lisflood" | "anuga" | "routing";
export type ReceptorKind = "house" | "mobile" | "barn" | "road";
export type StepId = "record" | "breach" | "flood" | "screen" | "report";
/** ND Dam Safety hazard classes (REG_05.2026 5.2), plus Review when the desk cannot screen. */
export type ScreenLevel = "High" | "Medium" | "Low" | "Review";
export type RoadClass = "interstate" | "us_state" | "other";

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
  /** Roads: annual average daily traffic (HPMS). */
  aadt?: number;
  roadClass?: RoadClass;
  /** Structures: habitable? Houses and mobile homes default to yes, barns and sheds to no. */
  habitable?: boolean;
};

export type GeoJSONFeature = {
  type: "Feature";
  geometry: {
    type: "Point" | "LineString" | "Polygon";
    coordinates: number[] | number[][] | number[][][];
  };
  properties: Record<string, unknown>;
};

export type DamGeometry = {
  reservoirPoint?: GeoJSONFeature;
  damLine?: GeoJSONFeature;
  breachPoint?: GeoJSONFeature;
  aoi?: GeoJSONFeature;
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
  geometry?: DamGeometry;
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
      /** ACER TM 11 zone (structures only). */
      zone: "low" | "judgment" | "high" | null;
      /** ND class this receptor supports, or null if it does not count. */
      ndClass: "High" | "Medium" | "Low" | null;
      rankReason: string;
      /** The class call needs an engineer (judgment zone, unknown AADT, rail service). */
      needsJudgment: boolean;
    }[];
  };
  screen: {
    level: ScreenLevel;
    /** Worst ACER TM 11 zone over habitable inundated structures, or null if none is wet. */
    acerLevel: "low" | "judgment" | "high" | null;
    reasons: string[];
    /** Items an engineer must decide (unknown AADT, judgment zone, unverified structures). */
    judgment: string[];
  };
};
