// @ts-check
/**
 * The Crest build version stamped into every export (src/lib/crest/version.ts): "<package.json version>+<short git
 * SHA of HEAD>", e.g. "0.1.0+424065c", or "<version>+local" when git is not available (a tarball, a checkout with no
 * .git). Both Vite configs inject it as the `__CREST_VERSION__` define; under `node --test` nothing injects it and
 * version.ts falls back to "<version>+local" itself.
 *
 * It is deliberately NOT a build timestamp: two builds of the same tree give the same string, so the committed
 * desktop bundle and Crest.html stay reproducible (vite.desktop.config.ts) and a rebuild of an unchanged tree is a
 * no-op diff. A dirty tree is not marked either (the hash manifest in build-info.json already records the sources).
 */
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

/** package.json "version", or "0.0.0" when the field is missing. */
export function packageVersion(root = ROOT) {
  try {
    const pkg = /** @type {{ version?: unknown }} */ (JSON.parse(readFileSync(join(root, "package.json"), "utf8")));
    return typeof pkg.version === "string" && pkg.version ? pkg.version : "0.0.0";
  } catch {
    return "0.0.0";
  }
}

/** Short SHA of HEAD, or null when git (or the repository) is not there. */
export function gitShortSha(root = ROOT) {
  try {
    const sha = execFileSync("git", ["rev-parse", "--short=7", "HEAD"], { cwd: root, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
    return /^[0-9a-f]{7,}$/.test(sha) ? sha : null;
  } catch {
    return null;
  }
}

/** "<version>+<sha>" or "<version>+local". */
export function crestVersion(root = ROOT) {
  return `${packageVersion(root)}+${gitShortSha(root) ?? "local"}`;
}
