import type { OutlineCache } from "./outline.ts";
import type { Nd911Cache } from "./nd911.ts";
import type { PlssCache } from "./plss.ts";
import type { Warning } from "./warnings.ts";
export type DatumName = "Local" | "NAVD88" | "NGVD29";
export type FailureMode = "overtopping" | "piping";
export type BreachMethod = "froehlich" | "macdonald" | "tr60";
export type EngineName = "lisflood" | "anuga" | "routing";
export type ReceptorKind = "house" | "mobile" | "barn" | "road";
export type StepId = "setup" | "record" | "breach" | "flood" | "results" | "dashboard" | "screen" | "report";
/** ND Dam Safety hazard classes (REG_05.2026 5.2), plus Review when the desk cannot screen. */
export type ScreenLevel = "High" | "Medium" | "Low" | "Review";
export type RoadClass = "interstate" | "us_state" | "other" | "private";

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
  /**
   * Mapped location (WGS 84). When set, model rasters are sampled here; otherwise at the place the desk's straight
   * valley puts the seed (station / distanceKm along the downstream direction, lateral across the half-width).
   */
  lon?: number;
  lat?: number;
  /** Where the seed came from when the desk made it: "nd911" = added from an ND NG911 address point for review. */
  source?: "nd911";
  /** The source's id (Site_NGUID for nd911). */
  sourceRef?: string;
  /**
   * How padFt was set for a 911 seed (nd911-pads.ts): "dem" = 3DEP ground - section bed, "dem-slope" = ground -
   * streambed at the dam along the valley slope, "estimated" = no terrain at the point, pad kept 0 (Review). Unset
   * on a 911 seed = not looked up yet.
   */
  padSource?: "dem" | "dem-slope" | "estimated";
  padNote?: string;
};

