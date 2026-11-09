import { defineConfig } from "vite";
import { tanstackStart } from "@tanstack/react-start/plugin/vite";
import viteReact from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { nitro } from "nitro/vite";
import { crestVersion } from "./scripts/crest-version.mjs";

/**
 * The web deploy: TanStack Start (SSR shell, one route, `/`) built with Nitro's Vercel preset. The desk itself is
 * client-only (Leaflet, localStorage, IndexedDB); the server renders the `<html>` shell and serves the client bundle.
 * It has no database, no auth and no server middleware: the app-builder scaffolding that once lived here (PGLite
 * bootstrap, the OAuth popup, the app-env endpoint, the PWA install page) was never used by the desk and was removed
 * on 2026-11-08. The desktop app and Crest.html are built by vite.desktop.config.ts instead.
 *
 * `0.0.0.0:8080` is the dev-server contract (startup.sh and the smoke tests expect it).
 */
export default defineConfig(({ command, isPreview }) => ({
  // The build version every export is stamped with (src/lib/crest/version.ts): package.json version + git SHA of
  // HEAD, the same for every build of the same tree. The desktop config defines the same constant.
  define: { __CREST_VERSION__: JSON.stringify(crestVersion()) },
  server: {
    host: "0.0.0.0",
    port: 8080,
    strictPort: true,
  },
  preview: {
    host: "127.0.0.1",
    port: 8081,
    strictPort: true,
  },
  resolve: { tsconfigPaths: true },
  plugins: [
    tailwindcss(),
    tanstackStart(),
    ...(command === "build" || isPreview ? [nitro({ preset: "vercel" })] : []),
    viteReact(),
  ],
}));
