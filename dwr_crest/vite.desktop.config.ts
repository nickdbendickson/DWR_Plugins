/**
 * Static build of the Crest desk for the desktop app (crest-desktop/). It is separate from vite.config.ts, so the web
 * deploy (TanStack Start + Nitro/Vercel) is unchanged. Output: crest-desktop/crest/web/ — plain files, relative paths,
 * Leaflet / leaflet-draw / marker icons bundled, no CDN. Only basemap tiles need the network.
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
import type { Plugin } from "vite";
import { defineConfig } from "vite";
import viteReact from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

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

const desktop = defineConfig({
  root: r("./src/desktop"),
  base: "./",
  publicDir: false,
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
        chunkFileNames: (chunk) => (chunk.isDynamicEntry ? "assets/[name].js" : "assets/samples.js"),
        assetFileNames: "assets/[name][extname]",
      },
    },
  },
});

/** Crest.html: the desk alone (no engine facade), everything in one file. */
const singleFile = defineConfig({
  ...desktop,
  plugins: [tailwindcss(), viteReact(), singleFilePlugin()],
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
