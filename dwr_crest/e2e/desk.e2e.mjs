// Browser click-through of the desk's eight steps, on the three ways Crest ships (see walk.mjs):
//   Crest.html opened from disk, the desktop bundle behind the Python server, and the web app.
// Each also checks that its Content-Security-Policy is in force: present, blocking nothing the walk needs, and
// blocking a script injected into the page.
// Then the batch screen on the same three (see batch-walk.mjs): run, stop, reload, resume, pause when the terrain
// service drops, retry, CSV, open a row on the desk.
//
// Run with `npm run test:e2e` (after `npm run build` for the web app; without a build that one test is skipped,
// unless E2E_WEB=1 demands it). Needs Chromium: `npx playwright install chromium`. No network is used: every public
// service is stubbed (fake-network.mjs).
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { after, before, describe, it } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import { chromium } from "playwright";

import { walkBatch } from "./batch-walk.mjs";
import { walkDesk } from "./walk.mjs";

const REPO = join(dirname(fileURLToPath(import.meta.url)), "..");
const TIMEOUT = 300_000;

let browser;
before(async () => {
  browser = await chromium.launch();
});
after(async () => {
  await browser?.close();
});

const newPage = () =>
  browser.newPage({ viewport: { width: 1400, height: 1000 }, acceptDownloads: true });

/** An inline script added to the page must not run. Returns true when the policy stopped it. */
async function inlineScriptBlocked(page) {
  return page.evaluate(
    () =>
      new Promise((resolve) => {
        document.addEventListener("securitypolicyviolation", (e) => {
          if (e.violatedDirective.startsWith("script-src")) resolve(true);
        });
        const s = document.createElement("script");
        s.textContent = "window.__injected = true";
        document.head.appendChild(s);
        setTimeout(() => resolve(!window.__injected), 500);
      }),
  );
}

/** Start a child process and resolve with the first stdout / stderr line that matches `ready` (its match). */
function start(cmd, args, opts, ready) {
  const child = spawn(cmd, args, { ...opts, stdio: ["ignore", "pipe", "pipe"] });
  const found = new Promise((resolve, reject) => {
    let text = "";
    const on = (chunk) => {
      // Without colour codes: on a CI terminal vite prints the port in bold ("127.0.0.1:\x1b[1m8081").
      // eslint-disable-next-line no-control-regex
      text += String(chunk).replace(/\x1b\[[0-9;]*m/g, "");
      const m = text.match(ready);
      if (m) resolve(m);
    };
    child.stdout.on("data", on);
    child.stderr.on("data", on);
    child.on("exit", (code) =>
      reject(new Error(`${cmd} exited (${code}) before it was ready:\n${text}`)),
    );
    setTimeout(() => reject(new Error(`${cmd} was not ready in 60 s:\n${text}`)), 60_000).unref();
  });
  return { child, found };
}

/** The desktop bundle behind its Python server. */
function desktopServer() {
  const code =
    "from crest.__main__ import serve\nimport time\ns = serve(port=0)\nprint('PORT', s.server_address[1], flush=True)\ntime.sleep(3600)\n";
  return start(
    process.env.PYTHON ?? "python3",
    ["-c", code],
    { cwd: join(REPO, "crest-desktop") },
    /PORT (\d+)/,
  );
}

/** vite preview of the production build. */
function webServer() {
  return start(
    process.execPath,
    [join(REPO, "node_modules/vite/bin/vite.js"), "preview"],
    { cwd: REPO },
    /http:\/\/127\.0\.0\.1:(\d+)/,
  );
}

const built = existsSync(join(REPO, ".vercel/output/config.json"));
const skip =
  !built && process.env.E2E_WEB !== "1" ? "the web app is not built (run `npm run build`)" : false;

describe("desk click-through", { timeout: TIMEOUT * 3 }, () => {
  it("Crest.html, opened from disk", { timeout: TIMEOUT }, async () => {
    const page = await newPage();
    try {
      const url = pathToFileURL(join(REPO, "Crest.html")).href;
      await walkDesk(page, url, { ownOrigin: "null" });
      const policy = await page
        .locator('meta[http-equiv="Content-Security-Policy"]')
        .getAttribute("content");
      assert.match(policy, /script-src 'sha256-/);
      assert.doesNotMatch(policy, /script-src[^;]*'unsafe-inline'/);
      assert.equal(await inlineScriptBlocked(page), true, "an injected inline script ran");
    } finally {
      await page.close();
    }
  });

  it("the desktop bundle, served by the Python server", { timeout: TIMEOUT }, async () => {
    const { child, found } = desktopServer();
    const page = await newPage();
    try {
      const url = `http://127.0.0.1:${(await found)[1]}/`;
      const { response } = await walkDesk(page, url);
      const policy = response.headers()["content-security-policy"];
      assert.match(
        policy ?? "",
        /script-src 'self';/,
        "the desktop server sent no Content-Security-Policy",
      );
      assert.equal(response.headers()["x-content-type-options"], "nosniff");
      assert.equal(
        await page.locator('script[integrity^="sha384-"]').count(),
        1,
        "index.html entry script has no integrity hash",
      );
      assert.equal(await inlineScriptBlocked(page), true, "an injected inline script ran");
    } finally {
      await page.close();
      child.kill();
    }
  });

  it("the web app (vite preview of the production build)", { timeout: TIMEOUT, skip }, async () => {
    const { child, found } = webServer();
    const page = await newPage();
    try {
      const url = `http://127.0.0.1:${(await found)[1]}/`;
      const { response } = await walkDesk(page, url);
      const policy = response.headers()["content-security-policy"] ?? "";
      const nonce = policy.match(/script-src 'self' 'nonce-([^']+)'/);
      assert.ok(nonce, `the web app's policy has no script nonce: ${policy}`);
      assert.doesNotMatch(policy, /script-src[^;]*'unsafe-inline'/);
      // A second request gets its own nonce.
      const again = await page.request.get(url);
      assert.notEqual(again.headers()["content-security-policy"], policy);
      assert.equal(await inlineScriptBlocked(page), true, "an injected inline script ran");
    } finally {
      await page.close();
      child.kill();
    }
  });
});

describe("batch click-through", { timeout: TIMEOUT * 3 }, () => {
  it("Crest.html, opened from disk", { timeout: TIMEOUT }, async () => {
    const page = await newPage();
    try {
      await walkBatch(page, pathToFileURL(join(REPO, "Crest.html")).href, { ownOrigin: "null" });
    } finally {
      await page.close();
    }
  });

  it("the desktop bundle, served by the Python server", { timeout: TIMEOUT }, async () => {
    const { child, found } = desktopServer();
    const page = await newPage();
    try {
      await walkBatch(page, `http://127.0.0.1:${(await found)[1]}/`);
    } finally {
      await page.close();
      child.kill();
    }
  });

  it("the web app (vite preview of the production build)", { timeout: TIMEOUT, skip }, async () => {
    const { child, found } = webServer();
    const page = await newPage();
    try {
      await walkBatch(page, `http://127.0.0.1:${(await found)[1]}/`);
    } finally {
      await page.close();
      child.kill();
    }
  });
});
