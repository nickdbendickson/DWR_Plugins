// The desktop server (crest-desktop/crest/__main__.py) hands the page its per-launch API token at GET /api/token,
// guarded by the server's Host/Origin checks. The token is never written into the page markup, so a saved copy of
// the HTML cannot carry it. It is fetched once, lazily, and kept only in memory for the X-Crest-Token header that
// every /api/ call must send. On the web desk there is no such endpoint; the fetch returns "" and nothing breaks.
let cached = "";
let inflight: Promise<string> | null = null;

export async function crestToken(): Promise<string> {
  if (cached) return cached;
  if (typeof fetch === "undefined") return "";
  if (!inflight) {
    inflight = (async () => {
      try {
        const res = await fetch("/api/token", { credentials: "same-origin" });
        if (!res.ok) return "";
        const data = (await res.json()) as { token?: unknown };
        if (typeof data.token === "string") cached = data.token;
        return cached;
      } catch {
        return "";
      } finally {
        inflight = null;
      }
    })();
  }
  return inflight;
}
