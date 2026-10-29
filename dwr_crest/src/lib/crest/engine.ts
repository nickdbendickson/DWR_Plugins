import type { Analysis, BreachMethod, DamRecord, EngineName, FailureMode } from "./types";

const FT = 0.3048;
const ACFT_TO_M3 = 1233.48184;
const CFS_PER_CMS = 35.3146667;
const G = 9.80665;
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

function froehlich(volumeM3: number, heightM: number, mode: FailureMode) {
  const ko = mode === "overtopping" ? 1.3 : 1.0;
  const widthM = 0.27 * ko * volumeM3 ** 0.32 * heightM ** 0.04;
  const timeS = 63.2 * Math.sqrt(volumeM3 / (G * heightM * heightM));
  const peakCms = 0.607 * volumeM3 ** 0.295 * heightM ** 1.24;
  return { widthM, timeS, peakCms };
}

function macdonald(storageAcFt: number, heightFt: number, lengthFt: number) {
  const erodedAcFt = 0.0261 * (storageAcFt * heightFt) ** 0.769;
  const throughFt = 16 + 3 * heightFt;
  let widthFt = (erodedAcFt * 43560) / Math.max(heightFt * throughFt, 1);
  const capped = widthFt > lengthFt;
  widthFt = Math.min(widthFt, lengthFt);
  const timeHr = clamp(0.0035 * heightFt * (storageAcFt / 200) ** 0.15, 0.15, 4);
  return { widthFt, timeHr, erodedAcFt, capped };
}

function tr60(heightFt: number, lengthFt: number, mode: FailureMode) {
  const mult = mode === "overtopping" ? 3 : 2;
  let widthFt = mult * heightFt;
  const capped = widthFt > lengthFt;
  widthFt = Math.min(widthFt, lengthFt);
  const timeHr = clamp(heightFt / 90, 0.15, 3);
  return { widthFt, timeHr, capped };
}

function hydrograph(peakCms: number, timeS: number, volumeM3: number) {
  const n = 56;
  const tEnd = timeS * 2.4;
  const shape: { t: number; q: number }[] = [];
  for (let i = 0; i <= n; i++) {
    const t = (i / n) * tEnd;
    let s: number;
    if (t <= timeS) {
      const p = 0.5 * (1 - Math.cos(Math.PI * (t / timeS)));
      s = Math.sin((Math.PI * p) / 2);
    } else {
      s = Math.exp(-(t - timeS) / (0.42 * timeS));
    }
    shape.push({ t, q: s });
  }
  let area = 0;
  for (let i = 1; i < shape.length; i++) {
    area += 0.5 * (shape[i].q + shape[i - 1].q) * (shape[i].t - shape[i - 1].t);
  }
  const scale = area > 0 ? volumeM3 / area : peakCms;
  const fittedCms = Math.max(...shape.map((p) => p.q)) * scale;
  return {
    empiricalPeakCfs: peakCms * CFS_PER_CMS,
    fittedPeakCfs: fittedCms * CFS_PER_CMS,
    series: shape.map((p) => ({
      tHr: p.t / 3600,
      qCfs: p.q * scale * CFS_PER_CMS,
    })),
  };
}

