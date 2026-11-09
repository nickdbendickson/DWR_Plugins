/**
 * Static build of the Crest desk for the desktop app (crest-desktop/). It is separate from vite.config.ts, so the web
 * deploy (TanStack Start + Nitro/Vercel) is unchanged. Output: crest-desktop/crest/web/ — plain files, relative paths,
 * Leaflet / leaflet-draw / marker icons bundled, no CDN, no Google Fonts. Basemap tiles need the network, and so do the
 * on-request lookups (3DEP terrain, 911 points, PLSS sections), each one request that is then saved with the dam.
 *
 * `--mode singlefile` (`npm run build:crest-html`) builds the same desk into ONE self-contained file, Crest.html at the
 * repo root (JS, CSS, Leaflet images and the favicon inlined), with the same source hashes in an HTML comment so
 * `npm test` catches a stale copy.
 *
 * Build with `npm run build:desktop`. The bundle is committed so `python run.py` needs no Node. build-info.json lists
 * every repo source file that went into it with its SHA-256; the desktop tests recompute those hashes and fail when the
 * committed bundle is older than the sources (rebuild and commit it).
 *
 * Build after `npm ci`, so node_modules matches package-lock.json. The build is reproducible from the lockfile (two
 * builds give identical files); a bundle built after a plain `npm install` picked up React 19.3.1 instead of the
 * locked 19.3.0, and a later lockfile build then rewrote Crest.html and the assets with small diffs.
 */
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { relative } from "node:path";
import { fileURLToPath } from "node:url";
import { gzipSync } from "node:zlib";
import type { Plugin } from "vite";
import { defineConfig } from "vite";
import viteReact from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { crestVersion } from "./scripts/crest-version.mjs";

const r = (p: string) => fileURLToPath(new URL(p, import.meta.url));
const ROOT = r("./");

/** SHA-256 of a text file with CRLF folded to LF, so a Windows checkout hashes the same as Linux. */
function sha256(file: string): string {
  return createHash("sha256").update(readFileSync(file, "utf8").replace(/\r\n/g, "\n")).digest("hex");
}

