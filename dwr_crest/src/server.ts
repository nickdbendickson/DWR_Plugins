/**
 * The web app's server entry: the framework's default handler, with a Content-Security-Policy and the other security
 * headers added to every response it renders. The policy carries a fresh nonce per request; the router reads the same
 * nonce (src/router.tsx) and puts it on the one inline script the framework writes (the hydration payload), so the
 * page needs no 'unsafe-inline' for scripts. Static files under /assets are served by the host without passing here;
 * vercel.json gives those the non-CSP headers.
 */
import handler, { createServerEntry } from "@tanstack/react-start/server-entry";

import { contentSecurityPolicy, SECURITY_HEADERS } from "@/lib/crest/csp";

/** 128 random bits, base64. */
export function makeNonce(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let s = "";
  for (const b of bytes) s += String.fromCharCode(b);
  return btoa(s);
}

/** A copy of the response with the security headers set (a rendered response's headers can be immutable). */
export function withSecurityHeaders(res: Response, nonce: string): Response {
  const headers = new Headers(res.headers);
  for (const [k, v] of Object.entries(SECURITY_HEADERS)) headers.set(k, v);
  headers.set(
    "Content-Security-Policy",
    contentSecurityPolicy({
      nonce,
      tileUrl: import.meta.env.VITE_CREST_TILE_URL as string | undefined,
    }),
  );
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers });
}

export default createServerEntry({
  async fetch(request) {
    const nonce = makeNonce();
    const res = await handler.fetch(request, { context: { nonce } });
    return withSecurityHeaders(res, nonce);
  },
});
