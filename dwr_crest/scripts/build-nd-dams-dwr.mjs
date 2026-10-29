#!/usr/bin/env node
/**
 * Build src/lib/crest/data/nd_dams_dwr.json (the DWR card's offline data) from a Dams.zip saved BY HAND from the
 * ND Department of Water Resources Dams page (mapservice.dwr.nd.gov, "Download Data"). One pull per refresh, by a
 * person, only when Nick asks; nothing here downloads. Keep the zip in data-raw/ (git-ignored).
 *
 *   npm run data:nd-dams-dwr -- --from data-raw/Dams.zip [--pulled 2026-10-29T22:07:32Z] [--out <file>]
 *
 * Joins on `national_i` = NID ID, main dams only (the ids in nd_dams.json). Carries NUMERIC fields only plus the
 * vertical datum code (needed to decide whether elevations may be offered) and the DWR county only where it differs
 * from the NID county. Never carries owner names (primary_ow, other_resp), owner type, private-dam flags, remarks,
 * permits, names or any other text field.
 * No new dependencies: zip entries are inflated with node:zlib and the .dbf is read by a small reader here.
 */
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { inflateRawSync } from "node:zlib";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
export const DEFAULT_OUT = join(ROOT, "src/lib/crest/data/nd_dams_dwr.json");
export const NID_BUNDLE = join(ROOT, "src/lib/crest/data/nd_dams.json");
export const FORM_URL = "https://mapservice.dwr.nd.gov/php/dams/attribute.php";
export const DISCLAIMER =
  "This data is provided by the ND Department of Water Resources for your convenience. This data is provisional. This service is provided 'AS IS' and without warranty of any kind. The ND Department of Water Resources is not responsible for any errors or damages that may occur resulting from the use or mis-use of the data.";
export const ATTRIBUTION =
  "Dam data: North Dakota Department of Water Resources, Dams layer (mapservice.dwr.nd.gov), downloaded {date}. Provisional, provided 'as is'.";

/** Output columns, in order, and the DBF field each comes from. All numeric except datum and county. */
export const FIELDS = [
  ["nid", "national_i"],
  ["dwrId", "structure_"],
  ["heightFt", "max_dam_he"],
  ["topElevFt", "max_pool_e"],
  ["streambedElevFt", "streambed_"],
  ["datum", "vertical_d"],
  ["auxSpillwayElevFt", "emergenc_2"],
  ["normalPoolElevFt", "normal_ele"],
  ["maxPoolStorageAcFt", "max_pool_v"],
  ["maxPoolAreaAc", "max_pool_a"],
  ["normalStorageAcFt", "normal_vol"],
  ["normalAreaAc", "normal_are"],
  ["lengthFt", "dam_length"],
  ["topWidthFt", "top_width"],
  ["county", "county"],
];
export const COLS = FIELDS.map(([c]) => c);
/** Never read into the output (spec §9 + Nick's round-2 rule: no owner names or other personal fields). */
export const EXCLUDED = ["primary_ow", "other_resp", "owner_type", "private_da", "remarks", "water_perm"];

/** Minimal zip reader: { name -> Buffer } for stored (0) and deflated (8) entries. */
export function unzip(buf) {
  let eocd = -1;
  for (let i = buf.length - 22; i >= Math.max(0, buf.length - 65557); i--) {
    if (buf.readUInt32LE(i) === 0x06054b50) {
      eocd = i;
      break;
    }
  }
  if (eocd < 0) throw new Error("not a zip file (no end of central directory)");
  const n = buf.readUInt16LE(eocd + 10);
  let p = buf.readUInt32LE(eocd + 16);
  const out = {};
  for (let k = 0; k < n; k++) {
    if (buf.readUInt32LE(p) !== 0x02014b50) throw new Error("bad zip central directory");
    const method = buf.readUInt16LE(p + 10);
    const csize = buf.readUInt32LE(p + 20);
    const nameLen = buf.readUInt16LE(p + 28);
    const extraLen = buf.readUInt16LE(p + 30);
    const commentLen = buf.readUInt16LE(p + 32);
    const local = buf.readUInt32LE(p + 42);
    const name = buf.subarray(p + 46, p + 46 + nameLen).toString("utf8");
    const lNameLen = buf.readUInt16LE(local + 26);
    const lExtraLen = buf.readUInt16LE(local + 28);
    const data = buf.subarray(local + 30 + lNameLen + lExtraLen, local + 30 + lNameLen + lExtraLen + csize);
    if (method === 0) out[name] = Buffer.from(data);
    else if (method === 8) out[name] = inflateRawSync(data);
    else throw new Error(`zip entry ${name}: unsupported method ${method}`);
    p += 46 + nameLen + extraLen + commentLen;
  }
  return out;
}

