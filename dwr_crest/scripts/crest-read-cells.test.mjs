// The summary of the crest-read cell-size test (crest-read-cells.mjs): the reads themselves need 3DEP, the summary does not.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { summarize } from "./crest-read-cells.mjs";

const row = (id, heightFt, topNavdFt, c10, c5, c2, c1, datum = "NAVD88") => ({ id, heightFt, topNavdFt, datum, c10, c5, c2, c1 });

describe("crest-read-cells summarize", () => {
  it("leaves out a dam with a failed read and reports the differences from the 10 m read", () => {
    const lines = summarize([
      row("A", 10, 1000, 1000, 1000.5, 1001, 1001),
      row("B", 20, 1500, 1498, 1499, 1500, 1500.1),
      row("C", 40, 1200, 1190, 1192, 1193, 1193, "NGVD29"),
      row("D", 12, 900, 900, 900, null, 900),
    ]);
    assert.equal(lines[0], "4 dams read, 3 with all 4 reads");
    assert.ok(lines.includes("c1 - c10 (ft): median 2.10, p10 1.00, p90 3.00, max |d| 3.00"), lines.join("\n"));
    assert.ok(lines.includes("1 m read more than 1 ft above the 10 m read: 2; more than 0.25 ft below: 0"));
    assert.ok(lines.includes("height 30 to Infinity ft: 1 dams, median (c1 - c10) 3.00 ft"));
    // C is 7 to 10 ft off DWR at every cell: left out of the comparison with DWR, not counted as an error of the read.
    assert.ok(lines.includes("c10 within 5 ft of DWR's top of dam: 2 of 3"));
    assert.ok(lines.includes("all, the 2 with the 1 m read within 5 ft of DWR: median |c1 - DWR| 1.00 ft"));
  });
});
