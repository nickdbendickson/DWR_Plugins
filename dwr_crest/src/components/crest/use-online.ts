import { useEffect, useState } from "react";

/**
 * `navigator.onLine`, SSR-safe. It starts `true` — the value the server always renders (there is no `navigator`
 * there) and so the value the first client render must also use, or React tears the tree down with a #418 hydration
 * mismatch. The real status is read in an effect after mount, and kept current on the online/offline events.
 *
 * Read it with this hook, never `navigator.onLine` inline during render: an inline read on a client that happens to
 * be offline disagrees with the server's `true` and mismatches.
 */
export function useOnline() {
  const [online, setOnline] = useState(true);
  useEffect(() => {
    const set = () => setOnline(typeof navigator === "undefined" ? true : navigator.onLine);
    set();
    window.addEventListener("online", set);
    window.addEventListener("offline", set);
    return () => {
      window.removeEventListener("online", set);
      window.removeEventListener("offline", set);
    };
  }, []);
  return online;
}
