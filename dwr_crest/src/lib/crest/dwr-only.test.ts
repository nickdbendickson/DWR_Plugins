import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { makeNdData, prefillPatch } from "./nd-dams.ts";
import { blankDam } from "./samples.ts";

const load = (f: string) => JSON.parse(readFileSync(new URL(`./data/${f}`, import.meta.url), "utf8"));
const data = makeNdData(load("nd_dams.json"), load("nd_dams_dwr.json"));

describe("DWR-only dams in the picker", () => {
  it("lists DWR-4031 (ND30554, not in the NID bundle) with no name and no local elevations", () => {
    const d = data.byNid.get("DWR-4031");
    assert.ok(d);
    assert.equal(d.source, "DWR");
    assert.equal(d.dwrNidId, "ND30554");
    assert.equal(d.name, "DWR-4031 (DWR only, ND30554)");
    assert.equal(d.county, "Ward");
    const dwr = data.dwr.get("DWR-4031");
    assert.equal(dwr?.datum, "Local");
    assert.equal(dwr?.topNavdFt, null);
    assert.equal(dwr?.streambedNavdFt, null);
  });
  it("prefills with DWR provenance and leaves elevations blank", () => {
    const d = data.byNid.get("DWR-4031")!;
    const { patch } = prefillPatch(d, data, blankDam());
    assert.equal(patch.heightFt, 20.7);
    assert.equal(patch.storageAcFt, 6.9);
    assert.ok(Number.isNaN(patch.crestFt));
    assert.equal(patch.provenance?.nid?.src, "DWR");
  });
  it("carries no owner or dam-name text", () => {
    const b = load("nd_dams_dwr.json");
    assert.deepEqual(b.dwrOnly.cols, ["key", "nidId", "county", "lat", "lon", "yearCompleted", "drainageMi2"]);
  });
});
