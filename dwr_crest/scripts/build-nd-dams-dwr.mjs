#!/usr/bin/env node
/**
 * Build src/lib/crest/data/nd_dams_dwr.json (the DWR card's offline data) from the ND Department of Water Resources
 * Dams and Dugouts layers (mapservice.dwr.nd.gov), as pre-converted to NAVD 88 by a person (Docs & QA, 2026-10-30):
 * a folder holding Dams_navd88.csv and Dugouts_navd88.csv (per-point NOAA VERTCON, NGVD 29 -> NAVD 88, done once,
 * outside the app). Nothing here downloads and nothing here converts datums: the bundle carries the NAVD 88 values
 * from the folder, with the stated datum and the VERTCON shift kept as provenance. Keep the folder in data-raw/
 * (git-ignored). One refresh per request from Nick.
 *
 *   npm run data:nd-dams-dwr -- --from data-raw/crest_dwr_navd88_<stamp> [--pulled 2026-10-30T14:31:02Z] [--out <file>]
 *
 * Joins on `national_i` = NID ID, main dams only (the ids in nd_dams.json). Dams: NUMERIC fields only, plus the stated
 * datum code and the DWR county where it differs from the NID county. Dugouts: five numeric reference fields for the
 * dugout records whose NID ID is a main dam. Never reads owner names, contacts, designers, remarks, permits, dam
 * names (except to break a tie in memory) or any other text field.
 *
 * Elevation rules (Nick, 2026-10-30): offer NAVD 88 only.
 * - NGVD 29 / NAVD 88 stated: <field>_navd88 is carried; it is checked against stated + vshift_ft (no second shift).
 * - A field named in elev_qc (value outside 700-3,600 ft under an NGVD 29 / NAVD 88 label) is HELD: NAVD 88 null.
 * - drawdown_e is treated as local and never read.
 * - Local or blank datum: no NAVD 88 values; the stated values stay relative (an H check only).
 */
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
export const DEFAULT_OUT = join(ROOT, "src/lib/crest/data/nd_dams_dwr.json");
export const NID_BUNDLE = join(ROOT, "src/lib/crest/data/nd_dams.json");
export const FORM_URL = "https://mapservice.dwr.nd.gov/php/downloadShape.php";
export const LAYERS = "Dams (layer 254, qlayer Dams) and Dugouts (layer 277, qlayer Dugouts)";
export const DISCLAIMER =
  "This data is provided by the ND Department of Water Resources for your convenience. This data is provisional. This service is provided 'AS IS' and without warranty of any kind. The ND Department of Water Resources is not responsible for any errors or damages that may occur resulting from the use or mis-use of the data.";
export const ATTRIBUTION =
  "Dam data: North Dakota Department of Water Resources, Dams and Dugouts layers (mapservice.dwr.nd.gov), downloaded {date}. Provisional, provided 'as is'. NGVD 29 elevations converted to NAVD 88 with NOAA VERTCON.";
export const CONVERSION =
  "Per-point NOAA VERTCON at each record's own lat/lon (PROJ vgridshift, us_noaa_vertconc.tif), NGVD 29 -> NAVD 88, done before the build; NAVD 88 as published kept (shift 0). Values outside 700-3,600 ft under an NGVD 29 / NAVD 88 label are held (relative). drawdown_e is treated as local. Local or blank datums are not converted.";
export const QC_RANGE_FT = [700, 3600];

