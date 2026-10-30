/**
 * Second entry of the desktop bundle: the shared Crest engine with stable export names (assets/engine.js).
 * The desk chunk imports the same modules, so the smoke tests can run the exact code the desktop window runs.
 */
export { analyze, STEPS } from "@/lib/crest/engine";
export { exportDamJson, exportHydrographCsv, generateLisfloodPackage } from "@/lib/crest/export";
export { blankDam, SAMPLE_DAMS } from "@/lib/crest/samples";
