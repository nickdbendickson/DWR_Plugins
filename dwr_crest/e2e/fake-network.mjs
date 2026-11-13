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

/** The box and size of an exportImage request. */
export function demBox(url) {
  const q = new URL(url).searchParams;
  const [w, s, e, n] = q.get("bbox").split(",").map(Number);
  const [width, height] = q.get("size").split(",").map(Number);
  return { w, s, e, n, width, height };
}

/**
 * The fixture terrain on the requested box: nearest cell, the edge cell outside the fixture. Little-endian float32.
 * `shift` ([degrees east, degrees north]) moves the box first, so a structure anywhere in the state can be given the
 * ND00199 terrain (the batch walk).
 */
export function demResponse(url, shift = [0, 0]) {
  const b = demBox(url);
  const { width, height } = b;
  const [w, e] = [b.w + shift[0], b.e + shift[0]];
  const [s, n] = [b.s + shift[1], b.n + shift[1]];
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

/** Where the fixture's dam (ND00199) is in the inventory. */
const HOME = [-97.39113, 46.98337];

/**
 * Terrain for the batch walk, as fakeNetwork's `dem`: every structure the batch runs gets the ND00199 terrain, moved
 * to where the structure is, so any nine structures in the inventory finish like ND00199 does. The batch's first
 * request for a structure is a 1.2 km square centred on it (batch.ts nearRequest); that centre is the structure's
 * anchor, and every later request is answered for the nearest anchor. `rule(n)` says what the n-th structure seen
 * (from 0) gets: "ok", "fail" (the connection drops) or "hold" (no answer until `set` changes the rule).
 */
export function batchTerrain(rule = () => "ok") {
  const anchors = [];
  /** Terrain requests seen for each structure, in the order of `anchors`. */
  const asked = [];
  let waiting = [];
  const km = (a, b) =>
    Math.hypot((a[0] - b[0]) * 111.32 * Math.cos((a[1] * Math.PI) / 180), (a[1] - b[1]) * 110.57);
  const anchorOf = (url) => {
    const b = demBox(url);
    const c = [(b.w + b.e) / 2, (b.s + b.n) / 2];
    let best = -1;
    for (let i = 0; i < anchors.length; i++)
      if (best < 0 || km(anchors[i], c) < km(anchors[best], c)) best = i;
    const near = b.width === b.height && Math.abs((b.n - b.s) * 110.57 - 1.2) < 0.05;
    if (near && (best < 0 || km(anchors[best], c) > 0.2)) best = anchors.push(c) - 1;
    if (best >= 0) asked[best] = (asked[best] ?? 0) + 1;
    return best;
  };
  return {
    anchors,
    asked,
    /** Change the rule; requests on hold are decided again. */
    set(next) {
      rule = next;
      for (const go of waiting.splice(0)) go();
    },
    async dem(url) {
      const i = anchorOf(url);
      for (;;) {
        const r = i < 0 ? "ok" : rule(i);
        if (r === "fail") return { fail: true };
        if (r !== "hold")
          return { shift: i < 0 ? [0, 0] : [HOME[0] - anchors[i][0], HOME[1] - anchors[i][1]] };
        await new Promise((go) => waiting.push(go));
      }
    },
  };
}

/**
 * Install the routes on a Playwright page or context. Returns { unexpected: string[], served: Record<string, number> }.
 * `dem(url)`, when given, decides each terrain request: it resolves to { shift } (see demResponse) or to
 * { fail: true } for a dropped connection, and a request waits for as long as the promise does.
 */
export async function fakeNetwork(target, ownOrigin, { dem } = {}) {
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
        const how = dem ? await dem(url) : {};
        if (how.fail) return route.abort("internetdisconnected");
        return route.fulfill({
          status: 200,
          contentType: "application/octet-stream",
          headers: CORS,
          body: demResponse(url, how.shift),
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
