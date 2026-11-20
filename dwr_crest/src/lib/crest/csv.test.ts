// One CSV cell writer (csv.ts): RFC 4180 quoting, and third-party text can never open as a spreadsheet formula.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { batchCsv, type BatchRow } from "./batch.ts";
import { csvCell, csvRow } from "./csv.ts";
import { worklistCsv } from "./layer-of-record.ts";

describe("csvCell", () => {
  it("leaves plain text and numbers alone, empties null, quotes commas, quotes and line breaks", () => {
    assert.equal(csvCell("Absaraka"), "Absaraka");
    assert.equal(csvCell(12.5), "12.5");
    assert.equal(csvCell(-0.4), "-0.4", "a negative NUMBER is a number, not a formula");
    assert.equal(csvCell(null), "");
    assert.equal(csvCell(undefined), "");
    assert.equal(csvCell(Number.NaN), "");
    assert.equal(csvCell("a,b"), '"a,b"');
    assert.equal(csvCell('say "hi"'), '"say ""hi"""');
    assert.equal(csvCell("two\nlines"), '"two\nlines"');
    assert.equal(csvCell("cr\rlf"), '"cr\rlf"');
  });

  it("neutralises a string that a spreadsheet would read as a formula (=, +, -, @, also after a tab or CR)", () => {
    assert.equal(csvCell("=HYPERLINK(\"http://x\")"), "\"'=HYPERLINK(\"\"http://x\"\")\"");
    assert.equal(csvCell("+1 (701) 555-0100"), "\"'+1 (701) 555-0100\"");
    assert.equal(csvCell("-Dam removed"), "\"'-Dam removed\"");
    assert.equal(csvCell("@cmd"), "\"'@cmd\"");
    assert.equal(csvCell("\t=1+1"), "\"'\t=1+1\"");
    // A formatted number (the 911 list's lon/lat, a negative offset) is a number to the spreadsheet too: left alone.
    assert.equal(csvCell("12"), "12");
    assert.equal(csvCell("-97.325185"), "-97.325185");
    assert.equal(csvCell("+0.5"), "+0.5");
    assert.equal(csvCell("-1e-3"), "-1e-3");
    assert.equal(csvCell("12th St SE"), "12th St SE");
    assert.equal(csvCell("-97 Main St"), "\"'-97 Main St\"");
  });

  it("joins a row", () => {
    assert.equal(csvRow(["a", 1, null, "=x"]), "a,1,,\"'=x\"");
  });
});

describe("the exports use it", () => {
  it("a 911-derived name starting with = never reaches the batch CSV as a formula", () => {
    const row: BatchRow = {
      id: "DWR-1",
      kind: "dam",
      label: "=cmd|' /C calc'!A0",
      county: "Cass",
      huc8: null,
      status: "needs-data",
      reason: "-no height",
      heightFt: null,
      storageAcFt: null,
      crestFt: null,
      crestFrom: null,
      bearingDeg: null,
      releasedAcFt: null,
      peakCfs: null,
      layer: null,
      floodedAc: null,
      screen: null,
      screenLabel: null,
      structuresInside: null,
      structuresFlooded: null,
      triage: null,
      seconds: 0,
    };
    const csv = batchCsv([row]);
    const line = csv.trim().split("\n").find((l) => l.startsWith("DWR-1,"))!;
    assert.ok(line.includes("\"'=cmd|' /C calc'!A0\""), line);
    assert.ok(line.endsWith("\"'-no height\""), line);
    assert.ok(!/,=cmd/.test(line));
  });

  it("the Runner worklist CSV too", () => {
    const csv = worklistCsv([{ id: "a", nid: "ND1", name: "@dam", county: "Cass", heightFt: 10, storageAcFt: 600, releasedAcFt: 600, peakCfs: 1000, status: "no run" }]);
    assert.match(csv, /,"'@dam",/);
  });
});
