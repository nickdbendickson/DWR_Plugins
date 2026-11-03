import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { analyze } from "./engine.ts";
import { SAMPLE_DAMS, blankDam } from "./samples.ts";
import { dwr2801 } from "./fixtures.ts";

describe("crest engine", () => {
  it("analyzes every sample and a blank record without throwing", () => {
    for (const dam of [...SAMPLE_DAMS, blankDam()]) {
      const a = analyze(dam);
      assert.ok(Number.isFinite(a.breach.peakCfs), dam.id);
      assert.ok(a.breach.series.length > 2, dam.id);
    }
  });

  it("DWR-2801 Froehlich 2008 piping: 100 ft average width, 0.94 h formation (Runner)", () => {
    const a = analyze(dwr2801());
    assert.ok(Math.abs(a.breach.widthFt - 100) < 1, `width ${a.breach.widthFt}`);
    assert.ok(Math.abs(a.breach.timeHr - 0.944) < 0.01, `tf ${a.breach.timeHr}`);
  });

  it("ties a 0–300 ft local record to the DEM crest", () => {
    const a = analyze(SAMPLE_DAMS[1]);
    assert.equal(a.datum.crestNavdFt, 1629);
    assert.ok(Math.abs((a.datum.offsetFt ?? 0) - (1629 - 120.7)) < 1e-9);
    assert.ok(Math.abs((a.datum.streambedNavdFt ?? 0) - (100 + 1629 - 120.7)) < 1e-9);
  });

  it("a 0–300 ft local crest with no DEM crest is held at Review, not silently tied to 1000 ft", () => {
    const local = SAMPLE_DAMS[1];
    const a = analyze({ ...local, demCrestFt: Number.NaN });
    // No fabricated offset: the stated crest is left unconverted, offset is null.
    assert.equal(a.datum.offsetFt, null, "offset must be null when the crest cannot be tied");
    assert.equal(a.datum.crestNavdFt, local.crestFt, "crest must stay the stated local value, never 1000 ft");
    assert.notEqual(a.datum.crestNavdFt, 1000, "must not fall back to the old fabricated 1000 ft");
    // The user is warned and screening is held, rather than fed invented elevations.
    assert.ok(a.datum.warnings.some((w) => w.startsWith("No DEM tie")), "a No DEM tie warning is raised");
    assert.equal(a.screen.level, "Review", "screen is held at Review");
    assert.equal(a.screen.label, "Review (crest not tied)");
  });

  it("an out-of-band DEM crest leaves the crest unconverted rather than exporting the discarded value", () => {
    const local = SAMPLE_DAMS[1];
    const a = analyze({ ...local, demCrestFt: 4200 }); // above the 500–3,600 band
    assert.equal(a.datum.offsetFt, null, "no offset when the DEM crest is out of band");
    assert.equal(a.datum.crestNavdFt, local.crestFt, "crest stays the stated value, not the discarded 4200 ft");
    assert.ok(a.datum.warnings.some((w) => w.startsWith("No DEM tie")));
    assert.equal(a.screen.level, "Review");
  });
});

import { test } from "node:test";
test("blankDam leaves spillway and normal pool blank", () => {
  const d = blankDam();
  assert.ok(Number.isNaN(d.spillwayFt));
  assert.ok(Number.isNaN(d.normalPoolFt));
  const a = analyze(d);
  assert.ok(a);
});
