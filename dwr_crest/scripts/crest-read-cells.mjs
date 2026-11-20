/**
 * The crest read at several terrain cell sizes: for every structure with a DWR NAVD 88 top of dam, read the crest the
 * way the batch does (the highest cell within 30 m of the inventory point, batch.ts deriveFromDem) from USGS 3DEP at
 * 10, 5, 2 and 1 m, and print how the reads differ from each other and from DWR's figure.
 *
 *   node --experimental-strip-types scripts/crest-read-cells.mjs [--limit 99] [--json out.json] [--from out.json]
 *
 * --from skips the reads and summarizes a file an earlier run wrote. It is here to repeat the test in
 * docs/accuracy-envelope.md ("Finer terrain cells"); nothing in the desk uses it. Needs the network (3DEP); the 1 and
 * 2 m requests fail now and then, and a dam with a failed read is left out of the comparison.
 */
import { readFileSync, writeFileSync } from "node:fs";

export const CELLS = [10, 5, 2, 1];

const med = (v) => {
  const s = [...v].sort((x, y) => x - y);
  return s.length ? s[Math.floor(s.length / 2)] : NaN;
};
const q = (v, p) => {
  const s = [...v].sort((x, y) => x - y);
  return s.length ? s[Math.min(s.length - 1, Math.floor(p * s.length))] : NaN;
};
const f = (x) => (Number.isFinite(x) ? x.toFixed(2) : "-");

/** The comparison printed at the end: differences from the 10 m read, 2 m against 1 m, by height, and against DWR. */
export function summarize(rows) {
  const full = rows.filter((r) => CELLS.every((c) => Number.isFinite(r[`c${c}`])));
  const out = [`${rows.length} dams read, ${full.length} with all ${CELLS.length} reads`];
  for (const k of ["c5", "c2", "c1"]) {
    const d = full.map((r) => r[k] - r.c10);
    out.push(`${k} - c10 (ft): median ${f(med(d))}, p10 ${f(q(d, 0.1))}, p90 ${f(q(d, 0.9))}, max |d| ${f(Math.max(...d.map(Math.abs)))}`);
  }
  const d21 = full.map((r) => r.c2 - r.c1);
  out.push(`c2 - c1 (ft): median |d| ${f(med(d21.map(Math.abs)))}, max |d| ${f(Math.max(...d21.map(Math.abs)))}`);
  out.push(`1 m read more than 1 ft above the 10 m read: ${full.filter((r) => r.c1 - r.c10 > 1).length}; more than 0.25 ft below: ${full.filter((r) => r.c1 - r.c10 < -0.25).length}`);
  for (const [lo, hi] of [[0, 15], [15, 30], [30, Infinity]]) {
    const g = full.filter((r) => r.heightFt >= lo && r.heightFt < hi);
    out.push(`height ${lo} to ${hi} ft: ${g.length} dams, median (c1 - c10) ${f(med(g.map((r) => r.c1 - r.c10)))} ft`);
  }
  // Against DWR's top of dam, only where a read is within 5 ft of it: further off, the point or the datum is wrong.
  for (const k of ["c10", "c1"]) out.push(`${k} within 5 ft of DWR's top of dam: ${full.filter((r) => Math.abs(r[k] - r.topNavdFt) < 5).length} of ${full.length}`);
  for (const dat of ["NAVD88", "all"]) {
    const close = full.filter((r) => (dat === "all" || r.datum === dat) && Math.abs(r.c1 - r.topNavdFt) < 5);
    for (const k of ["c10", "c1"]) out.push(`${dat}, the ${close.length} with the 1 m read within 5 ft of DWR: median |${k} - DWR| ${f(med(close.map((r) => Math.abs(r[k] - r.topNavdFt))))} ft`);
  }
  return out;
}

async function main(argv) {
  const o = {};
  for (let i = 0; i < argv.length; i++) if (argv[i].startsWith("--")) o[argv[i].slice(2)] = argv[i + 1]?.startsWith("--") || argv[i + 1] == null ? true : argv[++i];
  let rows = [];
  if (typeof o.from === "string") rows = JSON.parse(readFileSync(o.from, "utf8"));
  else {
    // Loaded here so the summary (and its test) needs neither the TypeScript loader nor the inventory.
    const { deriveFromDem, loadStructures, nearRequest } = await import("../src/lib/crest/batch.ts");
    const { fetchDem } = await import("../src/lib/crest/outline.ts");
    const { nd, structures } = await loadStructures();
    const dwrOf = (s) => nd.dwr.get(s.id) ?? (s.dwrId ? nd.dwr.get(`DWR-${s.dwrId}`) : undefined);
    const todo = structures.filter((s) => Number.isFinite(dwrOf(s)?.topNavdFt) && Number.isFinite(s.lon) && Number.isFinite(s.lat));
    console.log(`${todo.length} structures with a DWR NAVD 88 top of dam`);
    for (const s of todo.slice(0, Number(o.limit ?? todo.length))) {
      const d = dwrOf(s);
      const row = { id: s.id, heightFt: d.heightFt ?? s.heightFt, topNavdFt: d.topNavdFt, datum: d.datum };
      for (const c of CELLS) {
        try {
          row[`c${c}`] = deriveFromDem(await fetchDem(nearRequest(s.lon, s.lat, 0.1, c)), s.lon, s.lat)?.crestFt ?? null;
        } catch (e) {
          row[`c${c}`] = null;
          row.err = String(e.message).slice(0, 80);
        }
      }
      rows.push(row);
      console.log(JSON.stringify(row));
      if (typeof o.json === "string") writeFileSync(o.json, JSON.stringify(rows));
    }
  }
  for (const line of summarize(rows)) console.log(line);
}

if (import.meta.url === `file://${process.argv[1]}`) await main(process.argv.slice(2));
