import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import { analyze } from "./engine.ts";
import { floodStrip } from "./floodstrip.ts";
import { fetchNd911, nd911AutoPatch, nd911State, removeNd911Seeds, wantsNd911Lookup } from "./nd911.ts";
import { loadNdData, prefillPatch } from "./nd-dams.ts";
import { blankDam, SAMPLE_DAMS } from "./samples.ts";
import type { DamRecord } from "./types.ts";

const PAGE = JSON.parse(readFileSync(new URL("./__fixtures__/nd911_nd00199.json", import.meta.url), "utf8"));
const fake = async () => ({ ok: true, status: 200, json: async () => PAGE });
const data = await loadNdData();

describe("real receptors: picked ND dams screen ND 911 points, samples keep the generic seeds", () => {
  const sample = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;
  const base = blankDam();
  const pick = prefillPatch(data.byNid.get("ND00199")!, data).patch;
  const fromPick: DamRecord = { ...base, ...pick };
  // Screening tests: the sample's tied elevations and geometry with the pick's receptor settings.
  const picked: DamRecord = {
    ...sample,
    receptors: pick.receptors!,
    receptorSource: pick.receptorSource,
    judgmentAsProbable: pick.judgmentAsProbable,
  };

  it("samples and blank records keep their generic seeds; a pick drops them", () => {
    assert.ok(sample.receptors.length > 0 && sample.receptorSource === undefined);
    assert.ok(base.receptors.length > 0 && base.receptorSource === undefined);
    assert.deepEqual(fromPick.receptors, []);
    assert.equal(fromPick.receptorSource, "nd911");
    assert.equal(fromPick.judgmentAsProbable, false, "judgment zone screens ND MEDIUM");
  });

  it("before the lookup the screen holds at Review instead of reading Low on an empty list", () => {
    const r = analyze(picked);
    assert.equal(r.screen.level, "Review");
    assert.match(r.screen.label, /911 points not looked up/);
  });

  it("looks up once, adds every point inside the strip once, and later removals stick", async () => {
    const r = analyze(picked);
    const strip = floodStrip(picked, r).strip!;
    assert.equal(wantsNd911Lookup(picked, nd911State(picked, strip, true)), true);
    const withCache: DamRecord = { ...picked, nd911: await fetchNd911(strip, fake, new Date("2026-10-31T00:00:00Z")) };
    const st = nd911State(withCache, strip, true);
    assert.equal(st.state, "ready");
    const p = nd911AutoPatch(withCache, st, r)!;
    assert.ok(p && p.nd911AutoAdded);
    const inside = st.state === "ready" ? st.table.rows.length : -1;
    assert.equal(p.receptors!.length, inside);
    assert.ok(p.receptors!.every((s) => s.source === "nd911"));
    const done: DamRecord = { ...withCache, ...p };
    assert.equal(nd911AutoPatch(done, nd911State(done, strip, true), r), null);
    assert.equal(wantsNd911Lookup(done, nd911State(done, strip, true)), false);
    const cleared: DamRecord = { ...done, receptors: removeNd911Seeds(done.receptors) };
    assert.equal(nd911AutoPatch(cleared, nd911State(cleared, strip, true), r), null);
    const screened = analyze(done);
    assert.doesNotMatch(screened.screen.label, /not looked up/);
    assert.ok(!/flagged: judgment zone\)/.test(screened.screen.label) || /Medium/.test(screened.screen.label));
  });

  it("samples never auto-add", () => {
    const r = analyze(sample);
    const strip = floodStrip(sample, r).strip!;
    assert.equal(wantsNd911Lookup(sample, nd911State(sample, strip, true)), false);
  });
});
