/**
 * Static build of the Crest desk for the desktop app (crest-desktop/). It is separate from vite.config.ts, so the web
 * deploy (TanStack Start + Nitro/Vercel) is unchanged. Output: crest-desktop/crest/web/ — plain files, relative paths,
 * Leaflet / leaflet-draw / marker icons bundled, no CDN, no Google Fonts. Basemap tiles need the network, and so do the
 * on-request lookups (3DEP terrain, 911 points, PLSS sections), each one request that is then saved with the dam.
 *
 * Build with `npm run build:desktop`. The bundle is committed so `python run.py` needs no Node. build-info.json lists
 * every repo source file that went into it with its SHA-256; the desktop tests recompute those hashes and fail when the
 * committed bundle is older than the sources (rebuild and commit it).
 *
 * Build after `npm ci`, so node_modules matches package-lock.json. The build is reproducible from the lockfile (two
 * builds give identical files); a bundle built after a plain `npm install` picked up React 19.3.1 instead of the
 * locked 19.3.0, and a later lockfile build then rewrote the assets with small diffs.
 */
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import type { Plugin } from "vite";
import { defineConfig, loadEnv } from "vite";
import viteReact from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { crestVersion } from "./scripts/crest-version.mjs";
import { contentSecurityPolicy, SECURITY_HEADERS } from "./src/lib/crest/csp.ts";

const r = (p: string) => fileURLToPath(new URL(p, import.meta.url));
const ROOT = r("./");

/** SHA-256 of a text file with CRLF folded to LF, so a Windows checkout hashes the same as Linux. */
function sha256(file: string): string {
  return createHash("sha256")
    .update(readFileSync(file, "utf8").replace(/\r\n/g, "\n"))
    .digest("hex");
}

/** Writes build-info.json. */
function buildInfoPlugin(): Plugin {
  return {
    name: "crest-desktop:build-info",
    apply: "build",
    generateBundle() {
      this.emitFile({
        type: "asset",
        fileName: "build-info.json",
        source: buildInfo(this.getModuleIds()),
      });
    },
  };
}

/** build-info JSON: the repo files (not node_modules) that went into the bundle, with their hashes. */
function buildInfo(moduleIds: Iterable<string>, builtBy = "npm run build:desktop"): string {
  const sources: Record<string, string> = {};
  for (const id of moduleIds) {
    const file = id.split("?")[0];
    if (!file.startsWith(ROOT) || file.includes("/node_modules/") || file.includes("\0")) continue;
    const rel = relative(ROOT, file).split("\\").join("/");
    sources[rel] = sha256(file);
  }
  const sorted = Object.fromEntries(Object.entries(sources).sort(([a], [b]) => a.localeCompare(b)));
  const pkg = JSON.parse(readFileSync(r("./package.json"), "utf8")) as { name?: string };
  return JSON.stringify({ app: pkg.name ?? "crest", builtBy, sources: sorted }, null, 2) + "\n";
}

/** The build's VITE_CREST_TILE_URL (basemap.ts), read the way Vite reads it for the bundle, so the policy allows it. */
function configuredTileUrl(mode: string): string | undefined {
  return loadEnv(mode, r("./src/desktop"), "VITE_").VITE_CREST_TILE_URL || undefined;
}

/** `sha384-…` / `sha256-…` of a string or bytes, as Subresource Integrity and CSP hash sources write it. */
function integrity(algo: "sha256" | "sha384", data: string | Uint8Array): string {
  return `${algo}-${createHash(algo).update(data).digest("base64")}`;
}

/**
 * Desktop bundle security files (2026-11-13):
 *  - security-headers.json: the Content-Security-Policy (src/lib/crest/csp.ts) and the other security headers; the
 *    Python server (crest-desktop/crest/__main__.py) sends them with every response, so the policy has one source.
 *  - `integrity="sha384-…"` on index.html's entry script, its preloads and its stylesheet (Subresource Integrity): the
 *    window refuses a bundle file that does not match the index.html it shipped with. .gitattributes keeps git from
 *    rewriting line endings in the bundle, which would change the hashes on a Windows checkout.
 */
