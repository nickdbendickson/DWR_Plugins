/**
 * The click-through: open the desk on its first sample dam and walk the eight steps (Guided setup, Record, Breach,
 * Flood, Results, Dashboard, Screen, Report), doing on each what a user does there, and fail on anything a user would
 * see as broken: an uncaught error, a console error, a request the Content-Security-Policy blocked, a request to a
 * host the desk is not supposed to call. The same walk runs against Crest.html, the desktop bundle and the web app.
 */
import assert from "node:assert/strict";

import { fakeNetwork } from "./fake-network.mjs";

const STEPS = [
  "Guided setup",
  "Record",
  "Breach",
  "Flood",
  "Results",
  "Dashboard",
  "Screen",
  "Report",
];

/** Collect what must stay empty for the page's whole life. Call before goto. */
export async function watch(page, ownOrigin) {
  const problems = [];
  page.on("pageerror", (e) => problems.push(`uncaught error: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error") problems.push(`console error: ${m.text().slice(0, 400)}`);
  });
  await page.addInitScript(() => {
    document.addEventListener("securitypolicyviolation", (e) => {
      console.error(`CSP blocked ${e.violatedDirective}: ${e.blockedURI || "(inline)"}`);
    });
  });
  const net = await fakeNetwork(page, ownOrigin);
  return { problems, net };
}

const step = (page, name) => page.getByRole("button", { name, exact: true }).first();

/**
 * Walk the desk. `url` is where it is served (or a file: URL for Crest.html). Returns what was seen, for the caller's
 * own assertions (e.g. that the policy header was present).
 */
export async function walkDesk(page, url, { ownOrigin = new URL(url).origin } = {}) {
  const { problems, net } = await watch(page, ownOrigin);
  const response = await page.goto(url, { waitUntil: "load" });

  // The desk opens on Guided setup with the first sample dam picked and its breach already computed.
  await step(page, "Guided setup").waitFor({ timeout: 30_000 });
  for (const name of STEPS)
    await assert.doesNotReject(step(page, name).waitFor(), `step button "${name}"`);
  await page.getByText("Swan Buffalo Detention 12").first().waitFor();

  // Settings: the shared defaults open from the header, a changed value saves, and the panel closes.
  await page.getByTestId("settings-toggle").click();
  await page.getByTestId("settings-panel").waitFor();
  assert.equal(await page.getByTestId("setting-sunny_day_start_pool").inputValue(), "max_pool");
  assert.equal(await page.getByTestId("setting-failure_mode").inputValue(), "piping");
  await page.getByTestId("setting-manning_n").fill("0.05");
  await page.getByTestId("settings-save").click();
  await page.getByTestId("settings-note").waitFor();
  await page.getByTestId("settings-close").click();
  await page.getByTestId("settings-panel").waitFor({ state: "detached" });

  // Record: the record form, and an edit goes through to the engine (the height field feeds the breach).
  await step(page, "Record").click();
  await page.getByRole("heading", { name: "Dam record" }).waitFor();
  await page.getByRole("heading", { name: "Tied to NAVD88" }).waitFor();

  // Breach: a peak outflow in cfs.
  await step(page, "Breach").click();
  await page.getByRole("heading", { name: "Outflow" }).waitFor();
  const breachText = await page.locator("body").innerText();
  assert.match(breachText, /[\d,]+\s*cfs/, "Breach step shows a peak outflow");
  // Advanced: the breach starts at the top of dam; an entered elevation moves it, and the reset puts it back.
  const poolUsed = () => page.getByTestId("start-pool-used").innerText();
  assert.match(await poolUsed(), /top of dam/);
  await page.getByTestId("breach-advanced").locator("summary").click();
  await page.getByTestId("start-pool").selectOption("elevation");
  await page.getByTestId("start-pool-ft").fill("1065");
  await page.waitForFunction(() =>
    /entered elevation, 1,065\.0 ft/.test(document.querySelector('[data-testid="start-pool-used"]')?.textContent ?? ""),
  );
  await page.getByTestId("differs-from-shared").first().waitFor();
  await page.getByTestId("reset-to-shared").click();
  await page.waitForFunction(() =>
    /top of dam/.test(document.querySelector('[data-testid="start-pool-used"]')?.textContent ?? ""),
  );

  // Flood: the map draws, the terrain layer builds from (stubbed) 3DEP in the worker, and the 911 lookup lists points.
  await step(page, "Flood").click();
  await page.getByRole("heading", { name: "Layout & schematic flood" }).waitFor();
  await page.locator(".leaflet-container").first().waitFor();
  await page.getByRole("button", { name: "Build the inundation layer" }).first().click();
  // The button reads "Building…" while it runs and "Build again" once a layer is saved with the dam.
  await page.getByRole("button", { name: "Build again" }).first().waitFor({ timeout: 180_000 });
  assert.ok(net.served.dem > 0, "the terrain layer asked 3DEP for terrain");
  await page.getByRole("button", { name: "Look up 911 address points" }).click();
  await page.waitForFunction(() => /address point/i.test(document.body.innerText), null, {
    timeout: 60_000,
  });
  assert.ok(net.served.nd911 > 0, "the 911 lookup asked the ND GIS Hub service");

  // Results: the comparison view.
  await step(page, "Results").click();
  await page.getByRole("heading", { name: /Results: imported model runs/ }).waitFor();

  // Dashboard: the per-structure table.
  await step(page, "Dashboard").click();
  await page.getByRole("button", { name: /Depth ft/ }).waitFor();

  // Screen: the screening view names a class or asks for review.
  await step(page, "Screen").click();
  await page.waitForFunction(() => /(High|Medium|Low|Review)/.test(document.body.innerText));

  // Report: the paper report and the exports; dam.json downloads.
  await step(page, "Report").click();
  await page.getByRole("heading", { name: "Export" }).waitFor();
  const [download] = await Promise.all([
    page.waitForEvent("download", { timeout: 30_000 }),
    page.getByRole("button", { name: "dam.json", exact: true }).click(),
  ]);
  const damJson = JSON.parse(
    await (
      await download.createReadStream()
    )
      .toArray()
      .then((c) => Buffer.concat(c).toString("utf8")),
  );
  assert.ok(damJson && typeof damJson === "object", "dam.json is JSON");
  // The record says what it used of the shared defaults (with the value saved in Settings above).
  assert.equal(damJson.assumptions?.tool, "Crest");
  assert.equal(damJson.assumptions.used.sunny_day_start_pool, "max_pool");
  assert.equal(damJson.assumptions.shared_values.manning_n, 0.05);
  // The Breach step's reset put the record on the saved value, so nothing differs.
  assert.equal(damJson.assumptions.used.manning_n, 0.05);
  assert.equal(damJson.manning_n_floodplain, 0.05);
  assert.deepEqual(damJson.assumptions.differs, {});

  await page.waitForTimeout(500);
  assert.deepEqual(
    net.unexpected,
    [],
    "the desk called a host the test does not know (add it to e2e/fake-network.mjs and csp.ts)",
  );
  assert.deepEqual(problems, [], "the page reported problems");
  return { response, net, damJson };
}
