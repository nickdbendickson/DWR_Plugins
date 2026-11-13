/**
 * The network for the browser click-through (e2e/): nothing leaves the machine. Every request the desk makes to a
 * public service is answered here, so the walk runs the same in CI, offline, and when a service is down:
 *   - basemap / hillshade tiles           a 1 × 1 transparent PNG
 *   - USGS 3DEP exportImage               the committed ND00199 terrain (src/lib/crest/__fixtures__), resampled to
 *                                         the box and size the desk asked for, as the raw float32 the service sends
 *   - ND NG911 address points             the committed ND00199 page, then an empty page
 *   - BLM PLSS sections                   an empty feature set
 * Any other request off the page's own origin is refused and listed in `unexpected`, which the walk asserts is empty:
 * a new service the desk starts calling must be added here and to the Content-Security-Policy (src/lib/crest/csp.ts).
 */
import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";

const fx = (name) => new URL(`../src/lib/crest/__fixtures__/${name}`, import.meta.url);
const META = JSON.parse(readFileSync(fx("dem3dep_nd00199_30m.json"), "utf8"));
const U16 = new Uint16Array(
  new Uint8Array(gunzipSync(readFileSync(fx("dem3dep_nd00199_30m.u16cm.gz")))).buffer,
);
const PAGE911 = readFileSync(fx("nd911_nd00199.json"), "utf8");
const BLANK_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=",
  "base64",
);
const CORS = { "access-control-allow-origin": "*" };

/** The fixture terrain on the requested box: nearest cell, the edge cell outside the fixture. Little-endian float32. */
export function demResponse(url) {
  const q = new URL(url).searchParams;
  const [w, s, e, n] = q.get("bbox").split(",").map(Number);
  const [width, height] = q.get("size").split(",").map(Number);
  const [fw, fs, fe, fn] = META.bbox;
  const out = new Float32Array(width * height);
  const clamp = (v, hi) => Math.max(0, Math.min(hi, v));
  for (let j = 0; j < height; j++) {
    const lat = n - ((j + 0.5) / height) * (n - s);
    const fj = clamp(Math.floor(((fn - lat) / (fn - fs)) * META.height), META.height - 1);
    for (let i = 0; i < width; i++) {
      const lon = w + ((i + 0.5) / width) * (e - w);
      const fi = clamp(Math.floor(((lon - fw) / (fe - fw)) * META.width), META.width - 1);
      out[j * width + i] = U16[fj * META.width + fi] / 100 + 250;
    }
  }
  return Buffer.from(out.buffer);
}

/** Install the routes on a Playwright page or context. Returns { unexpected: string[], served: Record<string, number> }. */
export async function fakeNetwork(target, ownOrigin) {
  const unexpected = [];
  const served = { tiles: 0, dem: 0, nd911: 0, plss: 0 };
  await target.route(
    (url) => /^https?:$/.test(url.protocol) && url.origin !== ownOrigin,
    async (route) => {
      const url = route.request().url();
      const host = new URL(url).host;
      if (host === "basemap.nationalmap.gov" || host === "tile.openstreetmap.org") {
        served.tiles++;
        return route.fulfill({
          status: 200,
          contentType: "image/png",
          headers: CORS,
          body: BLANK_PNG,
        });
      }
      if (host === "elevation.nationalmap.gov" && url.includes("/exportImage")) {
        served.dem++;
        return route.fulfill({
          status: 200,
          contentType: "application/octet-stream",
          headers: CORS,
          body: demResponse(url),
        });
      }
      if (host === "services1.arcgis.com") {
        served.nd911++;
        const first = !/resultOffset=[1-9]/.test(url);
        const body = first ? PAGE911 : JSON.stringify({ type: "FeatureCollection", features: [] });
        return route.fulfill({ status: 200, contentType: "application/json", headers: CORS, body });
      }
      if (host === "gis.blm.gov") {
        served.plss++;
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          headers: CORS,
          body: JSON.stringify({ type: "FeatureCollection", features: [] }),
        });
      }
      unexpected.push(url);
      return route.abort("blockedbyclient");
    },
  );
  return { unexpected, served };
}
