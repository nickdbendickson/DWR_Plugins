import { getGlobalStartContext } from "@tanstack/react-start";
import { createRouter } from "@tanstack/react-router";
import { AppErrorComponent } from "@/lib/error-component";
import { routeTree } from "./routeTree.gen";

/**
 * The Content-Security-Policy nonce for this request (src/server.ts makes one per request and sends it in the
 * header). On the server the router puts it on the inline hydration script and in a `csp-nonce` meta tag; in the
 * browser getGlobalStartContext() is undefined and the router reads the nonce back from that meta tag by itself.
 */
function requestNonce(): string | undefined {
  try {
    return (getGlobalStartContext() as { nonce?: string } | undefined)?.nonce;
  } catch {
    return undefined; // no request context (a build-time call): no nonce
  }
}

export function getRouter() {
  const nonce = requestNonce();
  return createRouter({
    routeTree,
    defaultErrorComponent: AppErrorComponent,
    ...(nonce ? { ssr: { nonce } } : {}),
  });
}
