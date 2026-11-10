#!/usr/bin/env node
/**
 * Engine parity check: verify that crest's engine.ts and its shared support files match the
 * canonical content hashes in engine-pin.json. The same pin file lives in nickdbendickson/damit
 * (damit_crest, where the disclaimer line differs) and in this repo.
 *
 * Usage:
 *   node scripts/check-engine-parity.mjs          # check (exit 1 on mismatch)
 *   node scripts/check-engine-parity.mjs --update  # recompute and write the pin
 *
 * The shared files (engine.ts and warnings.ts) are canonical in damit_crest. When the engine changes,
 * run --update there, then copy the same pin + files to crest and run --update here (patching the
 * disclaimer line). CI runs the check without --update.
 */
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const CREST_LIB = resolve(__dirname, "../src/lib/crest");
const PIN_PATH = resolve(__dirname, "engine-pin.json");

/** Files that must stay identical between crest and damit_crest (modulo the disclaimer line).
 *  The hash is computed AFTER normalizing the disclaimer line to a placeholder, so both repos
 *  produce the same hash despite the one intentional difference. */
const SHARED_FILES = ["engine.ts", "warnings.ts"];

const DISCLAIMER_PATTERNS = [
  /It is not an ND Dam Safety classification of record\./g,
  /It is not an official hazard classification of record\./g,
];
const DISCLAIMER_PLACEHOLDER = "It is not a classification of record.";

function normalizedHash(filePath) {
  let content = readFileSync(filePath, "utf-8");
  for (const pat of DISCLAIMER_PATTERNS) {
    content = content.replace(pat, DISCLAIMER_PLACEHOLDER);
  }
  return createHash("sha256").update(content).digest("hex");
}

const update = process.argv.includes("--update");

if (update) {
  const pin = {};
  for (const f of SHARED_FILES) {
    pin[f] = normalizedHash(join(CREST_LIB, f));
  }
  writeFileSync(PIN_PATH, JSON.stringify(pin, null, 2) + "\n");
  console.log(`engine-pin.json updated: ${JSON.stringify(pin)}`);
  process.exit(0);
}

// Check mode
let pin;
try {
  pin = JSON.parse(readFileSync(PIN_PATH, "utf-8"));
} catch {
  console.error("engine-pin.json not found. Run with --update first.");
  process.exit(1);
}

let ok = true;
for (const f of SHARED_FILES) {
  const actual = normalizedHash(join(CREST_LIB, f));
  const expected = pin[f];
  if (actual !== expected) {
    console.error(`MISMATCH: ${f}\n  expected ${expected}\n  got      ${actual}`);
    ok = false;
  } else {
    console.log(`OK: ${f}`);
  }
}
if (!ok) {
  console.error("\nEngine files have drifted from the pin. If this is intentional, run --update and sync the other repo.");
  process.exit(1);
}
console.log("All shared engine files match the pin.");
