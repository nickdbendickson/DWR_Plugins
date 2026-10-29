// Temp folders for script tests, removed when the test file finishes, so
// `npm test` leaves nothing behind in the OS temp dir.
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after } from "node:test";

const made = [];

after(() => {
  for (const dir of made.splice(0)) rmSync(dir, { recursive: true, force: true });
});

/** mkdtempSync under os.tmpdir(), registered for removal after this file's tests. */
export function tempDir(prefix) {
  const dir = mkdtempSync(join(tmpdir(), prefix));
  made.push(dir);
  return dir;
}
