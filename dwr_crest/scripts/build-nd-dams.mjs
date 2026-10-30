#!/usr/bin/env node
/**
 * Build src/lib/crest/data/nd_dams.json (the ND dam picker's offline NID bundle) from the USACE National Inventory
 * of Dams ND extract. Run by hand only (`npm run data:nd-dams`), when Nick asks for a refresh; never chained into
 * build, dev or test, and it never touches the database. The output is committed.
 *
 *   npm run data:nd-dams                         download the ND bulk CSV (saved to data-raw/, git-ignored)
 *   npm run data:nd-dams -- --from data-raw/nid_ND.csv [--fetched 2026-10-29T21:56:00Z]
 *   ... --allow-shrink                           accept a bundle >20% smaller than the committed one
 *   ... --out <file>                             write somewhere else (tests)
 *
 * No per-dam NID API calls (decision 11): decimal heights come from the DWR file at runtime (nd_dams_dwr.json).
 * No new dependencies: a small RFC 4180 CSV parser lives here.
 * Never reads "Owner Names" (an NID owner can be a private person; Nick has not decided on showing them), so no
 * owner text reaches the bundle or the picker.
 */
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
export const DEFAULT_OUT = join(ROOT, "src/lib/crest/data/nd_dams.json");
export const PRIMARY_URL = "https://nid.sec.usace.army.mil/api/query?sy=%40stateKey%3AND&out=csv";
export const NATION_URL = "https://nid.sec.usace.army.mil/api/nation/csv";
export const ND_BOX = { latMin: 45.9, latMax: 49.05, lonMin: -104.1, lonMax: -96.5 };
export const MIN_ROWS = 800;
export const ATTRIBUTION =
  "Dam inventory data: U.S. Army Corps of Engineers, National Inventory of Dams (nid.sec.usace.army.mil), downloaded {date}. Values as reported by the owning agency; verify against the dam file.";

/** Every NID column the bundle reads (exact headers, spec §2.3). A missing one fails the build. */
export const REQUIRED = [
  "NID ID",
  "Federal ID",
  "Dam Name",
  "Other Names",
  "County",
  "Latitude",
  "Longitude",
  "NID Height (Ft)",
  "Dam Height (Ft)",
  "Structural Height (Ft)",
  "Hydraulic Height (Ft)",
  "NID Storage (Acre-Ft)",
  "Max Storage (Acre-Ft)",
  "Normal Storage (Acre-Ft)",
  "Surface Area (Acres)",
  "Hazard Potential Classification",
  "Dam Length (Ft)",
  "Year Completed",
  "Drainage Area (Sq Miles)",
  "River or Stream Name",
  "City",
  "Primary Purpose",
  "Primary Dam Type",
  "Data Last Updated",
  "Last Inspection Date",
  "EAP Prepared",
];

export const COLS = [
  "nid",
  "name",
  "otherNames",
  "county",
  "countyNote",
  "lat",
  "lon",
  "nidHeightFt",
  "damHeightFt",
  "structuralHeightFt",
  "hydraulicHeightFt",
  "nidStorageAcFt",
  "maxStorageAcFt",
  "normalStorageAcFt",
  "surfaceAreaNormalAc",
  "lengthFt",
  "yearCompleted",
  "drainageMi2",
  "nidHazard",
  "river",
  "city",
  "purpose",
  "damType",
  "nidUpdated",
  "lastInspection",
  "eap",
];

/** RFC 4180: quoted fields, "" escapes, CRLF or LF, a BOM is dropped. Returns an array of rows (arrays). */
export function parseCsv(text) {
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1);
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i++;
        } else quoted = false;
      } else field += c;
    } else if (c === '"') quoted = true;
    else if (c === ",") {
      row.push(field);
      field = "";
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else field += c;
  }
  if (field !== "" || row.length) {
    row.push(field);
    rows.push(row);
  }
  return rows.filter((r) => !(r.length === 1 && r[0] === ""));
}

const str = (s) => {
  const t = (s ?? "").trim();
  return t === "" ? null : t;
};
/** Numbers: "" or NaN -> null; 0 -> null when zeroIsMissing (heights, storages, area, length, drainage, year). */
const numv = (s, zeroIsMissing = true) => {
  const t = (s ?? "").trim();
  if (t === "") return null;
  const n = Number(t);
  if (!Number.isFinite(n)) return null;
  if (zeroIsMissing && n === 0) return null;
  return n;
};
/** MM/DD/YYYY -> YYYY-MM-DD; anything else is kept as is. */
const isoDate = (s) => {
  const t = str(s);
  const m = t && /^(\d{2})\/(\d{2})\/(\d{4})$/.exec(t);
  return m ? `${m[3]}-${m[1]}-${m[2]}` : t;
};

