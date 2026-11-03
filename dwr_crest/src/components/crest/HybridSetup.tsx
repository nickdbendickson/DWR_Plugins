import { lazy, Suspense, useMemo, useState, type ReactNode } from "react";
import { boundsOf, crest, DISCLAIMER } from "@/lib/crest/dashboard";
import { floodStrip, type FloodStrip } from "@/lib/crest/floodstrip";
import {
  type BuildStatus,
  type GuidedKey,
  guidedFlags,
  guidedSteps,
  stackFlags,
  type StepState,
} from "@/lib/crest/guided";
import { num } from "@/lib/crest/format";
import { outlineState } from "@/lib/crest/outline";
import { receptorPoints } from "@/lib/crest/results";
import { useDesk } from "@/lib/crest/store";
import type { Analysis, DamRecord } from "@/lib/crest/types";
import type { DashFlag } from "@/lib/crest/dashboard";
import type { DashPoint } from "./DashboardMap";

const DashboardMap = lazy(() =>
  import("./DashboardMap").then((m) => ({ default: m.DashboardMap })),
);

const CLASS_COLOR: Record<string, string> = {
  High: crest.red,
  Medium: crest.amber,
  Low: crest.green,
};

/** What the desk hands the guided setup for each step: the same step views as the classic desk, so nothing differs. */
export type RenderStep = (
  key: Exclude<GuidedKey, "review">,
  onStatus: (s: BuildStatus) => void,
) => ReactNode;

/**
 * Style C: guided setup. Left rail = setup steps with their state, right = a live map preview of the current record
 * and flood layer, flags stacked bottom-left of the map. The active step's form is below, fully editable (nothing is
 * locked, C4). A broken step says why on its own card (C2). The map redraws as soon as the record changes or the
 * terrain build saves its layer (C1), with no reload.
 */
export function HybridSetup({
  dam,
  result,
  renderStep,
}: {
  dam: DamRecord;
  result: Analysis;
  renderStep: RenderStep;
}) {
  const setStep = useDesk((s) => s.setStep);
  const [active, setActive] = useState<GuidedKey>("record");
  const [build, setBuild] = useState<BuildStatus>({ busy: null, error: null });
  const online = typeof navigator === "undefined" ? true : navigator.onLine;
  const outline = useMemo(() => outlineState(dam, result, online), [dam, result, online]);
  const steps = useMemo(
    () => guidedSteps(dam, result, outline, active, build),
    [dam, result, outline, active, build],
  );
  const flags = useMemo(() => guidedFlags(steps), [steps]);
  const depth = useMemo<FloodStrip | null>(
    () => (outline.state === "ready" ? outline.outline : floodStrip(dam, result).strip),
    [outline, dam, result],
  );

  const points = useMemo<DashPoint[]>(() => {
    const byId = new Map(result.flood.receptors.map((r) => [r.id, r]));
    const pts: DashPoint[] = receptorPoints(dam, result).map((p) => {
      const r = byId.get(p.id);
      return {
        id: p.id,
        name: p.name,
        lon: p.lon,
        lat: p.lat,
        color: r?.ndClass ? CLASS_COLOR[r.ndClass] : r?.flooded ? crest.teal : "#9CA3AF",
        label: `${p.name}: ${r ? (r.flooded ? `${num(r.depthFt, 2)} ft` : r.unresolved ? "unresolved" : "dry") : "—"}`,
      };
    });
    if (dam.lat != null && dam.lon != null && Number.isFinite(dam.lat) && Number.isFinite(dam.lon))
      pts.unshift({
        id: "dam",
        name: dam.name,
        lon: dam.lon,
        lat: dam.lat,
        color: crest.tealDark,
        ring: true,
        label: `${dam.name} (dam)`,
      });
    return pts;
  }, [dam, result]);

  const fit = useMemo(() => {
    const b = boundsOf(depth);
    if (b) return b;
    if (
      dam.lat == null ||
      dam.lon == null ||
      !Number.isFinite(dam.lat) ||
      !Number.isFinite(dam.lon)
    )
      return null;
    return {
      west: dam.lon - 0.03,
      south: dam.lat - 0.02,
      east: dam.lon + 0.03,
      north: dam.lat + 0.02,
    };
  }, [depth, dam.lat, dam.lon]);

  const current = steps.find((s) => s.key === active)!;
  return (
    <section className="hyb-root" data-testid="guided-setup">
      <div className="hyb-split">
        <aside className="hyb-sidebar" aria-label="Setup steps">
          <h5>Set up {dam.nid || dam.name}</h5>
          <ol>
            {steps.map((s, i) => (
              <li key={s.key}>
                <StepCard
                  step={s}
                  n={i + 1}
                  current={s.key === active}
                  onClick={() => setActive(s.key)}
                />
              </li>
            ))}
          </ol>
          <p className="tip">
            Tip: nothing is locked — every auto-filled value stays editable, and the runner dialog
            lets you override before the model starts.
          </p>
        </aside>
        <div className="hyb-map" data-testid="guided-map">
          <Suspense fallback={<p className="p-4 text-sm text-muted">Loading the map…</p>}>
            <DashboardMap
              depth={depth}
              points={points}
              arrival={[]}
              hillshade={false}
              overlays={[]}
              fit={fit}
            />
          </Suspense>
          <FlagStack flags={flags} />
        </div>
      </div>

      <div className="hyb-form" data-testid={`guided-form-${active}`}>
        {current.state === "broken" && current.error ? (
          <p className="hyb-inline-error" role="alert">
            ⚑ {current.error}
          </p>
        ) : null}
        {active === "review" ? (
          <ReviewPane
            steps={steps}
            result={result}
            onOpen={() => setStep("dashboard")}
            onReport={() => setStep("report")}
          />
        ) : null}
        {/* All three stay mounted so a terrain build keeps reporting while another step is open. */}
        {(["record", "breach", "terrain"] as const).map((k) => (
          <div key={k} hidden={k !== active}>
            {renderStep(k, setBuild)}
          </div>
        ))}
      </div>
      <p className="disclaimer">{DISCLAIMER}</p>
    </section>
  );
}

