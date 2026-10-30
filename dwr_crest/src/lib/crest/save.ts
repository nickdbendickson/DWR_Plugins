/**
 * Save a text export (CSV, dam.json, LISFLOOD .par/.bci/.bdy), or a binary one (view PDF/PNG, `saveBytes`).
 *
 * In a browser this is the usual blob download. Inside the Crest desktop window (pywebview) blob downloads do
 * nothing, so the desktop exposes `window.pywebview.api.save_file(name, text)`, which opens the native Save dialog
 * and writes the file; binary files go through `save_file_b64(name, base64)`. Detection is by that bridge only, so the web desk behaves exactly as before.
 */

export type SaveResult = { status: "saved"; path?: string } | { status: "cancelled" } | { status: "error"; message: string };

type BridgeReply = { saved?: string; cancelled?: boolean; error?: string };

type DesktopApi = {
  save_file?: (name: string, text: string) => Promise<BridgeReply>;
  /** Binary files (PDF, PNG): the bytes as base64. Desktop builds older than the view export do not have it. */
  save_file_b64?: (name: string, b64: string) => Promise<BridgeReply>;
};

function desktopApi(): DesktopApi | undefined {
  if (typeof window === "undefined") return undefined;
  const api = (window as unknown as { pywebview?: { api?: DesktopApi } }).pywebview?.api;
  return api && typeof api.save_file === "function" ? api : undefined;
}

/** True when the desk runs inside the Crest desktop window (the pywebview bridge is up). */
export function isDesktop(): boolean {
  return desktopApi() !== undefined;
}

function browserDownload(name: string, data: string | Uint8Array, type: string): void {
  const url = URL.createObjectURL(new Blob([data as BlobPart], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export async function saveText(name: string, text: string, type = "text/plain"): Promise<SaveResult> {
  const api = desktopApi();
  if (!api?.save_file) {
    browserDownload(name, text, type);
    return { status: "saved" };
  }
  try {
    const r = await api.save_file(name, text);
    if (r?.error) return { status: "error", message: r.error };
    if (r?.cancelled || !r?.saved) return { status: "cancelled" };
    return { status: "saved", path: r.saved };
  } catch (e) {
    return { status: "error", message: (e as Error)?.message ?? String(e) };
  }
}

/** Bytes as base64, in chunks so a multi-megabyte PDF does not overflow the call stack. */
export function toBase64(bytes: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}

/** Save a binary export (the view PDF or PNG): a download in a browser, the native Save dialog on the desktop. */
export async function saveBytes(name: string, bytes: Uint8Array, type: string): Promise<SaveResult> {
  const api = desktopApi();
  if (!api) {
    browserDownload(name, bytes, type);
    return { status: "saved" };
  }
  if (typeof api.save_file_b64 !== "function") {
    return {
      status: "error",
      message: "this Crest desktop app is older than the PDF/image export; update crest-desktop (git pull) and restart it",
    };
  }
  try {
    const r = await api.save_file_b64(name, toBase64(bytes));
    if (r?.error) return { status: "error", message: r.error };
    if (r?.cancelled || !r?.saved) return { status: "cancelled" };
    return { status: "saved", path: r.saved };
  } catch (e) {
    return { status: "error", message: (e as Error)?.message ?? String(e) };
  }
}