export function loadCountyBoxes() {
  return JSON.parse(readFileSync(join(ROOT, "scripts/data/nd_county_bbox.json"), "utf8"));
}

/** ND counties whose bounding box (Census cb_2023 20m) contains the point. */
export function countiesAt(lat, lon, boxes = loadCountyBoxes()) {
  return Object.entries(boxes)
    .filter(([, [x0, y0, x1, y1]]) => lon >= x0 && lon <= x1 && lat >= y0 && lat <= y1)
    .map(([name]) => name);
}

/**
 * Build the bundle object from the CSV text. Throws on a schema error, duplicate main IDs, or too few rows.
 * opts: { fetchedUtc, url, previousRows (committed count, for the shrink guard), allowShrink, log }
 */
export function buildNdDams(csvText, opts = {}) {
  const log = opts.log ?? (() => {});
  const table = parseCsv(csvText);
  const header = table[0] ?? [];
  const missing = REQUIRED.filter((c) => !header.includes(c));
  if (missing.length) throw new Error(`NID schema changed: missing column(s) ${missing.join(", ")}`);
  const ix = Object.fromEntries(header.map((h, i) => [h, i]));
  const get = (r, c) => r[ix[c]] ?? "";
  const boxes = loadCountyBoxes();
  const ndCounties = new Set(Object.keys(boxes));

  const dropped = [];
  const seen = new Set();
  const out = [];
  const warnings = [];
  for (const r of table.slice(1)) {
    const nid = str(get(r, "NID ID"));
    const fed = str(get(r, "Federal ID"));
    if (!nid) continue;
    if (fed !== nid) {
      dropped.push({ federalId: fed, nid, name: str(get(r, "Dam Name")) });
      continue;
    }
    if (seen.has(nid)) throw new Error(`duplicate main dam NID ID ${nid}`);
    seen.add(nid);
    const county = str(get(r, "County"));
    const lat = numv(get(r, "Latitude"), false);
    const lon = numv(get(r, "Longitude"), false);
    let countyNote = null;
    if (county && !ndCounties.has(county)) {
      const inside = lat != null && lon != null ? countiesAt(lat, lon, boxes) : [];
      countyNote =
        inside.length === 1
          ? `NID county '${county}' is not an ND county; point lies in ${inside[0]} Co., ND`
          : `NID county '${county}' is not an ND county`;
      warnings.push(`${nid}: ${countyNote}`);
    }
    if (lat == null || lon == null || lat < ND_BOX.latMin || lat > ND_BOX.latMax || lon < ND_BOX.lonMin || lon > ND_BOX.lonMax) {
      warnings.push(`${nid}: point ${lat}, ${lon} is outside the ND box`);
    }
    const rec = {
      nid,
      name: str(get(r, "Dam Name")),
      otherNames: str(get(r, "Other Names")),
      county,
      countyNote,
      lat,
      lon,
      nidHeightFt: numv(get(r, "NID Height (Ft)")),
      damHeightFt: numv(get(r, "Dam Height (Ft)")),
      structuralHeightFt: numv(get(r, "Structural Height (Ft)")),
      hydraulicHeightFt: numv(get(r, "Hydraulic Height (Ft)")),
      nidStorageAcFt: numv(get(r, "NID Storage (Acre-Ft)")),
      maxStorageAcFt: numv(get(r, "Max Storage (Acre-Ft)")),
      normalStorageAcFt: numv(get(r, "Normal Storage (Acre-Ft)")),
      surfaceAreaNormalAc: numv(get(r, "Surface Area (Acres)")),
      lengthFt: numv(get(r, "Dam Length (Ft)")),
      yearCompleted: numv(get(r, "Year Completed")),
      drainageMi2: numv(get(r, "Drainage Area (Sq Miles)")),
      nidHazard: str(get(r, "Hazard Potential Classification")),
      river: str(get(r, "River or Stream Name")),
      city: str(get(r, "City")),
      purpose: str(get(r, "Primary Purpose")),
      damType: str(get(r, "Primary Dam Type")),
      nidUpdated: isoDate(get(r, "Data Last Updated")),
      lastInspection: isoDate(get(r, "Last Inspection Date")),
      eap: str(get(r, "EAP Prepared")),
    };
    out.push(rec);
  }
  if (out.length < MIN_ROWS && !opts.allowShrink) {
    throw new Error(`only ${out.length} main dams (minimum ${MIN_ROWS}); pass --allow-shrink to accept`);
  }
  if (opts.previousRows && out.length < opts.previousRows * 0.8 && !opts.allowShrink) {
    throw new Error(`${out.length} main dams is more than 20% below the committed ${opts.previousRows}; pass --allow-shrink to accept`);
  }
  const key = (s) => (s ?? "").toLowerCase();
  out.sort((a, b) => key(a.county).localeCompare(key(b.county)) || key(a.name).localeCompare(key(b.name)) || a.nid.localeCompare(b.nid));
  for (const d of dropped) log(`dropped associated structure ${d.federalId} (${d.name ?? "unnamed"}) of ${d.nid}`);
  for (const w of warnings) log(`warning: ${w}`);

  const counties = {};
  const hazards = {};
  for (const r of out) {
    counties[r.county ?? ""] = (counties[r.county ?? ""] ?? 0) + 1;
    hazards[r.nidHazard ?? ""] = (hazards[r.nidHazard ?? ""] ?? 0) + 1;
  }
  const fetchedUtc = opts.fetchedUtc ?? new Date().toISOString().replace(/\.\d+Z$/, "Z");
  const source = {
    url: opts.url ?? PRIMARY_URL,
    fetchedUtc,
    sha256Raw: createHash("sha256").update(csvText).digest("hex"),
    rows: out.length,
    dropped,
    counties: Object.fromEntries(Object.entries(counties).sort(([a], [b]) => a.localeCompare(b))),
    hazards: Object.fromEntries(Object.entries(hazards).sort(([a], [b]) => a.localeCompare(b))),
    inventoryPass: null,
    attribution: ATTRIBUTION.replace("{date}", fetchedUtc.slice(0, 10)),
  };
  return { v: 2, source, cols: COLS, rows: out.map((r) => COLS.map((c) => r[c])) };
}

