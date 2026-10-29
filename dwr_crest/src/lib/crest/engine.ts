import { rankReceptor } from "./acer.ts";
import { ACFT, ACRE, CFS, FT, froehlich1995Peak, froehlich2008, routedHydrograph, thin } from "./breach.ts";
import type { Analysis, BreachMethod, DamRecord, EngineName, FailureMode, ReceptorSeed, ScreenLevel } from "./types.ts";

const TIE_BAND = 100;

export const STEPS = [
  { id: "record", label: "Record" },
  { id: "breach", label: "Breach" },
  { id: "flood", label: "Flood" },
  { id: "screen", label: "Screen" },
  { id: "report", label: "Report" },
] as const;

function clamp(n: number, lo: number, hi: number) {
  return Math.min(hi, Math.max(lo, n));
}

function finite(n: number, fallback: number) {
  return Number.isFinite(n) ? n : fallback;
}

export function isLocalElev(ft: number) {
  return Number.isFinite(ft) && ft >= 0 && ft <= 300;
}

function fmtFt(n: number) {
  return n.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}

type Tied = { navd: number | null; warning: string | null };

function tieValue(
  label: string,
  stated: number,
  offset: number | null,
  crestNavd: number,
  localRecord: boolean,
): Tied {
  if (!Number.isFinite(stated)) return { navd: null, warning: null };
  if (localRecord && offset != null) {
    const navd = stated + offset;
    if (Math.abs(navd - crestNavd) > TIE_BAND) {
      const crestField = label === "Top of dam";
      if (!crestField) {
        return {
          navd: null,
          warning: `${label} ${fmtFt(stated)} ft ties to ${fmtFt(navd)} ft, ${fmtFt(Math.abs(navd - crestNavd))} ft from the DEM crest. Left out (100 ft guard).`,
        };
      }
    }
    return { navd, warning: null };
  }
  if (!localRecord && isLocalElev(stated)) {
    return {
      navd: null,
      warning: `${label} ${fmtFt(stated)} ft is in the 0–300 ft band on a real-datum record. Not used as given.`,
    };
  }
  if (!localRecord && offset != null) {
    return { navd: stated + offset, warning: null };
  }
  return { navd: stated, warning: null };
}

/** Embankment cross-section at the breach (ft²), trapezoid (Runner core/breach.py embankment_area_ft2). */
function embankmentAreaFt2(heightFt: number, crestWidthFt: number, usH: number, dsH: number) {
  return heightFt * (crestWidthFt + 0.5 * (usH + dsH) * heightFt);
}

/**
 * MacDonald & Langridge-Monopolis (1984), earthfill, as Runner core/breach.py mlm_1984: SI only.
 * Ver = 0.0261 (Vw·hw)^0.769 in m³ (Vw m³, hw m); tf = 0.0179 Ver^0.364 hours; average width =
 * Ver / embankment cross-section (m²); side slope 1H:1V. The old desk form fed ac-ft·ft to the SI
 * regression and read the result as ac-ft (about 13× the eroded volume on DWR-2801).
 */
function macdonald(volumeM3: number, hwM: number, heightFt: number, lengthFt: number) {
  const erodedM3 = 0.0261 * (volumeM3 * Math.max(hwM, 0.01)) ** 0.769;
  const timeHr = 0.0179 * erodedM3 ** 0.364;
  const areaM2 = embankmentAreaFt2(heightFt, 12, 3, 3) * FT * FT;
  let widthFt = erodedM3 / Math.max(areaM2, 1e-6) / FT;
  const capped = widthFt > lengthFt;
  widthFt = Math.min(widthFt, lengthFt);
  return { widthFt, timeHr, erodedAcFt: erodedM3 / ACFT, capped };
}

function tr60(heightFt: number, lengthFt: number, mode: FailureMode) {
  const mult = mode === "overtopping" ? 3 : 2;
  let widthFt = mult * heightFt;
  const capped = widthFt > lengthFt;
  widthFt = Math.min(widthFt, lengthFt);
  const timeHr = clamp(heightFt / 90, 0.15, 3);
  return { widthFt, timeHr, capped };
}

/** Decay rates of the schematic valley. These are desk curves, not runs of any 2D model. */
const ENGINE_DECAY: Record<EngineName, { k: number; label: string }> = {
  lisflood: { k: 1.85, label: "Slow decay (schematic valley, not a LISFLOOD-FP run)" },
  anuga: { k: 2.25, label: "Medium decay (schematic valley, not an ANUGA run)" },
  routing: { k: 3.15, label: "Fast decay (schematic valley, not a routing model)" },
};

