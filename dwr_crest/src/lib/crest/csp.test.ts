// The Content-Security-Policy (csp.ts): no inline or third-party script, and every service the source calls is named.
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, it } from "node:test";
import { fileURLToPath } from "node:url";

import {
  contentSecurityPolicy,
  cspDirectives,
  DATA_HOSTS,
  LINK_ONLY_HOSTS,
  NAMESPACE_HOSTS,
  SECURITY_HEADERS,
  TILE_HOSTS,
  tileSource,
} from "./csp.ts";

const SRC = fileURLToPath(new URL("../..", import.meta.url));

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    if (e.isDirectory())
      return e.name === "__fixtures__" || e.name === "__snapshots__" || e.name === "data"
        ? []
        : sourceFiles(p);
    return /\.(ts|tsx)$/.test(e.name) && !/\.test\.ts$/.test(e.name) ? [p] : [];
  });
}

describe("content security policy", () => {
  it("lets scripts come from the page's own origin only", () => {
    const d = cspDirectives();
    assert.deepEqual(d["script-src"], ["'self'"]);
    assert.deepEqual(d["object-src"], ["'none'"]);
    assert.deepEqual(d["frame-ancestors"], ["'none'"]);
    assert.deepEqual(d["base-uri"], ["'self'"]);
    for (const [name, sources] of Object.entries(d)) {
      if (name !== "style-src")
        assert.ok(!sources.includes("'unsafe-inline'"), `${name} allows inline`);
      assert.ok(!sources.includes("'unsafe-eval'"), `${name} allows eval`);
      assert.ok(!sources.includes("*") && !sources.includes("https:"), `${name} allows any host`);
    }
  });

  it("carries the nonce (web app) or the script hash (Crest.html)", () => {
    assert.match(contentSecurityPolicy({ nonce: "abc123" }), /script-src 'self' 'nonce-abc123';/);
    const file = cspDirectives({ delivery: "meta", self: false, scriptHashes: ["sha256-xyz="] });
    assert.deepEqual(file["script-src"], ["'sha256-xyz='"]);
    assert.deepEqual(file["default-src"], ["'none'"]);
    assert.equal(
      file["frame-ancestors"],
      undefined,
      "frame-ancestors is not allowed in a <meta> policy",
    );
    assert.ok(
      !contentSecurityPolicy({
        delivery: "meta",
        self: false,
        scriptHashes: ["sha256-xyz="],
      }).includes("'self'"),
    );
  });

  it("names every host the source calls (add a new service to csp.ts)", () => {
    const allowed = new Set<string>([
      ...TILE_HOSTS,
      ...DATA_HOSTS,
      ...LINK_ONLY_HOSTS,
      ...NAMESPACE_HOSTS,
    ]);
    const missing: string[] = [];
    for (const file of sourceFiles(SRC)) {
      // Comments name hosts too (docs, issue links); only string literals can be fetched.
      const text = readFileSync(file, "utf8");
      for (const m of text.matchAll(/["'`](https?:\/\/[a-zA-Z0-9.-]+)/g)) {
        if (!allowed.has(m[1])) missing.push(`${file.slice(SRC.length)}: ${m[1]}`);
      }
    }
    assert.deepEqual(missing, []);
    const d = cspDirectives();
    for (const h of TILE_HOSTS)
      assert.ok(d["img-src"].includes(h) && d["connect-src"].includes(h), h);
    for (const h of DATA_HOSTS) assert.ok(d["connect-src"].includes(h), h);
    for (const h of LINK_ONLY_HOSTS) assert.ok(!d["connect-src"].includes(h), `${h} is link-only`);
  });

  it("adds a configured tile server's origin (VITE_CREST_TILE_URL) and nothing wider", () => {
    assert.equal(
      tileSource("https://tiles.example.org/{z}/{x}/{y}.png"),
      "https://tiles.example.org",
    );
    assert.equal(
      tileSource("https://{s}.tiles.example.org/{z}/{x}/{y}.png"),
      "https://*.tiles.example.org",
    );
    assert.equal(tileSource("http://localhost:8090/{z}/{x}/{y}.png"), "http://localhost:8090");
    assert.equal(tileSource(""), null);
    assert.equal(tileSource("javascript:alert(1)"), null);
    assert.equal(tileSource("https://a.example.org 'unsafe-inline'/{z}"), null);
    assert.equal(tileSource("https://{z}/{x}/{y}"), null);
    const d = cspDirectives({ tileUrl: "https://tiles.example.org/{z}/{x}/{y}.png" });
    assert.ok(
      d["img-src"].includes("https://tiles.example.org") &&
        d["connect-src"].includes("https://tiles.example.org"),
    );
    assert.ok(!d["script-src"].includes("https://tiles.example.org"));
    assert.deepEqual(cspDirectives({ tileUrl: "nonsense" }), cspDirectives());
  });

  it("allows the inlined workers and nothing else for workers", () => {
    assert.deepEqual(cspDirectives()["worker-src"], ["'self'", "blob:"]);
  });

  it("sends the companion headers", () => {
    assert.equal(SECURITY_HEADERS["X-Content-Type-Options"], "nosniff");
    assert.equal(SECURITY_HEADERS["X-Frame-Options"], "DENY");
    for (const v of Object.values(SECURITY_HEADERS)) assert.doesNotMatch(v, /[\r\n]/);
  });
});
