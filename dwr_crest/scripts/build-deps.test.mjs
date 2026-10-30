// vercel.json installs with `npm install --omit=dev`, then runs `npm run build:deploy`
// (vite build + scripts/migrate.mjs). Anything the build tooling or the app imports
// must be a runtime `dependency`, or the Vercel build fails while a local build
// (with dev deps installed) passes.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { builtinModules } from "node:module";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("..", import.meta.url));
const read = (p) => readFileSync(join(ROOT, p), "utf8");
const pkg = JSON.parse(read("package.json"));
const vercel = JSON.parse(read("vercel.json"));
const BUILTINS = new Set(builtinModules);

/** Package name of a bare specifier, or null for relative, alias, builtin or virtual ones. */
function packageOf(spec) {
  if (/^(\.|\/|@\/|~\/|#|virtual:|node:)/.test(spec)) return null;
  const name = spec.startsWith("@") ? spec.split("/").slice(0, 2).join("/") : spec.split("/")[0];
  return BUILTINS.has(name) ? null : name.replace(/\?.*$/, "");
}

/** Packages a source file imports at runtime (`import type` / `export type` are erased, so skipped). */
function imported(src) {
  const specs = [];
  for (const m of src.matchAll(
    /^\s*(?:import|export)\s+(type\s)?[^;"']*?\bfrom\s*["']([^"']+)["']/gm,
  ))
    if (!m[1]) specs.push(m[2]);
  for (const m of src.matchAll(/^\s*import\s*["']([^"']+)["']/gm)) specs.push(m[1]);
  for (const m of src.matchAll(/\bimport\(\s*["']([^"']+)["']\s*\)/g)) specs.push(m[1]);
  for (const m of src.matchAll(/^\s*@(?:import|plugin)\s+["']([^"']+)["']/gm)) specs.push(m[1]);
  return new Set(specs.map(packageOf).filter(Boolean));
}

/** App source files under src/, excluding tests (they never reach the bundle). */
function appFiles(dir = join(ROOT, "src")) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    if (e.isDirectory()) return appFiles(p);
    return /\.(m?[jt]sx?|css)$/.test(e.name) && !/\.test\./.test(e.name) ? [p] : [];
  });
}

function missing(files) {
  const out = [];
  for (const f of files)
    for (const n of imported(readFileSync(f, "utf8")))
      if (!pkg.dependencies?.[n])
        out.push(
          `${n} (imported by ${relative(ROOT, f)}) is ${pkg.devDependencies?.[n] ? "in devDependencies only" : "not in dependencies"}`,
        );
  return [...new Set(out)];
}

test("the import scanner sees multi-line, side-effect, dynamic and CSS imports, and skips types", () => {
  const src = [
    'import {\n  a,\n  b,\n} from "multi-line";',
    'import "side-effect/dist/x.css";',
    'const m = await import("dynamic");',
    'import type { T } from "type-only";',
    'export type { U } from "type-reexport";',
    'export { v } from "@scope/reexport/sub";',
    'import x from "./local"; import y from "@/alias"; import z from "node:fs"; import w from "path";',
    '@import "tailwindcss";',
  ].join("\n");
  assert.deepEqual(
    [...imported(src)].sort(),
    ["@scope/reexport", "dynamic", "multi-line", "side-effect", "tailwindcss"].sort(),
  );
});

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
  ].map((f) => join(ROOT, f));
  assert.deepEqual(missing(files), []);
  assert.ok(pkg.dependencies?.vite, "vite runs the build");
});

test("every package the app imports at runtime under src/ is in dependencies, not devDependencies", () => {
  const files = appFiles();
  assert.ok(files.length > 20, `scanned ${files.length} files`);
  const seen = new Set(files.flatMap((f) => [...imported(readFileSync(f, "utf8"))]));
  for (const n of ["react", "leaflet", "@tanstack/react-router"])
    assert.ok(seen.has(n), `scanner found ${n}`);
  assert.deepEqual(missing(files), []);
});
