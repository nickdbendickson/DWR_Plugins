/**
 * ND dam picker (County -> Dam) logic, pure and shared by the web desk and the desktop window.
 *
 * Data: two offline bundles, loaded on demand by dynamic import (no network; see nd-dams.test.ts):
 *   data/nd_dams.json      the NID's ND main dams (scripts/build-nd-dams.mjs, refreshed only on request)
 *   data/nd_dams_dwr.json  numeric DWR Dams-layer fields joined on NID ID, NGVD 29 already converted to NAVD 88 by
 *                          per-point VERTCON before the build, plus DWR Dugouts reference fields
 *                          (scripts/build-nd-dams-dwr.mjs)
 *
 * Rules (SPEC_crest_nd_dam_picker.md section 13, Nick's decisions 1-12):
 * - H = DWR max_dam_he where the dam joins ("from DWR", decimal ft), else NID Height (whole ft, rounded).
 * - V = NID Storage (= max(Max, Normal)); chip says which. Surface area is never filled.
 * - Elevations are never filled from the NID. DWR elevations are offered only as NAVD 88 (published, or converted
 *   from NGVD 29 by per-point VERTCON in the bundle; no conversion happens here) and only after the user confirms.
 *   Values the bundle holds (elev_qc: outside 700-3,600 ft) are never offered. Local or blank DWR datum: never
 *   offered as elevations; top - streambed is an H check only.
 * - DWR dugout records are shown for reference only; they never fill a field or change the screen.
 * - NID hazard is shown as reported (FEMA class), never used by the screen and never the class of record.
 */
import type { DamRecord, NidHazard, NidRef, Provenance } from "./types.ts";

export type NdDam = {
  nid: string;
  name: string | null;
  otherNames: string | null;
  county: string;
  countyNote: string | null;
  lat: number | null;
  lon: number | null;
  nidHeightFt: number | null;
  damHeightFt: number | null;
  structuralHeightFt: number | null;
  hydraulicHeightFt: number | null;
  nidStorageAcFt: number | null;
  maxStorageAcFt: number | null;
  normalStorageAcFt: number | null;
  surfaceAreaNormalAc: number | null;
  lengthFt: number | null;
  yearCompleted: number | null;
  drainageMi2: number | null;
  nidHazard: NidHazard | null;
  river: string | null;
  city: string | null;
  purpose: string | null;
  damType: string | null;
  nidUpdated: string | null;
  lastInspection: string | null;
  eap: string | null;
};

export type DwrDatum = "NGVD29" | "NAVD88" | "Local" | "Other";
export type DwrDam = {
  nid: string;
  dwrId: number | null;
  heightFt: number | null;
  topElevFt: number | null;
  streambedElevFt: number | null;
  datum: DwrDatum | null;
  auxSpillwayElevFt: number | null;
  normalPoolElevFt: number | null;
  maxPoolStorageAcFt: number | null;
  maxPoolAreaAc: number | null;
  normalStorageAcFt: number | null;
  normalAreaAc: number | null;
  lengthFt: number | null;
  topWidthFt: number | null;
  /** DWR county, only where it differs from the NID county. */
  county: string | null;
  /** NAVD 88 values from the bundle (converted from NGVD 29 by VERTCON, or published); null when not offered. */
  topNavdFt: number | null;
  streambedNavdFt: number | null;
  auxSpillwayNavdFt: number | null;
  normalPoolNavdFt: number | null;
  /** VERTCON shift applied before the build (NGVD 29), 0 for NAVD 88, null otherwise. Provenance only. */
  vshiftFt: number | null;
  /** Stated fields held by the QC (comma-separated DWR field names), never offered. */
  held: string | null;
};

/** DWR Dugouts-layer record for a main dam: reference only (units as DWR gives them). */
export type DwrDugout = {
  nid: string;
  dwrId: number | null;
  maxDepthFt: number | null;
  maxHeightFt: number | null;
  freeboardFt: number | null;
  maxSurfaceAreaAc: number | null;
  maxStorageAcFt: number | null;
};

