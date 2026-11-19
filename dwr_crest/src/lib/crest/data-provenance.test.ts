// Bundle age (data-provenance.ts): the inventories' pull times compared to an injected "now".
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { ageWords, BUNDLE_STALE_DAYS, bundleAge, bundleAgeLine, deskBundles, staleBundleBanner } from "./data-provenance.ts";
import { loadNdData } from "./nd-dams.ts";

const NID = "2026-10-29T21:56:00Z";
const DWR = "2026-10-30T14:31:02Z";

describe("bundleAge", () => {
  it("counts whole days and months from the pull to now, and never negative", () => {
    assert.deepEqual(bundleAge(NID, new Date("2026-11-07T00:00:00Z")), { pulledUtc: NID, days: 8, months: 0, stale: false });
    assert.deepEqual(bundleAge(NID, new Date("2027-06-01T00:00:00Z")), { pulledUtc: NID, days: 214, months: 7, stale: true });
    assert.equal(bundleAge(NID, new Date("2026-10-01T00:00:00Z"))!.days, 0);
    assert.equal(bundleAge(null, new Date()), null);
    assert.equal(bundleAge("not a date", new Date()), null);
  });

  it("is stale past 180 days, not before", () => {
    const pulled = "2026-01-01T00:00:00Z";
    assert.equal(bundleAge(pulled, new Date(`2026-01-01T00:00:00Z`))!.stale, false);
    assert.equal(bundleAge(pulled, new Date(Date.parse(pulled) + BUNDLE_STALE_DAYS * 86_400_000))!.stale, false);
    assert.equal(bundleAge(pulled, new Date(Date.parse(pulled) + (BUNDLE_STALE_DAYS + 1) * 86_400_000))!.stale, true);
    assert.equal(ageWords(bundleAge(pulled, new Date("2026-01-02T00:00:00Z"))!), "1 day");
    assert.equal(ageWords(bundleAge(pulled, new Date("2026-03-01T00:00:00Z"))!), "1 month");
  });
});

describe("banner and lines", () => {
  const bundles = deskBundles({ nidFetchedUtc: NID, dwrPulledUtc: DWR });

  it("no banner while the bundles are fresh", () => {
    assert.equal(staleBundleBanner(bundles, new Date("2026-11-07T00:00:00Z")), null);
    assert.equal(bundleAgeLine(bundles[0], new Date("2026-11-07T00:00:00Z")), "NID inventory pulled 2026-10-29 (8 days old)");
  });

  it("names the stale bundle, its age and the refresh command", () => {
    const one = staleBundleBanner(deskBundles({ nidFetchedUtc: NID }), new Date("2027-06-01T00:00:00Z"));
    assert.equal(one, "NID inventory is 7 months old (pulled 2026-10-29); `npm run data:nd-dams` refreshes it.");
    const both = staleBundleBanner(bundles, new Date("2027-06-01T00:00:00Z"))!;
    assert.match(both, /^Inventory is 7 months old \(NID inventory pulled 2026-10-29; DWR dam values pulled 2026-10-30\); `npm run data:nd-dams` and `npm run data:nd-dams-dwr` refresh it\.$/);
    assert.match(bundleAgeLine(bundles[1], new Date("2027-06-01T00:00:00Z"))!, /^DWR dam values pulled 2026-10-30 \(6 months old; stale, past 180 days: `npm run data:nd-dams-dwr` refreshes it\)$/);
  });

  it("skips bundles whose source is unknown", () => {
    assert.deepEqual(deskBundles({}).length, 0);
    assert.equal(deskBundles({ structuresPulledUtc: null }).length, 1);
    assert.equal(bundleAgeLine(deskBundles({ structuresPulledUtc: null })[0], new Date()), null);
  });

  it("the committed bundles are not stale as of the day this change was made (the banner would otherwise show today)", async () => {
    const nd = await loadNdData();
    const b = deskBundles({ nidFetchedUtc: nd.nidSource.fetchedUtc, dwrPulledUtc: nd.dwrSource?.pulledUtc ?? null });
    assert.equal(staleBundleBanner(b, new Date("2026-11-07T00:00:00Z")), null);
  });
});