/** Writes build-info.json. */
function buildInfoPlugin(): Plugin {
  return {
    name: "crest-desktop:build-info",
    apply: "build",
    generateBundle() {
      this.emitFile({ type: "asset", fileName: "build-info.json", source: buildInfo(this.getModuleIds()) });
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

/**
 * The three data bundles (2.2 MB of JSON) inside the one-file Crest.html: gzip + base64 (about 450 KB) instead of the
 * JSON flattened into the page's one script, so the file is about 1.8 MB smaller and opening the desk no longer
 * carries the 1.64 MB structures table the Batch view alone needs. src/lib/crest/data/packed.ts is the empty
 * stand-in every other build sees; here it is replaced by a module whose default export carries each bundle packed,
 * and the JSON modules data-bundle.ts would otherwise import are replaced by an empty stub so they are not inlined
 * as well. data-bundle.ts inflates a bundle on first use with DecompressionStream, or fflate's gunzip (already a
 * dependency) where a browser lacks it.
 */
const PACKED_BUNDLES = ["nd_dams", "nd_dams_dwr", "nd_structures"] as const;
const OMITTED_JSON = "\0crest:json-omitted";
function packedDataPlugin(): Plugin {
  const stub = r("./src/lib/crest/data/packed.ts");
  const loader = r("./src/lib/crest/data-bundle.ts");
  return {
    name: "crest-desktop:packed-data",
    apply: "build",
    enforce: "pre",
    resolveId(source, importer) {
      // Only data-bundle.ts's own fallback imports are stubbed; a test or script importing a bundle is left alone.
      if (importer && importer.split("?")[0] === loader && /^\.\/data\/nd_[a-z_]+\.json$/.test(source)) return OMITTED_JSON;
      return null;
    },
    load(id) {
      if (id === OMITTED_JSON) return "export default null;\n";
      if (id.split("?")[0] !== stub) return null;
      const entries = PACKED_BUNDLES.map((name) => {
        const json = readFileSync(r(`./src/lib/crest/data/${name}.json`), "utf8");
        return `  ${name}: ${JSON.stringify(gzipSync(Buffer.from(json, "utf8"), { level: 9 }).toString("base64"))},`;
      });
      return `const packed = {\n${entries.join("\n")}\n};\nexport default packed;\n`;
    },
  };
}

/** Inline the one JS chunk and the CSS into the HTML and emit it as Crest.html (what vite-plugin-singlefile does). */
function singleFilePlugin(): Plugin {
  return {
    name: "crest-desktop:single-file",
    apply: "build",
    enforce: "post",
    generateBundle(_options, bundle) {
      const html = Object.values(bundle).find((f) => f.type === "asset" && f.fileName.endsWith(".html"));
      if (!html || html.type !== "asset") throw new Error("single-file: no HTML in the bundle");
      let text = String(html.source);
      for (const [name, file] of Object.entries(bundle)) {
        if (file === html) continue;
        const ref = new RegExp(`<script[^>]*src="\\./${name.replace(/[.]/g, "\\.")}"[^>]*></script>|<link[^>]*href="\\./${name.replace(/[.]/g, "\\.")}"[^>]*>`);
        if (file.type === "chunk") {
          // With code splitting off Vite leaves its preload-deps placeholder unreplaced; there is nothing to preload.
          const code = file.code.replace(/__VITE_PRELOAD__/g, "void 0").replace(/<\/(script)/gi, "<\\/$1");
          text = text.replace(ref, () => `<script type="module">\n${code}</script>`);
        } else if (name.endsWith(".css")) {
          text = text.replace(ref, () => `<style>\n${String(file.source)}</style>`);
        } else {
          // An asset the HTML links directly (the favicon): Vite does not inline those, so use a data: URL.
          const type = name.endsWith(".svg") ? "image/svg+xml" : name.endsWith(".png") ? "image/png" : "";
          if (!type || !text.includes(`"./${name}"`)) throw new Error(`single-file: ${name} was not inlined`);
          const data = Buffer.from(file.source).toString("base64");
          text = text.split(`"./${name}"`).join(`"data:${type};base64,${data}"`);
        }
        delete bundle[name];
      }
      if (/(src|href)="\.\/assets\//.test(text)) throw new Error("single-file: a reference to ./assets/ is left");
      const info = buildInfo(this.getModuleIds(), "npm run build:crest-html").replace(/--/g, "- -");
      delete bundle[html.fileName];
      writeFileSync(
        r("./Crest.html"),
        `<!-- Generated by \`npm run build:crest-html\` from src/ (the same desk and engine as the web app and the desktop app). Do not edit.\ncrest-build-info ${info}-->\n${text}`,
      );
    },
  };
}

export default defineConfig(({ mode }) => (mode === "singlefile" ? singleFile : desktop));

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
  plugins: [tailwindcss(), viteReact(), buildInfoPlugin()],
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
        chunkFileNames: (chunk) => (chunk.isDynamicEntry || LAZY_CHUNKS.includes(chunk.name) ? "assets/[name].js" : "assets/samples.js"),
        assetFileNames: "assets/[name][extname]",
        // The data bundles (nd_dams, nd_dams_dwr, nd_structures: 2.2 MB) and geotiff stay their own chunks, loaded
        // only when the picker, the Batch view or a results import asks for them, whatever else shares a module with
        // them. Rolldown's `codeSplitting.groups` is the manualChunks of Vite 8 (Rollup's manualChunks is deprecated
        // there); the test is on the module id. `priority` puts a data module in its group before the default
        // sharing rules could fold it into the desk chunk.
        codeSplitting: {
          groups: [
            { name: "nd_structures", test: /[\\/]src[\\/]lib[\\/]crest[\\/]data[\\/]nd_structures\.json/, priority: 30 },
            { name: "nd_dams", test: /[\\/]src[\\/]lib[\\/]crest[\\/]data[\\/]nd_dams(_dwr)?\.json/, priority: 30 },
            { name: "geotiff", test: /[\\/]node_modules[\\/]geotiff[\\/]/, priority: 20 },
          ],
        },
      },
    },
  },
});

/** Crest.html: the desk alone (no engine facade), everything in one file. */
const singleFile = defineConfig({
  ...desktop,
  plugins: [packedDataPlugin(), tailwindcss(), viteReact(), singleFilePlugin()],
  build: {
    outDir: r("./node_modules/.cache/crest-html"), // stays empty: the plugin writes Crest.html at the repo root
    emptyOutDir: true,
    copyPublicDir: false,
    sourcemap: false,
    cssCodeSplit: false,
    assetsInlineLimit: () => true,
    modulePreload: { polyfill: false },
    rollupOptions: {
      input: { index: r("./src/desktop/index.html") },
      output: { codeSplitting: false, entryFileNames: "assets/[name].js", assetFileNames: "assets/[name][extname]" },
    },
  },
});