export const VELOCITY_BASIS =
  "Estimate: continuity on the schematic section, V = Q(x) / (valley width × depth), with Q(x) the routed peak decayed like the depth; limited to 0.1–14 ft/s. Not a hydraulic model velocity.";

/** Longest desk reach (km). Structures past the reach are reported unresolved. */
export const MAX_REACH_KM = 100;

const METHOD_LABEL: Record<BreachMethod, string> = {
  froehlich: "Froehlich 2008",
  macdonald: "MacDonald & Langridge-Monopolis",
  tr60: "TR-60 screening width",
};

export function analyze(dam: DamRecord): Analysis {
  const heightFt = clamp(finite(dam.heightFt, 10), 4, 200);
  const storage = clamp(finite(dam.storageAcFt, 10), 1, 500000);
  const lengthFt = clamp(finite(dam.lengthFt, 100), 40, 20000);
  const heightM = heightFt * FT;
  const volumeM3 = storage * ACFT;

  const localCrest = isLocalElev(dam.crestFt);
  const warnings: string[] = [];
  let offsetFt: number | null = null;
  let crestNavd = finite(dam.crestFt, 1000);
  let rule = "Stated elevations used as NAVD88.";

  if (localCrest) {
    const dem = finite(dam.demCrestFt, 1000);
    if (dem < 500 || dem > 3600) {
      rule = "0–300 ft crest, but the DEM crest is outside 500–3,600 ft. Flagged for review.";
      warnings.push("No DEM tie. Crest left unconverted.");
      crestNavd = dem;
    } else {
      offsetFt = dem - dam.crestFt;
      crestNavd = dem;
      rule = `0–300 ft is local. Offset ${fmtFt(offsetFt)} ft from the DEM crest. Datum choice is not applied.`;
    }
  } else if (dam.statedDatum === "NGVD29") {
    offsetFt = 1;
    crestNavd = dam.crestFt + 1;
    rule = "NGVD29 desk shift +1.0 ft toward NAVD88. Not the VERTCON grid.";
  } else if (dam.statedDatum === "Local" && !localCrest) {
    rule = "Marked Local, but the crest is outside 0–300 ft. Treated as NAVD88 and flagged.";
    warnings.push("Stated datum Local does not match a 0–300 ft crest.");
    crestNavd = dam.crestFt;
  } else {
    crestNavd = dam.crestFt;
    rule = "Elevations treated as NAVD88.";
  }

  const localRecord = localCrest && offsetFt != null;
  const bed = tieValue("Streambed", dam.streambedFt, offsetFt, crestNavd, localRecord);
  if (!localRecord && isLocalElev(dam.streambedFt)) {
    const toe = crestNavd - heightFt;
    bed.navd = toe;
    bed.warning = `Streambed was in the 0–300 ft band. Replaced with the desk toe, ${fmtFt(toe)} ft NAVD88 (crest minus height).`;
  }
  const spill = tieValue("Spillway", dam.spillwayFt, offsetFt, crestNavd, localRecord);
  const pool = tieValue("Normal pool", dam.normalPoolFt, offsetFt, crestNavd, localRecord);
  for (const w of [bed.warning, spill.warning, pool.warning]) {
    if (w) warnings.push(w);
  }

  // Breach geometry (DamSafety_Runner core/breach.py _common): the reservoir bottom is the top of
  // dam minus the dam height; the breach bottom is the tied streambed when it lies between them.
  const topM = crestNavd * FT;
  const resBottomM = topM - heightM;
  let breachBottomM = resBottomM;
  if (bed.navd != null && bed.navd * FT > resBottomM && bed.navd * FT < topM - FT) {
    breachBottomM = bed.navd * FT;
  }
  const wse0M = topM; // ND 5.1.1 sunny day: pool at the top of dam
  const headM = wse0M - breachBottomM;
  const hbM = topM - breachBottomM;
  const notes: string[] = [];
  const mode = dam.failureMode === "overtopping" ? "overtopping" : "piping";
  const fr = froehlich2008(volumeM3, hbM, mode);
  let widthFt = fr.bAvg / FT;
  let timeHr = fr.tf / 3600;
  let z: number = fr.z;
  let capped = false;

  if (dam.method === "macdonald") {
    const m = macdonald(volumeM3, headM, heightFt, lengthFt);
    widthFt = m.widthFt;
    timeHr = m.timeHr;
    capped = m.capped;
    z = 1;
    notes.push(
      `Eroded embankment about ${m.erodedAcFt.toFixed(1)} ac-ft, MacDonald & Langridge-Monopolis (1984) earthfill in SI (Ver = 0.0261 (Vw·hw)^0.769 m³, tf = 0.0179 Ver^0.364 h), as the Runner. Width = Ver over an assumed embankment section (12 ft crest, 3H:1V slopes; enter the real geometry in the Runner). Side slope taken as 1H:1V.`,
    );
  } else if (dam.method === "tr60") {
    const t = tr60(heightFt, lengthFt, dam.failureMode);
    widthFt = t.widthFt;
    timeHr = t.timeHr;
    capped = t.capped;
    z = 0;
    notes.push("TR-60 width is a screening multiple of height (rectangular opening), routed through the same weir.");
  } else {
    notes.push(`Froehlich (2008) ${mode}: average width, formation time and side slope ${z}H:1V in SI, shown in feet.`);
  }

  if (widthFt > lengthFt) {
    widthFt = lengthFt;
    capped = true;
  }
  if (capped) notes.push("Breach width was capped at the dam length.");

  const sa = finite(dam.surfaceAreaAc ?? NaN, NaN);
  const routed = routedHydrograph({
    topM,
    resBottomM,
    breachBottomM,
    wse0M,
    storageTopM3: volumeM3,
    surfaceAreaTopM2: sa > 0 ? sa * ACRE : null,
    bAvgM: widthFt * FT,
    tfS: Math.max(timeHr, 0.05) * 3600,
    z,
  });
  timeHr = routed.tfS / 3600;
  const peakCfs = routed.peakCms / CFS;
  const empiricalPeakCfs = froehlich1995Peak(routed.storageAtFailureM3, headM) / CFS;
  const thinned = thin(routed.t, routed.q);
  const series = thinned.t.map((t, i) => ({ tHr: t / 3600, qCfs: thinned.q[i] / CFS }));
  notes.push(
    "Outflow is routed through the opening (Runner core/breach.py): a trapezoidal weir, Q = 3.1·b·h^1.5 + 2.45·z·h^2.5 (US units), whose crest lowers from the top of dam to the breach bottom and whose bottom width grows linearly over the formation time, with the pool drawing down by level-pool routing until it is drained.",
  );
  notes.push(
    `Head on the breach ${fmtFt(headM / FT)} ft (top of dam to breach bottom). The Froehlich (1995) peak check uses that head, not the dam height.`,
  );
  notes.push(...routed.notes);

  // Schematic valley. L0 sets the decay scale; the reach runs until the desk depth falls below
  // the 0.1 ft wet threshold (or the reach the user sets), up to MAX_REACH_KM.
  const decay = ENGINE_DECAY[dam.engine] ?? ENGINE_DECAY.lisflood;
  const maxDepthFt = clamp(heightFt * 0.42 * (peakCfs / 8000) ** 0.18, 0.4, heightFt * 0.9);
  const baseM = 900 + Math.sqrt(storage) * 80;
  const wetReachM = (baseM * Math.log(Math.max(maxDepthFt / 0.1, 1.0001))) / decay.k;
  const userReachKm = finite(dam.reachKm ?? NaN, NaN);
  const reachSource = userReachKm > 0 ? "user" : "wet";
  const lengthM = clamp(userReachKm > 0 ? userReachKm * 1000 : wetReachM, 500, MAX_REACH_KM * 1000);
  const depthAt = (xM: number) => maxDepthFt * Math.exp((-decay.k * xM) / baseM);
  const halfAt = (xM: number) => 28 + 340 * (xM / baseM) ** 0.72;
  const NST = 81;
  const stations = Array.from({ length: NST }, (_, i) => {
    const s = i / (NST - 1);
    const xM = s * lengthM;
    return { s, xKm: xM / 1000, halfM: halfAt(xM), depthFt: depthAt(xM) };
  });

  let areaM2 = 0;
  for (let i = 1; i < stations.length; i++) {
    const a = stations[i - 1];
    const b = stations[i];
    if (a.depthFt > 0.1 || b.depthFt > 0.1) areaM2 += (b.s - a.s) * lengthM * (a.halfM + b.halfM);
  }
  const acres = areaM2 / ACRE;

  // The desk depth curve only resolves out to the 0.1 ft wet limit. A user reach past that limit
  // extends the drawn valley, but structures beyond the wet limit stay unresolved, not dry.
  const resolvedM = Math.min(lengthM, Math.max(wetReachM, 500));
  const receptors = dam.receptors.map((r) => {
    const xM =
      r.distanceKm != null && Number.isFinite(r.distanceKm) ? Math.max(0, r.distanceKm * 1000) : Math.max(0, r.station) * baseM;
    const unresolved = xM > resolvedM;
    const base = {
      id: r.id,
      name: r.name,
      kind: r.kind,
      station: Math.min(1, xM / lengthM),
      distanceKm: xM / 1000,
      lateral: r.lateral,
    };
    if (unresolved) {
      return {
        ...base,
        depthFt: 0,
        velocityFps: 0,
        arrivalHr: null,
        flooded: false,
        unresolved: true,
        zone: null,
        ndClass: null,
        rankReason:
          xM > lengthM
            ? `unresolved: ${(xM / 1000).toFixed(1)} km is past the ${(lengthM / 1000).toFixed(1)} km desk reach; not screened`
            : `unresolved: ${(xM / 1000).toFixed(1)} km is past the desk curve's 0.1 ft wet limit (${(resolvedM / 1000).toFixed(1)} km), inside the ${(lengthM / 1000).toFixed(1)} km user reach; the desk cannot call it dry`,
        needsJudgment: true,
      };
    }
    const depth = Math.max(0, depthAt(xM) - r.padFt);
    const flooded = depth >= 0.1;
    const widthFt = halfAt(xM) * 2 * 3.28084;
    const qHere = peakCfs * Math.exp((-decay.k * xM) / baseM);
    const velocity = flooded && depth > 0.05 ? clamp(qHere / (widthFt * depth), 0.1, 14) : 0;
    const arrival = flooded ? timeHr * 0.85 + xM / Math.max(velocity * FT, 0.3) / 3600 : null;
    const depthFt = flooded ? depth : 0;
    const velocityFps = flooded ? velocity : 0;
    const rank = rankReceptor({
      kind: r.kind,
      depthFt,
      velocityFps,
      aadt: r.aadt,
      roadClass: r.roadClass,
      habitable: r.habitable,
      judgmentAsProbable: dam.judgmentAsProbable ?? true,
    });
    return {
      ...base,
      depthFt,
      velocityFps,
      arrivalHr: arrival,
      flooded,
      unresolved: false,
      zone: rank.zone,
      ndClass: rank.class,
      rankReason: rank.reason,
      needsJudgment: rank.judgment,
    };
  });

  const maxVelocityFps = receptors.reduce((m, r) => Math.max(m, r.velocityFps), 0);
  const screen = screenReceptors(receptors, dam.receptors, {
    judgmentAsProbable: dam.judgmentAsProbable ?? true,
  });
  if (warnings.some((w) => w.startsWith("No DEM tie"))) {
    screen.level = "Review";
    screen.label = "Review (crest not tied)";
    screen.reasons.unshift("The crest could not be tied. Screening waits on a usable DEM crest.");
  }

  return {
    datum: {
      rule,
      offsetFt,
      crestNavdFt: crestNavd,
      streambedNavdFt: bed.navd,
      spillwayNavdFt: spill.navd,
      poolNavdFt: pool.navd,
      warnings,
    },
    breach: {
      label: METHOD_LABEL[dam.method],
      widthFt,
      timeHr,
      capped,
      headFt: headM / FT,
      bottomWidthFt: routed.bottomWidthM / FT,
      sideSlope: z,
      empiricalPeakCfs,
      peakCfs,
      timeToPeakHr: routed.timeToPeakS / 3600,
      durationHr: routed.t[routed.t.length - 1] / 3600,
      storageExponent: routed.storageExponent,
      volumeAcFt: routed.releasedM3 / ACFT,
      series,
      notes,
    },
    flood: {
      engineLabel: decay.label,
      maxDepthFt,
      maxVelocityFps,
      acres,
      lengthM,
      reachSource,
      velocityBasis: VELOCITY_BASIS,
      stations,
      receptors,
    },
    screen,
  };
}

