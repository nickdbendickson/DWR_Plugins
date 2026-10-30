import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import { analyze } from "./engine.ts";
import { buildPdf, LETTER_LANDSCAPE, winAnsi, wrap } from "./pdf.ts";
import { SAMPLE_DAMS } from "./samples.ts";
import { DISCLAIMER, pdfSlices, pictureBox, viewFileName, viewFooter, viewHeader, type ViewMeta, viewPdfPages } from "./viewexport.ts";

// An 8 × 4 baseline JPEG (what canvas.toBlob("image/jpeg") gives, only smaller).
const JPEG = Uint8Array.from(Buffer.from("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAYEBQYFBAYGBQYHBwYIChAKCgkJChQODwwQFxQYGBcUFhYaHSUfGhsjHBYWICwgIyYnKSopGR8tMC0oMCUoKSj/2wBDAQcHBwoIChMKChMoGhYaKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCj/wAARCAAEAAgDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDnKKKK+/PhD//Z", "base64"));

const dam = SAMPLE_DAMS.find((d) => d.nid === "ND00199")!;
const meta: ViewMeta = { kind: "flood", dam, result: analyze(dam), savedAt: new Date(2026, 9, 30, 10, 27) };
const latin1 = (b: Uint8Array) => Buffer.from(b).toString("latin1");

/** Every xref entry points at "N 0 obj", and startxref points at "xref". */
function checkXref(pdf: Uint8Array) {
  const s = latin1(pdf);
  const start = Number(s.match(/startxref\n(\d+)\n%%EOF\n$/)![1]);
  assert.equal(s.slice(start, start + 4), "xref");
  const [, count] = s.slice(start).match(/^xref\n0 (\d+)\n/)!;
  const rows = s.slice(start).split("\n").slice(3, 2 + Number(count));
  rows.forEach((row, i) => assert.equal(s.slice(Number(row.slice(0, 10)), Number(row.slice(0, 10)) + String(i + 1).length + 6), `${i + 1} 0 obj`));
}

