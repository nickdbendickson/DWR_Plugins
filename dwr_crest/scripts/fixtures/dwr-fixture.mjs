/**
 * DWR test fixture in the format of the Docs & QA folder (Dams_navd88.csv / Dugouts_navd88.csv): a few records with
 * numeric values copied from the 2026-10-30 files (dam structures 2801, 2753, 6192, 2785, 3056, 4978, 2625, 5976;
 * dugout 301). Owner, name, remark and permit text is MADE UP (markers below), never DWR text: it must never reach
 * the bundle.
 */
const OWNER = "FIXTURE-OWNER Jane Q. Notreal";
const MARK = {
  primary_ow: OWNER,
  other_resp: "FIXTURE-CONTACT 701-555-0100",
  other_rese: "FIXTURE-OTHERNAME Doe Family Pond",
  former_nam: "FIXTURE-FORMER Smith Dam",
  owner_type: "Private",
  private_da: "F",
  remarks: "FIXTURE-REMARK",
  water_perm: "FIX123",
};
export const FIXTURE_MARKERS = [OWNER, "Notreal", "701-555-0100", "FIXTURE-OTHERNAME", "Doe Family", "FIXTURE-FORMER", "Smith Dam", "FIXTURE-REMARK", "FIX123", "Private", "FIXTURE-DESIGNER", "FIXTURE-SITE-OWNER"];

const DAM_FIELDS = [
  "structure_", "county", "dam_name", "other_rese", "former_nam", "primary_ow", "other_resp", "owner_type", "private_da",
  "remarks", "water_perm", "max_dam_he", "dam_length", "max_pool_a", "max_pool_v", "max_pool_e", "streambed_",
  "emergenc_2", "normal_vol", "normal_are", "normal_ele", "drawdown_e", "vertical_d", "top_width", "national_i",
  "max_pool_e_local", "max_pool_e_navd88", "streambed__local", "streambed__navd88", "emergenc_2_local",
  "emergenc_2_navd88", "normal_ele_local", "normal_ele_navd88", "drawdown_e_local", "drawdown_e_navd88", "datum_raw",
  "datum_src", "vshift_ft", "vshift_grid", "elev_status", "elev_qc", "local_looks_absolute", "crest_inputs_navd88",
];
const DUG_FIELDS = ["structure_", "county", "site_owner", "designer", "max_surfac", "max_storag", "max_depth", "max_height", "freeboard", "top_width", "national_i"];

const ELEVS = ["max_pool_e", "streambed_", "emergenc_2", "normal_ele", "drawdown_e"];
/** Fill the *_local / *_navd88 columns the way the folder does: NGVD 29 + shift, NAVD 88 as is, Local -> _local. */
function dam(o, { held = [] } = {}) {
  const d = { ...MARK, ...o };
  const datum = d.datum_raw ?? "";
  const shift = datum === "NGVD 29" ? d.vshift_ft : datum === "NAVD 88" ? 0 : null;
  const qc = [];
  for (const f of ELEVS) {
    const v = Number(d[f] ?? 0);
    const nv = `${f}_navd88`;
    const lv = `${f}_local`;
    if (!v) continue;
    if (shift == null) d[lv] = v;
    else if (f === "drawdown_e" && (v < 700 || v > 3600)) qc.push(`${f}=${v} outside 700-3600 ft: looks relative despite ${datum} label, NAVD88 left null`);
    else if (held.includes(f)) qc.push(`${f}=${v} outside 700-3600 ft: looks relative despite ${datum} label, NAVD88 left null`);
    else d[nv] = Math.round((v + shift) * 100) / 100;
  }
  d.vertical_d = datum;
  d.vshift_ft = shift ?? "";
  d.elev_qc = d.elev_qc ?? qc.join("; ");
  d.crest_inputs_navd88 = d.max_pool_e_navd88 != null && d.streambed__navd88 != null ? "Y" : "N";
  return d;
}

