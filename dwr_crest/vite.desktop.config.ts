/**
 * Static build of the Crest desk for the desktop app (crest-desktop/). It is separate from vite.config.ts, so the web
 * deploy (TanStack Start + Nitro/Vercel) is unchanged. Output: crest-desktop/crest/web/ — plain files, relative paths,
 * Leaflet / leaflet-draw / marker icons bundled, no CDN. Only basemap tiles need the network.
 *
 * Build with `npm run build:desktop`. The bundle is committed so `python run.py` needs no Node. build-info.json lists
 * every repo source file that went into it with its SHA-256; the desktop tests recompute those hashes and fail when the
 * committed bundle is older than the sources (rebuild and commit it).
 */
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
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

/** Writes build-info.json: the repo files (not node_modules) bundled, with their hashes. */
function buildInfoPlugin(): Plugin {
  return {
    name: "crest-desktop:build-info",
    apply: "build",
    generateBundle() {
      const sources: Record<string, string> = {};
      for (const id of this.getModuleIds()) {
        const file = id.split("?")[0];
        if (!file.startsWith(ROOT) || file.includes("/node_modules/") || file.includes("\0")) continue;
        const rel = relative(ROOT, file).split("\\").join("/");
        sources[rel] = sha256(file);
      }
      const sorted = Object.fromEntries(Object.entries(sources).sort(([a], [b]) => a.localeCompare(b)));
      const pkg = JSON.parse(readFileSync(r("./package.json"), "utf8")) as { name?: string };
      this.emitFile({
        type: "asset",
        fileName: "build-info.json",
        source: JSON.stringify({ app: pkg.name ?? "crest", builtBy: "npm run build:desktop", sources: sorted }, null, 2) + "\n",
      });
    },
  };
}

export default defineConfig({
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
        // Stable names keep the committed bundle's diffs readable.
        entryFileNames: "assets/[name].js",
        chunkFileNames: "assets/[name].js",
        assetFileNames: "assets/[name][extname]",
      },
    },
  },
});