function pdfinfo(pdf: Uint8Array): string | null {
  try {
    const f = join(mkdtempSync(join(tmpdir(), "crest-pdf-")), "t.pdf");
    writeFileSync(f, pdf);
    return execFileSync("pdfinfo", [f], { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === "ENOENT") return null; // poppler not installed: xref checks still ran
    throw e;
  }
}

describe("view PDF writer", () => {
  it("writes a valid one-page landscape PDF with the picture and the text", () => {
    const pdf = buildPdf([{ texts: [{ x: 30, y: 40, size: 12, text: "Crest — Flood view (a) \\ b" }], images: [{ jpeg: JPEG, width: 8, height: 4, x: 30, y: 60, w: 400, h: 200 }] }], LETTER_LANDSCAPE, "Crest — Flood view: Swan Buffalo Detention 12");
    const s = latin1(pdf);
    assert.match(s, /^%PDF-1\.4\n/);
    assert.match(s, /\/MediaBox \[0 0 792 612\]/);
    assert.match(s, /\/Filter \/DCTDecode \/Length \d+/);
    assert.ok(s.includes("(Crest \x97 Flood view \\(a\\) \\\\ b) Tj"), "WinAnsi em dash and escaped ( ) \\");
    assert.ok(s.includes(latin1(JPEG)), "JPEG bytes embedded as is");
    checkXref(pdf);
    const info = pdfinfo(pdf);
    if (info) {
      assert.match(info, /Pages:\s+1/);
      assert.match(info, /Page size:\s+792 x 612 pts \(letter\)/);
      assert.match(info, /Title:\s+Crest — Flood view: Swan Buffalo/);
    }
  });

  it("multi-page PDFs index every object", () => {
    const page = { texts: [{ x: 10, y: 10, size: 8, text: "p" }], images: [{ jpeg: JPEG, width: 8, height: 4, x: 0, y: 0, w: 8, h: 4 }] };
    const pdf = buildPdf([page, page, page]);
    checkXref(pdf);
    const info = pdfinfo(pdf);
    if (info) assert.match(info, /Pages:\s+3/);
  });

  it("Helvetica text: WinAnsi, with what it cannot show replaced", () => {
    assert.equal(winAnsi("a…b–c ≥ 2 · 3°"), "a\x85b\x96c >= 2 \xb7 3\xb0");
    assert.equal(winAnsi("漢"), "?");
    assert.deepEqual(wrap("one two three four", 10, 40), ["one two", "three", "four"]);
  });
});

describe("view export layout", () => {
  it("names the files after the dam and the view", () => {
    assert.equal(viewFileName(meta, "pdf"), "nd00199_flood_view.pdf");
    assert.equal(viewFileName({ ...meta, kind: "report" }, "png"), "nd00199_results.png");
  });

  it("header: dam, date and the key results from the analysis", () => {
    const h = viewHeader(meta);
    assert.equal(h.title, "Crest — Flood view");
    assert.ok(h.dam.startsWith(`${dam.name} (ND00199)`));
    assert.match(h.dam, /saved 2026-10-30 10:27/);
    assert.match(h.results, /Peak discharge \(routed\) [\d,]+ cfs · Flooded area [\d,]+ ac · Max depth [\d.]+ ft/);
    assert.match(h.results, /Screen: /);
  });

  it("footer: map credits as on the map, capture notes first, the disclaimer last", () => {
    const f = viewFooter("Leaflet | Basemap: USGS The National Map | PLSS: BLM CadNSDI", ["3 of 12 basemap tiles could not be read"]);
    assert.deepEqual(f, ["Note: 3 of 12 basemap tiles could not be read", "Map: Basemap: USGS The National Map | PLSS: BLM CadNSDI (Leaflet)", DISCLAIMER]);
    assert.deepEqual(viewFooter("", []), [DISCLAIMER]);
  });

  it("one page when the view fits at a readable size, slices when it is very tall", () => {
    const box = { w: 732, h: 480 };
    assert.deepEqual(pdfSlices(1800, 1000, box), [{ y: 0, h: 1000, scale: 732 / 1800 }]);
    const tall = pdfSlices(1800, 6000, box);
    assert.ok(tall.length > 1);
    assert.equal(tall.reduce((n, s) => n + s.h, 0), 6000);
    tall.forEach((s, i) => assert.equal(s.y, i ? tall[i - 1].y + tall[i - 1].h : 0));
    assert.ok(tall.every((s) => s.h * s.scale <= box.h + 1e-9 && s.scale <= 732 / 1800 + 1e-12));
  });

  it("breaks pages where a panel starts, so the map panel stays whole", () => {
    const box = { w: 732, h: 480 }; // full width: 0.4067 pt/px, 1180 rows a page; at 75 %: 1573 rows
    // header 0–460, map panel 460–1840 (1380 rows: too tall at full width, fits at 84 %), then 1840–3600
    const s = pdfSlices(1800, 3600, box, [0, 120, 460, 1840, 2600]);
    assert.deepEqual(s.map((x) => [x.y, x.h]), [[0, 460], [460, 1380], [1840, 760], [2600, 1000]]);
    assert.ok(s[1].scale < 732 / 1800 && s[1].scale >= (732 / 1800) * 0.75);
    // a panel start wins over a part of a panel further down, which is used only when no panel start fits
    assert.deepEqual(pdfSlices(1800, 3600, box, [0, 460, 1840], [1000, 1500]).map((x) => x.y), [0, 460, 1840, 3020]);
    assert.deepEqual(pdfSlices(1800, 3600, box, [0, 3000], [1500]).map((x) => x.y), [0, 1500, 3000]);
    // a panel taller than the smallest allowed scale is cut mid-panel at full width
    assert.deepEqual(pdfSlices(1800, 4000, box, [0, 3990]).map((x) => x.h), [1180, 1180, 1180, 460]);
  });

  it("the pages keep the picture inside the page and above the footer", () => {
    const footer = viewFooter("Leaflet | Basemap: USGS The National Map", []);
    const box = pictureBox(meta, footer);
    const slices = pdfSlices(1600, 1000, box);
    const pages = viewPdfPages(meta, footer, slices.map((s) => ({ jpeg: JPEG, width: 1600, height: s.h, scale: s.scale })));
    assert.equal(pages.length, 1);
    const im = pages[0].images[0];
    assert.ok(im.x >= 0 && im.x + im.w <= LETTER_LANDSCAPE.w + 1e-9);
    const firstFoot = Math.min(...pages[0].texts.filter((t) => t.size === 7.5).map((t) => t.y - t.size));
    assert.ok(im.y + im.h <= firstFoot, `picture ${im.y + im.h} overlaps the footer at ${firstFoot}`);
    assert.ok(pages[0].texts.some((t) => t.text.startsWith("Peak discharge")));
    checkXref(buildPdf(pages));
  });
});
