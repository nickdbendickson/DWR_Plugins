// vercel.json installs with `npm install --omit=dev`, then runs `npm run build:deploy`
// (vite build + scripts/migrate.mjs). Anything those import must be a runtime
// `dependency`, or the Vercel build fails while a local build (with dev deps) passes.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const read = (p) => readFileSync(new URL(`../${p}`, import.meta.url), "utf8");
const pkg = JSON.parse(read("package.json"));
const vercel = JSON.parse(read("vercel.json"));

/** Bare package names imported (not `import type`) by a source file. */
function imported(src) {
  const names = new Set();
  for (const m of src.matchAll(/^import\s+(?!type\s)[^"']*["']([^"'.][^"']*)["']/gm)) {
    const spec = m[1];
    if (spec.startsWith("node:")) continue;
    names.add(spec.startsWith("@") ? spec.split("/").slice(0, 2).join("/") : spec.split("/")[0]);
  }
  return names;
}

test("vercel.json still installs without dev deps and builds with build:deploy", () => {
  assert.match(vercel.installCommand, /--omit=dev/);
  assert.equal(vercel.buildCommand, "npm run build:deploy");
  assert.match(pkg.scripts["build:deploy"], /npm run build && npm run db:migrate/);
});

test("every package the build and migrate import is in dependencies, not devDependencies", () => {
  const files = [
    "vite.config.ts",
    "scripts/migrate.mjs",
    "scripts/with-app-env.mjs",
    "scripts/app-env-plugin.mjs",
    "scripts/grok-pwa-plugin.mjs",
    "scripts/migration-plan.mjs",
  ];
  const need = new Set(["vite"]);
  for (const f of files) for (const n of imported(read(f))) need.add(n);
  for (const n of need) {
    assert.ok(
      pkg.dependencies?.[n],
      `${n} is needed by the Vercel build but is not in dependencies`,
    );
  }
});
