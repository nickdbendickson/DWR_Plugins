import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";
import { gzipSync } from "node:zlib";

import { analyze } from "./engine.ts";
import { exportDamJson } from "./export.ts";
import {
  applyDwrElevationsPatch,
  confirmText,
  countyOptions,
  damOptionLabel,
  demElevationsPatch,
  dwrAreaPatch,
  dwrElevationOffer,
  dwrReference,
  fieldTag,
  fold,
  heightFor,
  heightWarningOnElevations,
  heightWarningOnPick,
  loadNdData,
  missingForNext,
  nidHazardLine,
  prefillPatch,
  revertPatch,
  searchDams,
  storageFor,
  unlinkPatch,
  type NdDam,
} from "./nd-dams.ts";
import { blankDam, SAMPLE_DAMS } from "./samples.ts";
import { migrateDesk } from "./store.ts";
import type { DamRecord } from "./types.ts";

const data = await loadNdData();
const dam = (nid: string): NdDam => {
  const d = data.byNid.get(nid);
  assert.ok(d, nid);
  return d;
};
const filled = (nid: string, current: DamRecord = blankDam()): DamRecord => ({
  ...current,
  ...prefillPatch(dam(nid), data, current).patch,
});

describe("ND dam bundles", () => {
  it("load offline: 948 NID main dams, 839 DWR joins, sources and dates recorded", () => {
    assert.equal(data.dams.length, 948);
    assert.equal(data.dwr.size, 839);
    assert.equal(data.nidSource.fetchedUtc, "2026-10-29T21:56:00Z");
    assert.equal(data.dwrSource?.pulledUtc, "2026-10-29T22:07:32Z");
    assert.match(data.dwrSource!.disclaimer, /provisional/i);
  });

  it("size budget: NID <= 250 KB raw / 70 KB gzipped; NID + DWR <= 330 KB", () => {
    const nid = readFileSync(new URL("./data/nd_dams.json", import.meta.url));
    const dwr = readFileSync(new URL("./data/nd_dams_dwr.json", import.meta.url));
    assert.ok(nid.length <= 250_000, `${nid.length}`);
    assert.ok(gzipSync(nid, { level: 9 }).length <= 70_000);
    assert.ok(nid.length + dwr.length <= 330_000);
  });

  it("the picker modules never fetch: data comes by dynamic import only", () => {
    for (const f of ["./nd-dams.ts", "../../components/crest/NdDamPicker.tsx"]) {
      const src = readFileSync(new URL(f, import.meta.url), "utf8").replace(/\/\/.*$|\/\*[\s\S]*?\*\//gm, "");
      assert.doesNotMatch(src, /\bfetch\s*\(|XMLHttpRequest|https?:\/\/[^"'\s]*\.json/, f);
    }
    const src = readFileSync(new URL("./nd-dams.ts", import.meta.url), "utf8");
    assert.match(src, /import\("\.\/data\/nd_dams\.json"/);
  });
});

describe("search and labels", () => {
  it("finds ND00199 by NID ID, digits, name words and other name; case- and accent-insensitive", () => {
    for (const q of ["nd00199", "ND00199", "199", "swan buff", "SWAN  Buffalo 12", "absaraka", "Ábsaraká"])
      assert.ok(searchDams(data.dams, q).some((d) => d.nid === "ND00199"), q);
    assert.equal(fold("Lac Qui Parlé — Dam"), "lac qui parle dam");
    assert.ok(!searchDams(data.dams, "absaraka", "Barnes").length);
    assert.equal(searchDams(data.dams, "").length, 948);
  });

  it("option label: name · NID ID · NID hazard · H ft · V ac-ft", () => {
    assert.equal(
      damOptionLabel(dam("ND00199"), data.dwr.get("ND00199")),
      "Swan Buffalo Detention Dam 12 · ND00199 · NID hazard Significant · 27.5 ft · 1,616 ac-ft",
    );
  });

  it("an unnamed dam reads (unnamed) · NID ID, never an owner name", () => {
    const d = dam("ND00971");
    assert.equal(d.name, null);
    assert.ok(!("owner" in d));
    assert.match(damOptionLabel(d, data.dwr.get(d.nid)), /^\(unnamed\) · ND00971 · NID hazard/);
    const f = filled("ND00971");
    assert.equal(f.name, "Unnamed dam ND00971");
    assert.ok(f.nidRef && !("owner" in f.nidRef));
    const out = JSON.stringify(f) + exportDamJson(f, analyze(f));
    assert.ok(!/"owner/i.test(out));
  });

  it("counties with counts; Clay is flagged as not an ND county", () => {
    const opts = countyOptions(data.dams);
    assert.equal(opts.reduce((s, o) => s + o.count, 0), 948);
    assert.equal(opts.find((o) => o.county === "Clay")?.label, "Clay (1) (not an ND county, see note)");
    assert.match(opts.find((o) => o.county === "Cass")!.label, /^Cass \(\d+\)$/);
  });

  it("ND00182 stays in Clay with a Cass Co. note; DWR also says Cass", () => {
    const d = dam("ND00182");
    assert.equal(d.county, "Clay");
    assert.match(d.countyNote!, /Cass Co\., ND/);
    assert.equal(data.dwr.get("ND00182")?.county, "Cass");
  });
});

describe("prefill", () => {
  it("ND00199: H 27.5 from DWR, V 1615.5 NID Max Storage, L 1700; no elevations, no datum, no area", () => {
    const r = filled("ND00199");
    assert.equal(r.name, "Swan Buffalo Detention Dam 12");
    assert.equal(r.nidId, "ND00199");
    assert.equal(r.heightFt, 27.5);
    assert.equal(r.provenance?.heightFt?.src, "DWR");
    assert.equal(r.storageAcFt, 1615.5);
    assert.equal(r.lengthFt, 1700);
    assert.equal(r.year, 1960);
    assert.equal(r.drainageMi2, 13.7);
    assert.ok(Number.isNaN(r.crestFt) && Number.isNaN(r.streambedFt));
    assert.equal(r.statedDatum, "");
    assert.equal(r.surfaceAreaAc, undefined);
    assert.equal(r.classOfRecord, undefined);
    assert.equal(r.nidHazard, "Significant");
    assert.equal(r.nidRef?.surfaceAreaNormalAc, 29.8);
    assert.deepEqual(missingForNext(r), ["crest", "breach bottom", "datum"]);
  });

  it("H falls back to NID Height (whole ft) when the dam has no DWR record", () => {
    const nidOnly = [...data.dams].find((d) => !data.dwr.has(d.nid) && d.nidHeightFt != null)!;
    assert.deepEqual(heightFor(nidOnly, undefined), {
      value: nidOnly.nidHeightFt,
      src: "NID",
      field: "NID Height (Ft)",
      rounded: true,
    });
  });

  it("confirm dialog lists what is replaced; applying another dam clears its geometry, class and area", () => {
    const cur: DamRecord = { ...SAMPLE_DAMS[0], surfaceAreaAc: 50, classOfRecord: "High" };
    const { patch, replaced } = prefillPatch(dam("ND00486"), data, cur);
    assert.ok(replaced.includes("surfaceAreaAc") && replaced.includes("classOfRecord"));
    assert.match(confirmText(replaced), /^Fill this record from the NID\? These fields will be replaced: .*crest \(cleared\).*class of record \(cleared\)/);
    assert.equal(patch.surfaceAreaAc, undefined);
    const again = prefillPatch(dam("ND00486"), data, { ...cur, ...patch, surfaceAreaAc: 40 });
    assert.ok(!("surfaceAreaAc" in again.patch), "re-applying the same dam leaves area as it was");
  });

  it("required values missing in the NID become NaN with a red tag; optional ones read not in NID", () => {
    const d: NdDam = { ...dam("ND00486"), nid: "ND99999", nidHeightFt: null, damHeightFt: null, lengthFt: null, yearCompleted: null, name: null, otherNames: null };
    const r = { ...blankDam(), ...prefillPatch(d, data).patch };
    assert.ok(Number.isNaN(r.heightFt) && Number.isNaN(r.lengthFt));
    assert.deepEqual(fieldTag(r, "heightFt"), { kind: "missing", text: "not in NID: enter a value" });
    assert.deepEqual(fieldTag(r, "year"), { kind: "missing-optional", text: "not in NID" });
    assert.ok(missingForNext(r).includes("height") && missingForNext(r).includes("length"));
    assert.equal(r.name, "Unnamed dam ND99999");
  });

  it("tags: from NID / edited (NID: x) / revert / unlink", () => {
    const r = filled("ND00486");
    assert.equal(fieldTag(r, "storageAcFt")?.kind, "from");
    const e = { ...r, storageAcFt: 99 };
    assert.equal(fieldTag(e, "storageAcFt")?.text, "edited (NID: 150)");
    assert.equal(revertPatch(e, "storageAcFt").storageAcFt, 150);
    const u = { ...e, ...unlinkPatch() };
    assert.equal(fieldTag(u, "storageAcFt"), null);
    assert.equal(u.storageAcFt, 99);
  });
});

describe("storage chip", () => {
  it("NID Max Storage normally; Normal Storage with a warning when no max is reported; range note over 500k", () => {
    assert.equal(storageFor(dam("ND00199")).chip, "NID Max Storage");
    const noMax = storageFor(dam("ND00182"));
    assert.equal(noMax.chip, "NID Normal Storage (no max reported)");
    assert.match(noMax.warning!, /no Max Storage/);
    assert.equal(noMax.value, 452);
    assert.match(storageFor(dam("ND00145")).rangeNote!, /Over 500,000 ac-ft/);
    assert.equal(storageFor(dam("ND00199")).rangeNote, null);
  });
});

describe("height warnings", () => {
  it("W-H1, NID-only: ND00001 NID Height 24 vs Dam Height 16 offers Use 16; ND00486 (23 vs blank) does not offer", () => {
    const w = heightWarningOnPick(dam("ND00001"));
    assert.equal(w?.id, "W-H1");
    assert.equal(w?.useHeightFt, 16);
    assert.equal(heightWarningOnPick(dam("ND00486"))?.useHeightFt, undefined);
  });

  it("W-H1 with DWR: ND00001 H = DWR 16, NID 24 is more than 1 ft above, informational; ND00199 none", () => {
    const w = heightWarningOnPick(dam("ND00001"), data.dwr.get("ND00001"));
    assert.equal(w?.id, "W-H1");
    assert.equal(w?.useHeightFt, undefined);
    assert.equal(heightWarningOnPick(dam("ND00199"), data.dwr.get("ND00199")), null);
  });

  it("W-H2: ND00199 with the DWR elevations gives no warning; a 5 ft mismatch warns (tol = max(1, 5% D) + 0.5 if rounded)", () => {
    const r = filled("ND00199");
    const withElev = { ...r, ...applyDwrElevationsPatch(r, data.dwr.get("ND00199")!, "2026-10-29") };
    assert.equal(heightWarningOnElevations(withElev), null);
    const off = { ...withElev, streambedFt: 1037.5 };
    const w = heightWarningOnElevations(off);
    assert.equal(w?.id, "W-H2");
    assert.equal(w?.useHeightFt, 32.5);
    // A dam with no DWR record: H is the rounded NID Height, so the tolerance gets +0.5 ft.
    const only = data.dams.find((d) => !data.dwr.has(d.nid) && d.nidHeightFt != null && d.nidHeightFt < 25)!;
    const nid = filled(only.nid);
    const H = nid.heightFt;
    const r14 = { ...nid, crestFt: 100 + H - 1.4, streambedFt: 100, statedDatum: "NAVD88" as const };
    assert.equal(heightWarningOnElevations(r14), null, "1.4 ft is within 1 + 0.5 for a rounded NID H");
    const typed = { ...r14, provenance: { ...r14.provenance, heightFt: undefined } };
    assert.equal(heightWarningOnElevations(typed)?.id, "W-H2", "1.4 ft is outside 1 ft for a typed H");
  });
});

describe("DWR card", () => {
  it("ND00199: NGVD 29 1070.0 / 1042.5 offered as 1071.09 / 1043.59 NAVD 88 (VERTCON at the dam)", () => {
    const d = dam("ND00199");
    const o = dwrElevationOffer(data.dwr.get("ND00199"), d.lon, d.lat);
    assert.ok(o.ok);
    if (!o.ok) return;
    assert.equal(o.datum, "NGVD29");
    assert.equal(o.crestNavdFt, 1071.09);
    assert.equal(o.streambedNavdFt, 1043.59);
    assert.ok(Math.abs(o.shiftFt! - 1.0886) < 0.001);
  });

  it("using them (after confirming) stores the stated NGVD values; the engine ties to 1071.09 / 1043.59, H 27.5", () => {
    const r = filled("ND00199");
    const p = applyDwrElevationsPatch(r, data.dwr.get("ND00199")!, "2026-10-29");
    assert.equal(p.provenance?.crestFt?.confirmed, true);
    const a = analyze({ ...r, ...p });
    assert.equal(a.datum.crestNavdFt.toFixed(2), "1071.09");
    assert.equal(a.datum.streambedNavdFt!.toFixed(2), "1043.59");
    assert.equal(a.breach.headFt.toFixed(2), "27.50");
    assert.deepEqual(missingForNext({ ...r, ...p }), []);
  });

  it("Local or blank DWR datum is never offered as elevations; NAVD 88 needs no shift", () => {
    const local = [...data.dwr.values()].find((x) => x.datum === "Local" && x.topElevFt != null)!;
    const o = dwrElevationOffer(local, -100, 47);
    assert.equal(o.ok, false);
    if (!o.ok) assert.match(o.reason, /local datum/);
    assert.equal(dwrElevationOffer(data.dwr.get("ND00145"), -101.4, 47.5).ok, false);
    const navd = dwrElevationOffer(data.dwr.get("ND00182"), -96.8, 46.7);
    assert.ok(navd.ok && navd.shiftFt === null && navd.crestNavdFt === 894.08);
  });

  it("reference values: Local datum label, H check, max-pool storage flag above 5%", () => {
    const g = dwrReference(dam("ND00145"), data.dwr.get("ND00145")!, 202);
    assert.equal(g.datumLabel, "no datum given, relative only");
    assert.match(g.storageFlag ?? "", /differs from the NID V/);
    assert.equal(dwrReference(dam("ND00199"), data.dwr.get("ND00199")!, 27.5).storageFlag, null);
    const local = [...data.dwr.values()].find((x) => x.datum === "Local")!;
    assert.equal(dwrReference(dam(local.nid), local, 1).datumLabel, "local datum, relative only");
  });

  it("max-pool area only by Use; DEM entry is NAVD 88 tagged from DEM", () => {
    const r = filled("ND00199");
    assert.equal(r.surfaceAreaAc, undefined);
    assert.equal(dwrAreaPatch(r, data.dwr.get("ND00199")!, "2026-10-29").surfaceAreaAc, 154.5);
    const dem = demElevationsPatch(r, 1071.1, 1043.6);
    assert.equal(dem.statedDatum, "NAVD88");
    assert.equal(dem.provenance?.crestFt?.src, "DEM");
  });
});

describe("engine guard and export", () => {
  it("no elevations: Review (elevations missing), no breach, no flood", () => {
    const a = analyze(filled("ND00199"));
    assert.equal(a.screen.level, "Review");
    assert.equal(a.screen.label, "Review (elevations missing)");
    assert.equal(a.flood.engineLabel, "not run (elevations missing)");
    assert.equal(a.breach.series.length, 0);
    assert.ok(Number.isNaN(a.breach.peakCfs));
  });

  it("NID hazard is never fed into the screen or the class of record", () => {
    const r = filled("ND00199");
    const p = { ...r, ...applyDwrElevationsPatch(r, data.dwr.get("ND00199")!, "2026-10-29") };
    const a = analyze(p);
    const b = analyze({ ...p, nidHazard: "High" });
    assert.equal(a.screen.level, b.screen.level);
    assert.equal(a.screen.classOfRecord, null);
    assert.equal(nidHazardLine("Significant"), "NID hazard (as reported to the NID, FEMA class): Significant");
  });

  it("dam.json gets provenance, NID hazard and attribution only for NID-filled records", () => {
    const sample = SAMPLE_DAMS.find((d) => d.id === "nd00199")!;
    const plain = JSON.parse(exportDamJson(sample, analyze(sample)));
    assert.ok(!("provenance" in plain) && !("nid_hazard" in plain));
    const r = filled("ND00199");
    const p = { ...r, ...applyDwrElevationsPatch(r, data.dwr.get("ND00199")!, "2026-10-29") };
    const j = JSON.parse(exportDamJson(p, analyze(p)));
    assert.equal(j.nid_id, "ND00199");
    assert.equal(j.nid_hazard, "Significant");
    assert.match(j.nid_hazard_note, /not the class of record/);
    assert.equal(j.provenance.crestFt.src, "DWR");
    assert.equal(j.crest_ft.toFixed(2), "1071.09");
    assert.ok(j.attribution.some((s: string) => /National Inventory of Dams/.test(s)));
    assert.ok(j.attribution.some((s: string) => /Water Resources/.test(s)));
  });
});

describe("stored desk migration (crest-desk-v2, version 0 -> 1)", () => {
  it("moves an untouched ND00199 sample from 1072.24 / 1044.74 to 1071.09 / 1043.59; keeps edited ones", () => {
    const now = SAMPLE_DAMS.find((d) => d.id === "nd00199")!;
    const old = { ...now, crestFt: 1072.24, streambedFt: 1044.74 };
    const edited = { ...now, crestFt: 1080, streambedFt: 1044.74 };
    const m = migrateDesk({ dams: [old], activeId: "nd00199" }, 0);
    assert.equal(m.dams[0].crestFt, 1071.09);
    assert.equal(m.dams[0].streambedFt, 1043.59);
    assert.equal(migrateDesk({ dams: [edited] }, 0).dams[0].crestFt, 1080);
    assert.equal(now.crestFt, 1071.09);
  });
});