/** Minimal dBASE III reader: { fields: [{name,type,len}], rows: [{name: string}] } (deleted records skipped). */
export function readDbf(buf) {
  const n = buf.readUInt32LE(4);
  const headerLen = buf.readUInt16LE(8);
  const recLen = buf.readUInt16LE(10);
  const fields = [];
  for (let p = 32; buf[p] !== 0x0d; p += 32) {
    fields.push({
      name: buf.subarray(p, p + 11).toString("latin1").split("\0")[0],
      type: String.fromCharCode(buf[p + 11]),
      len: buf[p + 16],
    });
  }
  const rows = [];
  for (let r = 0; r < n; r++) {
    const s = headerLen + r * recLen;
    if (s + recLen > buf.length) break;
    if (buf[s] === 0x2a) continue;
    let o = s + 1;
    const row = {};
    for (const f of fields) {
      row[f.name] = buf.subarray(o, o + f.len).toString("latin1").trim();
      o += f.len;
    }
    rows.push(row);
  }
  return { fields, rows };
}

/** DBF numeric text -> number, rounded to 4 decimals; "", NaN and 0 -> null (DWR uses 0 for "not entered"). */
const numv = (s) => {
  const t = (s ?? "").trim();
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

/**
 * Join DWR records to the NID main dams.
 * nid: [{ nid, name, county }] main dams. Returns the bundle object.
 */
export function buildDwr(zipBuf, nid, opts = {}) {
  const log = opts.log ?? (() => {});
  const files = unzip(zipBuf);
  const dbfName = Object.keys(files).find((k) => /\.dbf$/i.test(k));
  if (!dbfName) throw new Error("no .dbf in the zip");
  const { fields, rows } = readDbf(files[dbfName]);
  const have = new Set(fields.map((f) => f.name));
  const missing = [...FIELDS.map(([, f]) => f), "dam_name"].filter((f) => !have.has(f));
  if (missing.length) throw new Error(`DWR schema changed: missing field(s) ${missing.join(", ")}`);

  const byNid = new Map();
  for (const r of rows) {
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
        log(`${dam.nid}: ${cands.length} DWR records; chose structure ${named[0].structure_} (name matches "${dam.name}")`);
        cands = named;
      } else {
        skipped.push({ nid: dam.nid, candidates: cands.map((r) => Number(r.structure_)) });
        log(`${dam.nid}: ${cands.length} DWR records and no single name match; skipped`);
        continue;
      }
    }
    const r = cands[0];
    const dwrCounty = (r.county ?? "").trim() || null;
    const rec = {};
    for (const [col, f] of FIELDS) {
      if (col === "nid") rec[col] = dam.nid;
      else if (col === "dwrId") rec[col] = Number(r[f]) || null;
      else if (col === "datum") rec[col] = normDatum(r[f]);
      else if (col === "county") rec[col] = dwrCounty && dwrCounty !== dam.county ? dwrCounty : null;
      else rec[col] = numv(r[f]);
    }
    datums[rec.datum ?? "blank"] = (datums[rec.datum ?? "blank"] ?? 0) + 1;
    out.push(rec);
  }
  out.sort((a, b) => a.nid.localeCompare(b.nid));
  const pulledUtc = opts.pulledUtc ?? new Date().toISOString().replace(/\.\d+Z$/, "Z");
  return {
    v: 1,
    source: {
      formUrl: FORM_URL,
      pulledUtc,
      sha256Zip: createHash("sha256").update(zipBuf).digest("hex"),
      records: rows.length,
      matched: out.length,
      skipped,
      datums: Object.fromEntries(Object.entries(datums).sort(([a], [b]) => a.localeCompare(b))),
      excludedFields: EXCLUDED,
      disclaimer: DISCLAIMER,
      attribution: ATTRIBUTION.replace("{date}", pulledUtc.slice(0, 10)),
    },
    cols: COLS,
    rows: out.map((r) => COLS.map((c) => r[c])),
  };
}

/** Main dams from the committed NID bundle: [{ nid, name, county }]. */
export function nidMainDams(bundle) {
  const i = (c) => bundle.cols.indexOf(c);
  return bundle.rows.map((r) => ({ nid: r[i("nid")], name: r[i("name")], county: r[i("county")] }));
}

export function main(argv) {
  const arg = (k) => {
    const i = argv.indexOf(k);
    return i >= 0 ? argv[i + 1] : undefined;
  };
  const from = arg("--from");
  if (!from) throw new Error("--from <Dams.zip> is required (save it by hand from the DWR Dams page; see README)");
  const outFile = resolve(arg("--out") ?? DEFAULT_OUT);
  const zip = readFileSync(from);
  const nid = nidMainDams(JSON.parse(readFileSync(arg("--nid") ?? NID_BUNDLE, "utf8")));
  const bundle = buildDwr(zip, nid, {
    pulledUtc: arg("--pulled") ?? statSync(from).mtime.toISOString().replace(/\.\d+Z$/, "Z"),
    log: (m) => console.log(`[nd-dams-dwr] ${m}`),
  });
  const text = JSON.stringify(bundle);
  mkdirSync(dirname(outFile), { recursive: true });
  writeFileSync(outFile, text);
  console.log(
    `[nd-dams-dwr] ${bundle.source.records} DWR records, ${bundle.source.matched} joined to ${nid.length} NID main dams; datums ${JSON.stringify(bundle.source.datums)}`,
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
