/**
 * "Save as PDF…" / "Save image…" for the Flood and Report views: a picture of what is on screen (map, flood strip,
 * PLSS, results), with the dam, the date, the key numbers and the map credits.
 *
 * The capture is html-to-image (the page drawn into a canvas). Basemap tiles come from servers that allow it (USGS and
 * OSM send Access-Control-Allow-Origin: *); a tile that cannot be read (offline, blocked) is left blank and the file
 * carries a note saying so, instead of the export failing. The PDF is written by ./pdf.ts, so nothing else is loaded.
 */
import { num } from "./format.ts";
import { buildPdf, LETTER_LANDSCAPE, type PdfPage, type PdfText, wrap } from "./pdf.ts";
import type { Analysis, DamRecord } from "./types.ts";

export type ViewKind = "flood" | "report";

export const VIEW_TITLE: Record<ViewKind, string> = { flood: "Flood view", report: "Results" };

/** Put on anything that should not appear in the saved picture (the export buttons themselves). */
export const NO_EXPORT_ATTR = "data-crest-noexport";

/** Map controls left out of the picture: they are buttons, not information. The attribution stays. */
const SKIP_CLASSES = ["leaflet-control-zoom", "leaflet-draw", "leaflet-control-layers"];

export const DISCLAIMER =
  "Crest desk screen: schematic flood strip and desk estimates, not a model run or a mapped flood extent, and not an official hazard class.";

export type ViewMeta = {
  kind: ViewKind;
  dam: Pick<DamRecord, "name" | "nid" | "id"> & { county?: string };
  result: Analysis;
  savedAt: Date;
};

/** File name root from the dam, e.g. nd00199_flood_view. */
export function viewFileName(meta: Pick<ViewMeta, "kind" | "dam">, ext: "pdf" | "png"): string {
  const root = (meta.dam.nid || meta.dam.id || "crest").toLowerCase().replace(/[^a-z0-9]/g, "_");
  return `${root}_${meta.kind === "flood" ? "flood_view" : "results"}.${ext}`;
}

