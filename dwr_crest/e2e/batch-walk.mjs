/**
 * The batch click-through: pick nine structures, run them, and do to the run what a long batch meets in real use.
 * Stop it part way, reload the page (a closed tab), resume, lose the terrain service (the batch pauses and says when
 * it tries again), have the connection come back while the service is still down (it tries again and waits longer),
 * stop the retries, resume once the service answers, then download the CSV, open a row on the desk and clear the
 * results. Nothing finished is lost at any point, and nothing is run twice.
 *
 * Every structure is given the ND00199 terrain (fake-network.mjs batchTerrain), so the nine finish the same way on any
 * machine; which of them answer, wait or fail is the walk's to say.
 */
import assert from "node:assert/strict";

import { batchTerrain } from "./fake-network.mjs";
import { watch } from "./walk.mjs";

/** Nine NID dams in nine counties, each with a height and a storage in the inventory. */
export const BATCH_IDS = [
  "ND00827",
  "ND00246",
  "ND00168",
  "ND00978",
  "ND00201",
  "ND00492",
  "ND00622",
  "ND01033",
  "ND01182",
];
const N = BATCH_IDS.length;
/** Structures let through before the first Stop. */
const FIRST = 2;

const text = async (stream) => Buffer.concat(await stream.toArray()).toString("utf8");

