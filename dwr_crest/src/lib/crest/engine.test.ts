import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { analyze } from "./engine.ts";
import { SAMPLE_DAMS, blankDam } from "./samples.ts";
import { dwr2801 } from "./fixtures.ts";

describe("crest engine", () => {
  it("analyzes every sample and a blank record without throwing", () => {
    for (const dam of [...SAMPLE_DAMS, blankDam()]) {
      const a = analyze(dam);
      assert.ok(Number.isFinite(a.breach.fittedPeakCfs), dam.id);
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
});
