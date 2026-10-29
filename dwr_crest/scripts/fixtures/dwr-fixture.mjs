/**
 * DWR Dams-layer test fixture: a few records with numeric values copied from the 2026-10-29 Dams.zip
 * (structures 2801, 2753, 6192, 2785, 3056, 4978, 2625), written to a small .dbf in a .zip at test time.
 * Text fields that must never reach the bundle (owners, remarks, permits) carry made-up markers, not DWR text.
 */
import { deflateRawSync } from "node:zlib";

const MARK = "FIXTURE-OWNER-NOT-REAL";
const rec = (o) => ({
  primary_ow: MARK,
  other_resp: MARK,
  owner_type: "Private",
  remarks: "FIXTURE-REMARK",
  water_perm: "FIX123",
  private_da: "F",
  ...o,
});

export const FIXTURE_MARKERS = [MARK, "FIXTURE-REMARK", "FIX123", "Private"];

export const DWR_FIXTURE = [
  rec({ structure_: 2801, national_i: "ND00199", dam_name: "Swan Buffalo Detention Dam 12", county: "Cass", vertical_d: "NGVD 29",
    max_dam_he: 27.5, max_pool_e: 1070, streambed_: 1042.5, emergenc_2: 1066, normal_ele: 1051, max_pool_v: 1615.5,
    max_pool_a: 154.5, normal_vol: 87.8, normal_are: 29.8, dam_length: 1700, top_width: 12 }),
  rec({ structure_: 6192, national_i: "ND00145", dam_name: "Snake Creek Embankment", county: "McLean", vertical_d: "",
    max_dam_he: 0, max_pool_e: 0, streambed_: 0, emergenc_2: 0, normal_ele: 0, max_pool_v: 0, max_pool_a: 0,
    normal_vol: 0, normal_are: 0, dam_length: 12900, top_width: 0 }),
  rec({ structure_: 2753, national_i: "ND00145", dam_name: "Garrison Dam", county: "McLean", vertical_d: "",
    max_dam_he: 202, max_pool_e: 1875, streambed_: 1673, emergenc_2: 1850, normal_ele: 1837.5, max_pool_v: 24200000,
    max_pool_a: 383000, normal_vol: 18400000, normal_are: 315000, dam_length: 11300, top_width: 60 }),
  rec({ structure_: 2785, national_i: "ND00182", dam_name: "Hickson Dam", county: "Cass", vertical_d: "NAVD 88",
    max_dam_he: 18, max_pool_e: 894.08, streambed_: 877.25, emergenc_2: 887.08, normal_ele: 887.08, max_pool_v: 0,
    max_pool_a: 0, normal_vol: 452, normal_are: 187, dam_length: 100, top_width: 5 }),
  rec({ structure_: 3056, national_i: "ND00486", dam_name: "Mud Creek Impoundment", county: "Bowman", vertical_d: "",
    max_dam_he: 22, max_pool_e: 2919, streambed_: 2897, emergenc_2: 2916, normal_ele: 2912, max_pool_v: 380,
    max_pool_a: 55, normal_vol: 104.4, normal_are: 23, dam_length: 0, top_width: 12 }),
  rec({ structure_: 4978, national_i: "ND00001", dam_name: "Beaver Creek Diversion Dam", county: "Steele", vertical_d: "",
    max_dam_he: 16, max_pool_e: 1151, streambed_: 1135, emergenc_2: 1145, normal_ele: 1145, max_pool_v: 300,
    max_pool_a: 25, normal_vol: 50, normal_are: 8, dam_length: 210, top_width: 0 }),
  rec({ structure_: 2625, national_i: "ND00012", dam_name: "Local Datum Example", county: "Grand Forks", vertical_d: "Local",
    max_dam_he: 16.4, max_pool_e: 824.2, streambed_: 807.8, emergenc_2: 818.2, normal_ele: 818.2, max_pool_v: 166,
    max_pool_a: 0, normal_vol: 166, normal_are: 0, dam_length: 80, top_width: 1.167 }),
  rec({ structure_: 9999, national_i: "", dam_name: "No NID ID", county: "Cass", vertical_d: "Local", max_dam_he: 5 }),
];