/** Dam output columns, in order, and the CSV field each comes from. */
export const FIELDS = [
  ["nid", "national_i"],
  ["dwrId", "structure_"],
  ["heightFt", "max_dam_he"],
  ["topElevFt", "max_pool_e"],
  ["streambedElevFt", "streambed_"],
  ["datum", "datum_raw"],
  ["auxSpillwayElevFt", "emergenc_2"],
  ["normalPoolElevFt", "normal_ele"],
  ["maxPoolStorageAcFt", "max_pool_v"],
  ["maxPoolAreaAc", "max_pool_a"],
  ["normalStorageAcFt", "normal_vol"],
  ["normalAreaAc", "normal_are"],
  ["lengthFt", "dam_length"],
  ["topWidthFt", "top_width"],
  ["county", "county"],
  ["topNavdFt", "max_pool_e_navd88"],
  ["streambedNavdFt", "streambed__navd88"],
  ["auxSpillwayNavdFt", "emergenc_2_navd88"],
  ["normalPoolNavdFt", "normal_ele_navd88"],
  ["vshiftFt", "vshift_ft"],
  ["held", "elev_qc"],
];
export const COLS = FIELDS.map(([c]) => c);
/** The NAVD 88 columns and the stated field each was converted from. drawdown_e is deliberately absent (local). */
export const ELEV = [
  ["topNavdFt", "max_pool_e"],
  ["streambedNavdFt", "streambed_"],
  ["auxSpillwayNavdFt", "emergenc_2"],
  ["normalPoolNavdFt", "normal_ele"],
];
/** Dugout reference columns (units as DWR gives them: ft, ft, ft, acres, acre-feet). */
export const DUGOUT_FIELDS = [
  ["nid", "national_i"],
  ["dwrId", "structure_"],
  ["maxDepthFt", "max_depth"],
  ["maxHeightFt", "max_height"],
  ["freeboardFt", "freeboard"],
  ["maxSurfaceAreaAc", "max_surfac"],
  ["maxStorageAcFt", "max_storag"],
];
export const DUGOUT_COLS = DUGOUT_FIELDS.map(([c]) => c);
/** Never read into the output (spec §9 + Nick's rule: no owner names or other personal fields). */
export const EXCLUDED = {
  dams: ["primary_ow", "other_resp", "owner_type", "private_da", "remarks", "water_perm", "dam_name", "other_rese", "former_nam"],
  dugouts: ["site_owner", "designer", "water_perm", "creation_i"],
};

/** RFC 4180 CSV -> array of objects keyed by the header row. */
export function parseCsv(text) {
  const rows = [];
  let row = [];
  let f = "";
  let q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          f += '"';
          i++;
        } else q = false;
      } else f += c;
    } else if (c === '"') q = true;
    else if (c === ",") {
      row.push(f);
      f = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(f);
      rows.push(row);
      row = [];
      f = "";
    } else f += c;
  }
  if (f !== "" || row.length) {
    row.push(f);
    rows.push(row);
  }
  const [head, ...body] = rows.filter((r) => !(r.length === 1 && r[0] === ""));
  return { fields: head ?? [], rows: body.map((r) => Object.fromEntries((head ?? []).map((h, i) => [h, r[i] ?? ""]))) };
}

/** Numeric text -> number, rounded to 4 decimals; "", NaN and 0 -> null (DWR uses 0 for "not entered"). */
export const numv = (s) => {
  const t = String(s ?? "").trim();
  if (t === "") return null;
  const n = Number(t);
  if (!Number.isFinite(n) || n === 0) return null;
  return Math.round(n * 1e4) / 1e4;
};
export function normDatum(s) {
  const t = (s ?? "").trim().toUpperCase().replace(/\s+/g, "");
  if (t === "") return null;
  if (t === "NGVD29") return "NGVD29";
  if (t === "NAVD88") return "NAVD88";
  if (t === "LOCAL") return "Local";
  return "Other";
}
const norm = (s) => (s ?? "").toLowerCase().replace(/[^a-z0-9]/g, "");

/** Stated fields that elev_qc names as held ("max_pool_e=128 outside 700-3600 ft: ..."). */
export function heldFields(qc) {
  return [...String(qc ?? "").matchAll(/\b([a-z_0-9]+)=/g)].map((m) => m[1]);
}

function requireFields(fields, need, what) {
  const have = new Set(fields);
  const missing = need.filter((f) => !have.has(f));
  if (missing.length) throw new Error(`DWR ${what} schema changed: missing field(s) ${missing.join(", ")}`);
}