function StepCard({
  step: s,
  n,
  current,
  onClick,
}: {
  step: StepState;
  n: number;
  current: boolean;
  onClick: () => void;
}) {
  const cls =
    "hyb-step " +
    (current ? "active " : "") +
    (s.state === "done" ? "done " : "") +
    (s.state === "broken" ? "broken" : "");
  return (
    <button
      type="button"
      className={cls}
      onClick={onClick}
      aria-current={current ? "step" : undefined}
      data-testid={`guided-step-${s.key}`}
    >
      <b>
        {n}. {s.label}
        {s.state === "done" ? (
          <span style={{ color: crest.green }}>
            {" "}
            ✓<span className="sr-only"> done</span>
          </span>
        ) : null}
      </b>
      <span className="mini">
        {s.state === "broken" ? (
          <span className="err" data-testid={`guided-error-${s.key}`}>
            ⚑ <span className="sr-only">Problem: </span>
            {s.error}
          </span>
        ) : (
          s.detail
        )}
      </span>
      {s.progress != null && s.progress < 100 ? (
        <span
          className="prog"
          role="progressbar"
          aria-valuenow={s.progress}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={`${s.label} progress`}
        >
          <span style={{ width: `${s.progress}%` }} />
        </span>
      ) : null}
    </button>
  );
}

const SEV_WORD = { critical: "Critical", warning: "Warning", info: "Note" } as const;

export function FlagStack({ flags }: { flags: DashFlag[] }) {
  const { shown, moreText } = stackFlags(flags);
  if (!shown.length) return null;
  return (
    <div
      className="flag-stack"
      data-testid="flag-stack"
      title={flags.map((f) => f.text).join("\n")}
    >
      {shown.map((f) => (
        <span key={f.key} className={"flag-pill " + f.sev}>
          <span className="sr-only">{SEV_WORD[f.sev]}: </span>
          {f.text}
        </span>
      ))}
      {moreText ? <span className="flag-pill info">{moreText}</span> : null}
    </div>
  );
}

function ReviewPane({
  steps,
  result,
  onOpen,
  onReport,
}: {
  steps: StepState[];
  result: Analysis;
  onOpen: () => void;
  onReport: () => void;
}) {
  const open = steps.filter((s) => s.key !== "review" && s.state !== "done");
  return (
    <div className="rounded-md border border-line bg-card p-4">
      <h2 className="text-xl">Review results</h2>
      <p className="mt-1 text-sm text-muted">
        Desk screen {result.screen.label}. Peak {num(result.breach.peakCfs, 0)} cfs,{" "}
        {result.flood.receptors.filter((r) => r.flooded).length} of {result.flood.receptors.length}{" "}
        structures wet.
      </p>
      {open.length ? (
        <p className="mt-2 text-sm text-amber">
          Still open: {open.map((s) => s.label).join(", ")}. You can review now; the results say
          which layer they use.
        </p>
      ) : null}
      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          onClick={onOpen}
          className="inline-flex h-10 items-center rounded-sm bg-water px-4 text-sm text-card"
        >
          Open the dashboard
        </button>
        <button
          type="button"
          onClick={onReport}
          className="inline-flex h-10 items-center rounded-sm border border-line bg-paper px-4 text-sm text-ink"
        >
          Open the report
        </button>
      </div>
    </div>
  );
}