type Table = { cols: string[]; rows: unknown[][] };
type Bundle = Table & { source: Record<string, unknown>; dugouts?: Table };
export type NidSource = { url: string; fetchedUtc: string; rows: number; attribution: string };
export type DwrSource = {
  formUrl: string;
  layers?: string;
  pulledUtc: string;
  matched: number;
  conversion?: string;
  disclaimer: string;
  attribution: string;
};
export type NdData = {
  dams: NdDam[];
  byNid: Map<string, NdDam>;
  dwr: Map<string, DwrDam>;
  dugouts: Map<string, DwrDugout>;
  nidSource: NidSource;
  dwrSource: DwrSource | null;
};

export function decode<T>(b: Table): T[] {
  return b.rows.map((r) => Object.fromEntries(b.cols.map((c, i) => [c, r[i] ?? null])) as T);
}

export function makeNdData(nid: Bundle, dwr: Bundle | null): NdData {
  const dams = decode<NdDam>(nid);
  const dw = dwr ? decode<DwrDam>(dwr) : [];
  const dg = dwr?.dugouts ? decode<DwrDugout>(dwr.dugouts) : [];
  return {
    dams,
    byNid: new Map(dams.map((d) => [d.nid, d])),
    dwr: new Map(dw.map((d) => [d.nid, d])),
    dugouts: new Map(dg.map((d) => [d.nid, d])),
    nidSource: nid.source as unknown as NidSource,
    dwrSource: dwr ? (dwr.source as unknown as DwrSource) : null,
  };
}

let cache: Promise<NdData> | null = null;
/** Load both bundles once (separate chunks; never part of the first paint). */
export function loadNdData(): Promise<NdData> {
  cache ??= Promise.all([
    import("./data/nd_dams.json", { with: { type: "json" } }),
    import("./data/nd_dams_dwr.json", { with: { type: "json" } }),
  ]).then(([n, d]) =>
    makeNdData(((n as { default?: Bundle }).default ?? n) as Bundle, ((d as { default?: Bundle }).default ?? d) as Bundle),
  );
  return cache;
}

// ---------------------------------------------------------------- search, labels, counties