export const DAMS_FIXTURE = [
  dam({ structure_: 2801, national_i: "ND00199", dam_name: "Swan Buffalo Detention Dam 12", county: "Cass", datum_raw: "NGVD 29", vshift_ft: 1.0887,
    max_dam_he: 27.5, max_pool_e: 1070, streambed_: 1042.5, emergenc_2: 1066, normal_ele: 1051, drawdown_e: 145.5, max_pool_v: 1615.5,
    max_pool_a: 154.5, normal_vol: 87.8, normal_are: 29.8, dam_length: 1700, top_width: 12 }),
  dam({ structure_: 6192, national_i: "ND00145", dam_name: "Snake Creek Embankment", county: "McLean", datum_raw: "",
    max_dam_he: 0, max_pool_e: 0, streambed_: 0, emergenc_2: 0, normal_ele: 0, max_pool_v: 0, max_pool_a: 0,
    normal_vol: 0, normal_are: 0, dam_length: 12900, top_width: 0 }),
  dam({ structure_: 2753, national_i: "ND00145", dam_name: "Garrison Dam", county: "McLean", datum_raw: "",
    max_dam_he: 202, max_pool_e: 1875, streambed_: 1673, emergenc_2: 1850, normal_ele: 1837.5, max_pool_v: 24200000,
    max_pool_a: 383000, normal_vol: 18400000, normal_are: 315000, dam_length: 11300, top_width: 60 }),
  dam({ structure_: 2785, national_i: "ND00182", dam_name: "Hickson Dam", county: "Cass", datum_raw: "NAVD 88",
    max_dam_he: 18, max_pool_e: 894.08, streambed_: 877.25, emergenc_2: 887.08, normal_ele: 887.08, max_pool_v: 0,
    max_pool_a: 0, normal_vol: 452, normal_are: 187, dam_length: 100, top_width: 5 }),
  dam({ structure_: 3056, national_i: "ND00486", dam_name: "Mud Creek Impoundment", county: "Bowman", datum_raw: "",
    max_dam_he: 22, max_pool_e: 2919, streambed_: 2897, emergenc_2: 2916, normal_ele: 2912, max_pool_v: 380,
    max_pool_a: 55, normal_vol: 104.4, normal_are: 23, dam_length: 0, top_width: 12 }),
  dam({ structure_: 4978, national_i: "ND00001", dam_name: "Beaver Creek Diversion Dam", county: "Steele", datum_raw: "",
    max_dam_he: 16, max_pool_e: 1151, streambed_: 1135, emergenc_2: 1145, normal_ele: 1145, max_pool_v: 300,
    max_pool_a: 25, normal_vol: 50, normal_are: 8, dam_length: 210, top_width: 0 }),
  dam({ structure_: 2625, national_i: "ND00012", dam_name: "Local Datum Example", county: "Grand Forks", datum_raw: "Local",
    max_dam_he: 16.4, max_pool_e: 824.2, streambed_: 807.8, emergenc_2: 818.2, normal_ele: 818.2, max_pool_v: 166,
    max_pool_a: 0, normal_vol: 166, normal_are: 0, dam_length: 80, top_width: 1.167 }),
  dam({ structure_: 5976, national_i: "ND01032", dam_name: "", county: "Stark", datum_raw: "NGVD 29", vshift_ft: 1.7564,
    max_dam_he: 28, max_pool_e: 128, streambed_: 100, emergenc_2: 123, normal_ele: 120, max_pool_v: 324, max_pool_a: 36.46,
    normal_vol: 127, normal_are: 21.27, dam_length: 300, top_width: 16 }, { held: ["max_pool_e", "streambed_", "emergenc_2", "normal_ele"] }),
  dam({ structure_: 9999, national_i: "", dam_name: "No NID ID", county: "Cass", datum_raw: "Local", max_dam_he: 5 }),
];

export const DUGOUTS_FIXTURE = [
  { structure_: 301, county: "Morton", site_owner: "FIXTURE-SITE-OWNER", designer: "FIXTURE-DESIGNER", max_surfac: 56.9,
    max_storag: 230, max_depth: 8, max_height: 8, freeboard: 0, top_width: 12, national_i: "ND00450" },
  { structure_: 302, county: "Cass", site_owner: "FIXTURE-SITE-OWNER", designer: "", max_surfac: 3, max_storag: 45.6,
    max_depth: 0, max_height: 6.5, freeboard: 1, national_i: "ND09998" },
  { structure_: 303, county: "Cass", site_owner: "FIXTURE-SITE-OWNER", max_surfac: 2.51, max_storag: 50, max_depth: 9, national_i: "" },
];

const cell = (v) => {
  const s = v == null ? "" : typeof v === "number" ? v.toFixed(15).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, ".0") : String(v);
  return /[",\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
};
/** Records -> CSV text (header + rows, RFC 4180 quoting). `omit` drops columns (schema-change test). */
export function toCsv(fields, records, { omit = [] } = {}) {
  const f = fields.filter((x) => !omit.includes(x));
  return [f.join(","), ...records.map((r) => f.map((k) => cell(r[k])).join(","))].join("\r\n") + "\r\n";
}
export const damsCsv = (o) => toCsv(DAM_FIELDS, DAMS_FIXTURE, o);
export const dugoutsCsv = (o) => toCsv(DUG_FIELDS, DUGOUTS_FIXTURE, o);
export const fixtureCsvs = () => ({ damsCsv: damsCsv(), dugoutsCsv: dugoutsCsv() });
export { DAM_FIELDS, DUG_FIELDS, dam as fixtureDam };