/** One dam record -> bundle row object, with the NAVD 88 rules applied and checked. */
function damRow(dam, r) {
  const datum = normDatum(r.datum_raw ?? r.vertical_d);
  const vshift = r.vshift_ft === "" ? null : Number(r.vshift_ft);
  const held = new Set(heldFields(r.elev_qc));
  const rec = {};
  for (const [col, f] of FIELDS) {
    if (col === "nid") rec[col] = dam.nid;
    else if (col === "dwrId") rec[col] = Number(r[f]) || null;
    else if (col === "datum") rec[col] = datum;
    else if (col === "county") {
      const c = (r.county ?? "").trim() || null;
      rec[col] = c && c !== dam.county ? c : null;
    } else if (col === "vshiftFt") rec[col] = datum === "NGVD29" || datum === "NAVD88" ? vshift : null;
    else if (col === "held") {
      const h = ELEV.map(([, s]) => s).filter((s) => held.has(s));
      rec[col] = h.length ? h.join(",") : null;
    } else if (ELEV.some(([c]) => c === col)) continue;
    else rec[col] = numv(r[f]);
  }
  for (const [col, stated] of ELEV) {
    const raw = String(r[`${stated}_navd88`] ?? "").trim();
    let v = raw === "" ? null : Number(raw);
    if (v != null && !Number.isFinite(v)) throw new Error(`${dam.nid}: ${stated}_navd88 "${raw}" is not a number`);
    if (datum !== "NGVD29" && datum !== "NAVD88") {
      if (v != null) throw new Error(`${dam.nid}: ${datum ?? "blank"} datum but ${stated}_navd88 = ${v}; local values are never converted`);
      rec[col] = null;
      continue;
    }
    if (held.has(stated)) v = null; // elev_qc: never offered
    if (v != null && (v < QC_RANGE_FT[0] || v > QC_RANGE_FT[1] + 5)) v = null; // belt and braces on the QC range
    if (v != null) {
      const s = numv(r[stated]);
      if (s == null || vshift == null || !Number.isFinite(vshift))
        throw new Error(`${dam.nid}: ${stated}_navd88 without a stated value or vshift_ft`);
      if (datum === "NAVD88" && vshift !== 0) throw new Error(`${dam.nid}: NAVD 88 record with vshift_ft ${vshift}`);
      if (Math.abs(s + vshift - v) > 0.0051)
        throw new Error(`${dam.nid}: ${stated}_navd88 ${v} != ${s} + ${vshift} (double or missing conversion?)`);
      v = Math.round(v * 100) / 100;
    }
    rec[col] = v;
  }
  return rec;
}

/**
 * Join the converted DWR Dams and Dugouts CSVs to the NID main dams.
 * nid: [{ nid, name, county }] main dams. Returns the bundle object.
 */
export function buildDwr({ damsCsv, dugoutsCsv }, nid, opts = {}) {
  const log = opts.log ?? (() => {});
  const dams = parseCsv(damsCsv);
  requireFields(dams.fields, [...FIELDS.map(([, f]) => f), ...ELEV.map(([, s]) => s), "dam_name"], "Dams");
  const dug = parseCsv(dugoutsCsv);
  requireFields(dug.fields, DUGOUT_FIELDS.map(([, f]) => f), "Dugouts");

  const byNid = new Map();
  for (const r of dams.rows) {
    const id = (r.national_i ?? "").trim();
    if (!id) continue;
    if (!byNid.has(id)) byNid.set(id, []);
    byNid.get(id).push(r);
  }
  const out = [];
  const skipped = [];
  const datums = {};
  for (const dam of nid) {
    let cands = byNid.get(dam.nid) ?? [];
    if (cands.length === 0) continue;
    if (cands.length > 1) {
      const named = cands.filter((r) => norm(r.dam_name) === norm(dam.name));
      if (named.length === 1) {
        log(`${dam.nid}: ${cands.length} DWR records; chose structure ${named[0].structure_} (name matches the NID name)`);
        cands = named;
      } else {
        skipped.push({ nid: dam.nid, candidates: cands.map((r) => Number(r.structure_)) });
        log(`${dam.nid}: ${cands.length} DWR records and no single name match; skipped`);
        continue;
      }
    }
    const rec = damRow(dam, cands[0]);
    datums[rec.datum ?? "blank"] = (datums[rec.datum ?? "blank"] ?? 0) + 1;
    out.push(rec);
  }
  out.sort((a, b) => a.nid.localeCompare(b.nid));

  const mainIds = new Set(nid.map((d) => d.nid));
  const dugByNid = new Map();
  let dugWithNid = 0;
  for (const r of dug.rows) {
    const id = (r.national_i ?? "").trim();
    if (!id) continue;
    dugWithNid++;
    if (!mainIds.has(id)) continue;
    if (dugByNid.has(id)) {
      log(`${id}: more than one dugout record; kept structure ${dugByNid.get(id).dwrId}`);
      continue;
    }
    const rec = {};
    for (const [col, f] of DUGOUT_FIELDS) rec[col] = col === "nid" ? id : col === "dwrId" ? Number(r[f]) || null : numv(r[f]);
    dugByNid.set(id, rec);
  }
  const dugouts = [...dugByNid.values()].sort((a, b) => a.nid.localeCompare(b.nid));

  const offers = out.filter((r) => r.topNavdFt != null && r.streambedNavdFt != null).length;
  const held = out.filter((r) => r.held).map((r) => `${r.nid}:${r.held}`);
  const pulledUtc = opts.pulledUtc ?? new Date().toISOString().replace(/\.\d+Z$/, "Z");
  const sha = (t) => createHash("sha256").update(t).digest("hex");
  return {
    v: 2,
    source: {
      formUrl: FORM_URL,
      layers: LAYERS,
      pulledUtc,
      sha256: {
        ...(opts.rawSha256 ?? {}),
        "Dams_navd88.csv": sha(damsCsv),
        "Dugouts_navd88.csv": sha(dugoutsCsv),
      },
      conversion: CONVERSION,
      ...(opts.gridSha256 ? { gridSha256: opts.gridSha256 } : {}),
      records: dams.rows.length,
      matched: out.length,
      skipped,
      datums: Object.fromEntries(Object.entries(datums).sort(([a], [b]) => a.localeCompare(b))),
      navd88Offers: offers,
      held,
      dugouts: { records: dug.rows.length, withNid: dugWithNid, matched: dugouts.length },
      excludedFields: EXCLUDED,
      disclaimer: DISCLAIMER,
      attribution: ATTRIBUTION.replace("{date}", pulledUtc.slice(0, 10)),
    },
    cols: COLS,
    rows: out.map((r) => COLS.map((c) => r[c])),
    dugouts: { cols: DUGOUT_COLS, rows: dugouts.map((r) => DUGOUT_COLS.map((c) => r[c])) },
  };
}

