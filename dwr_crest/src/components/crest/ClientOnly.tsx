import { useSyncExternalStore, type ReactNode } from "react";

/**
 * Render children only on the client, never during server-side render or the first hydration pass.
 *
 * Crest is a TanStack Start (SSR) app, and the Leaflet maps touch `window` at module load, so a map that renders on
 * the server throws `ReferenceError: window is not defined`. `useSyncExternalStore` reports `false` on the server and
 * on the first client render (its server snapshot), then `true` once mounted, so the server and the first client pass
 * agree on the fallback — no React #418 hydration mismatch — and the real child mounts only in the browser.
 */
const noopSubscribe = () => () => {};

export function ClientOnly({ children, fallback = null }: { children: ReactNode; fallback?: ReactNode }) {
  const isClient = useSyncExternalStore(
    noopSubscribe,
    () => true,
    () => false,
  );
  return <>{isClient ? children : fallback}</>;
}