function desktopSecurityPlugin(mode: string): Plugin {
  return {
    name: "crest-desktop:security",
    apply: "build",
    enforce: "post",
    // After the files are written: Vite still rewrites the entry chunk (its preload list) after generateBundle, so the
    // hashes are taken from the bytes on disk, the bytes the window will load.
    writeBundle(options) {
      const out = options.dir ?? r("./crest-desktop/crest/web");
      const index = join(out, "index.html");
      let n = 0;
      const text = readFileSync(index, "utf8").replace(
        /<(?:script|link)\b[^>]*?\s(?:src|href)="\.\/(assets\/[^"]+\.(?:js|css))"[^>]*>/g,
        (tag, name: string) => {
          n++;
          return tag.replace(
            /\s*\/?>$/,
            (end: string) =>
              ` integrity="${integrity("sha384", readFileSync(join(out, name)))}"${end}`,
          );
        },
      );
      if (!n)
        throw new Error(
          "security: index.html has no script or stylesheet to put an integrity hash on",
        );
      writeFileSync(index, text);
      const headers = {
        "Content-Security-Policy": contentSecurityPolicy({ tileUrl: configuredTileUrl(mode) }),
        ...SECURITY_HEADERS,
      };
      writeFileSync(join(out, "security-headers.json"), JSON.stringify(headers, null, 2) + "\n");
    },
  };
}

/** Chunks the groups below name; kept at their own names (a group chunk is not a dynamic entry). */
const LAZY_CHUNKS = ["nd_structures", "nd_dams", "geotiff"];

const desktop = defineConfig({
  root: r("./src/desktop"),
  base: "./",
  publicDir: false,
  // Build version for the exports (src/lib/crest/version.ts): package.json version + git SHA of HEAD, never a
  // timestamp, so the committed bundle and Crest.html stay reproducible from the same tree.
  define: { __CREST_VERSION__: JSON.stringify(crestVersion()) },
  resolve: { alias: { "@": r("./src") } },
  plugins: [tailwindcss(), viteReact(), buildInfoPlugin(), desktopSecurityPlugin("production")],
  build: {
    outDir: r("./crest-desktop/crest/web"),
    emptyOutDir: true,
    sourcemap: false,
    modulePreload: { polyfill: false },
    rollupOptions: {
      input: { index: r("./src/desktop/index.html"), engine: r("./src/desktop/engine-entry.ts") },
      preserveEntrySignatures: "exports-only",
      output: {
        // Stable names keep the committed bundle's diffs readable. Rollup names a shared chunk after one of its
        // modules, and which one moves with small source edits (samples.js became export.js), so the one chunk
        // index and engine share always keeps the name it was first committed under.
        entryFileNames: "assets/[name].js",
        chunkFileNames: (chunk) =>
          chunk.isDynamicEntry || LAZY_CHUNKS.includes(chunk.name)
            ? "assets/[name].js"
            : "assets/samples.js",
        assetFileNames: "assets/[name][extname]",
        // The data bundles (nd_dams, nd_dams_dwr, nd_structures: 2.2 MB) and geotiff stay their own chunks, loaded
        // only when the picker, the Batch view or a results import asks for them, whatever else shares a module with
        // them. Rolldown's `codeSplitting.groups` is the manualChunks of Vite 8 (Rollup's manualChunks is deprecated
        // there); the test is on the module id. `priority` puts a data module in its group before the default
        // sharing rules could fold it into the desk chunk.
        codeSplitting: {
          groups: [
            {
              name: "nd_structures",
              test: /[\\/]src[\\/]lib[\\/]crest[\\/]data[\\/]nd_structures\.json/,
              priority: 30,
            },
            {
              name: "nd_dams",
              test: /[\\/]src[\\/]lib[\\/]crest[\\/]data[\\/]nd_dams(_dwr)?\.json/,
              priority: 30,
            },
            { name: "geotiff", test: /[\\/]node_modules[\\/]geotiff[\\/]/, priority: 20 },
          ],
        },
      },
    },
  },
});

export default desktop;