export async function walkBatch(page, url, { ownOrigin = new URL(url).origin } = {}) {
  const terrain = batchTerrain((n) => (n < FIRST ? "ok" : "hold"));
  const { problems, net } = await watch(page, ownOrigin, { dem: terrain.dem });
  const id = (name) => page.getByTestId(name);
  // The web app's page arrives rendered by the server: a press before its script has started is lost, so press until
  // the view changes.
  const openBatch = async () => {
    const button = page.getByRole("button", { name: "Batch", exact: true });
    for (let i = 0; ; i++) {
      await button.click();
      const shown = await id("batch-screen")
        .waitFor({ timeout: 2_000 })
        .then(() => true)
        .catch(() => false);
      if (shown) return;
      assert.ok(i < 15, "the Batch view did not open");
    }
  };
  const progress = (re) =>
    page.waitForFunction(
      (src) =>
        new RegExp(src).test(document.querySelector('[aria-live="polite"] p')?.textContent ?? ""),
      re.source,
      { timeout: 120_000 },
    );
  const banner = async () => (await id("batch-job-banner").textContent()) ?? "";
  const rowIds = () =>
    page.locator('[data-testid="batch-results"] [data-batch-id]').allTextContents();

  await page.goto(url, { waitUntil: "load" });
  await openBatch();

  // Pick the nine, one search at a time.
  await page.getByRole("button", { name: "Pick structures", exact: true }).click();
  const find = page.getByPlaceholder(/Absaraka, ND00199/);
  for (const s of BATCH_IDS) {
    await find.fill(s);
    await page.locator("ul li label input[type=checkbox]").first().check();
    await page.getByRole("button", { name: `Remove ${s}` }).waitFor();
  }
  await find.fill("");
  assert.match(
    (await id("batch-summary").textContent()) ?? "",
    new RegExp(`${N} selected · ${N} ready to run · 0 need data`),
  );

  // Run, and stop once two have finished. The rest are waiting on the terrain service; Stop does not record them.
  await page.getByRole("button", { name: `Run ${N}`, exact: true }).click();
  await progress(new RegExp(`^${FIRST} / ${N} `));
  await page.getByRole("button", { name: "Stop", exact: true }).click();
  terrain.set(() => "ok");
  await progress(/Stopped\. Resume picks up where it left off\./);
  assert.match(
    await banner(),
    new RegExp(
      `A batch of ${N} structures stopped with ${FIRST} finished and ${N - FIRST} to go\\.`,
    ),
  );
  assert.equal((await rowIds()).length, FIRST, "the two finished rows are listed");
  const firstTwo = (await rowIds()).sort();
  const askedForFirstTwo = terrain.asked.slice(0, FIRST);

  // A closed tab: the page comes back on the same selection, with the rows and the offer to resume.
  await page.reload({ waitUntil: "load" });
  await openBatch();
  await id("batch-job-banner").waitFor();
  assert.match(await banner(), new RegExp(`stopped with ${FIRST} finished and ${N - FIRST} to go`));
  await page.waitForFunction(
    (n) => document.querySelectorAll("[data-batch-id]").length === n,
    FIRST,
  );
  assert.deepEqual((await rowIds()).sort(), firstTwo, "the same two rows after the reload");
  for (const s of BATCH_IDS) await page.getByRole("button", { name: `Remove ${s}` }).waitFor();

  // The terrain service goes away: after six structures in a row cannot reach it the batch pauses, keeps what is
  // finished, and says when it tries again.
  terrain.set(() => "fail");
  await id("batch-resume").click();
  await progress(/Paused: the terrain service is not answering\. Trying again in 60 s/);
  await id("batch-stop-retrying").waitFor();
  assert.match(
    await banner(),
    new RegExp(
      `paused with ${FIRST} finished and ${N - FIRST} to go\\..*Trying again by itself in 60 s`,
    ),
  );
  assert.match(
    (await page.getByTestId("batch-results").textContent()) ?? "",
    /Could not reach the 3DEP terrain service/,
    "the failed rows say why",
  );

  // The connection comes back but the service is still down: the batch tries at once, and then waits longer.
  await page.evaluate(() => window.dispatchEvent(new Event("online")));
  await progress(/Paused: the terrain service is not answering\. Trying again in 2 min/);
  await id("batch-stop-retrying").waitFor();

  // Stop the retries, then resume by hand once the service answers: the seven left finish, the two are not rerun.
  await id("batch-stop-retrying").click();
  await progress(/Stopped\. Resume picks up where it left off\./);
  assert.match(await banner(), /stopped with 2 finished.*Automatic retries were stopped\./);
  terrain.set(() => "ok");
  await id("batch-resume").click();
  await progress(new RegExp(`^${N - FIRST} / ${N - FIRST} · Done\\.`));
  assert.equal(await id("batch-job-banner").count(), 0, "nothing is left to resume");
  assert.deepEqual((await rowIds()).sort(), [...BATCH_IDS].sort(), "every structure has one row");
  assert.equal(
    terrain.anchors.length,
    N,
    "terrain was asked for the nine structures and no other place",
  );
  assert.deepEqual(
    terrain.asked.slice(0, FIRST),
    askedForFirstTwo,
    "the two finished before the first Stop were not run again",
  );
  assert.doesNotMatch(
    (await page.getByTestId("batch-results").textContent()) ?? "",
    /Failed|Could not reach/,
    "no failed row is left",
  );

  // The CSV has the nine, all done.
  const [download] = await Promise.all([
    page.waitForEvent("download", { timeout: 30_000 }),
    page.getByRole("button", { name: "Download CSV", exact: true }).click(),
  ]);
  const csv = await text(await download.createReadStream());
  const lines = csv.split("\n").filter((l) => l && !l.startsWith("#"));
  assert.match(lines[0], /^id,kind,name,county,huc8,run_status,/);
  const status = lines[0].split(",").indexOf("run_status");
  for (const s of BATCH_IDS) {
    const line = lines.find((l) => l.startsWith(`${s},`));
    assert.ok(line, `${s} is in the CSV`);
    assert.match(line, /,done,/, `${s} is done in the CSV`);
  }
  assert.equal(lines.length, N + 1, "one CSV line per structure");
  assert.ok(status > 0);

  // A row from before the reload opens on the desk as a new record (it is rebuilt from the inventory and terrain).
  await page
    .locator("tr", { has: page.locator(`[data-batch-id="${firstTwo[0]}"]`) })
    .getByRole("button", { name: "Open in desk", exact: true })
    .click();
  await page
    .getByText(`New record from the batch for ${firstTwo[0]}.`)
    .waitFor({ timeout: 60_000 });
  await page.getByRole("button", { name: "Guided setup", exact: true }).first().waitFor();

  // Back on the batch screen the rows are still there (they are kept in the browser); Clear results removes them.
  await openBatch();
  await page.waitForFunction((n) => document.querySelectorAll("[data-batch-id]").length === n, N);
  await page.getByRole("button", { name: "Clear results", exact: true }).click();
  await page.waitForFunction(() => document.querySelectorAll("[data-batch-id]").length === 0);
  // The rows leave the screen at once and the browser's store a moment later (one IndexedDB transaction).
  await page.waitForTimeout(500);
  await page.reload({ waitUntil: "load" });
  await openBatch();
  await page.getByRole("button", { name: /^Run/ }).waitFor();
  assert.equal(await page.locator("[data-batch-id]").count(), 0, "cleared results stay cleared");
  assert.equal(await id("batch-job-banner").count(), 0);

  await page.waitForTimeout(300);
  assert.deepEqual(net.unexpected, [], "the batch called a host the test does not know");
  // The dropped connection is the walk's own doing; anything else the page reported is a failure.
  assert.deepEqual(
    problems.filter((p) => !/ERR_INTERNET_DISCONNECTED/.test(p)),
    [],
    "the page reported problems",
  );
  return { net, csv };
}
