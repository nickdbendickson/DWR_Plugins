// The warning catalogue (warnings.ts): codes beside the free text, the text itself unchanged.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { analyze } from "./engine.ts";
import { volumeCheck } from "./inundation.ts";
import { heightWarningOnElevations } from "./nd-dams.ts";
import { outlineState } from "./outline.ts";
import { SAMPLE_DAMS } from "./samples.ts";
import {
  classifyWarning,
  datumFlags,
  floodFlags,
  WARNING_CATALOGUE,
  warning,
  warningKey,
  warningLine,
  warningText,
  warningTexts,
} from "./warnings.ts";

const dam = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;

describe("warning catalogue", () => {
  it("lists the eight codes with a severity each", () => {
    assert.deepEqual(Object.keys(WARNING_CATALOGUE).sort(), ["W-D1", "W-E1", "W-H1", "W-H2", "W-P1", "W-R1", "W-S1", "W-V1"]);
    assert.equal(warning("W-D1", "x").severity, "warn");
    assert.equal(warning("W-P1", "x").severity, "stop");
    assert.equal(warning("W-D1", "x", "stop").severity, "stop", "the producer can raise the severity");
  });

  it("the text adapter keeps string consumers whole: strings pass through, warnings give their text", () => {
    assert.equal(warningText("plain"), "plain");
    assert.equal(warningText(warning("W-V1", "clamped")), "clamped");
    assert.deepEqual(warningTexts(["a", warning("W-R1", "b")]), ["a", "b"]);
    assert.equal(warningLine(warning("W-E1", "probe failed")), "W-E1 · probe failed");
  });

  it("keys are code and position, so two flags with the same text no longer collide", () => {
    const w = warning("W-V1", "same");
    assert.notEqual(warningKey(w, 0), warningKey(w, 1));
    assert.equal(warningKey("free", 2), "w-2");
  });

  it("the engine's datum and flood flags carry codes and the same text as `warnings`", () => {
    const clamped = analyze({ ...dam, heightFt: 300, storageAcFt: Number.NaN });
    assert.deepEqual(
      clamped.datum.flags!.map((w) => w.text),
      clamped.datum.warnings,
      "flags and warnings are the same list",
    );
    const h = clamped.datum.flags!.find((w) => w.text.startsWith("Height 300"))!;
    assert.equal(h.code, "W-V1");
    assert.equal(h.severity, "warn");
    const v = clamped.datum.flags!.find((w) => w.text.startsWith("Storage is missing"))!;
    assert.equal(v.code, "W-V1");
    assert.equal(v.severity, "info", "a placeholder for a missing input is information, a clamp is a warning");
    const local = analyze({ ...dam, crestFt: 100, streambedFt: 72, demCrestFt: Number.NaN, statedDatum: "Local" });
    const tie = local.datum.flags!.find((w) => w.text.startsWith("No DEM tie"))!;
    assert.equal(tie.code, "W-D1");
    assert.equal(tie.severity, "stop");
    assert.equal(local.screen.label, "Review (crest not tied)", "the stop still holds the screen the way it did");
    assert.deepEqual(analyze(dam).flood.flags!.map((w) => w.text), analyze(dam).flood.warnings);
  });

  it("datumFlags / floodFlags read `flags` when the engine set them and classify the text otherwise", () => {
    const a = analyze({ ...dam, heightFt: 300 });
    assert.equal(datumFlags(a)[0].code, "W-V1");
    const handBuilt = { datum: { warnings: ["No DEM tie. Crest left unconverted.", "Volume: 1 ac-ft laid down for 2 ac-ft released (0.5×). Below 2×: x. Check against a model run."] } };
    assert.deepEqual(
      datumFlags(handBuilt).map((w) => [w.code, w.severity]),
      [
        ["W-D1", "stop"],
        ["W-V1", "warn"],
      ],
    );
    assert.deepEqual(floodFlags({ flood: { warnings: ["Drawn area not applied: no breach location."] } })[0].code, "W-V1");
    // A spread analysis with `warnings` blanked (the dashboard tests do this) leaves stale `flags` behind: warnings win.
    assert.deepEqual(datumFlags({ datum: { ...a.datum, warnings: [] } }), []);
  });

  it("classifies the free texts the producers still write, and leaves the text alone", () => {
    const t = "The schematic flood is still 0.3 ft deep where the valley leaves the drawn area (1.0 km downstream; wet limit 3.0 km). The drawn area may be too small.";
    const w = classifyWarning(t, "flood");
    assert.equal(w.code, "W-R1");
    assert.equal(w.text, t);
    assert.equal(classifyWarning("Pad estimated for A: no terrain at the point, kept on the channel bed (pad 0). Enter the real pad height.").code, "W-P1");
    assert.equal(classifyWarning("The desk inputs changed since the terrain outline was built. Build it again.", "layer").code, "W-S1");
    assert.equal(classifyWarning("The saved terrain layer has no extent check (built before it).", "layer").code, "W-E1");
    assert.equal(classifyWarning("DEM crest 1070.00 ft differs from the tied crest 1071.09 ft NAVD88 by 1.09 ft. Check.").code, "W-D1");
  });

  it("volumeCheck, the NID height checks and a stale layer carry their codes", () => {
    const vc = volumeCheck(2130, 167)!;
    assert.equal(vc.flag.code, "W-V1");
    assert.equal(vc.flag.severity, "warn");
    assert.equal(vc.flag.text, vc.text);
    assert.equal(volumeCheck(310, 71)!.flag.severity, "info");
    const off = { ...dam, streambedFt: dam.streambedFt - 5, nidId: "ND00199" };
    const h2 = heightWarningOnElevations(off)!;
    assert.equal(h2.id, "W-H2");
    assert.equal(h2.code, "W-H2");
    assert.equal(h2.severity, "warn");
    const stale = outlineState({ ...dam, heightFt: dam.heightFt + 1 }, analyze({ ...dam, heightFt: dam.heightFt + 1 }), true);
    if (dam.outline) {
      assert.equal(stale.state, "build");
      assert.equal(stale.state === "build" ? stale.stale?.code : null, "W-S1");
    }
  });
});
