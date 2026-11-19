/**
 * A tiny PDF writer for the view export: pages with JPEG images and Helvetica text. No dependency and no network, so it
 * works offline and in the one-file Crest.html. Coordinates are PDF points measured from the top-left of the page.
 */

export type PdfText = { x: number; y: number; size: number; text: string; bold?: boolean; gray?: number };
/** A baseline JPEG (what canvas.toDataURL("image/jpeg") gives), drawn at x, y with size w × h points. */
export type PdfImage = { jpeg: Uint8Array; width: number; height: number; x: number; y: number; w: number; h: number };
export type PdfPage = { texts: PdfText[]; images: PdfImage[] };

/** US Letter landscape, in points. */
export const LETTER_LANDSCAPE = { w: 792, h: 612 };

// WinAnsiEncoding bytes 0x80-0x9F that differ from Latin-1.
const WIN_ANSI: Record<string, number> = {
  "€": 0x80, "‚": 0x82, "ƒ": 0x83, "„": 0x84, "…": 0x85, "†": 0x86, "‡": 0x87, "ˆ": 0x88, "‰": 0x89, "Š": 0x8a,
  "‹": 0x8b, "Œ": 0x8c, "Ž": 0x8e, "‘": 0x91, "’": 0x92, "“": 0x93, "”": 0x94, "•": 0x95, "–": 0x96, "—": 0x97,
  "˜": 0x98, "™": 0x99, "š": 0x9a, "›": 0x9b, "œ": 0x9c, "ž": 0x9e, "Ÿ": 0x9f,
};
const FALLBACK: Record<string, string> = { "→": "->", "←": "<-", "≥": ">=", "≤": "<=", "≈": "~", "−": "-", "×": "x", "\u00a0": " " };

/** Text as WinAnsi bytes (one char per byte, 0-255), with characters Helvetica cannot show replaced. */
export function winAnsi(text: string): string {
  let out = "";
  for (const ch of String(text)) {
    const code = ch.codePointAt(0) ?? 63;
    if (ch in FALLBACK) out += FALLBACK[ch];
    else if (code === 0x09) out += " ";
    else if (code >= 0x20 && code < 0x7f) out += ch;
    else if (ch in WIN_ANSI) out += String.fromCharCode(WIN_ANSI[ch]);
    else if (code >= 0xa0 && code <= 0xff) out += ch;
    else out += "?";
  }
  return out;
}

const escapePdf = (s: string) => s.replace(/[\\()]/g, (c) => "\\" + c);

/** Approximate Helvetica width (points) of a string: good enough for wrapping a header line. */
export function textWidth(text: string, size: number, bold = false): number {
  let em = 0;
  for (const ch of text) {
    if (" il.,:;'|!".includes(ch)) em += 0.28;
    else if ("ftrjI()[]-".includes(ch)) em += 0.36;
    else if ("mwMW@".includes(ch)) em += 0.86;
    else if (ch >= "A" && ch <= "Z") em += 0.68;
    else em += 0.56;
  }
  return em * size * (bold ? 1.06 : 1);
}

/** Word-wrap to a width in points. */
export function wrap(text: string, size: number, width: number, bold = false): string[] {
  const lines: string[] = [];
  let line = "";
  for (const word of String(text).split(/\s+/).filter(Boolean)) {
    const next = line ? `${line} ${word}` : word;
    if (line && textWidth(next, size, bold) > width) {
      lines.push(line);
      line = word;
    } else line = next;
  }
  if (line) lines.push(line);
  return lines;
}

const latin1 = (s: string) => {
  const b = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) b[i] = s.charCodeAt(i) & 0xff;
  return b;
};

const fmt = (n: number) => (Math.round(n * 100) / 100).toString();

/** Build the PDF file. */
/** A PDF text string for the document info (UTF-16BE with a BOM, so any title shows right in viewers). */
export function utf16Hex(s: string): string {
  let hex = "FEFF";
  for (let i = 0; i < s.length; i++) hex += s.charCodeAt(i).toString(16).padStart(4, "0").toUpperCase();
  return hex;
}

export function buildPdf(pages: PdfPage[], size = LETTER_LANDSCAPE, title = "Crest"): Uint8Array {
  if (pages.length === 0) throw new Error("a PDF needs at least one page");
  const chunks: Uint8Array[] = [];
  const offsets: number[] = [];
  let length = 0;
  const push = (b: Uint8Array) => {
    chunks.push(b);
    length += b.length;
  };
  const str = (s: string) => push(latin1(s));
  const obj = (n: number, body: () => void) => {
    offsets[n] = length;
    str(`${n} 0 obj\n`);
    body();
    str("\nendobj\n");
  };

  // 1 catalog, 2 pages, 3 Helvetica, 4 Helvetica-Bold, 5 info, then per page: page, contents, images.
  let next = 6;
  const plan = pages.map((p) => {
    const page = next++;
    const contents = next++;
    const images = p.images.map(() => next++);
    return { p, page, contents, images };
  });

  str("%PDF-1.4\n%\xe2\xe3\xcf\xd3\n");
  obj(1, () => str("<< /Type /Catalog /Pages 2 0 R >>"));
  obj(2, () => str(`<< /Type /Pages /Count ${plan.length} /Kids [${plan.map((x) => `${x.page} 0 R`).join(" ")}] >>`));
  obj(3, () => str("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"));
  obj(4, () => str("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"));
  obj(5, () => str(`<< /Title <${utf16Hex(title)}> /Producer (Crest) >>`));

  for (const { p, page, contents, images } of plan) {
    const xobjects = images.map((n, i) => `/Im${i} ${n} 0 R`).join(" ");
    obj(page, () =>
      str(
        `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${size.w} ${size.h}] /Contents ${contents} 0 R ` +
          `/Resources << /Font << /F1 3 0 R /F2 4 0 R >>${xobjects ? ` /XObject << ${xobjects} >>` : ""} >> >>`,
      ),
    );
    let ops = "";
    p.images.forEach((im, i) => {
      ops += `q ${fmt(im.w)} 0 0 ${fmt(im.h)} ${fmt(im.x)} ${fmt(size.h - im.y - im.h)} cm /Im${i} Do Q\n`;
    });
    for (const t of p.texts) {
      const g = t.gray ?? 0;
      ops += `BT /${t.bold ? "F2" : "F1"} ${fmt(t.size)} Tf ${fmt(g)} g ${fmt(t.x)} ${fmt(size.h - t.y)} Td (${escapePdf(winAnsi(t.text))}) Tj ET\n`;
    }
    obj(contents, () => {
      str(`<< /Length ${ops.length} >>\nstream\n`);
      str(ops);
      str("\nendstream");
    });
    p.images.forEach((im, i) =>
      obj(images[i], () => {
        str(
          `<< /Type /XObject /Subtype /Image /Width ${im.width} /Height ${im.height} /ColorSpace /DeviceRGB ` +
            `/BitsPerComponent 8 /Filter /DCTDecode /Length ${im.jpeg.length} >>\nstream\n`,
        );
        push(im.jpeg);
        str("\nendstream");
      }),
    );
  }

  const xref = length;
  str(`xref\n0 ${next}\n0000000000 65535 f \n`);
  for (let n = 1; n < next; n++) str(`${String(offsets[n]).padStart(10, "0")} 00000 n \n`);
  str(`trailer\n<< /Size ${next} /Root 1 0 R /Info 5 0 R >>\nstartxref\n${xref}\n%%EOF\n`);

  const out = new Uint8Array(length);
  let at = 0;
  for (const c of chunks) {
    out.set(c, at);
    at += c.length;
  }
  return out;
}