/** Lower case, accents folded, punctuation -> space, collapsed. */
export function fold(s: string): string {
  return s
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

const fmtInt = (n: number) => Math.round(n).toLocaleString("en-US");

/** Display name: Dam Name, else Other Names, else "(unnamed)". */
export function displayName(d: NdDam): string {
  return d.name ?? d.otherNames ?? "(unnamed)";
}

/** "name · NID ID · NID hazard · H ft · V ac-ft"; an unnamed dam reads "(unnamed) · ND00971 · ...", never an owner name. */
export function damOptionLabel(d: NdDam, dwr?: DwrDam): string {
  const h = heightFor(d, dwr).value;
  const parts = [displayName(d), d.nid];
  parts.push(d.nidHazard ? `NID hazard ${d.nidHazard}` : "NID hazard n/a");
  parts.push(Number.isFinite(h) ? `${h} ft` : "H n/a");
  parts.push(d.nidStorageAcFt != null ? `${fmtInt(d.nidStorageAcFt)} ac-ft` : "V n/a");
  return parts.join(" · ");
}

/**
 * Case- and accent-insensitive match on name, other names and NID ID. Every query word must match; a number-only
 * query also matches the NID ID's digits ("199" -> ND00199, "nd00199"). county "" = all.
 */
export function searchDams(dams: NdDam[], query: string, county = ""): NdDam[] {
  const words = fold(query).split(" ").filter(Boolean);
  const list = county ? dams.filter((d) => d.county === county) : dams;
  if (!words.length) return list;
  return list.filter((d) => {
    const hay = fold(`${d.name ?? ""} ${d.otherNames ?? ""} ${d.nid}`);
    const digits = d.nid.replace(/^\D+0*/, "");
    return words.every((w) => hay.includes(w) || (/^\d+$/.test(w) && digits === w.replace(/^0+/, "")));
  });
}

export type CountyOption = { county: string; count: number; label: string };
/** County dropdown: "Cass (28)"; a non-ND NID county gets "(not an ND county, see note)". */
export function countyOptions(dams: NdDam[]): CountyOption[] {
  const m = new Map<string, { count: number; note: boolean }>();
  for (const d of dams) {
    const e = m.get(d.county) ?? { count: 0, note: false };
    e.count++;
    if (d.countyNote) e.note = true;
    m.set(d.county, e);
  }
  return [...m.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([county, { count, note }]) => ({
      county,
      count,
      label: `${county} (${count})${note ? " (not an ND county, see note)" : ""}`,
    }));
}

// ---------------------------------------------------------------- field rules

export type HeightPick = { value: number; src: "DWR" | "NID"; field: string; rounded: boolean };
/** Decision 11: DWR max_dam_he (decimal) where the dam joins, else NID Height (whole ft). NaN when neither. */
export function heightFor(d: NdDam, dwr?: DwrDam): HeightPick {
  if (dwr?.heightFt != null && dwr.heightFt > 0) return { value: dwr.heightFt, src: "DWR", field: "max_dam_he", rounded: false };
  if (d.nidHeightFt != null) return { value: d.nidHeightFt, src: "NID", field: "NID Height (Ft)", rounded: true };
  return { value: Number.NaN, src: "NID", field: "NID Height (Ft)", rounded: true };
}

export type StorageChip = { value: number; chip: string; warning: string | null; rangeNote: string | null };
/** Decision 2: V = NID Storage; chip says which value it is. */
export function storageFor(d: NdDam): StorageChip {
  const v = d.nidStorageAcFt ?? Number.NaN;
  const max = d.maxStorageAcFt;
  const normal = d.normalStorageAcFt;
  let chip = "NID Max Storage";
  let warning: string | null = null;
  if (max == null || (normal != null && max < normal)) {
    chip = "NID Normal Storage (no max reported)";
    warning =
      max == null
        ? "The NID reports no Max Storage for this dam, so V is the Normal Storage. Storage at the top of dam is likely larger; check the dam file."
        : `The NID's Max Storage (${fmtInt(max)} ac-ft) is below its Normal Storage, so V is the Normal Storage. Check the dam file.`;
  }
  const rangeNote =
    Number.isFinite(v) && v > 500_000
      ? "Over 500,000 ac-ft: well outside the range of the desk's empirical breach equations (fit to dams mostly under 50,000 ac-ft). Treat the result as indicative only."
      : null;
  return { value: v, chip, warning, rangeNote };
}

export const REQUIRED_FIELDS = ["heightFt", "storageAcFt", "lengthFt"] as const;
export const OPTIONAL_FIELDS = ["year", "drainageMi2"] as const;

/** Fields a filled record still needs before Next (required NID fields and the elevations). */
export function missingForNext(dam: DamRecord): string[] {
  const out: string[] = [];
  if (!Number.isFinite(dam.heightFt)) out.push("height");
  if (!Number.isFinite(dam.storageAcFt)) out.push("storage");
  if (!Number.isFinite(dam.lengthFt)) out.push("length");
  if (!Number.isFinite(dam.crestFt)) out.push("crest");
  if (!Number.isFinite(dam.streambedFt)) out.push("breach bottom");
  if (!dam.statedDatum) out.push("datum");
  return out;
}

export function nidRefFor(d: NdDam, src: NidSource, dwrSource?: DwrSource | null): NidRef {
  return {
    nidHeightFt: d.nidHeightFt,
    damHeightFt: d.damHeightFt,
    structuralHeightFt: d.structuralHeightFt,
    hydraulicHeightFt: d.hydraulicHeightFt,
    nidStorageAcFt: d.nidStorageAcFt,
    maxStorageAcFt: d.maxStorageAcFt,
    normalStorageAcFt: d.normalStorageAcFt,
    surfaceAreaNormalAc: d.surfaceAreaNormalAc,
    river: d.river,
    city: d.city,
    purpose: d.purpose,
    damType: d.damType,
    nidUpdated: d.nidUpdated,
    lastInspection: d.lastInspection,
    eap: d.eap,
    countyNote: d.countyNote,
    downloaded: src.fetchedUtc.slice(0, 10),
    attribution: src.attribution,
    ...(dwrSource ? { dwrAttribution: dwrSource.attribution } : {}),
  };
}

/** Labels for the confirm dialog, by DamRecord field. */
export const FIELD_LABEL: Record<string, string> = {
  name: "name",
  nid: "NID ID",
  county: "county",
  year: "year completed",
  lat: "latitude",
  lon: "longitude",
  heightFt: "dam height",
  lengthFt: "dam length",
  storageAcFt: "storage",
  drainageMi2: "drainage area",
  crestFt: "crest (cleared)",
  streambedFt: "breach bottom (cleared)",
  demCrestFt: "DEM crest (cleared)",
  statedDatum: "datum (cleared)",
  spillwayFt: "spillway crest (cleared)",
  normalPoolFt: "normal pool (cleared)",
  surfaceAreaAc: "surface area (cleared)",
  classOfRecord: "class of record (cleared)",
  geometry: "drawn dam, breach point and AOI (cleared)",
};

/**
 * Decision 6-8: the patch "Fill this record from the NID" applies, its provenance, and the replaced-field list.
 * Elevations are cleared (NaN, datum unset), never filled. When the record was a different dam, its drawn geometry,
 * class of record, notes and surface area are cleared too, so nothing from the previous dam carries over.
 */
export function prefillPatch(
  d: NdDam,
  data: Pick<NdData, "dwr" | "nidSource"> & { dwrSource?: DwrSource | null },
  current?: DamRecord,
): { patch: Partial<DamRecord>; replaced: string[] } {
  const asOf = data.nidSource.fetchedUtc.slice(0, 10);
  const dwr = data.dwr.get(d.nid);
  const h = heightFor(d, dwr);
  const v = storageFor(d);
  const nan = Number.NaN;
  const prov: Record<string, Provenance> = {};
  const nidP = (field: string, value: number | string | null, note?: string): Provenance => ({
    src: "NID",
    field,
    value,
    asOf,
    ...(note ? { note } : {}),
  });
  const name = d.name ?? d.otherNames ?? `Unnamed dam ${d.nid}`;
  prov.name = nidP(d.name ? "Dam Name" : d.otherNames ? "Other Names" : "Dam Name (blank)", name);
  prov.nid = nidP("NID ID", d.nid);
  prov.county = nidP("County", d.county);
  if (d.yearCompleted != null) prov.year = nidP("Year Completed", d.yearCompleted);
  if (d.lat != null) prov.lat = nidP("Latitude", d.lat);
  if (d.lon != null) prov.lon = nidP("Longitude", d.lon);
  if (Number.isFinite(h.value))
    prov.heightFt =
      h.src === "DWR"
        ? {
            src: "DWR",
            field: "max_dam_he",
            value: h.value,
            asOf: data.dwrSource?.pulledUtc.slice(0, 10) ?? asOf,
            note: "DWR dam height",
          }
        : nidP("NID Height (Ft)", h.value, "NID Height (whole ft)");
  if (d.lengthFt != null) prov.lengthFt = nidP("Dam Length (Ft)", d.lengthFt);
  if (Number.isFinite(v.value)) prov.storageAcFt = nidP("NID Storage (Acre-Ft)", v.value, v.chip);
  if (d.drainageMi2 != null) prov.drainageMi2 = nidP("Drainage Area (Sq Miles)", d.drainageMi2);

  const patch: Partial<DamRecord> = {
    name,
    nid: d.nid,
    county: d.county,
    year: d.yearCompleted ?? nan,
    lat: d.lat ?? undefined,
    lon: d.lon ?? undefined,
    heightFt: h.value,
    lengthFt: d.lengthFt ?? nan,
    storageAcFt: v.value,
    drainageMi2: d.drainageMi2 ?? nan,
    crestFt: nan,
    streambedFt: nan,
    demCrestFt: nan,
    statedDatum: "",
    datumOffsetFt: undefined,
    datumNote: undefined,
    spillwayFt: nan,
    normalPoolFt: nan,
    nidId: d.nid,
    nidHazard: d.nidHazard ?? undefined,
    provenance: prov,
    nidRef: nidRefFor(d, data.nidSource, dwr ? data.dwrSource : null),
  };
  const replaced = ["name", "nid", "county", "year", "lat", "lon", "heightFt", "lengthFt", "storageAcFt", "drainageMi2"];
  replaced.push("crestFt", "streambedFt", "demCrestFt", "statedDatum", "spillwayFt", "normalPoolFt");
  const otherDam = !current || (current.nidId ?? current.nid) !== d.nid;
  if (otherDam) {
    Object.assign(patch, {
      surfaceAreaAc: undefined,
      classOfRecord: undefined,
      sampleNote: undefined,
      geometry: undefined,
      reachKm: undefined,
      judgmentAsProbable: undefined,
    } as Partial<DamRecord>);
    if (current?.surfaceAreaAc != null) replaced.push("surfaceAreaAc");
    if (current?.classOfRecord) replaced.push("classOfRecord");
    if (current?.geometry) replaced.push("geometry");
  }
  return { patch, replaced };
}

/** The confirm dialog's text. */
export function confirmText(replaced: string[]): string {
  return `Fill this record from the NID? These fields will be replaced: ${replaced.map((f) => FIELD_LABEL[f] ?? f).join(", ")}.`;
}

// ---------------------------------------------------------------- provenance tags

export type FieldTag = { kind: "from" | "edited" | "missing" | "missing-optional"; text: string; original?: Provenance };
/** Tag for a field of a picked record: "from NID", "edited (NID: 28)", "not in NID: enter a value". */
export function fieldTag(dam: DamRecord, field: string): FieldTag | null {
  if (!dam.nidId) return null;
  const p = dam.provenance?.[field];
  const cur = (dam as unknown as Record<string, unknown>)[field];
  if (!p) {
    if ((REQUIRED_FIELDS as readonly string[]).includes(field) && !(typeof cur === "number" && Number.isFinite(cur)))
      return { kind: "missing", text: "not in NID: enter a value" };
    if ((OPTIONAL_FIELDS as readonly string[]).includes(field) && !(typeof cur === "number" && Number.isFinite(cur)))
      return { kind: "missing-optional", text: "not in NID" };
    return null;
  }
  const same = typeof p.value === "number" && typeof cur === "number" ? Math.abs(p.value - cur) < 1e-9 : p.value === cur;
  if (same) return { kind: "from", text: p.note ? `from ${p.src}: ${p.note}` : `from ${p.src}`, original: p };
  return { kind: "edited", text: `edited (${p.src}: ${p.value ?? "blank"})`, original: p };
}

/** Patch that restores a field to its source value. */
export function revertPatch(dam: DamRecord, field: string): Partial<DamRecord> {
  const p = dam.provenance?.[field];
  if (!p) return {};
  return { [field]: p.value ?? Number.NaN } as Partial<DamRecord>;
}

/** "Unlink NID": keep the values, drop the link, tags and NID reference. */
export function unlinkPatch(): Partial<DamRecord> {
  return { nidId: undefined, nidHazard: undefined, provenance: undefined, nidRef: undefined };
}

// ---------------------------------------------------------------- height warnings

export type HeightWarning = { id: "W-H1" | "W-H2"; text: string; useHeightFt?: number };

/**
 * W-H1, when the dam is picked. Unjoined dams (H = NID Height): NID Height above the NID Dam Height by more than
 * 0.5 ft -> offer "Use dam height Y". Joined dams (H = DWR max_dam_he): NID Height more than 1 ft above the DWR
 * height -> informational (decision 11), H already uses the DWR value.
 */
export function heightWarningOnPick(d: NdDam, dwr?: DwrDam): HeightWarning | null {
  const h = heightFor(d, dwr);
  const nh = d.nidHeightFt;
  if (nh == null) return null;
  if (h.src === "DWR") {
    if (nh - h.value > 1)
      return {
        id: "W-H1",
        text: `NID Height ${nh} ft is more than 1 ft above the DWR dam height ${h.value} ft used for H. The NID value is often the structural height (from the foundation), which would overstate the breach head. Check H against the elevations once they are entered.`,
      };
    return null;
  }
  if (d.damHeightFt != null && nh - d.damHeightFt > 0.5)
    return {
      id: "W-H1",
      text: `NID Height ${nh} ft is the largest of the NID's heights; the NID's Dam Height is ${d.damHeightFt} ft. H for breach is crest minus breach bottom, which may be closer to ${d.damHeightFt} ft.`,
      useHeightFt: d.damHeightFt,
    };
  if (d.damHeightFt == null && d.structuralHeightFt != null && d.structuralHeightFt === nh)
    return {
      id: "W-H1",
      text: `NID Height ${nh} ft equals the structural height and no dam height is reported; H may be overstated. Check it against the crest and breach bottom.`,
    };
  return null;
}

/**
 * W-H2, once crest and breach bottom are entered: D = crest - breach bottom vs H; warn when |D - H| > tol,
 * tol = max(1 ft, 5% of D), +0.5 ft when H is a rounded whole-foot NID value.
 */
export function heightWarningOnElevations(dam: DamRecord): HeightWarning | null {
  if (!dam.nidId || !Number.isFinite(dam.crestFt) || !Number.isFinite(dam.streambedFt) || !Number.isFinite(dam.heightFt))
    return null;
  const D = dam.crestFt - dam.streambedFt;
  const rounded = dam.provenance?.heightFt?.src === "NID" && dam.provenance.heightFt.value === dam.heightFt;
  const tol = Math.max(1, 0.05 * Math.abs(D)) + (rounded ? 0.5 : 0);
  if (Math.abs(D - dam.heightFt) <= tol) return null;
  return {
    id: "W-H2",
    text: `Crest minus breach bottom is ${D.toFixed(2)} ft but H is ${dam.heightFt} ft (difference ${(D - dam.heightFt).toFixed(2)} ft, tolerance ${tol.toFixed(2)} ft). Check both; H drives the breach.`,
    useHeightFt: Math.round(D * 100) / 100,
  };
}

// ---------------------------------------------------------------- DWR card

export type DwrElevationOffer =
  | {
      ok: true;
      /** The DWR record's stated datum (provenance); the offered values are always NAVD 88. */
      statedDatum: "NGVD29" | "NAVD88";
      statedCrestFt: number;
      statedStreambedFt: number;
      /** VERTCON shift applied before the build (NGVD 29 only), else null. */
      shiftFt: number | null;
      crestNavdFt: number;
      streambedNavdFt: number;
      /** "converted from NGVD29 (+1.089 ft, VERTCON)" or "published in NAVD 88". */
      sourceNote: string;
    }
  | { ok: false; reason: string };

const fmtShift = (s: number) => `${s >= 0 ? "+" : "-"}${Math.abs(s).toFixed(3)} ft`;

/**
 * DWR elevations are offered only as NAVD 88, straight from the bundle: published NAVD 88, or NGVD 29 converted
 * before the build by per-point VERTCON. No shift is applied here (never a second conversion). Held values (elev_qc)
 * and local or blank datums are never offered.
 */
export function dwrElevationOffer(dwr: DwrDam | undefined): DwrElevationOffer {
  if (!dwr) return { ok: false, reason: "No DWR record for this dam." };
  if (dwr.datum === "Local" || dwr.datum == null)
    return {
      ok: false,
      reason:
        "DWR elevations are on a local datum (or no datum is given): relative only, not usable as elevations. Enter the crest and breach bottom from the DEM or type them.",
    };
  if (dwr.datum === "Other") return { ok: false, reason: "DWR datum is not NGVD 29 or NAVD 88; not offered." };
  const held = (dwr.held ?? "").split(",").filter(Boolean);
  if (held.includes("max_pool_e") || held.includes("streambed_"))
    return {
      ok: false,
      reason: `DWR labels these ${DATUM_LABEL[dwr.datum]}, but the values (${dwr.topElevFt ?? "—"} / ${dwr.streambedElevFt ?? "—"} ft) are outside 700-3,600 ft and look relative: held, not offered. Enter the crest and breach bottom from the DEM or type them.`,
    };
  if (dwr.topElevFt == null || dwr.streambedElevFt == null || dwr.topNavdFt == null || dwr.streambedNavdFt == null)
    return { ok: false, reason: "The DWR record lacks a NAVD 88 top-of-dam or streambed elevation." };
  const shift = dwr.datum === "NGVD29" ? dwr.vshiftFt : null;
  if (dwr.datum === "NGVD29" && shift == null)
    return { ok: false, reason: "No VERTCON shift recorded for this NGVD 29 record; not offered." };
  return {
    ok: true,
    statedDatum: dwr.datum,
    statedCrestFt: dwr.topElevFt,
    statedStreambedFt: dwr.streambedElevFt,
    shiftFt: shift,
    crestNavdFt: dwr.topNavdFt,
    streambedNavdFt: dwr.streambedNavdFt,
    sourceNote: shift != null ? `converted from NGVD29 (${fmtShift(shift)}, VERTCON)` : "published in NAVD 88",
  };
}

/**
 * "Use DWR elevations (checked against the dam file)": the NAVD 88 values from the bundle, stated datum NAVD88 (so
 * the engine applies no shift), with confirmed provenance that keeps the DWR stated value, datum and VERTCON shift.
 */
export function applyDwrElevationsPatch(dam: DamRecord, dwr: DwrDam, asOf: string): Partial<DamRecord> {
  const offer = dwrElevationOffer(dwr);
  if (!offer.ok) return {};
  const p = (field: string, value: number, stated: number): Provenance => ({
    src: "DWR",
    field: `${field}_navd88`,
    value,
    asOf,
    confirmed: true,
    note: offer.sourceNote,
    statedFt: stated,
    statedDatum: offer.statedDatum,
    ...(offer.shiftFt != null ? { vshiftFt: offer.shiftFt } : {}),
  });
  return {
    crestFt: offer.crestNavdFt,
    streambedFt: offer.streambedNavdFt,
    statedDatum: "NAVD88",
    datumOffsetFt: undefined,
    provenance: {
      ...dam.provenance,
      crestFt: p("max_pool_e", offer.crestNavdFt, offer.statedCrestFt),
      streambedFt: p("streambed_", offer.streambedNavdFt, offer.statedStreambedFt),
      statedDatum: {
        src: "DWR",
        field: "vertical_d",
        value: "NAVD88",
        asOf,
        confirmed: true,
        note: offer.sourceNote,
        statedDatum: offer.statedDatum,
        ...(offer.shiftFt != null ? { vshiftFt: offer.shiftFt } : {}),
      },
    },
  };
}

/**
 * "Typed from a DEM": crest and breach bottom the user read off a DEM (NAVD 88) and typed in, tagged "typed from DEM".
 * Nothing is sampled here, so the typed crest is NOT copied into demCrestFt: the engine's DEM-vs-crest check keeps
 * whatever DEM crest the record has and can still fire.
 */
export function demElevationsPatch(dam: DamRecord, crestFt: number, streambedFt: number, demName = "DEM"): Partial<DamRecord> {
  const asOf = new Date().toISOString().slice(0, 10);
  const p = (field: string, value: number): Provenance => ({ src: "DEM", field, value, asOf, note: `typed in (${demName})` });
  return {
    crestFt,
    streambedFt,
    statedDatum: "NAVD88",
    datumOffsetFt: undefined,
    provenance: {
      ...dam.provenance,
      crestFt: p("crest (typed from DEM)", crestFt),
      streambedFt: p("streambed (typed from DEM)", streambedFt),
    },
  };
}

/** "Use" for DWR max_pool_a (never auto-filled). */
export function dwrAreaPatch(dam: DamRecord, dwr: DwrDam, asOf: string): Partial<DamRecord> {
  if (dwr.maxPoolAreaAc == null) return {};
  return {
    surfaceAreaAc: dwr.maxPoolAreaAc,
    provenance: {
      ...dam.provenance,
      surfaceAreaAc: { src: "DWR", field: "max_pool_a", value: dwr.maxPoolAreaAc, asOf, confirmed: true },
    },
  };
}

export type DwrReference = {
  heightFt: number | null;
  /** top - streambed in the DWR record: an H check only (any datum, since it is a difference). */
  heightCheckFt: number | null;
  heightCheckNote: string | null;
  maxPoolStorageAcFt: number | null;
  storageFlag: string | null;
  auxSpillwayElevFt: number | null;
  normalPoolElevFt: number | null;
  county: string | null;
  datumLabel: string;
  /** Set when the bundle holds the stated values (elev_qc). */
  heldNote: string | null;
};

const DATUM_LABEL: Record<string, string> = {
  NGVD29: "NGVD 29",
  NAVD88: "NAVD 88",
  Local: "local datum, relative only",
  Other: "other datum, relative only",
};

/** Reference values on the DWR card (decision 10). */
export function dwrReference(d: NdDam, dwr: DwrDam, H: number): DwrReference {
  const diff =
    dwr.topElevFt != null && dwr.streambedElevFt != null ? Math.round((dwr.topElevFt - dwr.streambedElevFt) * 100) / 100 : null;
  let heightCheckNote: string | null = null;
  if (diff != null && Number.isFinite(H)) {
    const tol = Math.max(1, 0.05 * Math.abs(diff));
    if (Math.abs(diff - H) > tol)
      heightCheckNote = `DWR top minus streambed is ${diff} ft, H is ${H} ft (tolerance ${tol.toFixed(2)} ft): check H.`;
  }
  const v = d.nidStorageAcFt;
  const mp = dwr.maxPoolStorageAcFt;
  const storageFlag =
    v != null && mp != null && Math.abs(mp - v) > 0.05 * v
      ? `DWR max-pool storage ${fmtInt(mp)} ac-ft differs from the NID V ${fmtInt(v)} ac-ft by ${Math.round((Math.abs(mp - v) / v) * 100)}%.`
      : null;
  return {
    heightFt: dwr.heightFt,
    heightCheckFt: diff,
    heightCheckNote,
    maxPoolStorageAcFt: mp,
    storageFlag,
    auxSpillwayElevFt: dwr.auxSpillwayElevFt,
    normalPoolElevFt: dwr.normalPoolElevFt,
    county: dwr.county,
    datumLabel: dwr.datum ? DATUM_LABEL[dwr.datum] : "no datum given, relative only",
    heldNote: dwr.held
      ? `Held by the QC (outside 700-3,600 ft, looks relative despite the ${dwr.datum ? DATUM_LABEL[dwr.datum] : ""} label): ${dwr.held.split(",").join(", ")}. Not offered as elevations.`
      : null,
  };
}

/** Hover text for the DWR field meanings (DWR publishes no data dictionary; these are inferred). */
export const DWR_FIELD_HINTS: Record<string, string> = {
  heightFt: "max_dam_he: maximum dam height, ft (inferred).",
  topElevFt: "max_pool_e: maximum pool elevation; for most records it equals the top of dam (inferred).",
  streambedElevFt: "streambed_: streambed elevation at the dam, ft (inferred).",
  auxSpillwayElevFt: "emergenc_2: emergency (auxiliary) spillway crest elevation, ft (inferred).",
  normalPoolElevFt: "normal_ele: normal pool elevation, ft (inferred).",
  maxPoolStorageAcFt: "max_pool_v: storage at maximum pool, ac-ft (inferred).",
  maxPoolAreaAc: "max_pool_a: surface area at maximum pool, acres (inferred).",
  datum: "vertical_d: vertical datum of the elevations as DWR states it (NGVD 29, NAVD 88, Local or blank).",
  navd: "NAVD 88 value carried in the bundle: NGVD 29 converted by per-point NOAA VERTCON before the build, or published NAVD 88.",
};

/** Hover text for the DWR Dugouts fields (inferred; reference only). */
export const DUGOUT_FIELD_HINTS: Record<string, string> = {
  maxDepthFt: "max_depth: maximum depth, ft (inferred).",
  maxHeightFt: "max_height: maximum height, ft (inferred).",
  freeboardFt: "freeboard: freeboard, ft (inferred).",
  maxSurfaceAreaAc: "max_surfac: maximum surface area, acres (inferred).",
  maxStorageAcFt: "max_storag: maximum storage, acre-feet (inferred).",
};

/** NID hazard line on Record and Screen. Neutral; not used by the screen. */
export function nidHazardLine(h: NidHazard | undefined): string | null {
  return h ? `NID hazard (as reported to the NID, FEMA class): ${h}` : null;
}

export const NID_ATTRIBUTION_FALLBACK =
  "Dam inventory data: U.S. Army Corps of Engineers, National Inventory of Dams (nid.sec.usace.army.mil).";
