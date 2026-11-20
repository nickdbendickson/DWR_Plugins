/**
 * The gzip + base64 data bundles of the one-file Crest.html, or none. This file is the stand-in every other build
 * (web, desktop, Node tests, Vite dev) sees: empty, so data-bundle.ts takes the JSON modules. `npm run build:crest-html`
 * swaps it (vite.desktop.config.ts packedDataPlugin) for a module whose default export holds each bundle packed, and
 * leaves the JSON modules themselves out of the single file.
 */
const packed: Partial<Record<"nd_dams" | "nd_dams_dwr" | "nd_structures", string>> = {};
export default packed;
