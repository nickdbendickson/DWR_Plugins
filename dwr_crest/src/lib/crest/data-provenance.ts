/**
 * How old the offline dam bundles are. Each bundle records when its source was pulled (nd_dams.json `fetchedUtc`,
 * nd_dams_dwr.json and nd_structures.json `pulledUtc`), the picker footer and the Report print the date, but nothing
 * compared it to today: a desk opened a year on would still say "downloaded 2026-10-29" as if that were fine. This
 * module does the comparison once, for the picker, About the dam data, the Report and the batch CSV. `now` is passed in
 * so the tests (and a reproducible build) never read the clock.
 */

/** Days after which an inventory counts as stale: two NID release cycles. */
export const BUNDLE_STALE_DAYS = 180;

const DAY_MS = 86_400_000;

export type BundleAge = {
  /** The bundle's own pull time, as recorded. */
  pulledUtc: string;
  /** Whole days from the pull to `now` (0 when `now` is earlier, e.g. a clock set back). */
  days: number;
  /** Whole months (30.44-day), for the banner text. */
  months: number;
  /** Older than BUNDLE_STALE_DAYS. */
  stale: boolean;
};

/** Age of a bundle pulled at `pulledUtc`, as of `now`. null when the pull time is not a date. */
export function bundleAge(pulledUtc: string | null | undefined, now: Date): BundleAge | null {
  if (!pulledUtc) return null;
  const t = Date.parse(pulledUtc);
  if (!Number.isFinite(t)) return null;
  const days = Math.max(0, Math.floor((now.getTime() - t) / DAY_MS));
  return { pulledUtc, days, months: Math.floor(days / 30.44), stale: days > BUNDLE_STALE_DAYS };
}

/** "N months" / "N days" for a banner. */
export function ageWords(a: BundleAge): string {
  if (a.months >= 1) return `${a.months} month${a.months === 1 ? "" : "s"}`;
  return `${a.days} day${a.days === 1 ? "" : "s"}`;
}

export type BundleInfo = { label: string; pulledUtc: string | null | undefined; refresh: string };

/**
 * The amber banner for the picker and About the dam data when any bundle is past BUNDLE_STALE_DAYS, else null.
 * "Inventory is 7 months old; `npm run data:nd-dams` refreshes it."
 */
export function staleBundleBanner(bundles: BundleInfo[], now: Date): string | null {
  const stale = bundles
    .map((b) => ({ b, age: bundleAge(b.pulledUtc, now) }))
    .filter((x): x is { b: BundleInfo; age: BundleAge } => !!x.age && x.age.stale);
  if (!stale.length) return null;
  if (stale.length === 1) {
    const { b, age } = stale[0];
    return `${b.label} is ${ageWords(age)} old (pulled ${age.pulledUtc.slice(0, 10)}); \`${b.refresh}\` refreshes it.`;
  }
  const oldest = stale.reduce((a, x) => (x.age.days > a.age.days ? x : a));
  return `Inventory is ${ageWords(oldest.age)} old (${stale.map(({ b, age }) => `${b.label} pulled ${age.pulledUtc.slice(0, 10)}`).join("; ")}); ${stale
    .map(({ b }) => `\`${b.refresh}\``)
    .join(" and ")} refresh it.`;
}

/** One line per bundle for the Report and the batch CSV: "NID inventory pulled 2026-10-29 (9 days old)". */
export function bundleAgeLine(b: BundleInfo, now: Date): string | null {
  const age = bundleAge(b.pulledUtc, now);
  if (!age) return null;
  return `${b.label} pulled ${age.pulledUtc.slice(0, 10)} (${ageWords(age)} old${age.stale ? `; stale, past ${BUNDLE_STALE_DAYS} days: \`${b.refresh}\` refreshes it` : ""})`;
}

/** The three bundles the desk ships, from the loaded sources (nd-dams.ts NdData, batch.ts loadStructures). */
export function deskBundles(src: {
  nidFetchedUtc?: string | null;
  dwrPulledUtc?: string | null;
  structuresPulledUtc?: string | null;
}): BundleInfo[] {
  const out: BundleInfo[] = [];
  if (src.nidFetchedUtc !== undefined) out.push({ label: "NID inventory", pulledUtc: src.nidFetchedUtc, refresh: "npm run data:nd-dams" });
  if (src.dwrPulledUtc !== undefined) out.push({ label: "DWR dam values", pulledUtc: src.dwrPulledUtc, refresh: "npm run data:nd-dams-dwr" });
  if (src.structuresPulledUtc !== undefined)
    out.push({ label: "DWR structures list", pulledUtc: src.structuresPulledUtc, refresh: "npm run data:nd-structures" });
  return out;
}
