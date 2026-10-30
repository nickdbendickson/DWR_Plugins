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
    assert.deepEqual(stale.map(([rel]) => rel), []);
  });

  it("runs the shared engine: DWR-2801 H 27.50 / V 1615.5 / SA 154.5 -> 28,671 cfs", async () => {
    const m = await import(pathToFileURL(join(web, "assets", "engine.js")).href);
    const s = m.SAMPLE_DAMS.find((x) => x.nid === "ND00199");
    const a = m.analyze({ ...s, heightFt: 27.5, storageAcFt: 1615.5, surfaceAreaAc: 154.5, failureMode: "piping", method: "froehlich" });
    assert.equal(Math.round(a.breach.peakCfs), 28671);
  });

  it("can reach the BLM PLSS service: no Content-Security-Policy in the page or the local server, URL in the bundle", () => {
    const html = readFileSync(join(web, "index.html"), "utf8");
    assert.doesNotMatch(html, /Content-Security-Policy/i);
    const server = readFileSync(join(repo, "crest-desktop", "crest", "__main__.py"), "utf8");
    assert.doesNotMatch(server, /Content-Security-Policy/i);
    const js = readdirSync(join(web, "assets")).filter((f) => f.endsWith(".js"));
    assert.ok(js.some((f) => readFileSync(join(web, "assets", f), "utf8").includes("https://gis.blm.gov/arcgis/rest/services/Cadastral/BLM_Natl_PLSS_CadNSDI/MapServer")));
  });

  it("can reach the ND NG911 address point service: URL in the bundle", () => {
    const js = readdirSync(join(web, "assets")).filter((f) => f.endsWith(".js"));
    assert.ok(js.some((f) => readFileSync(join(web, "assets", f), "utf8").includes("https://services1.arcgis.com/GOcSXpzwBHyk2nog/arcgis/rest/services/NDGISHUBSiteStructureAddressPoints/FeatureServer/0")));
  });
});
