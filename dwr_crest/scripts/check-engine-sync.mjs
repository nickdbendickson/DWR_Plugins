#!/usr/bin/env node
/**
 * Engine sync check: crest and DamIT's damit_crest (nickdbendickson/damit) must carry the same engine files,
 * line for line, apart from the differences listed in engine-sync-allow.json.
 *
 * The old check (check-engine-parity.mjs + engine-pin.json) only compared each repo with a hash pinned in the same
 * repo, so the two repos could drift apart with both checks green (their pins had already differed), and it covered
 * engine.ts and warnings.ts only. This one reads both copies and compares them.
 *
 * The same script lives in both repos (crest: scripts/, damit: damit_crest/scripts/); it finds which repo it is in
 * and reads the other copy from:
 *   --other <dir>     a checkout of the other repo (its root). CI checks the other repo out with actions/checkout.
 *   (default)         raw.githubusercontent.com, branch --ref (default main).
 *
 * Usage:
 *   node scripts/check-engine-sync.mjs [--other ../damit] [--ref main]
 *
 * Exit 0 when every file matches; 1 on a difference that is not allowed, a missing file, or a stale allow entry
 * (one whose line is no longer in the file, so the list cannot rot).
 */
import { existsSync, readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

/** Repo layout: where src/lib/crest sits, from each repo's root. */
export const LIB = { crest: "src/lib/crest", damit: "damit_crest/src/lib/crest" };
export const GITHUB = { crest: "nickdbendickson/crest", damit: "nickdbendickson/damit" };

export function loadAllowList(path = join(__dirname, "engine-sync-allow.json")) {
  return JSON.parse(readFileSync(path, "utf8"));
}

/**
 * Compare one file's two copies. Each allow entry names a whole line as it reads in crest and in damit; a crest line
 * that equals an entry's `crest` text is compared as its `damit` text. Returns { ok, problems: string[] }.
 */
export function compareFile(file, crestText, damitText, allow) {
  const problems = [];
  const entries = allow.filter((a) => a.file === file);
  const crestLines = crestText.split("\n");
  const damitLines = damitText.split("\n");
  for (const e of entries) {
    if (!crestLines.includes(e.crest))
      problems.push(`${file}: stale allow entry, crest no longer has the line: ${e.crest}`);
    if (!damitLines.includes(e.damit))
      problems.push(`${file}: stale allow entry, damit no longer has the line: ${e.damit}`);
  }
  const map = new Map(entries.map((e) => [e.crest, e.damit]));
  const mapped = crestLines.map((l) => (map.has(l) ? map.get(l) : l));
  const n = Math.max(mapped.length, damitLines.length);
  let shown = 0;
  let diffs = 0;
  for (let i = 0; i < n; i++) {
    if (mapped[i] === damitLines[i]) continue;
    diffs++;
    if (shown < 5) {
      shown++;
      problems.push(
        `${file}:${i + 1} differs\n    crest: ${crestLines[i] === undefined ? "(end of file)" : crestLines[i]}\n    damit: ${damitLines[i] === undefined ? "(end of file)" : damitLines[i]}`,
      );
    }
  }
  if (diffs > shown) problems.push(`${file}: ${diffs - shown} more differing line(s)`);
  return { ok: problems.length === 0, problems };
}

/** Which repo is this script in? crest has scripts/ at the root; damit has it under damit_crest/. */
export function selfRepo(dir = __dirname) {
  return /damit_crest[\\/]scripts$/.test(dir) ? "damit" : "crest";
}

async function readOther(other, file, { otherDir, ref }) {
  if (otherDir) {
    const p = join(resolve(otherDir), LIB[other], file);
    return existsSync(p) ? readFileSync(p, "utf8") : null;
  }
  const url = `https://raw.githubusercontent.com/${GITHUB[other]}/${ref}/${LIB[other]}/${file}`;
  const r = await fetch(url);
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.text();
}

function arg(name) {
  const i = process.argv.indexOf(name);
  return i >= 0 ? process.argv[i + 1] : undefined;
}

export async function main() {
  const self = selfRepo();
  const other = self === "crest" ? "damit" : "crest";
  const root = resolve(__dirname, self === "crest" ? ".." : "../..");
  const { files, allow } = loadAllowList();
  const otherDir = arg("--other");
  const ref = arg("--ref") ?? "main";
  console.log(
    `Engine sync: ${self} (this checkout) vs ${other} (${otherDir ? resolve(otherDir) : `${GITHUB[other]}@${ref}`})`,
  );
  let bad = 0;
  for (const file of files) {
    const mine = join(root, LIB[self], file);
    const selfText = existsSync(mine) ? readFileSync(mine, "utf8") : null;
    const otherText = await readOther(other, file, { otherDir, ref });
    if (selfText === null || otherText === null) {
      console.error(`MISSING: ${file} is not in ${selfText === null ? self : other}`);
      bad++;
      continue;
    }
    const [crestText, damitText] = self === "crest" ? [selfText, otherText] : [otherText, selfText];
    const res = compareFile(file, crestText, damitText, allow);
    if (res.ok) console.log(`OK: ${file}`);
    else {
      bad++;
      for (const p of res.problems) console.error(`DIFF: ${p}`);
    }
  }
  if (bad) {
    console.error(
      `\n${bad} engine file(s) differ between crest and damit_crest. Port the change to the other repo, or, when the` +
        ` difference is meant to stay, add the line pair to scripts/engine-sync-allow.json in BOTH repos.`,
    );
    return 1;
  }
  console.log("Engine files match (allow-listed lines aside).");
  return 0;
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  main().then(
    (code) => process.exit(code),
    (e) => {
      console.error(e);
      process.exit(2);
    },
  );
}