/** Local date and time, e.g. 2026-10-30 10:27 CDT. */
export function stamp(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  let zone = "";
  try {
    zone = new Intl.DateTimeFormat("en-US", { timeZoneName: "short" }).formatToParts(d).find((x) => x.type === "timeZoneName")?.value ?? "";
  } catch {
    zone = "";
  }
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}${zone ? " " + zone : ""}`;
}

/** Title line, dam line and the key results, as printed on the PDF and the image. */
export function viewHeader(meta: ViewMeta): { title: string; dam: string; results: string } {
  const { dam, result } = meta;
  const wet = result.flood.receptors.filter((r) => r.flooded).length;
  return {
    title: `Crest — ${VIEW_TITLE[meta.kind]}`,
    dam: `${dam.name}${dam.nid ? ` (${dam.nid})` : ""}${dam.county ? ` · ${dam.county}` : ""} · saved ${stamp(meta.savedAt)}`,
    results: [
      `Peak discharge (routed) ${num(result.breach.peakCfs, 0)} cfs`,
      `Flooded area ${num(result.flood.acres, 0)} ac`,
      `Max depth ${num(result.flood.maxDepthFt, 1)} ft`,
      `Max velocity (est.) ${num(result.flood.maxVelocityFps, 1)} ft/s`,
      `Wet receptors ${wet}`,
      `Screen: ${result.screen.label}`,
    ].join(" · "),
  };
}

/** Credits and notes under the picture: map attribution as shown on the map, the disclaimer, capture notes. */
export function viewFooter(attribution: string, notes: string[]): string[] {
  const credit = attribution.replace(/\s+/g, " ").replace(/^\s*Leaflet\s*\|\s*/, "").trim();
  return [...notes.map((n) => `Note: ${n}`), ...(credit ? [`Map: ${credit} (Leaflet)`] : []), DISCLAIMER];
}

export type PdfSlice = { y: number; h: number; scale: number };

/**
 * How a W × H pixel picture goes onto landscape pages. One page when it fits at a readable scale (at least
 * `minScale` of full width). Otherwise it is cut top to bottom, preferably where a panel starts (`cuts`), then
 * where a part of a panel starts (`minor`), so the map is not cut in half: a slice may shrink to `shrink` of full
 * width to keep a panel whole, and is cut mid-panel only when there is no such place. Each slice has its own
 * scale (points per pixel).
 */
export function pdfSlices(
  width: number,
  height: number,
  box: { w: number; h: number },
  cuts: number[] = [],
  minor: number[] = [],
  minScale = 0.5,
  shrink = 0.75,
): PdfSlice[] {
  const fitW = box.w / width;
  const fit = Math.min(fitW, box.h / height);
  if (fit >= fitW * minScale) return [{ y: 0, h: height, scale: fit }];
  const full = Math.max(1, Math.floor(box.h / fitW)); // rows that fit at full width
  const most = Math.max(full, Math.floor(box.h / (fitW * shrink))); // rows that fit at the smallest allowed scale
  const clean = (c: number[]) => [...new Set(c.map(Math.round))].filter((v) => v > 0 && v < height).sort((x, y) => x - y);
  const tiers = [clean(cuts), clean(minor)];
  const slices: PdfSlice[] = [];
  for (let y = 0; y < height; ) {
    let end: number;
    if (height - y <= most) end = height;
    else {
      const best = tiers.map((t) => t.filter((c) => c > y + full * 0.2 && c <= y + most).pop()).find((c) => c !== undefined);
      end = best ?? y + full;
    }
    const h = end - y;
    slices.push({ y, h, scale: Math.min(fitW, box.h / h) });
    y = end;
  }
  return slices;
}

const M = 30; // page margin, points

/** The PDF pages around already-cut JPEG slices (pure, so it is tested without a browser). */
export function viewPdfPages(
  meta: ViewMeta,
  footer: string[],
  slices: { jpeg: Uint8Array; width: number; height: number; scale: number }[],
): PdfPage[] {
  const page = LETTER_LANDSCAPE;
  const head = viewHeader(meta);
  const textW = page.w - 2 * M;
  const headTexts = (first: boolean): { texts: PdfText[]; bottom: number } => {
    const texts: PdfText[] = [];
    let y = M + 14;
    texts.push({ x: M, y, size: 15, bold: true, text: head.title });
    y += 16;
    texts.push({ x: M, y, size: 10, text: head.dam });
    if (first) {
      for (const line of wrap(head.results, 9, textW)) {
        y += 12;
        texts.push({ x: M, y, size: 9, text: line, gray: 0.15 });
      }
    }
    return { texts, bottom: y + 8 };
  };
  const footLines = footer.flatMap((f) => wrap(f, 7.5, textW));
  const footH = footLines.length * 9.5 + 14;
  return slices.map((s, i) => {
    const { texts, bottom } = headTexts(i === 0);
    const w = s.width * s.scale;
    const h = s.height * s.scale;
    let y = page.h - footH + 2;
    for (const line of footLines) {
      y += 9.5;
      texts.push({ x: M, y, size: 7.5, text: line, gray: 0.3 });
    }
    if (slices.length > 1) texts.push({ x: page.w - M - 50, y: page.h - 10, size: 7.5, text: `Page ${i + 1} of ${slices.length}`, gray: 0.3 });
    return { texts, images: [{ jpeg: s.jpeg, width: s.width, height: s.height, x: M + (page.w - 2 * M - w) / 2, y: bottom, w, h }] };
  });
}

/** Room for the picture on page 1 (the header is a few lines, the footer depends on the notes). */
export function pictureBox(meta: ViewMeta, footer: string[]): { w: number; h: number } {
  const page = LETTER_LANDSCAPE;
  const textW = page.w - 2 * M;
  const headH = 14 + 16 + wrap(viewHeader(meta).results, 9, textW).length * 12 + 8;
  const footH = footer.flatMap((f) => wrap(f, 7.5, textW)).length * 9.5 + 14;
  return { w: textW, h: page.h - M - headH - footH };
}

// ---- browser only below ----------------------------------------------------------------------------------------

/** `cuts` / `minor`: canvas rows where a panel / a part of a panel starts (places to break a PDF page). */
export type Capture = { canvas: HTMLCanvasElement; attribution: string; notes: string[]; cuts: number[]; minor: number[] };

const BLANK_PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=";

/** Tiles on screen that cannot go into the picture: not loaded, or their server does not allow reading them. */
async function unreadableTiles(el: HTMLElement): Promise<{ missing: number; total: number }> {
  const tiles = Array.from(el.querySelectorAll<HTMLImageElement>("img.leaflet-tile")).filter((t) => {
    const r = t.getBoundingClientRect();
    const box = el.getBoundingClientRect();
    return r.right > box.left && r.left < box.right && r.bottom > box.top && r.top < box.bottom;
  });
  const results = await Promise.all(
    tiles.map(async (t) => {
      if (!t.complete || t.naturalWidth === 0 || !t.currentSrc) return false;
      try {
        const ctl = new AbortController();
        const timer = setTimeout(() => ctl.abort(), 8000);
        const r = await fetch(t.currentSrc, { mode: "cors", cache: "force-cache", signal: ctl.signal });
        clearTimeout(timer);
        return r.ok;
      } catch {
        return false;
      }
    }),
  );
  return { missing: results.filter((ok) => !ok).length, total: tiles.length };
}

/** Where the view's panels (its children) and their parts (grandchildren) start, in CSS px from its top. */
function panelTops(el: HTMLElement): { cuts: number[]; minor: number[] } {
  const top = el.getBoundingClientRect().top;
  const at = (e: Element) => e.getBoundingClientRect().top - top;
  const kids = Array.from(el.children);
  return { cuts: kids.map(at), minor: kids.flatMap((k) => Array.from(k.children).map(at)) };
}

const SVG_PAINT = ["fill", "stroke", "stop-color"] as const;

/**
 * SVG paint given as CSS variables (fill="var(--color-sand)", the valley chart) is set to its computed colour
 * while the picture is taken: in the copy the variables are gone, and some engines (Qt WebEngine) then paint it
 * black. Returns the undo.
 */
function resolveSvgVars(el: HTMLElement): () => void {
  const undo: (() => void)[] = [];
  for (const node of Array.from(el.querySelectorAll<SVGElement>("svg *"))) {
    for (const a of SVG_PAINT) {
      const v = node.getAttribute(a);
      if (!v || !v.includes("var(")) continue;
      const css = getComputedStyle(node).getPropertyValue(a);
      if (!css) continue;
      node.setAttribute(a, css);
      undo.push(() => node.setAttribute(a, v));
    }
  }
  return () => undo.forEach((u) => u());
}

/** Draw the element (as it is on screen) into a canvas. */
export async function captureView(el: HTMLElement): Promise<Capture> {
  const notes: string[] = [];
  const tiles = await unreadableTiles(el);
  if (tiles.total > 0 && tiles.missing === tiles.total) notes.push("the basemap could not be read (offline or blocked by the tile server), so the map background is blank; the flood strip and drawn shapes are shown.");
  else if (tiles.missing > 0) notes.push(`${tiles.missing} of ${tiles.total} basemap tiles could not be read (offline or blocked), so parts of the map background are blank.`);
  const attribution = el.querySelector(".leaflet-control-attribution")?.textContent ?? "";
  const { toCanvas } = await import("html-to-image");
  const bg = getComputedStyle(document.body).backgroundColor || "#ffffff";
  // The Save buttons are hidden on screen while the picture is taken, so the layout around them closes up
  // (only filtering them out of the copy would leave their space empty).
  const hidden = Array.from(el.querySelectorAll<HTMLElement>(`[${NO_EXPORT_ATTR}]`));
  const before = hidden.map((h) => h.style.display);
  hidden.forEach((h) => (h.style.display = "none"));
  const restoreSvg = resolveSvgVars(el);
  let canvas: HTMLCanvasElement;
  let cuts: number[] = [];
  let minor: number[] = [];
  try {
    const rect = el.getBoundingClientRect();
    const pixelRatio = Math.max(1, Math.min(2, 14000 / Math.max(1, rect.height), 8000 / Math.max(1, rect.width)));
    const tops = panelTops(el);
    cuts = tops.cuts.map((y) => y * pixelRatio);
    minor = tops.minor.map((y) => y * pixelRatio);
    canvas = await toCanvas(el, {
      pixelRatio,
      backgroundColor: bg,
      skipFonts: true,
      cacheBust: false,
      imagePlaceholder: BLANK_PNG,
      filter: (node) =>
        !(node instanceof Element) ||
        (!node.hasAttribute(NO_EXPORT_ATTR) && !SKIP_CLASSES.some((c) => node.classList.contains(c))),
    });
  } catch (e) {
    throw new Error(`the view could not be drawn into a picture (${(e as Error)?.message ?? String(e)})`);
  } finally {
    hidden.forEach((h, i) => (h.style.display = before[i]));
    restoreSvg();
  }
  if (!canvas.width || !canvas.height) throw new Error("the view could not be drawn into a picture (empty result)");
  try {
    canvas.getContext("2d")?.getImageData(0, 0, 1, 1);
  } catch {
    throw new Error("the browser blocked reading the picture (a map tile without CORS); switch the basemap and try again");
  }
  return { canvas, attribution, notes, cuts, minor };
}

const toBytes = async (blob: Blob) => new Uint8Array(await blob.arrayBuffer());

function canvasBlob(canvas: HTMLCanvasElement, type: string, quality?: number): Promise<Blob> {
  return new Promise((resolve, reject) =>
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error(`could not encode the ${type} file`))), type, quality),
  );
}

/** PNG: the picture with the header above and the credits below. */
export async function viewPng(cap: Capture, meta: ViewMeta): Promise<Uint8Array> {
  const head = viewHeader(meta);
  const foot = viewFooter(cap.attribution, cap.notes);
  const W = cap.canvas.width;
  const k = Math.max(1, W / 1100);
  const pad = 14 * k;
  const ctx0 = document.createElement("canvas").getContext("2d")!;
  const wrapPx = (text: string, font: string) => {
    ctx0.font = font;
    const out: string[] = [];
    let line = "";
    for (const w of text.split(/\s+/)) {
      const next = line ? `${line} ${w}` : w;
      if (line && ctx0.measureText(next).width > W - 2 * pad) {
        out.push(line);
        line = w;
      } else line = next;
    }
    if (line) out.push(line);
    return out;
  };
  const f = (px: number, bold = false) => `${bold ? "bold " : ""}${Math.round(px * k)}px Helvetica, Arial, sans-serif`;
  const resLines = wrapPx(head.results, f(13));
  const footLines = foot.flatMap((t) => wrapPx(t, f(11)));
  const headH = pad + 22 * k + 18 * k + resLines.length * 17 * k + pad;
  const footH = pad + footLines.length * 15 * k + pad / 2;
  const out = document.createElement("canvas");
  out.width = W;
  out.height = Math.round(headH + cap.canvas.height + footH);
  const ctx = out.getContext("2d")!;
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, out.width, out.height);
  ctx.fillStyle = "#1f2a30";
  ctx.textBaseline = "alphabetic";
  let y = pad + 20 * k;
  ctx.font = f(20, true);
  ctx.fillText(head.title, pad, y);
  y += 18 * k;
  ctx.font = f(13);
  ctx.fillText(head.dam, pad, y);
  ctx.fillStyle = "#38444a";
  for (const l of resLines) {
    y += 17 * k;
    ctx.fillText(l, pad, y);
  }
  ctx.drawImage(cap.canvas, 0, Math.round(headH));
  ctx.fillStyle = "#5c5a52";
  ctx.font = f(11);
  y = headH + cap.canvas.height + pad;
  for (const l of footLines) {
    y += 15 * k - 4 * k;
    ctx.fillText(l, pad, y);
    y += 4 * k;
  }
  return toBytes(await canvasBlob(out, "image/png"));
}

/** PDF: one landscape Letter page, or more when a tall view would otherwise be too small to read. */
export async function viewPdf(cap: Capture, meta: ViewMeta): Promise<Uint8Array> {
  const foot = viewFooter(cap.attribution, cap.notes);
  const slices = pdfSlices(cap.canvas.width, cap.canvas.height, pictureBox(meta, foot), cap.cuts, cap.minor);
  const parts: { jpeg: Uint8Array; width: number; height: number; scale: number }[] = [];
  for (const s of slices) {
    const c = document.createElement("canvas");
    c.width = cap.canvas.width;
    c.height = s.h;
    const ctx = c.getContext("2d")!;
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, c.width, c.height);
    ctx.drawImage(cap.canvas, 0, s.y, c.width, s.h, 0, 0, c.width, s.h);
    parts.push({ jpeg: await toBytes(await canvasBlob(c, "image/jpeg", 0.9)), width: c.width, height: c.height, scale: s.scale });
  }
  return buildPdf(viewPdfPages(meta, foot, parts), LETTER_LANDSCAPE, `${viewHeader(meta).title}: ${meta.dam.name}`);
}
