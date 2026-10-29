import { useEffect, useMemo, useState } from "react";
import { Droplets, Plus, RotateCcw } from "lucide-react";
import { analyze, STEPS } from "@/lib/crest/engine";
import { num } from "@/lib/crest/format";
import { activeDam, useDesk } from "@/lib/crest/store";
import type { BreachMethod, DamRecord, DatumName, EngineName, FailureMode, ScreenLevel, StepId } from "@/lib/crest/types";
import { HydroChart, ValleyMap } from "./charts";
import { MapDraw } from "./MapDraw";

const LEVEL_CLASS: Record<ScreenLevel, string> = {
  High: "bg-amber text-card",
  Significant: "bg-water text-card",
  Low: "bg-ink text-card",
  Review: "bg-sand text-ink",
};

export function Desk() {
  const dams = useDesk((s) => s.dams);
  const activeId = useDesk((s) => s.activeId);
  const step = useDesk((s) => s.step);
  const setStep = useDesk((s) => s.setStep);
  const select = useDesk((s) => s.select);
  const update = useDesk((s) => s.update);
  const add = useDesk((s) => s.add);
  const restore = useDesk((s) => s.restore);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    void useDesk.persist.rehydrate();
    setReady(true);
  }, []);

  const dam = activeDam({ dams, activeId, step, setStep, select, update, add, restore });
  const result = useMemo(() => analyze(dam), [dam]);

  return (
    <div className="min-h-screen bg-paper text-ink">
      <header className="border-b border-line bg-card">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-4 px-4 py-4">
          <div className="flex items-center gap-3">
            <span className="flex size-10 items-center justify-center rounded-sm bg-water text-card">
              <Droplets className="size-5" aria-hidden="true" />
            </span>
            <div>
              <p className="text-xs tracking-widest text-muted uppercase">Dam breach desk</p>
              <h1 className="text-2xl leading-none">Crest</h1>
            </div>
          </div>
          <p className="max-w-xl text-sm text-muted">
            A standalone stand-in for the QGIS suite. Elevations, breach, and the map update as you edit.
            It does not run LISFLOOD, ANUGA, or HEC-RAS, and it does not assign an official hazard class.
          </p>
        </div>
      </header>

      <div className="mx-auto grid w-full max-w-6xl min-w-0 gap-0 lg:grid-cols-[240px_minmax(0,1fr)]">
        <aside className="min-w-0 border-b border-line bg-card lg:border-r lg:border-b-0">
          <div className="flex items-center justify-between px-4 pt-4">
            <h2 className="text-base">Dams</h2>
            <button
              type="button"
              onClick={add}
              className="inline-flex h-11 items-center gap-1 rounded-sm px-2 text-sm text-water"
            >
              <Plus className="size-4" aria-hidden="true" />
              New
            </button>
          </div>
          <ul className="flex min-w-0 gap-2 overflow-x-auto px-3 py-3 lg:block lg:space-y-1 lg:overflow-visible">
            {dams.map((d) => {
              const on = d.id === dam.id;
              return (
                <li key={d.id}>
                  <button
                    type="button"
                    onClick={() => select(d.id)}
                    className={
                      "w-max rounded-sm px-3 py-2 text-left text-sm lg:w-full " +
                      (on ? "bg-water text-card" : "text-ink hover:bg-sand")
                    }
                  >
                    <span className="block font-medium">{d.name}</span>
                    <span className={on ? "text-card/80" : "text-muted"}>{d.nid}</span>
                  </button>
                </li>
              );
            })}
          </ul>
          <div className="hidden px-4 pb-4 lg:block">
            <button
              type="button"
              onClick={restore}
              className="inline-flex h-11 items-center gap-2 text-sm text-muted"
            >
              <RotateCcw className="size-4" aria-hidden="true" />
              Restore samples
            </button>
          </div>
        </aside>

        <main className="min-w-0 px-4 py-4">
          {!ready ? <p className="text-sm text-muted">Opening the desk…</p> : null}
          <nav className="mb-4 flex gap-2 overflow-x-auto" aria-label="Workflow">
            {STEPS.map((s) => {
              const on = step === s.id;
              return (
                <button
                  key={s.id}
                  type="button"
                  onClick={() => setStep(s.id as StepId)}
                  className={
                    "h-11 shrink-0 rounded-sm border px-4 text-sm " +
                    (on ? "border-water bg-water text-card" : "border-line bg-card text-ink")
                  }
                >
                  {s.label}
                </button>
              );
            })}
          </nav>

          {step === "record" ? <RecordStep dam={dam} result={result} /> : null}
          {step === "breach" ? <BreachStep dam={dam} result={result} /> : null}
          {step === "flood" ? <FloodStep dam={dam} result={result} /> : null}
          {step === "screen" ? <ScreenStep result={result} /> : null}
          {step === "report" ? <ReportStep dam={dam} result={result} /> : null}

          <button
            type="button"
            onClick={restore}
            className="mt-6 inline-flex h-11 items-center gap-2 text-sm text-muted lg:hidden"
          >
            <RotateCcw className="size-4" aria-hidden="true" />
            Restore samples
          </button>
        </main>
      </div>
    </div>
  );
}

