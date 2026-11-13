/**
 * The desk's Content-Security-Policy, in one place for its three shells:
 *   - the web app: an HTTP header from src/server.ts, with a per-request nonce for the framework's hydration script;
 *   - the desktop app: an HTTP header from the Python server, read from web/csp.txt (written by the desktop build);
 *   - Crest.html: a <meta http-equiv> with the SHA-256 of its one inline script (written by the single-file build).
 *
 * The desk loads no third-party script, style or font: everything is bundled (which is why there is nothing to put a
 * Subresource Integrity hash on from another origin; the desktop index.html carries `integrity` on its own entry
 * script and stylesheet). The policy therefore allows scripts from the page's own origin only, and names the few
 * public services the desk reads: basemap tiles, 3DEP terrain, the ND 911 address points and the BLM PLSS sections.
 * A new service must be added here or the browser blocks it; csp.test.ts fails when a host in the source is missing.
 */

/** Basemap and hillshade tiles: shown as images, and fetched (CORS) when a map view is exported to PNG / PDF. */
export const TILE_HOSTS = [
  "https://basemap.nationalmap.gov",
  "https://tile.openstreetmap.org",
] as const;

/** Lookups the desk makes on a button: 3DEP terrain, ND NG911 address points, BLM PLSS sections. */
export const DATA_HOSTS = [
  "https://elevation.nationalmap.gov",
  "https://services1.arcgis.com",
  "https://gis.blm.gov",
] as const;

/** Hosts that only appear in attribution links (an <a href>, which a CSP does not restrict). */
export const LINK_ONLY_HOSTS = ["https://www.usgs.gov", "https://www.openstreetmap.org"] as const;

/** XML namespace names (SVG, KML): identifiers written into a file, never fetched. */
export const NAMESPACE_HOSTS = ["http://www.w3.org", "http://www.opengis.net"] as const;

/**
 * The origin a configured tile template (VITE_CREST_TILE_URL, basemap.ts) loads from, as a CSP source:
 * "https://{s}.tiles.example.org/{z}/{x}/{y}.png" -> "https://*.tiles.example.org". Null when there is none or it is
 * not an http(s) URL with a plain host.
 */
export function tileSource(template: string | undefined | null): string | null {
  const m = /^(https?):\/\/([^/?#]+)/i.exec((template ?? "").trim());
  if (!m) return null;
  const host = m[2].replace(/^\{[a-z]\}\./i, "*.");
  if (!/^(\*\.)?[a-z0-9.-]+(:\d+)?$/i.test(host)) return null;
  return `${m[1].toLowerCase()}://${host.toLowerCase()}`;
}

export type CspOptions = {
  /** The build's VITE_CREST_TILE_URL, when set: its origin joins the tile hosts. */
  tileUrl?: string | null;
  /** Per-request nonce for inline scripts (the web app's hydration script). */
  nonce?: string;
  /** `sha256-…` hashes of inline scripts (Crest.html's one script). */
  scriptHashes?: string[];
  /**
   * "header" (default): sent as an HTTP header. "meta": written into a <meta http-equiv>, where frame-ancestors is
   * not allowed (a browser ignores it there and logs a warning), so it is left out.
   */
  delivery?: "header" | "meta";
  /** The page's own origin counts ('self'). False for Crest.html opened from disk, where everything is inline. */
  self?: boolean;
};

/** The policy as directive -> sources, so a test can read it. */
export function cspDirectives(opts: CspOptions = {}): Record<string, string[]> {
  const self = opts.self === false ? [] : ["'self'"];
  const custom = tileSource(opts.tileUrl);
  const tiles =
    custom && !(TILE_HOSTS as readonly string[]).includes(custom)
      ? [...TILE_HOSTS, custom]
      : [...TILE_HOSTS];
  const script = [
    ...self,
    ...(opts.nonce ? [`'nonce-${opts.nonce}'`] : []),
    ...(opts.scriptHashes ?? []).map((h) => `'${h}'`),
  ];
  const d: Record<string, string[]> = {
    "default-src": self.length ? self : ["'none'"],
    "script-src": script.length ? script : ["'none'"],
    // Leaflet, the charts and the PNG export set element styles; a style cannot run code, so inline styles stay allowed.
    "style-src": [...self, "'unsafe-inline'"],
    "img-src": [...self, "data:", "blob:", ...tiles],
    "font-src": [...self, "data:"],
    "connect-src": [...self, "data:", "blob:", ...DATA_HOSTS, ...tiles],
    // The inundation and results workers are inlined into the bundle and started from a blob: URL.
    "worker-src": [...self, "blob:"],
    "object-src": ["'none'"],
    "base-uri": self.length ? self : ["'none'"],
    "form-action": self.length ? self : ["'none'"],
  };
  if (opts.delivery !== "meta") d["frame-ancestors"] = ["'none'"];
  return d;
}

/** The policy as the header / meta content string. */
export function contentSecurityPolicy(opts: CspOptions = {}): string {
  return Object.entries(cspDirectives(opts))
    .map(([k, v]) => `${k} ${v.join(" ")}`)
    .join("; ");
}

/** The other response headers every shell that can send headers sends beside the policy. */
export const SECURITY_HEADERS: Record<string, string> = {
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "strict-origin-when-cross-origin",
  "X-Frame-Options": "DENY",
  "Cross-Origin-Opener-Policy": "same-origin",
  "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
};
