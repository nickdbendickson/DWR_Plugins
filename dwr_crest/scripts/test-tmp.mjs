/**
 * Temporary directories for the script tests, removed when the test process exits, so `npm test` leaves nothing
 * behind in the OS temp dir (it used to leave ~13 MB of brand-check-*, write-atomic-*, app-env-*, grok-og-* per run).
 */
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const made = [];
let hooked = false;

/** `mkdtempSync(join(os.tmpdir(), prefix))`, deleted (recursively) at process exit. */
export function tempDir(prefix) {
  const dir = mkdtempSync(join(tmpdir(), prefix));
  made.push(dir);
  if (!hooked) {
    hooked = true;
    process.on("exit", cleanupTempDirs);
  }
  return dir;
}

/** Remove every directory made by tempDir so far. Best effort: a busy dir (e.g. Windows cwd) is left. */
export function cleanupTempDirs() {
  while (made.length) {
    const dir = made.pop();
    try {
      rmSync(dir, { recursive: true, force: true });
    } catch {
      // ignore
    }
  }
}