/** Main dams from the committed NID bundle: [{ nid, name, county }]. */
export function nidMainDams(bundle) {
  const i = (c) => bundle.cols.indexOf(c);
  return bundle.rows.map((r) => ({ nid: r[i("nid")], name: r[i("name")], county: r[i("county")] }));
}

/** "<sha>  <name>" lines -> { name: sha }. */
function readSums(file) {
  if (!existsSync(file)) return undefined;
  return Object.fromEntries(
    readFileSync(file, "utf8")
      .split(/\r?\n/)
      .map((l) => l.trim().split(/\s+/))
      .filter((p) => p.length === 2 && /^[0-9a-f]{64}$/.test(p[0]))
      .map(([h, n]) => [n, h]),
  );
}

export function main(argv) {
  const arg = (k) => {
    const i = argv.indexOf(k);
    return i >= 0 ? argv[i + 1] : undefined;
  };
  const from = arg("--from");
  if (!from) throw new Error("--from <folder with Dams_navd88.csv and Dugouts_navd88.csv> is required (see README)");
  const outFile = resolve(arg("--out") ?? DEFAULT_OUT);
  const damsFile = join(from, "Dams_navd88.csv");
  const damsCsv = readFileSync(damsFile, "utf8");
  const dugoutsCsv = readFileSync(join(from, "Dugouts_navd88.csv"), "utf8");
  const nid = nidMainDams(JSON.parse(readFileSync(arg("--nid") ?? NID_BUNDLE, "utf8")));
  const bundle = buildDwr({ damsCsv, dugoutsCsv }, nid, {
    pulledUtc: arg("--pulled") ?? statSync(damsFile).mtime.toISOString().replace(/\.\d+Z$/, "Z"),
    rawSha256: readSums(join(from, "raw", "SHA256SUMS.txt")),
    gridSha256: readSums(join(from, "grids", "SHA256SUMS.txt")),
    log: (m) => console.log(`[nd-dams-dwr] ${m}`),
  });
  const text = JSON.stringify(bundle);
  mkdirSync(dirname(outFile), { recursive: true });
  writeFileSync(outFile, text);
  const s = bundle.source;
  console.log(
    `[nd-dams-dwr] ${s.records} DWR dam records, ${s.matched} joined to ${nid.length} NID main dams; datums ${JSON.stringify(s.datums)}; NAVD 88 offers ${s.navd88Offers}; held ${s.held.join(" ") || "none"}; dugouts ${s.dugouts.matched} of ${s.dugouts.records}`,
  );
  console.log(`[nd-dams-dwr] wrote ${outFile} (${text.length} bytes)`);
  return bundle;
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  try {
    main(process.argv.slice(2));
  } catch (e) {
    console.error(`[nd-dams-dwr] ${e.message}`);
    process.exit(1);
  }
}
