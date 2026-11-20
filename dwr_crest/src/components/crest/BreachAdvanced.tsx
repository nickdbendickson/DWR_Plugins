/**
 * Breach step, "Advanced": the starting pool and the Manning's n of this record.
 *
 * Presets (Nick, 2026-11-13): the pool at the top of dam and a piping failure, changeable when wanted. A new record
 * takes its presets from the shared defaults (Settings); what is set here belongs to the record and travels with its
 * dam.json and desk file. Where the record differs from the shared defaults is said in plain words below the fields.
 */
import { useMemo } from "react";

import type { analyze } from "@/lib/crest/engine";
import { DEFAULT_ROUGHNESS, MANNING_RANGE, roughnessOf } from "@/lib/crest/routing";
import { applyDefaults, assumptions, differsText, recordDefaults } from "@/lib/crest/settings";
import { useSettings } from "@/lib/crest/settings-store";
import { mergeDefaults } from "@/lib/crest/settings";
import { useDesk } from "@/lib/crest/store";
import type { DamRecord, StartPool } from "@/lib/crest/types";
import { CREST_VERSION } from "@/lib/crest/version";

const box = "mt-1 h-11 w-full rounded-sm border bg-paper px-3 text-ink";
const fmt = (n: number) => n.toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });

function nWarning(v: number | undefined): string | null {
  if (v == null || !Number.isFinite(v)) return null;
  return v < MANNING_RANGE.lo || v > MANNING_RANGE.hi ? `Outside ${MANNING_RANGE.lo} to ${MANNING_RANGE.hi}; held to that range.` : null;
}

function NField({ label, value, builtIn, testId, onChange }: { label: string; value: number | undefined; builtIn: number; testId: string; onChange: (v: number | undefined) => void }) {
  const warn = nWarning(value);
  return (
    <label className="block text-sm">
      <span className="text-muted">{label}</span>
      <input
        type="number"
        inputMode="decimal"
        step={0.005}
        min={MANNING_RANGE.lo}
        max={MANNING_RANGE.hi}
        data-testid={testId}
        aria-invalid={warn ? true : undefined}
        value={value != null && Number.isFinite(value) ? value : ""}
        placeholder={`${builtIn} (built in)`}
        onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))}
        className={`${box} tabular-nums ${warn ? "border-amber" : "border-line"}`}
      />
      {warn ? (
        <span className="mt-1 block text-xs text-amber" role="status">
          {warn}
        </span>
      ) : null}
    </label>
  );
}

export function BreachAdvanced({ dam, result }: { dam: DamRecord; result: ReturnType<typeof analyze> }) {
  const update = useDesk((s) => s.update);
  const yours = useSettings((s) => s.yours);
  const shared = useMemo(() => mergeDefaults(yours), [yours]);
  const choice: StartPool = dam.startPool ?? "top";
  const used = result.breach.startPool;
  const rough = roughnessOf(dam.roughness);
  const differs = useMemo(() => differsText(assumptions(dam, shared, CREST_VERSION, used)), [dam, shared, used]);
  const changed = choice !== "top" || rough !== DEFAULT_ROUGHNESS;
  const setRough = (patch: { nChannel?: number; nFloodplain?: number }) => {
    const next = { ...dam.roughness, ...patch };
    for (const k of ["nChannel", "nFloodplain"] as const) if (next[k] == null || !Number.isFinite(next[k])) delete next[k];
    update({ roughness: Object.keys(next).length ? next : undefined });
  };
  const reset = () => {
    const d = applyDefaults(dam, shared.values);
    update({ startPool: d.startPool, startPoolFt: undefined, roughness: d.roughness, failureMode: d.failureMode, method: d.method });
  };
  const presetNotes = recordDefaults(shared.values).notes;
  return (
    <details className="rounded-md border border-line bg-card p-4" open={changed || undefined} data-testid="breach-advanced">
      <summary className="cursor-pointer text-lg">
        Advanced: starting pool and roughness
        <span className="ml-2 text-sm text-muted" data-testid="start-pool-used">
          {Number.isFinite(result.breach.startPoolNavdFt)
            ? `Breach starts at the ${result.breach.startPoolLabel}, ${fmt(result.breach.startPoolNavdFt)} ft NAVD88.`
            : `Breach starts at the ${result.breach.startPoolLabel}.`}
        </span>
      </summary>
      <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <label className="block text-sm">
          <span className="text-muted">Starting pool</span>
          <select
            data-testid="start-pool"
            value={choice}
            onChange={(e) => update({ startPool: e.target.value === "top" ? undefined : (e.target.value as StartPool) })}
            className={`${box} border-line`}
          >
            <option value="top">Top of dam (default)</option>
            <option value="normal">Normal pool</option>
            <option value="elevation">An elevation I enter</option>
          </select>
        </label>
        {choice === "elevation" ? (
          <label className="block text-sm">
            <span className="text-muted">Starting pool elevation (ft, same datum as the crest)</span>
            <input
              type="number"
              inputMode="decimal"
              step={0.1}
              data-testid="start-pool-ft"
              value={dam.startPoolFt != null && Number.isFinite(dam.startPoolFt) ? dam.startPoolFt : ""}
              onChange={(e) => update({ startPoolFt: e.target.value === "" ? undefined : Number(e.target.value) })}
              className={`${box} border-line tabular-nums`}
            />
          </label>
        ) : choice === "normal" ? (
          <div className="rounded-sm bg-sand px-3 py-2 text-sm">
            <p className="text-xs tracking-wide text-muted uppercase">Normal pool on the record</p>
            <p className="font-medium tabular-nums">{Number.isFinite(dam.normalPoolFt) ? `${fmt(dam.normalPoolFt)} ft` : "Not entered (Record step)"}</p>
          </div>
        ) : null}
        <NField label="Manning's n, channel" value={dam.roughness?.nChannel} builtIn={DEFAULT_ROUGHNESS.nChannel} testId="n-channel" onChange={(nChannel) => setRough({ nChannel })} />
        <NField label="Manning's n, floodplain" value={dam.roughness?.nFloodplain} builtIn={DEFAULT_ROUGHNESS.nFloodplain} testId="n-floodplain" onChange={(nFloodplain) => setRough({ nFloodplain })} />
      </div>
      {choice !== "top" && used === "top" ? (
        <p className="mt-3 text-sm text-amber" role="status" data-testid="start-pool-fallback">
          {result.datum.warnings.find((w) => w.startsWith("Starting pool:")) ?? "The chosen starting pool could not be used. The breach starts at the top of dam."}
        </p>
      ) : null}
      <ul className="mt-3 space-y-1 text-sm text-muted">
        <li>
          The starting pool sets the head on the breach and the storage the breach equations use. Below the top of dam, both are
          smaller, so the peak is lower. Inflow cases (PMF, flow over the spillway with or without a breach) are the Runner&rsquo;s.
        </li>
        <li>Manning&rsquo;s n is used by the routed flood on the terrain: its travel time, depth and spread. A changed value asks for the terrain layer to be built again.</li>
        {differs.map((t) => (
          <li key={t} className="text-ink" data-testid="differs-from-shared">
            {t}
          </li>
        ))}
        {presetNotes.map((t) => (
          <li key={t}>{t}</li>
        ))}
      </ul>
      {differs.length ? (
        <button type="button" onClick={reset} data-testid="reset-to-shared" className="mt-3 inline-flex h-10 items-center rounded-sm border border-line bg-paper px-3 text-sm text-ink">
          Reset this record to the shared defaults
        </button>
      ) : null}
    </details>
  );
}
