/**
 * The three data bundles (nd_dams, nd_dams_dwr, nd_structures: 2.2 MB of JSON) on demand.
 *
 * Web and desktop builds: a dynamic `import()` of the JSON, which Vite keeps as its own chunk (vite.desktop.config.ts
 * keeps them out of the first parse), exactly as before. The one-file Crest.html cannot have a separate chunk, and
 * with every chunk flattened into it the 1.64 MB structures table rode along in the page's one script. There the
 * build packs each bundle as gzip + base64 (about 450 KB for all three) into data/packed.ts and leaves the JSON
 * modules out; this inflates one with DecompressionStream the first time it is asked for, or with fflate's gunzip
 * (already a dependency, for the results import) in a browser without that API.
 *
 * Node (the tests, the batch command line) and Vite dev have an empty `packed` table and take the JSON import.
 */

export type BundleName = "nd_dams" | "nd_dams_dwr" | "nd_structures";

/** gzip + base64 of each bundle when the single-file build packed it; empty otherwise (data/packed.ts). */
type Packed = Partial<Record<BundleName, string>>;

let packedTable: Promise<Packed> | null = null;
function packed(): Promise<Packed> {
  packedTable ??= import("./data/packed.ts").then(
    (m) => (m as { default?: Packed }).default ?? {},
    () => ({}),
  );
  return packedTable;
}

/** The bytes of a base64 string. */
export function base64ToBytes(b64: string): Uint8Array<ArrayBuffer> {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

/**
 * Inflate a gzip + base64 string to text: DecompressionStream where the browser has it (every current one; Safari
 * since 16.4), else fflate's gunzip, loaded on demand. Throws on a corrupt payload.
 */
export async function inflateBase64Gzip(b64: string, useStream = true): Promise<string> {
  const bytes = base64ToBytes(b64);
  const DS = (globalThis as { DecompressionStream?: typeof DecompressionStream }).DecompressionStream;
  if (useStream && typeof DS === "function" && typeof Response === "function") {
    try {
      const stream = new Blob([bytes]).stream().pipeThrough(new DS("gzip"));
      return await new Response(stream).text();
    } catch {
      // Fall through to fflate: a stream that fails mid-way (an old implementation) should not lose the bundle.
    }
  }
  const { gunzipSync, strFromU8 } = await import("fflate");
  return strFromU8(gunzipSync(bytes));
}

const loaders: Record<BundleName, () => Promise<unknown>> = {
  // The `with { type: "json" }` import attribute is required by Node's native ESM loader (the unit tests call this)
  // and accepted by Vite's production build, but Vite's dev server serves a dynamic `.json` import as a JS module,
  // which the attribute then rejects with a MIME-type error ("Expected a JSON module script … responded with
  // text/javascript"). So the attribute is dropped under Vite dev only. The `import.meta.env` guard is needed because
  // `import.meta.env` is undefined under `node --test`.
  nd_dams: () => (viteDev() ? import("./data/nd_dams.json") : import("./data/nd_dams.json", { with: { type: "json" } })),
  nd_dams_dwr: () => (viteDev() ? import("./data/nd_dams_dwr.json") : import("./data/nd_dams_dwr.json", { with: { type: "json" } })),
  nd_structures: () => (viteDev() ? import("./data/nd_structures.json") : import("./data/nd_structures.json", { with: { type: "json" } })),
};

const viteDev = () => typeof import.meta !== "undefined" && typeof import.meta.env !== "undefined" && import.meta.env.DEV === true;

const cache = new Map<BundleName, Promise<unknown>>();

/** The parsed bundle, from the packed table when the build has one, else from its JSON module. Loaded once. */
export function loadBundle<T>(name: BundleName): Promise<T> {
  let p = cache.get(name);
  if (!p) {
    p = (async () => {
      const table = await packed();
      const b64 = table[name];
      if (b64) return JSON.parse(await inflateBase64Gzip(b64)) as unknown;
      const m = await loaders[name]();
      const data = (m as { default?: unknown }).default ?? m;
      // The single-file build leaves the JSON modules out (vite.desktop.config.ts packedDataPlugin), so a missing
      // packed entry there is a build fault, not a missing file.
      if (data == null) throw new Error(`The ${name} bundle is not in this build.`);
      return data;
    })();
    cache.set(name, p);
  }
  return p as Promise<T>;
}