const FIELD_DEFS = [
  ["structure_", "N", 9], ["county", "C", 30], ["dam_name", "C", 35], ["primary_ow", "C", 25], ["other_resp", "C", 128],
  ["max_dam_he", "N", 24], ["dam_length", "N", 24], ["max_pool_a", "N", 24], ["max_pool_v", "N", 24],
  ["max_pool_e", "N", 24], ["streambed_", "N", 24], ["emergenc_2", "N", 24], ["normal_vol", "N", 24],
  ["normal_are", "N", 24], ["normal_ele", "N", 24], ["owner_type", "C", 15], ["private_da", "L", 1],
  ["vertical_d", "C", 10], ["top_width", "N", 24], ["remarks", "C", 254], ["water_perm", "C", 9], ["national_i", "C", 7],
];

/** dBASE III file from records; `deleted` adds one '*' record that must be skipped. */
export function makeDbf(records, { deleted = true, omit = [] } = {}) {
  const defs = FIELD_DEFS.filter(([n]) => !omit.includes(n));
  const recLen = 1 + defs.reduce((s, [, , l]) => s + l, 0);
  const all = deleted ? [...records, { __deleted: true, ...records[0] }] : records;
  const headerLen = 32 + 32 * defs.length + 1;
  const buf = Buffer.alloc(headerLen + recLen * all.length + 1, 0x20);
  buf.fill(0, 0, headerLen);
  buf[0] = 0x03;
  buf.writeUInt32LE(all.length, 4);
  buf.writeUInt16LE(headerLen, 8);
  buf.writeUInt16LE(recLen, 10);
  defs.forEach(([name, type, len], i) => {
    const p = 32 + 32 * i;
    buf.write(name, p, "latin1");
    buf[p + 11] = type.charCodeAt(0);
    buf[p + 16] = len;
  });
  buf[headerLen - 1] = 0x0d;
  all.forEach((r, k) => {
    let o = headerLen + k * recLen;
    buf[o++] = r.__deleted ? 0x2a : 0x20;
    for (const [name, type, len] of defs) {
      const v = r[name] ?? "";
      let s = type === "N" && v !== "" ? Number(v).toFixed(15) : String(v);
      s = s.slice(0, len);
      if (type === "N") s = s.padStart(len, " ");
      buf.write(s.padEnd(len, " "), o, "latin1");
      o += len;
    }
  });
  buf[buf.length - 1] = 0x1a;
  return buf;
}

/** Zip with the given entries ({ name: Buffer }), deflated. */
export function makeZip(entries) {
  const locals = [];
  const centrals = [];
  let offset = 0;
  for (const [name, data] of Object.entries(entries)) {
    const comp = deflateRawSync(data);
    const n = Buffer.from(name, "utf8");
    const lh = Buffer.alloc(30);
    lh.writeUInt32LE(0x04034b50, 0);
    lh.writeUInt16LE(20, 4);
    lh.writeUInt16LE(8, 8);
    lh.writeUInt32LE(comp.length, 18);
    lh.writeUInt32LE(data.length, 22);
    lh.writeUInt16LE(n.length, 26);
    const ch = Buffer.alloc(46);
    ch.writeUInt32LE(0x02014b50, 0);
    ch.writeUInt16LE(20, 4);
    ch.writeUInt16LE(20, 6);
    ch.writeUInt16LE(8, 10);
    ch.writeUInt32LE(comp.length, 20);
    ch.writeUInt32LE(data.length, 24);
    ch.writeUInt16LE(n.length, 28);
    ch.writeUInt32LE(offset, 42);
    locals.push(lh, n, comp);
    centrals.push(ch, n);
    offset += 30 + n.length + comp.length;
  }
  const cd = Buffer.concat(centrals);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(Object.keys(entries).length, 8);
  end.writeUInt16LE(Object.keys(entries).length, 10);
  end.writeUInt32LE(cd.length, 12);
  end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, cd, end]);
}

export function fixtureZip(opts) {
  return makeZip({ "Dams.dbf": makeDbf(DWR_FIXTURE, opts), "Dams.prj": Buffer.from("GEOGCS[\"fixture\"]") });
}
