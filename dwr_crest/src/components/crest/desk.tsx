import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { ChevronLeft, ChevronRight, Droplets, Plus, RotateCcw } from "lucide-react";
import { analyze, STEPS } from "@/lib/crest/engine";
import { exportDamJson, exportHydrographCsv, generateLisfloodPackage } from "@/lib/crest/export";
import { exportFloodStripGeoJson, exportFloodStripKml, floodStrip } from "@/lib/crest/floodstrip";
import { num } from "@/lib/crest/format";
import { saveText } from "@/lib/crest/save";
import { activeDam, useDesk } from "@/lib/crest/store";
import type {
  BreachMethod,
  DamGeometry,
  DamRecord,
  DatumName,
  EngineName,
  FailureMode,
  ScreenLevel,
  StepId,
} from "@/lib/crest/types";
import { HydroChart, ValleyMap } from "./charts";

// Leaflet touches `window` when it loads, so the map is a client-only chunk.
const MapDraw = lazy(() => import("./MapDraw").then((m) => ({ default: m.MapDraw })));

const LEVEL_CLASS: Record<ScreenLevel, string> = {
  High: "bg-amber text-card",
  Medium: "bg-water text-card",
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
          {step === "screen" ? <ScreenStep dam={dam} result={result} /> : null}
          {step === "report" ? <ReportStep dam={dam} result={result} /> : null}

          <StepNav step={step} setStep={setStep} />

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
          <NumField
            label="Surface area at top of dam"
            suffix="ac, 0 = unknown"
            value={dam.surfaceAreaAc ?? 0}
            step={0.1}
            onChange={(surfaceAreaAc) => update({ surfaceAreaAc })}
          />
          <NumField label="Drainage" suffix="mi²" value={dam.drainageMi2} step={0.1} onChange={(drainageMi2) => update({ drainageMi2 })} />
          <NumField label="DEM crest" suffix="ft NAVD88, from the DEM" value={dam.demCrestFt} step={0.1} onChange={(demCrestFt) => update({ demCrestFt })} />
          <SelectField
            label="Stated datum"
            value={dam.statedDatum}
            options={["Local", "NAVD88", "NGVD29"]}
            onChange={(statedDatum) => update({ statedDatum: statedDatum as DatumName })}
          />
          <NumField label="Latitude" suffix="°" value={dam.lat ?? NaN} step={0.00001} onChange={(lat) => update({ lat })} />
          <NumField label="Longitude" suffix="°" value={dam.lon ?? NaN} step={0.00001} onChange={(lon) => update({ lon })} />
          {dam.statedDatum === "NGVD29" ? (
            <NumField
              label="NGVD29 → NAVD88 offset"
              suffix="ft, 0 = VERTCON"
              value={dam.datumOffsetFt ?? 0}
              step={0.01}
              onChange={(datumOffsetFt) => update({ datumOffsetFt })}
            />
          ) : null}
        </div>
      </div>
      <aside className="rounded-md border border-line bg-sand p-4">
        <h3 className="text-lg">Tied to NAVD88</h3>
        <dl className="mt-3 space-y-2 text-sm">
          <Row k="Crest" v={`${num(result.datum.crestNavdFt, 2)} ft`} />
          <Row k="Streambed" v={result.datum.streambedNavdFt == null ? "not used" : `${num(result.datum.streambedNavdFt, 1)} ft`} />
          <Row k="Spillway" v={result.datum.spillwayNavdFt == null ? "not used" : `${num(result.datum.spillwayNavdFt, 1)} ft`} />
          <Row k="Pool" v={result.datum.poolNavdFt == null ? "not used" : `${num(result.datum.poolNavdFt, 1)} ft`} />
          <Row k="Offset" v={result.datum.offsetFt == null ? "—" : `${num(result.datum.offsetFt, 2)} ft`} />
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
          <Stat k="Head on breach" v={`${num(result.breach.headFt, 1)} ft`} />
          <Stat k="Routed peak" v={`${num(result.breach.peakCfs, 0)} cfs`} />
          <Stat k="Froehlich 1995 check" v={`${num(result.breach.empiricalPeakCfs, 0)} cfs`} />
          <Stat k="Time to peak" v={`${num(result.breach.timeToPeakHr, 2)} hr`} />
          <Stat k="Duration" v={`${num(result.breach.durationHr, 2)} hr`} />
          <Stat k="Bottom width" v={`${num(result.breach.bottomWidthFt, 0)} ft`} />
          <Stat k="Released volume" v={`${num(result.breach.volumeAcFt, 0)} ac-ft`} />
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
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const onGeometry = useCallback((geometry: DamGeometry) => update({ geometry }), [update]);
  const strip = useMemo(() => floodStrip(dam, result), [dam, result]);
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-xl">Layout & schematic flood</h2>
          <p className="text-sm text-muted">
            Draw the dam line, reservoir point, breach point, and downstream area (DSS-WISE style). Then see the
            schematic flood: {result.flood.engineLabel}. Receptors are seeds on this valley, not an inventory. Desk
            reach {num(result.flood.lengthM / 1000, 1)} km
            {result.flood.reachSource === "user"
              ? " (set by you)"
              : result.flood.reachSource === "aoi"
                ? " (to where the valley leaves the drawn area)"
                : " (to the 0.1 ft wet limit)"}
            ; receptors past it{result.flood.aoi?.applied ? " or outside the drawn area" : ""} are unresolved.
          </p>
          <p className="text-xs text-muted">Velocity: {result.flood.velocityBasis}</p>
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <SelectField
            label="Desk decay curve"
            value={dam.engine}
            options={[
              ["lisflood", "Slow decay (schematic)"],
              ["anuga", "Medium decay (schematic)"],
              ["routing", "Fast decay (schematic)"],
            ]}
            onChange={(engine) => update({ engine: engine as EngineName })}
          />
          <NumField
            label="Reach"
            suffix="km, 0 = auto"
            value={dam.reachKm ?? 0}
            step={0.5}
            onChange={(reachKm) => update({ reachKm })}
          />
        </div>
      </div>
      <div className="rounded-md border border-line bg-card p-4">
        <h3 className="text-lg">Dam line, points, and area of interest</h3>
        <p className="mt-1 text-sm text-muted">
          Draw the dam line, the reservoir point (first marker), the breach point (second marker), and the area of
          interest. They are saved on this dam record. A drawn area limits the schematic flood: the valley runs
          downstream from the breach (the dam line&apos;s side away from the reservoir point), the reach stops where it
          leaves the drawn area, the wet area is clipped to it, and receptors outside it are unresolved (a house that
          would rank higher is still named). The direction is square to the straight line from the dam line&apos;s first
          point to its last, so only the end points count: for a curved or L-shaped dam, draw just the straight stretch
          through the breach. With no dam line or reservoir point, a drawn area around the breach is cut at its nearest
          edge, and one that does not contain the breach is not applied.
        </p>
        <div className="mt-3">
          {mounted ? (
            <Suspense fallback={<p className="text-sm text-muted">Loading the map…</p>}>
              <MapDraw
                key={dam.id}
                geometry={dam.geometry}
                lat={dam.lat}
                lon={dam.lon}
                onChange={onGeometry}
                strip={strip.strip}
                stripMessage={strip.reason}
              />
            </Suspense>
          ) : (
            <p className="text-sm text-muted">Loading the map…</p>
          )}
        </div>
      </div>
      <div className="overflow-hidden rounded-md border border-line">
        <ValleyMap flood={result.flood} />
      </div>
      <div className="grid grid-cols-3 gap-px overflow-hidden rounded-sm bg-line">
        <Stat k="Max depth" v={`${num(result.flood.maxDepthFt, 1)} ft`} />
        <Stat k="Wet area" v={`${num(result.flood.acres, 0)} ac`} />
        <Stat k="Fastest seed (est.)" v={`${num(result.flood.maxVelocityFps, 1)} ft/s`} />
      </div>
      <AoiNote flood={result.flood} />
    </section>
  );
}

