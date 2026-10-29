/**
 * Save a text export (CSV, dam.json, LISFLOOD .par/.bci/.bdy).
 *
 * In a browser this is the usual blob download. Inside the Crest desktop window (pywebview) blob downloads do
 * nothing, so the desktop exposes `window.pywebview.api.save_file(name, text)`, which opens the native Save dialog
 * and writes the file. Detection is by that bridge only, so the web desk behaves exactly as before.
 */

export type SaveResult = { status: "saved"; path?: string } | { status: "cancelled" } | { status: "error"; message: string };

type DesktopApi = {
  save_file?: (name: string, text: string) => Promise<{ saved?: string; cancelled?: boolean; error?: string }>;
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

function browserDownload(name: string, text: string, type: string): void {
  const url = URL.createObjectURL(new Blob([text], { type }));
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
