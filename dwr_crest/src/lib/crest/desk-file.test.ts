// Desk backup and restore (desk-file.ts): the desk file round-trips every record whole, a dam.json comes back (whole
// when it carries crest_record, inputs-only otherwise), a bad file is refused with a reason, and opening a file never
// overwrites a dam on the desk.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  DESK_FILE_FORMAT,
  deskFileName,
  deskFileText,
  mergeIntoDesk,
  mergeSummary,
  openDeskFile,
  recordFromFile,
} from "./desk-file.ts";
import { analyze } from "./engine.ts";
import { exportDamJson } from "./export.ts";
import { blankDam, SAMPLE_DAMS } from "./samples.ts";
import type { DamRecord } from "./types.ts";

const ok = (r: ReturnType<typeof openDeskFile>) => {
  assert.ok(r.ok, r.ok ? "" : r.reason);
  return r;
};
/** What a record looks like after JSON (NaN -> null) and back through the reader (null -> NaN). */
const viaJson = (d: DamRecord) => JSON.parse(JSON.stringify(d));

describe("desk file", () => {
  it("round-trips the whole desk, including a terrain layer and blank outlet levels", () => {
    const withLayer = {
      ...SAMPLE_DAMS[0],
      outline: {
        builtAt: "2026-11-13T00:00:00Z",
        outline: { type: "FeatureCollection", features: [] },
      },
    } as unknown as DamRecord;
    const dams = [withLayer, ...SAMPLE_DAMS.slice(1), blankDam()];
    const r = ok(openDeskFile(deskFileText(dams, dams[1].id, new Date("2026-11-13T12:00:00Z"))));
    assert.equal(r.kind, "desk");
    assert.equal(r.activeId, dams[1].id);
    assert.equal(r.savedAt, "2026-11-13T12:00:00.000Z");
    assert.deepEqual(r.notes, []);
    assert.equal(r.dams.length, dams.length);
    for (let i = 0; i < dams.length; i++)
      assert.deepEqual(viaJson(r.dams[i]), viaJson(dams[i]), dams[i].name);
    // A blank outlet level is NaN on the desk, not null, after the trip.
    const blank = r.dams.at(-1)!;
    assert.ok(Number.isNaN(blank.spillwayFt) && Number.isNaN(blank.normalPoolFt));
    // The same analysis comes out of the restored record.
    assert.equal(analyze(r.dams[0]).breach.peakCfs, analyze(dams[0]).breach.peakCfs);
  });

  it("names the file by local date", () => {
    assert.equal(deskFileName(new Date(2026, 10, 3)), "crest-desk-2026-11-03.json");
  });

  it("refuses what it cannot read, with a reason, and never throws", () => {
    for (const bad of [
      "",
      "not json",
      "[]",
      "42",
      "null",
      '{"format":"crest-desk"}',
      '{"format":"crest-desk","dams":[]}',
      '{"format":"crest-desk","dams":[1,"x",{}]}',
      '{"hello":1}',
    ]) {
      const r = openDeskFile(bad);
      assert.equal(r.ok, false, bad);
      assert.ok(!r.ok && r.reason.length > 10);
    }
    const newer = openDeskFile(
      JSON.stringify({ format: DESK_FILE_FORMAT, formatVersion: 99, dams: [SAMPLE_DAMS[0]] }),
    );
    assert.ok(!newer.ok && /newer Crest/.test(newer.reason));
  });

  it("rebuilds each record field by field: wrong types are dropped with a note, unknown keys are not kept", () => {
    const notes: string[] = [];
    const d = recordFromFile(
      {
        ...SAMPLE_DAMS[0],
        heightFt: "tall",
        method: "magic",
        failureMode: "piping",
        receptors: [{ id: "a", kind: "house" }, { nope: 1 }],
        __proto__: { polluted: true },
        onload: "alert(1)",
        geometry: "x",
      },
      notes,
    )!;
    assert.equal(d.heightFt, blankDam().heightFt);
    assert.equal(d.method, "froehlich");
    assert.equal(d.receptors.length, 1);
    assert.equal(d.geometry, undefined);
    assert.equal((d as unknown as Record<string, unknown>).onload, undefined);
    assert.ok(notes.some((n) => /heightFt/.test(n)) && notes.some((n) => /receptor/.test(n)));
    assert.equal(recordFromFile({ name: "no id" }), null);
    assert.equal(recordFromFile({ id: "<img src=x>", name: "bad id" }), null);
    assert.equal(recordFromFile(null), null);
  });

  it("keeps a record's starting pool and Manning's n, and drops unusable ones", () => {
    const dam: DamRecord = { ...blankDam(), id: "p1", startPool: "elevation", startPoolFt: 95.5, roughness: { nChannel: 0.04, nFloodplain: 0.08 } };
    const back = ok(openDeskFile(deskFileText([dam], dam.id, new Date(0))));
    assert.equal(back.kind, "desk");
    const got = back.dams[0];
    assert.equal(got.startPool, "elevation");
    assert.equal(got.startPoolFt, 95.5);
    assert.deepEqual(got.roughness, { nChannel: 0.04, nFloodplain: 0.08 });
    const notes: string[] = [];
    const bad = recordFromFile({ ...viaJson(dam), startPool: "flood", startPoolFt: "high", roughness: { nChannel: "x", nFloodplain: -1, bankfullM: 9 } }, notes);
    assert.ok(bad);
    assert.equal(bad.startPool, undefined);
    assert.equal(bad.startPoolFt, undefined);
    assert.equal(bad.roughness, undefined);
    assert.deepEqual(recordFromFile({ ...viaJson(dam), roughness: { nFloodplain: 0.05, extra: 1 } }, [])?.roughness, { nFloodplain: 0.05 });
  });

  it("skips a second record with the same id inside one file", () => {
    const r = ok(
      openDeskFile(
        JSON.stringify({
          format: DESK_FILE_FORMAT,
          formatVersion: 1,
          dams: [SAMPLE_DAMS[0], SAMPLE_DAMS[0]],
        }),
      ),
    );
    assert.equal(r.dams.length, 1);
    assert.ok(r.notes.some((n) => /same id/.test(n)));
  });
});

