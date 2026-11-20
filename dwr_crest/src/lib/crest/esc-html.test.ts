// escHtml neutralizes HTML in strings that Leaflet inserts as innerHTML (map tooltips).
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { escHtml } from "./esc-html.ts";

describe("escHtml", () => {
  it("escapes the HTML metacharacters so tooltip markup cannot execute", () => {
    assert.equal(escHtml("<img src=x onerror=alert(1)>"), "&lt;img src=x onerror=alert(1)&gt;");
    assert.equal(escHtml("a & b"), "a &amp; b");
    assert.ok(!escHtml("<script>alert(1)</script>").includes("<"));
    assert.ok(!escHtml("<script>alert(1)</script>").includes(">"));
  });

  it("leaves ordinary dam/receptor names unchanged", () => {
    assert.equal(escHtml("Lake Delhi Dam"), "Lake Delhi Dam");
    assert.equal(escHtml("911: 123 Main St"), "911: 123 Main St");
    assert.equal(escHtml("Receptor 7 (mapped)"), "Receptor 7 (mapped)");
  });
});