export type GeoJSONFeature = {
  type: "Feature";
  geometry: {
    type: "Point" | "LineString" | "Polygon" | "MultiPolygon";
    coordinates: number[] | number[][] | number[][][] | number[][][][];
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
  /** "" = not chosen yet (a record filled from the NID, which has no elevations or datum). */
  statedDatum: DatumName | "";
  demCrestFt: number;
  failureMode: FailureMode;
  method: BreachMethod;
  engine: EngineName;
  /**
   * Where the pool stands when the breach starts. Absent = "top" (top of dam, the ND sunny-day default). "normal" is
   * the record's normal pool; "elevation" is `startPoolFt`. A choice that cannot be used falls back to the top of dam
   * with a warning (engine.ts startingPool).
   */
  startPool?: StartPool;
  /** Starting pool elevation (ft, in the record's stated datum like the crest) when `startPool` is "elevation". */
  startPoolFt?: number;
  /**
   * Manning's n for the routed flood, when it differs from the built-in values (routing.ts DEFAULT_ROUGHNESS:
   * channel 0.035, floodplain 0.06). Absent, or a key absent = the built-in value.
   */
  roughness?: { nChannel?: number; nFloodplain?: number };
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
  /** PLSS sections looked up for the flood strip (BLM CadNSDI), kept so the list works offline afterwards. */
  plss?: PlssCache;
  /**
   * Source of record for the Screen, the PLSS list and the Report: an imported model run's summary and its values at
   * the receptors. Absent = the desk estimate (default). Never holds raster data, only these few numbers.
   */
  resultsOfRecord?: ResultsOfRecord;
  /**
   * "desk": the user chose the desk estimate over a loaded run on Results, so a Runner run is not adopted as the source
   * of record on its own (layer-of-record.ts). Absent = a clean Runner run becomes the record when it is imported.
   */
  sourceOfRecordChoice?: "desk";
  /** ND NG911 address points looked up around the flood strip (ND GIS Hub), kept for offline use. */
  nd911?: Nd911Cache;
  /** Terrain outline built on request from 3DEP terrain (outline.ts), with the input fingerprint it was built for. */
  outline?: OutlineCache;
  /** Old option: ACER TM 11 judgment zone counts as probable loss of life (High, 5.3). Default false: a judgment-zone
   *  home is Medium by rule (Nick, 2026-10-30), as damsuite classify_nd now defaults. */
  judgmentAsProbable?: boolean;
  /**
   * Where the hazard-screen structures come from. "nd911" (picked ND dams): ND NG911 address points inside the flood
   * strip, looked up and added automatically. Unset: the record's own seeds (samples and blank records keep the
   * generic demo seeds).
   */
  receptorSource?: "nd911";
  /** Set once the 911 points inside the strip were added automatically, so removals stick. */
  nd911AutoAdded?: boolean;
  /** Set when the record was filled from the NID (ND dam picker). Optional, so crest-desk-v2 state still loads. */
  nidId?: string;
  /** The NID's hazard class (FEMA) as reported to the NID. Shown on Record and Screen; never used by the screen and
   * never the class of record. */
  nidHazard?: NidHazard;
  /** Where filled values came from, by DamRecord field (e.g. "heightFt"). An edit keeps the entry, so the desk can
   * show "edited (NID: x)" and offer a revert. */
  provenance?: Partial<Record<string, Provenance>>;
  /** Read-only NID reference values (not used by the engine). */
  nidRef?: NidRef;
};

/** Where the pool stands when the breach starts (DamRecord.startPool). */
export type StartPool = "top" | "normal" | "elevation";

export type NidHazard = "Low" | "Significant" | "High" | "Undetermined";
export type ProvenanceSource = "NID" | "DWR" | "DEM";
export type Provenance = {
  src: ProvenanceSource;
  /** Source field, e.g. "NID Height (Ft)" or "max_pool_e". */
  field: string;
  value: number | string | null;
  /** Date of the source data (NID download or DWR pull, YYYY-MM-DD). */
  asOf: string;
  /** Set when the user confirmed a suggested value (DWR elevations). */
  confirmed?: boolean;
  /** Short tag shown with the field, e.g. "NID Max Storage". */
  note?: string;
  /** DWR elevations: the value and datum as DWR states them, and the VERTCON shift applied before the build. */
  statedFt?: number;
  statedDatum?: string;
  vshiftFt?: number;
};
export type NidRef = {
  nidHeightFt: number | null;
  damHeightFt: number | null;
  structuralHeightFt: number | null;
  hydraulicHeightFt: number | null;
  nidStorageAcFt: number | null;
  maxStorageAcFt: number | null;
  normalStorageAcFt: number | null;
  surfaceAreaNormalAc: number | null;
  river: string | null;
  city: string | null;
  purpose: string | null;
  damType: string | null;
  nidUpdated: string | null;
  lastInspection: string | null;
  eap: string | null;
  countyNote: string | null;
  /** NID download date (YYYY-MM-DD). */
  downloaded: string;
  attribution: string;
  /** Set when the dam joined the DWR Dams layer. */
  dwrAttribution?: string;
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
    /** The datum review flags as text (kept for every reader that expects strings; the same list as `flags`). */
    warnings: string[];
    /** The same flags with their catalogue code and severity (warnings.ts). Optional only so hand-built test
     * analyses need not carry it; `datumFlags()` fills it in from the text. */
    flags?: Warning[];
  };
  breach: {
    label: string;
    widthFt: number;
    timeHr: number;
    capped: boolean;
    /** Head on the breach at failure: starting pool (top of dam unless the record says otherwise) minus breach bottom. */
    headFt: number;
    /** The starting pool the breach used ("top" also when a chosen pool could not be used), and its level. */
    startPool: StartPool;
    startPoolNavdFt: number;
    startPoolLabel: string;
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
      /** False when an AOI is drawn but cannot be used (no breach location, not a polygon, bad ring). */
      applied: boolean;
      /** Distance band along the downstream valley the AOI covers (km): where it starts and its nearest downstream exit. */
      nearKm: number;
      farKm: number;
      /** AOI area (acres), holes excluded. */
      areaAc: number;
      /** How the downstream direction was found, in plain words. */
      direction: string;
      directionBasis: "dam line" | "reservoir to breach" | "none";
      /** How the reach end was measured: along the downstream direction, or a fallback. */
      reachBasis: "downstream" | "nearest-boundary" | null;
      /** "clipped": the schematic strip clipped to the polygon; "band": cut by distance only (direction unknown). */
      areaBasis: "clipped" | "band" | null;
      /** Share of the AOI upstream of the breach (null when the direction is unknown). */
      upstreamFrac: number | null;
      /** Deepest schematic depth inside the AOI (ft); below the flood max when the breach is outside it. */
      maxDepthInsideFt: number;
      /** True when the AOI, not the wet limit or user reach, ended the reach. */
      limitsReach: boolean;
      /** Schematic depth at the downstream exit (ft); 0.1 ft or more with limitsReach means wet at the edge. */
      edgeDepthFt: number;
      wetAtEdge: boolean;
      /** "Limited by drawn area: X km / Y ac", or why the AOI was not applied. */
      label: string;
    } | null;
    /** Flood-step review flags (also listed in Things to check), as text. */
    warnings: string[];
    /** The same flags with their catalogue code and severity (warnings.ts); see datum.flags. */
    flags?: Warning[];
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
      /** Outside the drawn area but inside the un-clipped desk reach: how it would rank without the AOI. */
      wouldBe: { ndClass: "High" | "Medium" | "Low" | null; zone: "low" | "judgment" | "high" | null; depthFt: number; velocityFps: number } | null;
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

/** Model rasters Crest can import for comparison (results.ts). */
export type ResultSource = "lisflood" | "dsswise";

/** Full-resolution summary of one imported run (raster.ts scanRun), downstream of the dam line unless "All". */
export type RunStats = {
  /** Deepest cell downstream of the dam line (ft) and anywhere in the raster, reservoir included (ft). */
  peakDepthFt: number;
  peakDepthAllFt: number;
  /** Cells at or above 0.1 ft, as acres: downstream of the dam line, and the whole raster. */
  wetAc: number;
  wetAcAll: number;
  /** Farthest wet cell along the downstream direction from the breach (km, straight line, not along the channel). */
  farthestKm: number;
  /** Max velocity over wet downstream cells (ft/s); null without a velocity raster. */
  maxVelocityFps: number | null;
  /** Latest arrival over wet downstream cells (h); null without an arrival raster. */
  arrivalMaxHr: number | null;
  /** Distance from the record's breach location to the nearest wet cell (m). */
  nearestWetM: number;
  /** Wet cells on the raster's outer edge (the flood may run off the grid). */
  edgeWetCells: number;
  /** Where the downstream ray from the breach leaves the raster (km), or null when the breach is outside it. */
  coverKm: number | null;
  cellM: number;
  cells: number;
  /** Breach location these numbers were computed for. */
  breachLon: number;
  breachLat: number;
};

export type ReceptorSample = {
  id: string;
  /** Inside the raster's extent. */
  covered: boolean;
  depthFt: number;
  velocityFps: number | null;
  arrivalHr: number | null;
  lon: number;
  lat: number;
};

export type ResultsOfRecord = {
  source: ResultSource;
  /** e.g. "LISFLOOD-FP (DWR-2801, ND Dam Breach Runner 3.11.1)" */
  label: string;
  files: string[];
  importedAt: string;
  crs: string;
  stats: RunStats;
  samples: ReceptorSample[];
  warnings: string[];
};
