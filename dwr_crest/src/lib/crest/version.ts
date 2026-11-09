/**
 * The one Crest version string every export carries (flood strip and terrain GeoJSON / KML, dam.json, the batch CSV,
 * the Report): "<package.json version>+<short git SHA of HEAD>", e.g. "0.1.0+424065c".
 *
 * The build injects it: both Vite configs define `__CREST_VERSION__` from scripts/crest-version.mjs (the committed
 * version + the SHA of HEAD, or "+local" without git), so the same tree always builds the same string and the
 * committed desktop bundle and Crest.html stay reproducible. Outside a build (node --test, the scripts) nothing
 * defines it and the fallback below is "<version>+local".
 *
 * This is the BUILD version: which Crest wrote the file. ENGINE_VERSION (engine.ts) is the engine's own method
 * version and TRACE_VERSION (outline.ts) the terrain layer's; an export carries those beside it, because a method can
 * stay the same across many builds and a build can change nothing in the method.
 */
declare const __CREST_VERSION__: string | undefined;

/** Keep equal to package.json "version" (version.test.ts checks it); the build string starts with it. */
export const PACKAGE_VERSION = "0.1.0";

export const CREST_VERSION: string =
  typeof __CREST_VERSION__ === "string" && __CREST_VERSION__ ? __CREST_VERSION__ : `${PACKAGE_VERSION}+local`;

/** The version line the exports write, e.g. "Crest 0.1.0+424065c". */
export const CREST_VERSION_LABEL = `Crest ${CREST_VERSION}`;