/** Summary lines: counts by county and hazard, missing values per field. */
export function summary(bundle) {
  const lines = [`${bundle.source.rows} main dams, ${Object.keys(bundle.source.counties).length} county values`];
  lines.push(`hazard: ${Object.entries(bundle.source.hazards).map(([k, v]) => `${k} ${v}`).join(", ")}`);
  lines.push(`county: ${Object.entries(bundle.source.counties).map(([k, v]) => `${k} ${v}`).join(", ")}`);
  const miss = bundle.cols.map((c, i) => [c, bundle.rows.filter((r) => r[i] == null).length]).filter(([, n]) => n > 0);
  lines.push(`missing: ${miss.map(([c, n]) => `${c} ${n}`).join(", ")}`);
  return lines;
}

async function download(url) {
  for (let attempt = 0; attempt < 3; attempt++) {
    try {
      const res = await fetch(url, {
        headers: { "User-Agent": "crest-nd-dams-build" },
        signal: AbortSignal.timeout(60_000),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.text();
    } catch (e) {
      console.error(`[nd-dams] ${url} attempt ${attempt + 1}: ${e.message}`);
    }
  }
  return null;
}

/** Keep the header plus the rows whose State is North Dakota (nation CSV fallback). */
function filterNation(text) {
  const t = parseCsv(text);
  const si = t[0].indexOf("State");
  const q = (v) => `"${String(v).replace(/"/g, '""')}"`;
  return [t[0], ...t.slice(1).filter((r) => r[si] === "North Dakota")].map((r) => r.map(q).join(",")).join("\n") + "\n";
}

export async function main(argv) {
  const arg = (k) => {
    const i = argv.indexOf(k);
    return i >= 0 ? argv[i + 1] : undefined;
  };
  const outFile = resolve(arg("--out") ?? DEFAULT_OUT);
  let csv;
  let url = PRIMARY_URL;
  let fetchedUtc = arg("--fetched");
  const from = arg("--from");
  if (from) {
    csv = readFileSync(from, "utf8");
    fetchedUtc ??= statSync(from).mtime.toISOString().replace(/\.\d+Z$/, "Z");
  } else {
    csv = await download(PRIMARY_URL);
    if (csv == null) {
      url = NATION_URL;
      const nation = await download(NATION_URL);
      if (nation == null) throw new Error("NID download failed (ND query and nation CSV)");
      csv = filterNation(nation);
    }
    const raw = join(ROOT, "data-raw");
    mkdirSync(raw, { recursive: true });
    writeFileSync(join(raw, "nid_ND.csv"), csv);
  }
  const previousRows = existsSync(outFile) ? JSON.parse(readFileSync(outFile, "utf8")).source?.rows : undefined;
  const bundle = buildNdDams(csv, {
    url,
    fetchedUtc,
    previousRows,
    allowShrink: argv.includes("--allow-shrink"),
    log: (m) => console.log(`[nd-dams] ${m}`),
  });
  const text = JSON.stringify(bundle);
  mkdirSync(dirname(outFile), { recursive: true });
  writeFileSync(outFile, text);
  for (const l of summary(bundle)) console.log(`[nd-dams] ${l}`);
  console.log(`[nd-dams] wrote ${outFile} (${text.length} bytes)`);
  return bundle;
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  main(process.argv.slice(2)).catch((e) => {
    console.error(`[nd-dams] ${e.message}`);
    process.exit(1);
  });
}
