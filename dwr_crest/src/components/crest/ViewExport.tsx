import { type RefObject, useState } from "react";
import { saveBytes } from "@/lib/crest/save";
import { captureView, NO_EXPORT_ATTR, viewFileName, type ViewMeta, viewPdf, viewPng } from "@/lib/crest/viewexport";

type Props = Omit<ViewMeta, "savedAt"> & { target: RefObject<HTMLElement | null> };

/** "Save as PDF…" and "Save image…" for the view in `target` (left out of the picture itself). */
export function ViewExport({ target, ...meta }: Props) {
  const [busy, setBusy] = useState<"pdf" | "png" | null>(null);
  const [status, setStatus] = useState<{ text: string; error?: boolean } | null>(null);
  const run = async (ext: "pdf" | "png") => {
    const el = target.current;
    if (!el) {
      setStatus({ text: "Nothing to save yet: the view is still loading.", error: true });
      return;
    }
    setBusy(ext);
    setStatus({ text: ext === "pdf" ? "Making the PDF…" : "Making the image…" });
    const name = viewFileName(meta, ext);
    try {
      const cap = await captureView(el);
      const full = { ...meta, savedAt: new Date() };
      const bytes = ext === "pdf" ? await viewPdf(cap, full) : await viewPng(cap, full);
      const r = await saveBytes(name, bytes, ext === "pdf" ? "application/pdf" : "image/png");
      const note = cap.notes.length ? ` Note: ${cap.notes.join(" ")}` : "";
      if (r.status === "saved") setStatus({ text: `${r.path ? `Saved ${r.path}` : `Downloaded ${name}`}.${note}` });
      else if (r.status === "cancelled") setStatus(null);
      else setStatus({ text: `Could not save ${name}: ${r.message}`, error: true });
    } catch (e) {
      setStatus({ text: `Could not save ${name}: ${(e as Error)?.message ?? String(e)}`, error: true });
    } finally {
      setBusy(null);
    }
  };
  const btn =
    "inline-flex h-10 items-center rounded-sm border border-line bg-paper px-3 text-sm text-ink disabled:cursor-not-allowed disabled:opacity-50";
  return (
    <div {...{ [NO_EXPORT_ATTR]: "" }} data-testid="view-export" className="flex flex-col items-end gap-1">
      <div className="flex flex-wrap gap-2">
        <button type="button" className={btn} disabled={busy !== null} onClick={() => void run("pdf")}>
          Save as PDF…
        </button>
        <button type="button" className={btn} disabled={busy !== null} onClick={() => void run("png")}>
          Save image…
        </button>
      </div>
      {status ? (
        <p role="status" data-testid="view-export-status" className={`max-w-md text-right text-xs ${status.error ? "text-amber" : "text-muted"}`}>
          {status.text}
        </p>
      ) : null}
    </div>
  );
}
