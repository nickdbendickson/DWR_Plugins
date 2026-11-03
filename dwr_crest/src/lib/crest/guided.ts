/**
 * Style C (Crest build pack, 2026-11-02): the guided setup. A rail of setup steps (Record, Breach, Terrain layer,
 * Review) beside a live map preview, with each step's state worked out here from the same record, analysis and
 * saved fast layer the rest of the desk uses. Pure parts only; the view is src/components/crest/HybridSetup.tsx.
 *
 * Brief rules this keeps: a broken step says why on its own card (never a modal, C2); at most three flag pills on the
 * map plus "+N more" (C3); nothing is locked, so every step stays open to edit whatever its state (C4).
 */
import { dashboardFlags, type DashFlag, routedVolumes } from "./dashboard.ts";
import { missingForNext } from "./nd-dams.ts";
import type { OutlineState } from "./outline.ts";
import { num } from "./format.ts";
import type { Analysis, DamRecord } from "./types.ts";

export type GuidedKey = "record" | "breach" | "terrain" | "review";

export type StepState = {
  key: GuidedKey;
  label: string;
  detail: string;
  state: "pending" | "active" | "done" | "broken";
  error?: string;
  /** 0–100 while the step is working (the terrain build), else undefined. */
  progress?: number;
  flags?: DashFlag[];
};

/** The terrain build as the Flood step's panel reports it: what it is doing now, or the error it stopped on. */
export type BuildStatus = { busy: string | null; error: string | null };

export const GUIDED_LABEL: Record<GuidedKey, string> = {
  record: "Dam record",
  breach: "Breach",
  terrain: "Terrain layer",
  review: "Review results",
};

/** Problems that stop the record from giving a breach, worded for the step card. Empty when the record is usable. */
export function recordProblems(dam: DamRecord): string[] {
  const out: string[] = [];
  const missing = missingForNext(dam);
  if (missing.length) out.push(`Missing: ${missing.join(", ")}`);
  if (
    Number.isFinite(dam.crestFt) &&
    Number.isFinite(dam.streambedFt) &&
    dam.crestFt <= dam.streambedFt
  )
    out.push("Crest is not above the breach bottom");
  if (dam.lat == null || dam.lon == null || !Number.isFinite(dam.lat) || !Number.isFinite(dam.lon))
    out.push("No location: set latitude and longitude");
  else if (Math.abs(dam.lat) > 90 || Math.abs(dam.lon) > 180)
    out.push("Location out of range: check latitude and longitude");
  return out;
}

/** Phase of the terrain build as a rough progress figure: fetching the 3DEP terrain, then building on it. */
export function buildProgress(busy: string | null): number | undefined {
  if (!busy) return undefined;
  return /fetch/i.test(busy) ? 30 : 70;
}

export function guidedSteps(
  dam: DamRecord,
  result: Analysis,
  outline: OutlineState,
  active: GuidedKey,
  build: BuildStatus = { busy: null, error: null },
): StepState[] {
  const rec = recordProblems(dam);
  const breachOk =
    rec.length === 0 && Number.isFinite(result.breach.peakCfs) && result.breach.peakCfs > 0;
  const flags = dashboardFlags(result, outline, {
    volume: routedVolumes(outline, dam.storageAcFt) ?? undefined,
  });
  const ready = outline.state === "ready";

  const steps: Omit<StepState, "state">[] = [
    {
      key: "record",
      label: GUIDED_LABEL.record,
      detail: [dam.name || "Unnamed dam", dam.nid].filter(Boolean).join(" · "),
      error: rec.length ? rec.join("; ") : undefined,
      flags: result.datum.warnings.map((text, i) => ({
        key: `datum-${i}`,
        text,
        sev: "warning" as const,
      })),
    },
    {
      key: "breach",
      label: GUIDED_LABEL.breach,
      detail: breachOk
        ? `Peak ${num(result.breach.peakCfs, 0)} cfs · ${num(result.breach.volumeAcFt, 0)} ac-ft released`
        : "Waits on the dam record",
      error:
        rec.length === 0 && !breachOk
          ? "The breach gives no outflow: check height, storage and crest"
          : undefined,
    },
    {
      key: "terrain",
      label: GUIDED_LABEL.terrain,
      detail: build.busy
        ? build.busy
        : ready
          ? outline.layer === "routed" && outline.routed
            ? `Routed flood · ${num(outline.routed.floodedAc, 0)} ac`
            : `Terrain outline · ${num(outline.stats.outlineAc, 0)} ac`
          : outline.message,
      error:
        build.error ?? (outline.state === "none" && rec.length === 0 ? outline.message : undefined),
      progress: buildProgress(build.busy),
      flags: flags.filter((f) => f.key === "volume" || f.key === "edge"),
    },
    {
      key: "review",
      label: GUIDED_LABEL.review,
      detail: ready
        ? `Desk screen ${result.screen.label}: open the dashboard`
        : "Build the terrain layer first, or review on the strip",
      flags: [
        ...result.flood.warnings.map((text, i) => ({
          key: `flood-${i}`,
          text,
          sev: "warning" as const,
        })),
      ],
    },
  ];

  return steps.map((s) => {
    const done =
      s.key === "record"
        ? rec.length === 0
        : s.key === "breach"
          ? breachOk
          : s.key === "terrain"
            ? ready
            : false;
    const state: StepState["state"] = s.error
      ? "broken"
      : s.key === active
        ? "active"
        : done
          ? "done"
          : "pending";
    return { ...s, state };
  });
}

/** All the steps' flags, worst first, with only the first critical kept critical (brief rule 4). */
export function guidedFlags(steps: StepState[]): DashFlag[] {
  const rank = { critical: 0, warning: 1, info: 2 } as const;
  let seen = false;
  return steps
    .flatMap((s) => s.flags ?? [])
    .sort((a, b) => rank[a.sev] - rank[b.sev])
    .map((f) => {
      if (f.sev !== "critical") return f;
      if (seen) return { ...f, sev: "warning" as const };
      seen = true;
      return f;
    });
}

export const FLAG_STACK_MAX = 3;

/** The map's flag stack: at most three pills, then "+N more" (acceptance C3). */
export function stackFlags<T>(
  flags: T[],
  max = FLAG_STACK_MAX,
): { shown: T[]; more: number; moreText: string | null } {
  const shown = flags.slice(0, max);
  const more = flags.length - shown.length;
  return { shown, more, moreText: more > 0 ? `+${more} more` : null };
}