function RecordStep({ dam, result }: { dam: DamRecord; result: ReturnType<typeof analyze> }) {
  const update = useDesk((s) => s.update);
  return (
    <section className="grid gap-4 lg:grid-cols-[1fr_280px]">
      <div className="rounded-md border border-line bg-card p-4">
        <h2 className="text-xl">Dam record</h2>
        <p className="mt-1 text-sm text-muted">{result.datum.rule}</p>
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <TextField label="Name" value={dam.name} onChange={(name) => update({ name })} />
          <TextField label="NID" value={dam.nid} onChange={(nid) => update({ nid })} />
          <TextField label="County" value={dam.county} onChange={(county) => update({ county })} />
          <NumField label="Year" value={dam.year} onChange={(year) => update({ year })} />
          <NumField label="Height" suffix="ft" value={dam.heightFt} onChange={(heightFt) => update({ heightFt })} />
          <NumField label="Length" suffix="ft" value={dam.lengthFt} onChange={(lengthFt) => update({ lengthFt })} />
          <NumField label="Crest" suffix="ft" value={dam.crestFt} step={0.1} onChange={(crestFt) => update({ crestFt })} />
          <NumField label="Streambed" suffix="ft" value={dam.streambedFt} step={0.1} onChange={(streambedFt) => update({ streambedFt })} />
          <NumField label="Spillway" suffix="ft" value={dam.spillwayFt} step={0.1} onChange={(spillwayFt) => update({ spillwayFt })} />
          <NumField label="Normal pool" suffix="ft" value={dam.normalPoolFt} step={0.1} onChange={(normalPoolFt) => update({ normalPoolFt })} />
          <NumField label="Storage" suffix="ac-ft" value={dam.storageAcFt} onChange={(storageAcFt) => update({ storageAcFt })} />
          <NumField label="Drainage" suffix="mi²" value={dam.drainageMi2} step={0.1} onChange={(drainageMi2) => update({ drainageMi2 })} />
          <NumField label="DEM crest" suffix="ft NAVD88" value={dam.demCrestFt} step={0.1} onChange={(demCrestFt) => update({ demCrestFt })} />
          <SelectField
            label="Stated datum"
            value={dam.statedDatum}
            options={["Local", "NAVD88", "NGVD29"]}
            onChange={(statedDatum) => update({ statedDatum: statedDatum as DatumName })}
          />
        </div>
      </div>
      <aside className="rounded-md border border-line bg-sand p-4">
        <h3 className="text-lg">Tied to NAVD88</h3>
        <dl className="mt-3 space-y-2 text-sm">
          <Row k="Crest" v={`${num(result.datum.crestNavdFt, 1)} ft`} />
          <Row k="Streambed" v={result.datum.streambedNavdFt == null ? "not used" : `${num(result.datum.streambedNavdFt, 1)} ft`} />
          <Row k="Spillway" v={result.datum.spillwayNavdFt == null ? "not used" : `${num(result.datum.spillwayNavdFt, 1)} ft`} />
          <Row k="Pool" v={result.datum.poolNavdFt == null ? "not used" : `${num(result.datum.poolNavdFt, 1)} ft`} />
          <Row k="Offset" v={result.datum.offsetFt == null ? "—" : `${num(result.datum.offsetFt, 1)} ft`} />
        </dl>
        {result.datum.warnings.length > 0 ? (
          <ul className="mt-4 space-y-2 text-sm text-amber">
            {result.datum.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        ) : (
          <p className="mt-4 text-sm text-muted">No review flags on this record.</p>
        )}
      </aside>
    </section>
  );
}

function BreachStep({ dam, result }: { dam: DamRecord; result: ReturnType<typeof analyze> }) {
  const update = useDesk((s) => s.update);
  return (
    <section className="space-y-4">
      <div className="rounded-md border border-line bg-card p-4">
        <h2 className="text-xl">Breach</h2>
        <div className="mt-4 grid gap-3 sm:grid-cols-3">
          <SelectField
            label="Method"
            value={dam.method}
            options={[
              ["froehlich", "Froehlich 2008"],
              ["macdonald", "MacDonald"],
              ["tr60", "TR-60 width"],
            ]}
            onChange={(method) => update({ method: method as BreachMethod })}
          />
          <SelectField
            label="Failure"
            value={dam.failureMode}
            options={[
              ["overtopping", "Overtopping"],
              ["piping", "Piping"],
            ]}
            onChange={(failureMode) => update({ failureMode: failureMode as FailureMode })}
          />
          <div className="rounded-sm bg-sand px-3 py-2">
            <p className="text-xs tracking-wide text-muted uppercase">Formation</p>
            <p className="font-medium tabular-nums">{num(result.breach.timeHr, 2)} hr</p>
          </div>
        </div>
        <div className="mt-4 grid grid-cols-2 gap-px overflow-hidden rounded-sm bg-line sm:grid-cols-4">
          <Stat k="Average width" v={`${num(result.breach.widthFt, 0)} ft`} />
          <Stat k="Volume-fitted peak" v={`${num(result.breach.fittedPeakCfs, 0)} cfs`} />
          <Stat k="Froehlich peak" v={`${num(result.breach.empiricalPeakCfs, 0)} cfs`} />
          <Stat k="Outflow volume" v={`${num(result.breach.volumeAcFt, 0)} ac-ft`} />
        </div>
      </div>
      <div className="rounded-md border border-line bg-card p-4">
        <h3 className="text-lg">Outflow</h3>
        <HydroChart series={result.breach.series} />
        <ul className="mt-2 space-y-1 text-sm text-muted">
          {result.breach.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      </div>
    </section>
  );
}

function FloodStep({ dam, result }: { dam: DamRecord; result: ReturnType<typeof analyze> }) {
  const update = useDesk((s) => s.update);
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-xl">Layout & schematic flood</h2>
          <p className="text-sm text-muted">
            Draw the dam line, reservoir point, breach point, and downstream area (DSS-WISE style). Then see the schematic flood.
          </p>
        </div>
        <SelectField
          label="Desk engine"
          value={dam.engine}
          options={[
            ["lisflood", "LISFLOOD-style"],
            ["anuga", "ANUGA-style"],
            ["routing", "1D routing"],
          ]}
          onChange={(engine) => update({ engine: engine as EngineName })}
        />
      </div>

      <div className="overflow-hidden rounded-md border border-line bg-card p-2">
        <MapDraw
          geometry={dam.geometry}
          onChange={(geometry) => update({ geometry })}
        />
      </div>

      <div className="overflow-hidden rounded-md border border-line">
        <ValleyMap flood={result.flood} />
      </div>

      <div className="grid grid-cols-3 gap-px overflow-hidden rounded-sm bg-line">
        <Stat k="Max depth" v={`${num(result.flood.maxDepthFt, 1)} ft`} />
        <Stat k="Wet area" v={`${num(result.flood.acres, 0)} ac`} />
        <Stat k="Fastest seed" v={`${num(result.flood.maxVelocityFps, 1)} ft/s`} />
      </div>
    </section>
  );
}

function ScreenStep({ result }: { result: ReturnType<typeof analyze> }) {
  return (
    <section className="space-y-4">
      <div className="rounded-md border border-line bg-card p-4">
        <p className="text-xs tracking-widest text-muted uppercase">Desk screen</p>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <span className={`rounded-sm px-3 py-1 text-lg ${LEVEL_CLASS[result.screen.level]}`}>
            {result.screen.level}
          </span>
          <p className="max-w-lg text-sm text-muted">
            Houses or barns at 2 ft or more read High. A wet building under 2 ft, or a road at 1 ft, reads Significant. Otherwise Low.
          </p>
        </div>
        <ul className="mt-4 space-y-2 text-sm">
          {result.screen.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      </div>
      <ReceptorTable result={result} />
    </section>
  );
}

function ReportStep({ dam, result }: { dam: DamRecord; result: ReturnType<typeof analyze> }) {
  return (
    <section className="space-y-4">
      <div>
        <p className="text-xs tracking-widest text-muted uppercase">Read from this desk</p>
        <h2 className="text-2xl">
          {dam.name} <span className="text-muted">({dam.nid})</span>
        </h2>
        <p className="text-sm text-muted">
          {dam.county ? `${dam.county} · ` : ""}
          {result.breach.label} · {result.flood.engineLabel}
        </p>
      </div>
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-sm bg-line sm:grid-cols-3">
        <Stat k="Peak discharge" v={`${num(result.breach.fittedPeakCfs, 0)} cfs`} />
        <Stat k="Flooded area" v={`${num(result.flood.acres, 0)} ac`} />
        <Stat k="Max depth" v={`${num(result.flood.maxDepthFt, 1)} ft`} />
        <Stat k="Max velocity" v={`${num(result.flood.maxVelocityFps, 1)} ft/s`} />
        <Stat k="Wet receptors" v={String(result.flood.receptors.filter((r) => r.flooded).length)} />
        <Stat k="Screen" v={result.screen.level} />
      </div>
      <div className="rounded-md border border-line bg-card p-4">
        <h3 className="text-lg">Breach hydrograph</h3>
        <HydroChart series={result.breach.series} />
      </div>
      <ReceptorTable result={result} />
      {result.datum.warnings.length > 0 ? (
        <div className="rounded-md border border-line bg-sand p-4">
          <h3 className="text-lg">Things to check</h3>
          <ul className="mt-2 space-y-2 text-sm">
            {result.datum.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

function ReceptorTable({ result }: { result: ReturnType<typeof analyze> }) {
  const rows = [...result.flood.receptors].sort((a, b) => b.depthFt - a.depthFt);
  return (
    <div className="overflow-x-auto rounded-md border border-line bg-card">
      <table className="w-full text-left text-sm">
        <thead className="text-xs tracking-wide text-muted uppercase">
          <tr>
            <th className="px-3 py-2 font-medium">Feature</th>
            <th className="px-3 py-2 font-medium">Kind</th>
            <th className="px-3 py-2 font-medium">Depth ft</th>
            <th className="px-3 py-2 font-medium">Vel ft/s</th>
            <th className="px-3 py-2 font-medium">Arrival h</th>
            <th className="px-3 py-2 font-medium">D × V</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-t border-line">
              <td className="px-3 py-2">{r.name}</td>
              <td className="px-3 py-2 capitalize">{r.kind}</td>
              <td className="px-3 py-2 tabular-nums">{r.flooded ? num(r.depthFt, 2) : "dry"}</td>
              <td className="px-3 py-2 tabular-nums">{r.flooded ? num(r.velocityFps, 2) : "—"}</td>
              <td className="px-3 py-2 tabular-nums">{r.arrivalHr == null ? "—" : num(r.arrivalHr, 2)}</td>
              <td className="px-3 py-2 tabular-nums">
                {r.flooded ? num(r.depthFt * r.velocityFps, 1) : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="px-3 py-2 text-xs text-muted">D × V is depth times velocity. It is not a pass or fail.</p>
    </div>
  );
}

function Stat({ k, v }: { k: string; v: string }) {
  return (
    <div className="bg-card px-3 py-3">
      <p className="text-xs tracking-wide text-muted uppercase">{k}</p>
      <p className="text-lg tabular-nums">{v}</p>
    </div>
  );
}

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-muted">{k}</dt>
      <dd className="tabular-nums">{v}</dd>
    </div>
  );
}

function TextField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  return (
    <label className="block text-sm">
      <span className="text-muted">{label}</span>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 h-11 w-full rounded-sm border border-line bg-paper px-3 text-ink"
      />
    </label>
  );
}

function NumField({
  label,
  value,
  onChange,
  suffix,
  step = 1,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  suffix?: string;
  step?: number;
}) {
  return (
    <label className="block text-sm">
      <span className="text-muted">
        {label}
        {suffix ? ` (${suffix})` : ""}
      </span>
      <input
        type="number"
        step={step}
        value={Number.isFinite(value) ? value : 0}
        onChange={(e) => onChange(Number(e.target.value))}
        className="mt-1 h-11 w-full rounded-sm border border-line bg-paper px-3 tabular-nums text-ink"
      />
    </label>
  );
}

function SelectField({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: (string | [string, string])[];
  onChange: (v: string) => void;
}) {
  return (
    <label className="block text-sm">
      <span className="text-muted">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 h-11 w-full rounded-sm border border-line bg-paper px-3 text-ink"
      >
        {options.map((opt) => {
          const [id, text] = Array.isArray(opt) ? opt : [opt, opt];
          return (
            <option key={id} value={id}>
              {text}
            </option>
          );
        })}
      </select>
    </label>
  );
}