function ScreenStep({ dam, result }: { dam: DamRecord; result: ReturnType<typeof analyze> }) {
  const update = useDesk((s) => s.update);
  return (
    <section className="space-y-4">
      <div className="rounded-md border border-line bg-card p-4">
        <p className="text-xs tracking-widest text-muted uppercase">Desk screen</p>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <span className={`rounded-sm px-3 py-1 text-lg ${LEVEL_CLASS[result.screen.level]}`}>
            {result.screen.label}
          </span>
          <p className="max-w-lg text-sm text-muted">
            ND rules on the ACER TM 11 depth-velocity charts: a home in the high-danger zone reads High; in the judgment
            zone it reads {result.screen.judgmentAsProbable ? "High (flagged)" : "Medium (flagged)"}; any wet home reads at
            least Medium (economic loss); roads by AADT and class. Barns and sheds do not count.
            {result.screen.acerLevel ? ` Worst TM 11 zone: ${result.screen.acerLevel}.` : ""}
          </p>
        </div>
        {result.screen.classOfRecord || dam.sampleNote ? (
          <div className="mt-3 rounded-sm border border-line bg-sand p-3 text-sm">
            {result.screen.classOfRecord ? (
              <p>
                <span className="font-medium">Class of record: {result.screen.classOfRecord}.</span> The desk screen above
                ({result.screen.label}) is not the class of record.
              </p>
            ) : null}
            {dam.sampleNote ? <p className="mt-1 text-muted">{dam.sampleNote}</p> : null}
          </div>
        ) : null}
        <div className="mt-3 max-w-xs">
          <SelectField
            label="Judgment zone"
            value={dam.judgmentAsProbable === false ? "medium" : "high"}
            options={[
              ["high", "Count as probable loss of life (High, damsuite default)"],
              ["medium", "Do not count (Medium)"],
            ]}
            onChange={(v) => update({ judgmentAsProbable: v !== "medium" })}
          />
        </div>
        <ul className="mt-4 space-y-2 text-sm">
          {result.screen.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
        {result.screen.judgment.length > 0 ? (
          <div className="mt-4 rounded-sm bg-sand p-3 text-sm">
            <p className="font-medium">Needs an engineer&apos;s call</p>
            <ul className="mt-1 space-y-1">
              {result.screen.judgment.map((j) => (
                <li key={j}>{j}</li>
              ))}
            </ul>
          </div>
        ) : null}
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
      <ExportPanel dam={dam} result={result} />
      <div className="grid grid-cols-2 gap-px overflow-hidden rounded-sm bg-line sm:grid-cols-3">
        <Stat k="Peak discharge (routed)" v={`${num(result.breach.peakCfs, 0)} cfs`} />
        <Stat k="Flooded area" v={`${num(result.flood.acres, 0)} ac`} />
        <Stat k="Max depth" v={`${num(result.flood.maxDepthFt, 1)} ft`} />
        <Stat k="Max velocity (est.)" v={`${num(result.flood.maxVelocityFps, 1)} ft/s`} />
        <Stat k="Wet receptors" v={String(result.flood.receptors.filter((r) => r.flooded).length)} />
        <Stat k="Screen" v={result.screen.label} />
      </div>
      <AoiNote flood={result.flood} />
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

function AoiNote({ flood }: { flood: ReturnType<typeof analyze>["flood"] }) {
  if (!flood.aoi) return null;
  return (
    <div className="rounded-md border border-line bg-sand p-3 text-sm">
      <p className="font-medium">{flood.aoi.label}</p>
      {flood.aoi.applied ? (
        <>
          <p className="mt-1 text-muted">
            {flood.aoi.areaBasis === "clipped"
              ? `Wet area of the schematic strip inside the drawn area: ${num(flood.acres, 0)} ac`
              : `Wet area of the schematic strip within the drawn area's reach (direction unknown, so cut by distance, not clipped to the outline): ${num(flood.acres, 0)} ac`}
            ; reach {num(flood.lengthM / 1000, 1)} km
            {flood.aoi.maxDepthInsideFt < flood.maxDepthFt - 0.005
              ? `; max depth ${num(flood.maxDepthFt, 1)} ft at the breach, ${num(flood.aoi.maxDepthInsideFt, 1)} ft inside the drawn area`
              : ""}
            .
          </p>
          {flood.aoi.direction ? <p className="mt-1 text-muted">{flood.aoi.direction}</p> : null}
        </>
      ) : null}
      {flood.warnings.length > 0 ? (
        <ul className="mt-2 space-y-1 text-amber">
          {flood.warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function StepNav({ step, setStep }: { step: StepId; setStep: (s: StepId) => void }) {
  const i = STEPS.findIndex((s) => s.id === step);
  const prev = i > 0 ? STEPS[i - 1] : null;
  const next = i >= 0 && i < STEPS.length - 1 ? STEPS[i + 1] : null;
  const btn = "inline-flex h-11 items-center gap-1 rounded-sm border px-4 text-sm";
  return (
    <nav className="mt-6 flex items-center justify-between gap-3" aria-label="Step navigation">
      {prev ? (
        <button type="button" onClick={() => setStep(prev.id)} className={`${btn} border-line bg-card text-ink`}>
          <ChevronLeft className="size-4" aria-hidden="true" />
          Back: {prev.label}
        </button>
      ) : (
        <span />
      )}
      {next ? (
        <button type="button" onClick={() => setStep(next.id)} className={`${btn} border-water bg-water text-card`}>
          Next: {next.label}
          <ChevronRight className="size-4" aria-hidden="true" />
        </button>
      ) : null}
    </nav>
  );
}

function ExportPanel({ dam, result }: { dam: DamRecord; result: ReturnType<typeof analyze> }) {
  const update = useDesk((s) => s.update);
  const root = (dam.nid || dam.id).toLowerCase().replace(/[^a-z0-9]/g, "_");
  const lisflood = useMemo(() => {
    try {
      return {
        pkg: generateLisfloodPackage(dam, result.breach.series, {
          cellSizeM: dam.lisflood?.cellSizeM,
          pointX: dam.lisflood?.pointX,
          pointY: dam.lisflood?.pointY,
        }),
        error: null,
      };
    } catch (e) {
      return { pkg: null, error: (e as Error).message };
    }
  }, [dam, result.breach.series]);
  const strip = useMemo(() => floodStrip(dam, result), [dam, result]);
  const [saved, setSaved] = useState<string | null>(null);
  const save = useCallback(async (name: string, text: string, type?: string) => {
    const r = await saveText(name, text, type);
    if (r.status === "saved") setSaved(r.path ? `Saved ${r.path}` : null);
    else if (r.status === "cancelled") setSaved(null);
    else setSaved(`Could not save ${name}: ${r.message}`);
  }, []);
  const btn =
    "inline-flex h-10 items-center rounded-sm border border-line bg-paper px-3 text-sm text-ink disabled:cursor-not-allowed disabled:opacity-50";
  return (
    <div className="rounded-md border border-line bg-card p-4">
      <h3 className="text-lg">Export</h3>
      <p className="mt-1 text-sm text-muted">
        Hand-off files for the Breach Runner and LISFLOOD-FP. The LISFLOOD package runs 72 h with the point source at the
        breach; you supply the DEM.
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        <button type="button" className={btn} onClick={() => void save(`${root}_hydrograph.csv`, exportHydrographCsv(result.breach.series), "text/csv")}>
          Hydrograph CSV
        </button>
        <button type="button" className={btn} onClick={() => void save(`${root}_dam.json`, exportDamJson(dam, result), "application/json")}>
          dam.json
        </button>
        {(["par", "bci", "bdy"] as const).map((ext) => (
          <button
            key={ext}
            type="button"
            className={btn}
            disabled={!lisflood.pkg}
            onClick={() => lisflood.pkg && void save(`${root}.${ext}`, lisflood.pkg[ext])}
          >
            LISFLOOD .{ext}
          </button>
        ))}
        <button
          type="button"
          className={btn}
          disabled={!strip.strip}
          title={strip.reason ?? undefined}
          onClick={() => strip.strip && void save(`${root}_flood_strip.geojson`, exportFloodStripGeoJson(strip.strip), "application/geo+json")}
        >
          Flood strip GeoJSON
        </button>
        <button
          type="button"
          className={btn}
          disabled={!strip.strip}
          title={strip.reason ?? undefined}
          onClick={() => strip.strip && void save(`${root}_flood_strip.kml`, exportFloodStripKml(strip.strip), "application/vnd.google-earth.kml+xml")}
        >
          Flood strip KML
        </button>
      </div>
      <p className="mt-2 text-xs text-muted">
        Flood strip (WGS 84): {strip.strip ? "approximate screening strip, not a mapped flood extent; the schematic desk valley drawn as depth bands." : strip.reason}
      </p>
      {saved ? (
        <p className="mt-2 text-xs text-muted" role="status">
          {saved}
        </p>
      ) : null}
      <div className="mt-3 grid gap-3 sm:grid-cols-3">
        <OptNumField
          label="LISFLOOD cell size (m)"
          value={dam.lisflood?.cellSizeM}
          placeholder="10"
          onChange={(cellSizeM) => update({ lisflood: { ...dam.lisflood, cellSizeM } })}
        />
        <OptNumField
          label="Point X (DEM CRS, m)"
          value={dam.lisflood?.pointX}
          placeholder="breach point in UTM 14N"
          onChange={(pointX) => update({ lisflood: { ...dam.lisflood, pointX } })}
        />
        <OptNumField
          label="Point Y (DEM CRS, m)"
          value={dam.lisflood?.pointY}
          placeholder="breach point in UTM 14N"
          onChange={(pointY) => update({ lisflood: { ...dam.lisflood, pointY } })}
        />
      </div>
      <p className="mt-1 text-xs text-muted">
        Default grid: NAD83 / UTM 14N at 10 m, point at the drawn breach point (else the record lat/lon). Leave blank for the
        default; enter both X and Y for another DEM (e.g. the Runner&apos;s EPSG:5070 30 m grid).
      </p>
      <p className="mt-2 text-xs whitespace-pre-line text-muted">{lisflood.pkg ? lisflood.pkg.notes : lisflood.error}</p>
    </div>
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
            <th className="px-3 py-2 font-medium">km</th>
            <th className="px-3 py-2 font-medium">Depth ft</th>
            <th className="px-3 py-2 font-medium">Vel ft/s (est.)</th>
            <th className="px-3 py-2 font-medium">Arrival h</th>
            <th className="px-3 py-2 font-medium">D × V</th>
            <th className="px-3 py-2 font-medium">TM 11 zone</th>
            <th className="px-3 py-2 font-medium">ND class</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id} className="border-t border-line">
              <td className="px-3 py-2">{r.name}</td>
              <td className="px-3 py-2 capitalize">{r.kind}</td>
              <td className="px-3 py-2 tabular-nums">{num(r.distanceKm, 1)}</td>
              <td className="px-3 py-2 tabular-nums">{r.unresolved ? "unresolved" : r.flooded ? num(r.depthFt, 2) : "dry"}</td>
              <td className="px-3 py-2 tabular-nums">{r.flooded ? num(r.velocityFps, 2) : "—"}</td>
              <td className="px-3 py-2 tabular-nums">{r.arrivalHr == null ? "—" : num(r.arrivalHr, 2)}</td>
              <td className="px-3 py-2 tabular-nums">
                {r.flooded ? num(r.depthFt * r.velocityFps, 1) : "—"}
              </td>
              <td className="px-3 py-2">{r.flooded && r.zone ? r.zone : "—"}</td>
              <td className="px-3 py-2" title={r.rankReason}>
                {r.ndClass ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="px-3 py-2 text-xs text-muted">D × V is depth times velocity. It is not a pass or fail. Velocities are desk estimates. Unresolved receptors are past the desk reach and are not screened.</p>
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

/** A number input that may be left blank (undefined = use the default). */
function OptNumField({
  label,
  value,
  placeholder,
  onChange,
}: {
  label: string;
  value: number | undefined;
  placeholder?: string;
  onChange: (v: number | undefined) => void;
}) {
  return (
    <label className="block text-sm">
      <span className="text-muted">{label}</span>
      <input
        type="number"
        value={value != null && Number.isFinite(value) ? value : ""}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))}
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
