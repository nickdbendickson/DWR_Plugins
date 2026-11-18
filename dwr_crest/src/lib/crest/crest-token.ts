// The desktop server (crest-desktop/crest/__main__.py) opens the desk at index.html#crest-token=<token>. The URL
// fragment is never sent over HTTP, so no request to the loopback server (from this page or from another local
// process or OS user) can read the per-launch API token; the server has no endpoint that hands it out and never
// writes it into the page markup. On first load the token is moved from the fragment into sessionStorage (so a
// reload of the same window still works) and stripped from the address with history.replaceState. Every /api/
// call sends it back in the X-Crest-Token header. On the web desk there is no fragment; the token is "" and
// nothing breaks.
const FRAGMENT_KEY = "crest-token";
const STORAGE_KEY = "crest-token";

/** The token in a location hash such as "#crest-token=abc" or "#x=1&crest-token=abc" ("" when absent), and the
 * hash with that parameter removed. */
export function splitTokenFragment(hash: string): { token: string; rest: string } {
  const raw = hash.startsWith("#") ? hash.slice(1) : hash;
  let token = "";
  const kept: string[] = [];
  for (const part of raw ? raw.split("&") : []) {
    const eq = part.indexOf("=");
    if (eq > 0 && part.slice(0, eq) === FRAGMENT_KEY) {
      try {
        token = decodeURIComponent(part.slice(eq + 1));
      } catch {
        token = "";
      }
    } else if (part) {
      kept.push(part);
    }
  }
  return { token, rest: kept.length ? `#${kept.join("&")}` : "" };
}

function storage(): Storage | undefined {
  try {
    return typeof sessionStorage === "undefined" ? undefined : sessionStorage;
  } catch {
    return undefined;
  }
}

/** Move a token from the address fragment into sessionStorage and strip it from the address bar. */
export function adoptFragmentToken(): string {
  if (typeof location === "undefined") return "";
  const { token, rest } = splitTokenFragment(location.hash || "");
  if (!token) return "";
  try {
    storage()?.setItem(STORAGE_KEY, token);
  } catch {
    /* private mode: keep it in memory only */
  }
  try {
    if (typeof history !== "undefined") history.replaceState(history.state, "", `${location.pathname}${location.search}${rest}`);
  } catch {
    /* ignore */
  }
  return token;
}

let cached = adoptFragmentToken();

export async function crestToken(): Promise<string> {
  if (cached) return cached;
  cached = adoptFragmentToken();
  if (!cached) {
    try {
      cached = storage()?.getItem(STORAGE_KEY) || "";
    } catch {
      cached = "";
    }
  }
  return cached;
}
