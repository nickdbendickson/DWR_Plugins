// The desktop app (crest-desktop/) shows the committed static build in crest-desktop/crest/web/.
// Fail `npm test` when that build is older than src/ or stops matching the Runner on DWR-2801.
// Fuller checks (served over HTTP, offline assets): `npm run test:desktop`.
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const repo = fileURLToPath(new URL("..", import.meta.url));
const web = join(repo, "crest-desktop", "crest", "web");

describe("crest-desktop bundle", () => {
  it("is built from the current sources (else run `npm run build:desktop` and commit)", () => {
    const info = JSON.parse(readFileSync(join(web, "build-info.json"), "utf8"));
    const stale = Object.entries(info.sources).filter(([rel, digest]) => {
      const file = join(repo, rel);
      if (!existsSync(file)) return true;
      const text = readFileSync(file, "utf8").replace(/\r\n/g, "\n");
      return createHash("sha256").update(text).digest("hex") !== digest;
    });
    assert.deepEqual(
      stale.map(([rel]) => rel),
      [],
    );
  });

  it("runs the shared engine: DWR-2801 H 27.50 / V 1615.5 / SA 154.5 -> 28,671 cfs", async () => {
    const m = await import(pathToFileURL(join(web, "assets", "engine.js")).href);
    const s = m.SAMPLE_DAMS.find((x) => x.nid === "ND00199");
    const a = m.analyze({
      ...s,
      heightFt: 27.5,
      storageAcFt: 1615.5,
      surfaceAreaAc: 154.5,
      failureMode: "piping",
      method: "froehlich",
    });
    assert.equal(Math.round(a.breach.peakCfs), 28671);
  });

  it("ships the Content-Security-Policy csp.ts builds, sent by the local server, with the PLSS service allowed", async () => {
    // security-headers.json is written by the desktop build from src/lib/crest/csp.ts, which is not part of the
    // bundle (build-info.json does not list it), so a policy change is caught here.
    const child = await import("node:child_process");
    const out = child.spawnSync(
      process.execPath,
      [
        "--experimental-strip-types",
        "--no-warnings",
        "--input-type=module",
        "-e",
        `import { contentSecurityPolicy, SECURITY_HEADERS } from ${JSON.stringify(pathToFileURL(join(repo, "src/lib/crest/csp.ts")).href)}; process.stdout.write(JSON.stringify({ "Content-Security-Policy": contentSecurityPolicy(), ...SECURITY_HEADERS }));`,
      ],
      { encoding: "utf8" },
    );
    assert.equal(out.status, 0, out.stderr);
    const shipped = JSON.parse(readFileSync(join(web, "security-headers.json"), "utf8"));
    assert.deepEqual(
      shipped,
      JSON.parse(out.stdout),
      "security-headers.json is stale: run `npm run build:desktop` and commit",
    );
    assert.match(shipped["Content-Security-Policy"], /connect-src[^;]* https:\/\/gis\.blm\.gov/);
    const server = readFileSync(join(repo, "crest-desktop", "crest", "__main__.py"), "utf8");
    assert.match(server, /security-headers\.json/);
    const js = readdirSync(join(web, "assets")).filter((f) => f.endsWith(".js"));
    assert.ok(
      js.some((f) =>
        readFileSync(join(web, "assets", f), "utf8").includes(
          "https://gis.blm.gov/arcgis/rest/services/Cadastral/BLM_Natl_PLSS_CadNSDI/MapServer",
        ),
      ),
    );
  });

  it("index.html carries a Subresource Integrity hash for each script and stylesheet, matching the file", () => {
    const html = readFileSync(join(web, "index.html"), "utf8");
    const tags = [
      ...html.matchAll(
        /<(?:script|link)\b[^>]*?(?:src|href)="\.\/(assets\/[^"]+\.(?:js|css))"[^>]*>/g,
      ),
    ];
    assert.ok(tags.length >= 2, "index.html names no script or stylesheet");
    for (const [tag, name] of tags) {
      const m = tag.match(/integrity="sha384-([^"]+)"/);
      assert.ok(m, `${name} has no integrity attribute`);
      assert.equal(
        m[1],
        createHash("sha384")
          .update(readFileSync(join(web, name)))
          .digest("base64"),
        `${name} does not match its integrity hash`,
      );
      assert.match(tag, /crossorigin/, `${name}: integrity needs crossorigin on a module`);
    }
  });

  it("can reach the ND NG911 address point service: URL in the bundle", () => {
    const js = readdirSync(join(web, "assets")).filter((f) => f.endsWith(".js"));
    assert.ok(
      js.some((f) =>
        readFileSync(join(web, "assets", f), "utf8").includes(
          "https://services1.arcgis.com/GOcSXpzwBHyk2nog/arcgis/rest/services/NDGISHUBSiteStructureAddressPoints/FeatureServer/0",
        ),
      ),
    );
  });

  it("can reach the USGS 3DEP terrain service for the terrain outline: URL in the bundle", () => {
    const js = readdirSync(join(web, "assets")).filter((f) => f.endsWith(".js"));
    assert.ok(
      js.some((f) =>
        readFileSync(join(web, "assets", f), "utf8").includes(
          "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer",
        ),
      ),
    );
  });
});
