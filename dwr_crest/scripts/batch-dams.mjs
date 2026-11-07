#!/usr/bin/env node
/**
 * Batch screen from the command line: the same per-structure path as the desk's Batch screen (src/lib/crest/batch.ts),
 * for runs too big for a browser tab (statewide is ~10,000 structures and a day or more of 3DEP requests). Progress is
 * appended to a JSON-lines file, so a stopped run picks up where it left off when started again with the same --out.
 *
 *   node --experimental-strip-types scripts/batch-dams.mjs --out <dir> (--state | --county Cass[,Barnes] | --watershed "Maple" | --ids ND00199,DWR-4031)
 *        [--kinds nid,dam,dugout,unknown] [--statuses NID,Intact,Dugout,...] [--concurrency 2] [--limit N] [--list]
 *        [--dem /path/ND_1m_EPSG26914.vrt [--dem-feet] [--gdalwarp <path>]]
 *
 * --dem reads the terrain from a local raster (the statewide 1 m mosaic) through gdalwarp instead of the 3DEP service
 * (scripts/local-dem.mjs): no per-dam download, and the crest is read on a 2 m grid. The VRT must be in metres
 * (--dem-feet for a feet DEM). 911 address points still come from the ND GIS Hub.
 *
 * Writes <dir>/batch.jsonl (one row per structure, appended as each finishes) and <dir>/batch.csv (sorted, rewritten
 * at the end and every 25 rows). --list prints the selection and the time estimate without running anything.
 * Needs the 3DEP terrain service and the ND GIS Hub 911 service.
 */
import { appendFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { localDemFetcher } from "./local-dem.mjs";
import { DEFAULT_STATUSES, batchCsv, estimateSeconds, loadStructures, runBatch, selectStructures } from "../src/lib/crest/batch.ts";

export function parseArgs(argv) {
  const o = { kinds: ["nid", "dam", "dugout", "unknown"], statuses: [...DEFAULT_STATUSES], concurrency: 2 };
  const list = (v) => v.split(",").map((x) => x.trim()).filter(Boolean);
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--state") o.by = "state";
    else if (a === "--list") o.list = true;
    else if (a === "--county") Object.assign(o, { by: "county", counties: list(argv[++i]) });
    else if (a === "--watershed") Object.assign(o, { by: "watershed", watersheds: list(argv[++i]) });
    else if (a === "--ids") Object.assign(o, { by: "pick", ids: list(argv[++i]) });
    else if (a === "--kinds") o.kinds = list(argv[++i]);
    else if (a === "--statuses") o.statuses = list(argv[++i]);
    else if (a === "--concurrency") o.concurrency = Number(argv[++i]);
    else if (a === "--limit") o.limit = Number(argv[++i]);
    else if (a === "--out") o.out = argv[++i];
    else if (a === "--dem") o.dem = argv[++i];
    else if (a === "--dem-feet") o.demFeet = true;
    else if (a === "--gdalwarp") o.gdalwarp = argv[++i];
    else throw new Error(`unexpected ${a}`);
  }
  if (!o.by) throw new Error("pick one of --state, --county, --watershed, --ids");
  if (!o.out && !o.list) throw new Error("--out <dir> is required");
  return o;
}

export async function main(argv, log = console.log) {
  const o = parseArgs(argv);
  const { nd, structures } = await loadStructures();
  let sel = selectStructures(structures, { by: o.by, ids: o.ids, counties: o.counties, watersheds: o.watersheds, kinds: o.kinds, statuses: o.statuses });
  if (o.limit) sel = sel.slice(0, o.limit);
  const est = estimateSeconds(sel);
  log(`${sel.length} structures selected; about ${(est / 3600).toFixed(1)} h at one at a time (${o.concurrency} side by side: ~${(est / 3600 / o.concurrency).toFixed(1)} h).`);
  if (o.list) {
    for (const s of sel) log(`${s.id}\t${s.kind}\t${s.county ?? ""}\t${s.huc8 ?? ""}\tH ${s.heightFt ?? "—"}\tV ${s.storageAcFt ?? "—"}`);
    return { selected: sel.length };
  }
  mkdirSync(o.out, { recursive: true });
  const jsonl = join(o.out, "batch.jsonl");
  const rows = existsSync(jsonl)
    ? readFileSync(jsonl, "utf8")
        .split("\n")
        .filter(Boolean)
        .map((l) => JSON.parse(l))
    : [];
  // Resume skips only dams that finished; a "failed" row (e.g. the 3DEP/911 service was down)
  // is retried, not treated as done. batch.jsonl stays append-only; the CSV dedupes by id with
  // the last row winning, so a retried dam's new row supersedes its old failed one.
  const skip = new Set(rows.filter((r) => r.status !== "failed").map((r) => r.id));
  const retry = rows.filter((r) => r.status === "failed").length;
  if (skip.size || retry) log(`resuming: ${skip.size} already done, ${retry} failed will be retried`);
  const writeCsv = () =>
    writeFileSync(join(o.out, "batch.csv"), batchCsv([...new Map(rows.map((r) => [r.id, r])).values()]));
  const local = o.dem ? localDemFetcher(o.dem, { gdalwarp: o.gdalwarp, zFactor: o.demFeet ? 0.3048 : 1 }) : null;
  if (local) log(`terrain: ${o.dem} (local, through gdalwarp)`);
  await runBatch(sel, {
    nd,
    ...(local ? { fetchDem: local, nearCellM: 2, demLabel: "local DEM" } : {}),
    concurrency: o.concurrency,
    skip,
    onRow: (row, _dam, done, total) => {
      rows.push(row);
      appendFileSync(jsonl, JSON.stringify(row) + "\n");
      log(`[${done}/${total}] ${row.id.padEnd(12)} ${row.status === "done" ? `${row.screen ?? ""} · ${row.triage?.label ?? ""}` : `${row.status}: ${row.reason}`} (${row.seconds}s)`);
      if (done % 25 === 0) writeCsv();
    },
  });
  writeCsv();
  log(`wrote ${join(o.out, "batch.csv")}`);
  return { selected: sel.length, rows: rows.length };
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main(process.argv.slice(2)).catch((e) => {
    console.error(e instanceof Error ? e.stack : String(e));
    process.exit(1);
  });
}