type RankedReceptor = Analysis["flood"]["receptors"][number];

const CLASS_ORDER: Record<"High" | "Medium" | "Low", number> = { High: 3, Medium: 2, Low: 1 };
const ZONE_ORDER = { low: 1, judgment: 2, high: 3 } as const;

/** ND class from ranked receptors (damsuite classify_nd): the worst receptor class governs. */
export function screenReceptors(
  receptors: (Pick<RankedReceptor, "name" | "kind" | "depthFt" | "velocityFps" | "zone" | "ndClass" | "rankReason" | "needsJudgment"> & {
    unresolved?: boolean;
  })[],
  seeds: Pick<ReceptorSeed, "kind" | "habitable">[] = [],
  opts: { judgmentAsProbable?: boolean } = {},
): Analysis["screen"] {
  const judgmentAsProbable = opts.judgmentAsProbable ?? true;
  let level: ScreenLevel = "Low";
  let acerLevel: Analysis["screen"]["acerLevel"] = null;
  const reasons: string[] = [];
  const judgment: string[] = [];
  receptors.forEach((r, i) => {
    const habitable = seeds[i]?.habitable ?? (r.kind === "house" || r.kind === "mobile");
    if (habitable && r.zone && r.ndClass) {
      if (acerLevel == null || ZONE_ORDER[r.zone] > ZONE_ORDER[acerLevel]) acerLevel = r.zone;
    }
    if (r.unresolved) {
      reasons.push(`${r.name}: ${r.rankReason}. The class could be higher than shown.`);
    } else if (r.ndClass) {
      reasons.push(`${r.name}: ${r.ndClass} (${r.rankReason}; ${r.depthFt.toFixed(2)} ft at ${r.velocityFps.toFixed(2)} ft/s).`);
      if (level === "Low" || CLASS_ORDER[r.ndClass] > CLASS_ORDER[level as "High" | "Medium" | "Low"]) level = r.ndClass;
    } else if (r.depthFt > 0 && r.rankReason !== "dry") {
      reasons.push(`${r.name}: not counted (${r.rankReason}).`);
    }
    if (r.needsJudgment) {
      judgment.push(`${r.name}: ${r.rankReason}.`);
    }
  });
  if (reasons.length === 0) reasons.push("No seeded receptor is wet at 0.1 ft or more.");
  const unresolved = receptors.filter((r) => r.unresolved).length;
  if (unresolved > 0) {
    reasons.push(
      `${unresolved} receptor${unresolved === 1 ? " is" : "s are"} past the desk reach and unresolved. Extend the reach or model the valley before relying on this class.`,
    );
  }
  reasons.push("Velocities are desk estimates (continuity on the schematic section), so TM 11 zones near a curve are approximate.");
  // A High that only judgment-zone structures produce is labelled as such: it is the 5.3(f) default, not a TM 11 high-zone finding.
  const highDrivers = receptors.filter((r) => r.ndClass === "High");
  const judgmentDriven = (level as ScreenLevel) === "High" && highDrivers.length > 0 && highDrivers.every((r) => r.zone === "judgment");
  let label: string = judgmentDriven ? "High (judgment zone counted)" : level;
  if (judgmentDriven) {
    reasons.unshift(
      "High comes only from the ACER TM 11 judgment zone, counted as probable loss of life (damsuite default, 5.3(f)). An engineer may call it Medium; switch \"Judgment zone\" to test that.",
    );
  }
  // Nick (2026-10-29): a home or road past the modeled reach forces Review. It is never Low and never dropped.
  // A resolved High stays High (it cannot go higher) but the label says what is unresolved.
  const unresolvedCounting = receptors.filter((r, i) => {
    if (!r.unresolved) return false;
    const habitable = seeds[i]?.habitable ?? (r.kind === "house" || r.kind === "mobile");
    return habitable || r.kind === "road";
  }).length;
  if (unresolvedCounting > 0) {
    const n = `${unresolvedCounting} unresolved`;
    const what = unresolvedCounting === 1 ? "home or road" : "homes or roads";
    if ((level as ScreenLevel) === "High") {
      label = `${label} (${n} past the reach)`;
    } else {
      const resolvedAs = level;
      level = "Review";
      label = `Review (${resolvedAs} on resolved receptors; ${n})`;
      reasons.unshift(
        `Held at Review: resolved receptors screen ${resolvedAs}, but ${n} ${what} past the modeled reach could raise it. Extend the reach or model the valley.`,
      );
    }
  }
  reasons.push(
    `ND rules as in damsuite classify_nd: a habitable structure in the TM 11 high zone is High; in the judgment zone it is ${
      judgmentAsProbable ? "counted as probable loss of life (High, flagged)" : "not counted as probable loss of life (Medium, flagged)"
    }; any inundated habitable structure is at least Medium (economic, 5.2.2); roads by AADT and class. Barns and sheds are not habitable and do not count.`,
  );
  reasons.push("This is a desk screen on a schematic valley. It is not an ND Dam Safety classification of record.");
  return { level, label, judgmentAsProbable, acerLevel, reasons, judgment };
}
