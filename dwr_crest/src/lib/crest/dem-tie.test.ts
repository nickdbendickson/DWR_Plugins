import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { demTieOffer, needsDemTie } from "./nd-dams.ts";
import { blankDam } from "./samples.ts";

const rec = (o: object) => ({ ...blankDam(), ...o });

describe("Record-step DEM tie", () => {
  it("is offered only for a blank or local datum", () => {
    assert.equal(needsDemTie(rec({ statedDatum: "NAVD88" })), false);
    assert.equal(needsDemTie(rec({ statedDatum: "" })), true);
    assert.equal(demTieOffer(rec({ statedDatum: "NAVD88" })).patch, null);
  });
  it("shifts 0-300 ft local values by the DEM mid-crest offset", () => {
    const o = demTieOffer(rec({ statedDatum: "Local", crestFt: 130.7, streambedFt: 100.7, spillwayFt: 126, normalPoolFt: Number.NaN, demCrestFt: 1890.2 }));
    assert.equal(o.mode, "local");
    assert.equal(o.offsetFt, 1759.5);
    assert.equal(o.patch?.crestFt, 1890.2);
    assert.equal(o.patch?.streambedFt, 1860.2);
    assert.equal(o.patch?.spillwayFt, 1885.5);
    assert.ok(Number.isNaN(o.patch?.normalPoolFt));
    assert.equal(o.patch?.statedDatum, "NAVD88");
    assert.equal(o.review, null);
  });
  it("keeps NAVD88-like values on a blank datum", () => {
    const o = demTieOffer(rec({ statedDatum: "", crestFt: 1885, streambedFt: 1860, demCrestFt: 1890 }));
    assert.equal(o.mode, "as-is");
    assert.equal(o.patch?.crestFt, 1885);
  });
  it("flags a mismatch over 100 ft for review and offers nothing", () => {
    const o = demTieOffer(rec({ statedDatum: "", crestFt: 1500, demCrestFt: 1890 }));
    assert.equal(o.patch, null);
    assert.match(o.review ?? "", /over 100 ft/);
  });
});
