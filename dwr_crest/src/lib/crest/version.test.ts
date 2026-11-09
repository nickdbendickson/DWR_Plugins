// Every export carries the Crest build version (version.ts) and the engine's method version (engine.ts), so a file on
// disk can be matched to the build and the method that wrote it.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import { batchCsv } from "./batch.ts";
import { analyze, ENGINE_VERSION } from "./engine.ts";
import { exportDamJson } from "./export.ts";
import { exportFloodStripGeoJson, exportFloodStripKml, floodStrip } from "./floodstrip.ts";
import { SAMPLE_DAMS } from "./samples.ts";
import { CREST_VERSION, CREST_VERSION_LABEL, PACKAGE_VERSION } from "./version.ts";

const ND = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;

describe("CREST_VERSION", () => {
  it("is '<package.json version>+<git sha | local>', and PACKAGE_VERSION matches package.json", () => {
    const pkg = JSON.parse(readFileSync(new URL("../../../package.json", import.meta.url), "utf8")) as { version: string };
    assert.equal(PACKAGE_VERSION, pkg.version, "bump version.ts PACKAGE_VERSION with package.json");
    assert.match(CREST_VERSION, /^\d+\.\d+\.\d+\+([0-9a-f]{7,}|local)$/);
    assert.ok(CREST_VERSION.startsWith(`${PACKAGE_VERSION}+`));
    // Nothing defines __CREST_VERSION__ under node --test: the fallback is "+local".
    assert.equal(CREST_VERSION, `${PACKAGE_VERSION}+local`);
    assert.equal(CREST_VERSION_LABEL, `Crest ${CREST_VERSION}`);
  });

  it("ENGINE_VERSION is the engine's dated method version, separate from the build", () => {
    assert.match(ENGINE_VERSION, /^crest-desk-engine \d{4}\.\d{2}\.\d{2}$/);
    assert.equal(ENGINE_VERSION, "crest-desk-engine 2026.11.07");
  });
});

describe("the exports carry both versions", () => {
  const result = analyze(ND);
  const strip = floodStrip(ND, result).strip!;

  it("flood strip GeoJSON: on the collection and on every feature", () => {
    const g = JSON.parse(exportFloodStripGeoJson(strip));
    assert.equal(g.properties.crest_version, CREST_VERSION);
    assert.equal(g.properties.engine_version, ENGINE_VERSION);
    for (const f of g.features) {
      assert.equal(f.properties.crest_version, CREST_VERSION);
      assert.equal(f.properties.engine_version, ENGINE_VERSION);
    }
  });

  it("flood strip KML: in the description and the extended data", () => {
    const k = exportFloodStripKml(strip);
    assert.ok(k.includes(`Engine: ${ENGINE_VERSION}. Written by Crest ${CREST_VERSION}.`));
    assert.ok(k.includes(`<Data name="crest_version"><value>${CREST_VERSION}</value></Data>`));
  });

  it("dam.json", () => {
    const j = JSON.parse(exportDamJson(ND, result));
    assert.equal(j.crest_version, CREST_VERSION);
    assert.equal(j.engine_version, ENGINE_VERSION);
    assert.deepEqual(Object.keys(j).slice(0, 3), ["crest_version", "engine_version", "dam_id"]);
  });

  it("batch CSV: a comment row before the header", () => {
    const lines = batchCsv([], { now: new Date("2026-11-07T12:00:00Z") }).split("\n");
    assert.equal(lines[1], `# Crest ${CREST_VERSION}; ${ENGINE_VERSION}. Written 2026-11-07.`);
    assert.match(lines[2], /^id,kind,/);
  });
});
