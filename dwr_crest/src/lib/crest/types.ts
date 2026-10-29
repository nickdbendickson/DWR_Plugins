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
  /** Distance as a fraction of the base valley length (0 at the dam). Values above 1 lie further
   * downstream; anything past the desk reach is reported unresolved. Ignored when distanceKm is set. */
  station: number;
  /** Distance downstream of the dam along the valley, km. Overrides station. */
  distanceKm?: number;
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
  /** Dam location (decimal degrees). Used for the VERTCON NGVD29 offset; the drawn breach point is the fallback. */
  lat?: number;
  lon?: number;
  /** NGVD29 to NAVD88 offset (ft) entered by the user; overrides VERTCON. 0 or unset = use VERTCON. */
  datumOffsetFt?: number;
  /** Where the stated elevations came from, e.g. a datum conversion already applied. Shown with the datum rule. */
  datumNote?: string;
  /** Class of record, if one exists (e.g. a DWR benchmark). The desk screen is shown against it, never as it. */
  classOfRecord?: string;
  /** A note shown with the screen (sample records). */
  sampleNote?: string;
  /** LISFLOOD-FP export settings. Default: 10 m cells, point = breach location in UTM 14N. */
  lisflood?: { cellSizeM?: number; pointX?: number; pointY?: number };
  /** Reservoir surface area at the top of dam (acres). Sets the storage-depth curve for routing. */
  surfaceAreaAc?: number;
  /** Desk reach length (km). Default: until the schematic depth falls below 0.1 ft. */
  reachKm?: number;
  receptors: ReceptorSeed[];
  geometry?: DamGeometry;
  /** ACER TM 11 judgment zone counts as probable loss of life (High), as damsuite classify_nd defaults. Default true. */
  judgmentAsProbable?: boolean;
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
    /** Head on the breach at failure: pool (top of dam) minus breach bottom. */
    headFt: number;
    bottomWidthFt: number;
    sideSlope: number;
    /** Froehlich (1995) regression peak on the head (a check). */
    empiricalPeakCfs: number;
    /** Peak of the routed weir hydrograph. */
    peakCfs: number;
    timeToPeakHr: number;
    durationHr: number;
    storageExponent: number;
    /** Released volume (ac-ft). */
    volumeAcFt: number;
    series: SeriesPoint[];
    notes: string[];
  };
  flood: {
    engineLabel: string;
    maxDepthFt: number;
    maxVelocityFps: number;
    acres: number;
    /** Desk reach length (m). */
    lengthM: number;
    /** What ended the reach: the 0.1 ft wet limit, the user's reach, or the drawn area (AOI). */
    reachSource: "user" | "wet" | "aoi";
    /** Drawn area limit, or null when no AOI is drawn (behaviour then is unchanged). */
    aoi: {
      /** False when an AOI is drawn but cannot be placed (no breach location). */
      applied: boolean;
      /** Straight-line distance band from the breach the AOI covers (km). */
      nearKm: number;
      farKm: number;
      /** AOI polygon area (acres). */
      areaAc: number;
      /** True when the AOI, not the wet limit or user reach, ended the reach. */
      limitsReach: boolean;
      /** Schematic depth at the reach end (ft); 0.1 ft or more with limitsReach means wet at the edge. */
      edgeDepthFt: number;
      wetAtEdge: boolean;
      /** "Limited by drawn area: X km / Y ac", or why the AOI was not applied. */
      label: string;
    } | null;
    /** Flood-step review flags (also listed in Things to check). */
    warnings: string[];
    velocityBasis: string;
    stations: { s: number; xKm: number; halfM: number; depthFt: number }[];
    receptors: {
      id: string;
      name: string;
      kind: ReceptorKind;
      /** Fraction of the desk reach (1 = end; unresolved receptors sit at 1). */
      station: number;
      distanceKm: number;
      lateral: number;
      depthFt: number;
      velocityFps: number;
      arrivalHr: number | null;
      flooded: boolean;
      /** Past the desk reach or outside the drawn area: no depth computed, not screened. */
      unresolved: boolean;
      /** Unresolved because it lies outside the drawn area (AOI). */
      outsideAoi: boolean;
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
    /** Level as shown: "High (flagged: judgment zone)" when only judgment-zone structures make it High. */
    label: string;
    /** Whether the judgment zone counted as probable loss of life (damsuite JUDGMENT toggle). */
    judgmentAsProbable: boolean;
    /** The record's class of record, if any (the desk screen is not it). */
    classOfRecord: string | null;
    /** Worst ACER TM 11 zone over habitable inundated structures, or null if none is wet. */
    acerLevel: "low" | "judgment" | "high" | null;
    reasons: string[];
    /** Items an engineer must decide (unknown AADT, judgment zone, unverified structures). */
    judgment: string[];
  };
};