describe("dam.json", () => {
  it("written by the Report step comes back as the same record (crest_record), without the terrain layer", () => {
    const dam = { ...SAMPLE_DAMS[0], outline: { big: true } } as unknown as DamRecord;
    const text = exportDamJson(dam, analyze(dam));
    assert.ok(!text.includes('"big"'), "the terrain layer does not go into dam.json");
    const r = ok(openDeskFile(text));
    assert.equal(r.kind, "record");
    assert.deepEqual(viaJson(r.dams[0]), viaJson(SAMPLE_DAMS[0]));
    assert.deepEqual(r.notes, []);
  });

  it("without crest_record (older Crest) comes back from its inputs, saying what was not in the file", () => {
    const dam = SAMPLE_DAMS[0];
    const a = analyze(dam);
    const j = JSON.parse(exportDamJson(dam, a));
    delete j.crest_record;
    const r = ok(openDeskFile(JSON.stringify(j)));
    assert.equal(r.kind, "inputs");
    const d = r.dams[0];
    assert.equal(d.name, dam.name);
    assert.equal(d.nid, dam.nid);
    assert.equal(d.heightFt, dam.heightFt);
    assert.equal(d.storageAcFt, dam.storageAcFt);
    assert.equal(d.statedDatum, "NAVD88");
    assert.equal(d.crestFt, a.datum.crestNavdFt);
    assert.equal(d.method, dam.method);
    assert.equal(d.failureMode, dam.failureMode);
    assert.deepEqual(d.receptors, []);
    assert.ok(r.notes.some((n) => /engineering inputs only/.test(n)));
    // The breach, which depends on the inputs alone, is the same as the original's.
    assert.ok(Math.abs(analyze(d).breach.peakCfs - a.breach.peakCfs) / a.breach.peakCfs < 0.01);
  });

  it("from the desktop's dam folder (top_of_dam_ft, dam_length_ft) is read too", () => {
    const r = ok(
      openDeskFile(
        JSON.stringify({
          name: "Folder dam",
          dam_id: "DWR-1",
          height_ft: 22,
          storage_acft: 310,
          dam_length_ft: 640,
          top_of_dam_ft: 1500.5,
          top_of_dam_datum: "NAVD88 ft",
          lat: 47.1,
          lon: -99.2,
        }),
      ),
    );
    const d = r.dams[0];
    assert.equal(d.lengthFt, 640);
    assert.equal(d.crestFt, 1500.5);
    assert.equal(d.streambedFt, 1478.5);
    assert.equal(d.lat, 47.1);
  });
});

describe("merging an opened file into the desk", () => {
  const desk = SAMPLE_DAMS;
  let n = 0;
  const makeId = () => `desk-new${++n}`;

  it("add: new dams join, identical ones are left, a changed one is added as a copy; nothing on the desk changes", () => {
    const fresh = { ...blankDam(), id: "desk-fresh", name: "Fresh" };
    const edited = { ...desk[1], heightFt: desk[1].heightFt + 5 };
    const m = mergeIntoDesk(desk, [desk[0], edited, fresh], "add", edited.id, makeId);
    assert.deepEqual(m.dams.slice(0, desk.length), desk);
    assert.deepEqual([m.added, m.unchanged, m.copied], [1, 1, 1]);
    const copy = m.dams.find((d) => d.name === `${desk[1].name} (from file)`)!;
    assert.ok(copy && copy.id !== desk[1].id && copy.heightFt === edited.heightFt);
    assert.equal(m.activeId, copy.id);
    assert.equal(new Set(m.dams.map((d) => d.id)).size, m.dams.length);
    const s = mergeSummary({ ok: true, kind: "desk", dams: [], notes: [] }, m, "add");
    assert.match(s, /1 dam added; 1 dam added as a second copy.*1 dam already on the desk/);
  });

  it("a desk saved and opened again on the same desk changes nothing (field order does not matter)", () => {
    const opened = openDeskFile(deskFileText(desk, desk[0].id));
    assert.ok(opened.ok);
    const m = mergeIntoDesk(desk, opened.dams, "add", opened.activeId, makeId);
    assert.deepEqual([m.added, m.unchanged, m.copied], [0, desk.length, 0]);
    assert.deepEqual(m.dams, desk);
  });

  it("replace: the desk becomes the file's dams", () => {
    const only = [{ ...blankDam(), id: "desk-only" }];
    const m = mergeIntoDesk(desk, only, "replace", "missing");
    assert.deepEqual(m.dams, only);
    assert.equal(m.activeId, "desk-only");
  });
});