const ENGINE_DECAY: Record<EngineName, { k: number; label: string }> = {
  lisflood: { k: 1.85, label: "LISFLOOD-style spread" },
  anuga: { k: 2.25, label: "ANUGA-style mesh" },
  routing: { k: 3.15, label: "1D routing" },
};

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
  const volumeM3 = storage * ACFT_TO_M3;

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

  const fr = froehlich(volumeM3, heightM, dam.failureMode);
  let widthFt = fr.widthM / FT;
  let timeHr = fr.timeS / 3600;
  let capped = false;
  const notes: string[] = [];

  if (dam.method === "macdonald") {
    const m = macdonald(storage, heightFt, lengthFt);
    widthFt = m.widthFt;
    timeHr = m.timeHr;
    capped = m.capped;
    notes.push(
      `Eroded embankment about ${m.erodedAcFt.toFixed(0)} ac-ft (Wahl form of MacDonald, earthfill).`,
    );
  } else if (dam.method === "tr60") {
    const t = tr60(heightFt, lengthFt, dam.failureMode);
    widthFt = t.widthFt;
    timeHr = t.timeHr;
    capped = t.capped;
    notes.push("Average width is a screening multiple of height, not a fitted regression.");
  } else {
    notes.push("Width, time, and the reference peak are Froehlich 2008 / 1995 in SI, shown in feet and cfs.");
  }

  if (widthFt > lengthFt) {
    widthFt = lengthFt;
    capped = true;
  }
  if (capped) notes.push("Breach width was capped at the dam length.");

  const timeS = Math.max(timeHr, 0.05) * 3600;
  const hydro = hydrograph(fr.peakCms, timeS, volumeM3);
  notes.push(
    "The plotted hydrograph is sine-shaped, then scaled so its volume matches the stored water. The Froehlich peak is shown beside it and is not forced to that volume.",
  );

  const decay = ENGINE_DECAY[dam.engine] ?? ENGINE_DECAY.lisflood;
  const maxDepthFt = clamp((heightFt * 0.42 * (hydro.fittedPeakCfs / 8000) ** 0.18), 0.4, heightFt * 0.9);
  const lengthM = clamp(900 + Math.sqrt(storage) * 80, 1200, 6000);
  const stations = Array.from({ length: 41 }, (_, i) => {
    const s = i / 40;
    const halfM = 28 + 340 * s ** 0.72;
    const depthFt = maxDepthFt * Math.exp(-decay.k * s);
    return { s, halfM, depthFt };
  });

  const wetPairs: { a: (typeof stations)[number]; b: (typeof stations)[number] }[] = [];
  for (let i = 1; i < stations.length; i++) {
    const a = stations[i - 1];
    const b = stations[i];
    if (a.depthFt > 0.1 || b.depthFt > 0.1) wetPairs.push({ a, b });
  }
  let areaM2 = 0;
  for (const { a, b } of wetPairs) {
    const dx = ((b.s - a.s) * lengthM);
    areaM2 += dx * (a.halfM + b.halfM);
  }
  const acres = areaM2 / 4046.8564224;

  const receptors = dam.receptors.map((r) => {
    const s = clamp(r.station, 0, 1);
    const idx = Math.min(stations.length - 1, Math.round(s * (stations.length - 1)));
    const st = stations[idx];
    const depth = Math.max(0, st.depthFt - r.padFt);
    const flooded = depth >= 0.1;
    const widthFt = st.halfM * 2 * 3.28084;
    const qHere = hydro.fittedPeakCfs * Math.exp(-decay.k * s);
    const velocity = flooded && depth > 0.05 ? clamp(qHere / (widthFt * depth), 0.1, 14) : 0;
    const arrival = flooded ? (timeHr * 0.85 + s * (lengthM / Math.max(velocity * FT, 0.3)) / 3600) : null;
    return {
      id: r.id,
      name: r.name,
      kind: r.kind,
      station: s,
      lateral: r.lateral,
      depthFt: flooded ? depth : 0,
      velocityFps: flooded ? velocity : 0,
      arrivalHr: arrival,
      flooded,
    };
  });

  const maxVelocityFps = receptors.reduce((m, r) => Math.max(m, r.velocityFps), 0);
  const houses = receptors.filter((r) => r.kind === "house" || r.kind === "barn");
  const deepHouse = houses.some((r) => r.depthFt >= 2);
  const anyHouse = houses.some((r) => r.flooded);
  const wetRoad = receptors.some((r) => r.kind === "road" && r.depthFt >= 1);

  let level: Analysis["screen"]["level"] = "Low";
  const reasons: string[] = [];
  if (warnings.some((w) => w.startsWith("No DEM tie"))) {
    level = "Review";
    reasons.push("The crest could not be tied. Screening waits on a usable DEM crest.");
  } else if (deepHouse) {
    level = "High";
    reasons.push("A house or barn is at least 2 ft deep in this schematic.");
  } else if (anyHouse || wetRoad) {
    level = "Significant";
    reasons.push(
      anyHouse
        ? "A building is wet, but under 2 ft in this schematic."
        : "A road is at least 1 ft deep. No building is wet.",
    );
  } else {
    reasons.push("No seeded receptor is wet above 0.1 ft.");
  }
  reasons.push("This is a desk screen. It is not an ND Dam Safety or ACER classification.");

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
      empiricalPeakCfs: hydro.empiricalPeakCfs,
      fittedPeakCfs: hydro.fittedPeakCfs,
      volumeAcFt: storage,
      series: hydro.series,
      notes,
    },
    flood: {
      engineLabel: decay.label,
      maxDepthFt,
      maxVelocityFps,
      acres,
      lengthM,
      stations,
      receptors,
    },
    screen: { level, reasons },
  };
}
