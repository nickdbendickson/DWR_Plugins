import { lazy, type ReactNode, Suspense, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { ChevronLeft, ChevronRight, Plus, RotateCcw } from "lucide-react";
import { DamitMark } from "./DamitMark";
import { analyze, crestNavdLabel, INPUT_RANGE, inputRangeWarning, STEPS } from "@/lib/crest/engine";
import { datumFlags, floodFlags, warningKey, type Warning } from "@/lib/crest/warnings";
import { locationProblem } from "@/lib/crest/guided";
import { lookupPads, padPoints, padsToRetry, resetFailedPads } from "@/lib/crest/nd911-pads";
import { exportDamJson, exportHydrographCsv, generateLisfloodPackage } from "@/lib/crest/export";
import { floodStrip, type FloodStrip } from "@/lib/crest/floodstrip";
import {
  exportFloodStripGeoJsonWithPlss,
  exportFloodStripKmlWithPlss,
  exportPlssCsv,
  fetchPlss,
  PLSS_ATTRIBUTION,
  plssState,
  SLIVER_AC,
  type PlssState,
} from "@/lib/crest/plss";
import {
  addNd911Seeds,
  exportFloodGeoJson,
  exportFloodKml,
  exportNd911Csv,
  fetchNd911,
  nd911AutoPatch,
  wantsNd911Lookup,
  ND911_ATTRIBUTION,
  ND911_DROPPED_FIELDS,
  nd911State,
  removeNd911Seeds,
  seedId,
  type Nd911Row,
  type Nd911State,
} from "@/lib/crest/nd911";
import {
  DEM_ATTRIBUTION,
  DEM_ATTRIBUTION_SHORT,
  lookupArea,
  OUTLINE_LABEL,
  outlineAcresNote,
  traceStartNote,
  deepCellsNote,
  pitsExcludedNote,
  channelPitsNote,
  volumeLimitNote,
  SMALL_RELEASE_ACFT,
  fetchDem,
  outlineInputs,
  outlineState,
  type OutlineState,
} from "@/lib/crest/outline";
import { ROUTED_LABEL, volumeCheck } from "@/lib/crest/inundation";
import { buildInundationInWorker, CANCELLED } from "@/lib/crest/inundation-client";
import { layerOfRecord } from "@/lib/crest/layer-of-record";
import { CONFIDENCE_LABEL, triageDam } from "@/lib/crest/triage";
import { num } from "@/lib/crest/format";
import { saveText } from "@/lib/crest/save";
import { demTieOffer, missingForNext, needsDemTie } from "@/lib/crest/nd-dams";
import { onOutlineSaveChange, outlineSaveError } from "@/lib/crest/outline-store";
import { activeDam, hydrateDesk, useDesk, useSaveStatus } from "@/lib/crest/store";
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
import { useOnline } from "./use-online";
import { AboutData, FieldTag, NdDamPicker, NidHazardLine, NidRecordPanel, useNdData } from "./NdDamPicker";
import { ResultsStep } from "./ResultsStep";
import { useAnalysisOfRecord, useResultsSync, useStripOfRecord } from "./results-flow";
import { PaperReport } from "./PaperReport";
import { Dashboard } from "./Dashboard";
import { HybridSetup } from "./HybridSetup";
import { ViewExport } from "./ViewExport";
import { ClientOnly } from "./ClientOnly";
import { StructurePicker } from "./StructurePicker";
import { TerrainSource } from "./TerrainSource";
import { ensureDemSource } from "@/lib/crest/dem-source";
import type { PointRole } from "./MapDraw";
import { autoBreachPoint } from "@/lib/crest/breach-point";
import { CREST_SNAP_M, deriveFromDem, nearRequest } from "@/lib/crest/batch";

// Leaflet touches `window` when it loads, so the map is a client-only chunk.
const MapDraw = lazy(() => import("./MapDraw").then((m) => ({ default: m.MapDraw })));
// The batch screen loads the structure lists (about 2 MB), so it is its own chunk, fetched only when opened.
const LocationPicker = lazy(() => import("./LocationPicker").then((m) => ({ default: m.LocationPicker })));
const BatchScreen = lazy(() => import("./BatchScreen").then((m) => ({ default: m.BatchScreen })));

const LEVEL_CLASS: Record<ScreenLevel, string> = {
  High: "bg-amber text-card",
  Medium: "bg-water text-card",
  Low: "bg-ink text-card",
  Review: "bg-sand text-ink",
};
/** A non-colour mark beside the screen level, so the badge reads in grayscale and to a screen reader: a glyph and the
 * level's first letter in a bordered box, the way the paper report's stamp carries its word. */
const LEVEL_MARK: Record<ScreenLevel, { glyph: string; title: string }> = {
  High: { glyph: "▲ H", title: "High: probable loss of life" },
  Medium: { glyph: "■ M", title: "Medium: economic loss, no probable loss of life" },
  Low: { glyph: "● L", title: "Low" },
  Review: { glyph: "? R", title: "Review: the screen is held" },
};

/** The screen level as a badge: colour plus a glyph-and-letter mark and an aria-label, never colour alone. */
function LevelBadge({ level, label }: { level: ScreenLevel; label: string }) {
  const m = LEVEL_MARK[level];
  return (
    <span className={`inline-flex items-center gap-2 rounded-sm px-3 py-1 text-lg ${LEVEL_CLASS[level]}`} aria-label={`${label}. ${m.title}`} data-level={level}>
      <span className="rounded-sm border border-current px-1 text-xs font-semibold tracking-wide" aria-hidden="true">
        {m.glyph}
      </span>
      {label}
    </span>
  );
}

/** A review flag with its catalogue code (warnings.ts) in front, keyed by code and position, never by its text. */
function FlagList({ flags, className, testId }: { flags: Warning[]; className: string; testId?: string }) {
  return (
    <ul className={className} data-testid={testId}>
      {flags.map((w, i) => (
        <li key={warningKey(w, i)}>
          <code
            className="mr-1 rounded-sm border border-current/40 px-1 align-middle text-[0.7rem]"
            title={w.severity === "stop" ? "Holds the screen at Review" : w.severity === "warn" ? "Check before relying on the result" : "For information"}
          >
            {w.code}
          </code>
          {w.text}
        </li>
      ))}
    </ul>
  );
}

export function Desk() {
  const dams = useDesk((s) => s.dams);
  const activeId = useDesk((s) => s.activeId);
  const step = useDesk((s) => s.step);
  const setStep = useDesk((s) => s.setStep);
  const select = useDesk((s) => s.select);
  const update = useDesk((s) => s.update);
  const add = useDesk((s) => s.add);
  const restore = useDesk((s) => s.restore);
  const addDam = useDesk((s) => s.addDam);
  const [ready, setReady] = useState(false);
  const [view, setView] = useState<"desk" | "batch">("desk");
  // What "Open in desk" did (selected an existing record or added one), shown once over the desk.
  const [openNote, setOpenNote] = useState<string | null>(null);
  const saveBlocked = useSaveStatus((s) => s.blocked);
  const staleTab = useSaveStatus((s) => s.stale);

  useEffect(() => {
    void hydrateDesk();
    setReady(true);
    // The desktop's saved local DEM (if any) becomes the terrain for every read on the desk.
    void ensureDemSource();
  }, []);

  const dam = activeDam({ dams, activeId, step, setStep, select, update, add, addDam, restore });
  const result = useMemo(() => analyze(dam), [dam]);
  useResultsSync(dam, result);

  return (
    <div className="min-h-screen bg-paper text-ink">
      {staleTab ? (
        <div className="border-b border-line bg-card px-4 py-2 text-sm text-amber" role="alert" data-testid="stale-tab">
          This desk was changed in another tab, so your edits here are no longer being saved. Reload to pick up the
          latest, or your changes in this tab will be lost.
        </div>
      ) : saveBlocked ? (
        <div className="border-b border-line bg-card px-4 py-2 text-sm text-amber" role="alert" data-testid="save-blocked">
          This desk is over your browser&rsquo;s storage limit, so your latest edits are no longer being saved.
          Export your work and reload, or remove a dam&rsquo;s terrain layer to free space.
        </div>
      ) : null}
      <header className="border-b border-line bg-card">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-4 px-4 py-4">
          <div className="flex items-center gap-3">
            <DamitMark className="size-10 shrink-0" />
            <div>
              <p className="text-xs tracking-widest text-muted uppercase">
                <span className="normal-case">DaMiT</span> · Dam breach desk
              </p>
              <h1 className="text-2xl leading-none">Crest</h1>
            </div>
          </div>
          <p className="max-w-xl text-sm text-muted">
            A standalone stand-in for the QGIS suite. Elevations, breach, and the map update as you edit.
            It does not run LISFLOOD, ANUGA, or HEC-RAS, and it does not assign an official hazard class.
          </p>
          <div className="flex gap-2 sm:ml-auto" role="group" aria-label="View">
            {(["desk", "batch"] as const).map((v) => (
              <button
                key={v}
                type="button"
                aria-pressed={view === v}
                onClick={() => setView(v)}
                className={"h-11 rounded-sm border px-4 text-sm " + (view === v ? "border-water bg-water text-card" : "border-line bg-card text-ink")}
              >
                {v === "desk" ? "One dam" : "Batch"}
              </button>
            ))}
          </div>
        </div>
      </header>

      {view === "batch" ? (
        <main className="mx-auto w-full max-w-6xl min-w-0 px-4 py-4">
          <Suspense fallback={<p className="text-sm text-muted">Opening the batch screen…</p>}>
            <BatchScreen
              onOpen={(note) => {
                setOpenNote(note ?? null);
                setView("desk");
              }}
            />
          </Suspense>
        </main>
      ) : (
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
          {openNote ? (
            <p className="mb-3 flex items-start gap-2 rounded-sm border border-line bg-sand px-3 py-2 text-sm" role="status" data-testid="open-in-desk-note">
              <span className="min-w-0 flex-1">{openNote}</span>
              <button type="button" className="shrink-0 text-muted underline" onClick={() => setOpenNote(null)} aria-label="Dismiss">
                Dismiss
              </button>
            </p>
          ) : null}
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

          {step === "setup" ? (
            <HybridSetup
              dam={dam}
              result={result}
              renderStep={(k, onStatus) =>
                k === "record" ? (
                  <RecordStep dam={dam} result={result} />
                ) : k === "breach" ? (
                  <BreachStep dam={dam} result={result} />
                ) : (
                  <TerrainStep dam={dam} result={result} onStatus={onStatus} />
                )
              }
            />
          ) : null}
          {step === "record" ? <RecordStep dam={dam} result={result} /> : null}
          {step === "breach" ? <BreachStep dam={dam} result={result} /> : null}
          {step === "flood" ? <FloodStep dam={dam} result={result} /> : null}
          {step === "results" ? <ResultsStep dam={dam} result={result} /> : null}
          {step === "dashboard" ? <Dashboard dam={dam} result={result} /> : null}
          {step === "screen" ? <ScreenStep dam={dam} result={result} /> : null}
          {step === "report" ? <ReportStep dam={dam} result={result} /> : null}

          <StepNav step={step} setStep={setStep} blocked={step === "record" ? missingForNext(dam) : []} />

          <button
            type="button"
            onClick={restore}
            className="mt-6 inline-flex h-11 items-center gap-2 text-sm text-muted lg:hidden"
          >
            <RotateCcw className="size-4" aria-hidden="true" />
            Restore samples
          </button>
          <AboutData />
        </main>
      </div>
      )}
      <footer className="border-t border-line bg-card">
        <div className="mx-auto max-w-6xl px-4 py-3 text-xs text-muted">
          <details>
            <summary className="cursor-pointer">About Crest and its data sources</summary>
            <ul className="mt-2 list-disc space-y-1 pl-5">
              <li>Crest is a desk screen for dam breach studies. It does not run LISFLOOD, ANUGA, or HEC-RAS and does not assign an official hazard class.</li>
              <li>Basemaps: USGS The National Map, or OpenStreetMap contributors (as credited on the map).</li>
              <li data-testid="plss-attribution">{PLSS_ATTRIBUTION}</li>
              <li data-testid="nd911-attribution">{ND911_ATTRIBUTION}</li>
              <li data-testid="dem-attribution">{DEM_ATTRIBUTION}</li>
            </ul>
          </details>
          <p className="mt-1">
            Data: {PLSS_ATTRIBUTION.split(" Public data")[0]}; {ND911_ATTRIBUTION.split(" Public data")[0]}; {DEM_ATTRIBUTION_SHORT}.
          </p>
        </div>
      </footer>
    </div>
  );
}

/**
 * The flood areas of one dam: the schematic strip, the terrain outline when it is built, and the PLSS and 911 lists
 * against the outline when there is one (else the strip). Lookups cover the strip and the outline together, so one
 * saved lookup serves both.
 */
function useFloodAreas(dam: DamRecord, result: ReturnType<typeof analyze>) {
  const online = useOnline();
  // The strip of record: an imported run's raster strip when it is the source of record and loaded, else the desk strip.
  const strip = useStripOfRecord(dam, result);
  const basis = strip.basis;
  const outline = useMemo(() => outlineState(dam, result, online), [dam, result, online]);
  const ol = outline.state === "ready" ? outline.outline : null;
  // An imported run of record is the layer of record whatever the gate picks (layer-of-record.ts): once its raster
  // strip is built, the PLSS and 911 lists run against it, not against the terrain layer.
  const run = strip.fromRun && dam.resultsOfRecord ? dam.resultsOfRecord.label : null;
  const against = run ? `${run} run` : ol ? (outline.state === "ready" && outline.layer === "routed" ? "routed flood" : "terrain outline") : "flood strip";
  const area = run ? strip.strip : (ol ?? strip.strip);
  const lookup = useMemo(() => (strip.strip ? (run ? strip.strip : lookupArea(strip.strip, ol)) : null), [strip.strip, ol, run]);
  const plss = useMemo<PlssState>(() => plssState(dam, area, online, against), [dam, area, online, against]);
  const nd911 = useMemo<Nd911State>(() => nd911State(dam, area, online, against), [dam, area, online, against]);
  return { online, strip, outline, ol, area, against, lookup, plss, nd911, basis };
}

function DemTiePanel({ dam }: { dam: DamRecord }) {
  const update = useDesk((s) => s.update);
  if (!needsDemTie(dam)) return null;
  const offer = demTieOffer(dam);
  return (
    <div className="mt-3 rounded-md border border-line bg-sand p-3 text-sm" data-testid="dem-tie">
      <p>
        Datum is {dam.statedDatum === "" ? "blank" : "local"}: tie the elevations to the DEM crest (NAVD 88). Crest 0–300 ft counts as local and
        is shifted by the DEM mid-crest offset; a mismatch over 100 ft is flagged for review.
      </p>
      {offer.review ? <p className="mt-1 font-semibold text-red-700">{offer.review}</p> : <p className="mt-1 text-muted">{offer.note}</p>}
      {offer.patch ? (
        <button type="button" className="mt-2 rounded border border-line bg-card px-3 py-1" onClick={() => update(offer.patch!)}>
          Tie to DEM crest{offer.offsetFt ? ` (${offer.offsetFt > 0 ? "+" : ""}${offer.offsetFt} ft)` : ""}
        </button>
      ) : null}
    </div>
  );
}

/**
 * Record step: set the dam's latitude and longitude by clicking a map (a new dam with no inventory point), and read
 * the ground there from the terrain into the DEM crest field.
 */
function LocationPanel({ dam }: { dam: DamRecord }) {
  const update = useDesk((s) => s.update);
  const [open, setOpen] = useState(false);
  const [ground, setGround] = useState<{ busy: boolean; msg: string | null }>({ busy: false, msg: null });
  const has = Number.isFinite(dam.lat) && Number.isFinite(dam.lon);
  const readGround = async () => {
    setGround({ busy: true, msg: null });
    const id = dam.id;
    try {
      const g = await fetchDem(nearRequest(dam.lon!, dam.lat!, 0.3, 5));
      const d = deriveFromDem(g, dam.lon!, dam.lat!);
      if (!d) throw new Error("No terrain at this point.");
      if (useDesk.getState().activeId !== id) return setGround({ busy: false, msg: null });
      update({ demCrestFt: d.crestFt });
      setGround({ busy: false, msg: `DEM crest set to ${d.crestFt.toFixed(1)} ft NAVD 88: the highest ground within ${CREST_SNAP_M} m of the point. Use it for the crest if the point is on the embankment top.` });
    } catch (e) {
      setGround({ busy: false, msg: (e as Error).message });
    }
  };
  const btn = "inline-flex h-11 items-center rounded-sm border border-line bg-paper px-3 text-sm text-ink disabled:opacity-50";
  return (
    <div className="mt-4" data-testid="location-panel">
      <div className="flex flex-wrap items-center gap-2">
        <button type="button" className={btn} aria-expanded={open} onClick={() => setOpen(!open)}>
          {open ? "Close the map" : has ? "Move the dam on the map" : "Pick the dam's location on the map"}
        </button>
        {has ? (
          <button type="button" className={btn} disabled={ground.busy} onClick={() => void readGround()}>
            {ground.busy ? "Reading the terrain…" : "Read the ground here (DEM crest)"}
          </button>
        ) : null}
      </div>
      {open ? (
        <div className="mt-2">
          <p className="mb-2 text-sm text-muted">Click the dam (the embankment top, near the old channel). Latitude and longitude update as you click.</p>
          <ClientOnly fallback={<p className="text-sm text-muted">Loading the map…</p>}>
            <Suspense fallback={<p className="text-sm text-muted">Loading the map…</p>}>
              <LocationPicker lat={dam.lat} lon={dam.lon} onPick={(lat, lon) => update({ lat, lon })} />
            </Suspense>
          </ClientOnly>
        </div>
      ) : null}
      {ground.msg ? <p className="mt-2 text-sm text-muted">{ground.msg}</p> : null}
    </div>
  );
}

function RecordStep({ dam, result }: { dam: DamRecord; result: ReturnType<typeof analyze> }) {
  const update = useDesk((s) => s.update);
  return (
    <section className="grid gap-4 lg:grid-cols-[1fr_280px]">
      <div className="rounded-md border border-line bg-card p-4">
        <h2 className="text-xl">Dam record</h2>
        <div className="mt-3">
          <NdDamPicker dam={dam} />
          <StructurePicker />
        </div>
        <NidHazardLine dam={dam} />
        <p className="mt-2 text-sm text-muted">{result.datum.rule}</p>
        <DemTiePanel dam={dam} />
        {dam.nidId ? (
          <div className="mt-3">
            <NidRecordPanel dam={dam} />
          </div>
        ) : null}
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <TextField label="Name" value={dam.name} onChange={(name) => update({ name })} tag={<FieldTag dam={dam} field="name" />} />
          <TextField label="NID" value={dam.nid} onChange={(nid) => update({ nid })} tag={<FieldTag dam={dam} field="nid" />} />
          <TextField label="County" value={dam.county} onChange={(county) => update({ county })} tag={<FieldTag dam={dam} field="county" />} />
          <NumField label="Year" value={dam.year} onChange={(year) => update({ year })} tag={<FieldTag dam={dam} field="year" />} />
          <NumField label="Height" suffix="ft" value={dam.heightFt} step={0.1} {...inputRange("heightFt", dam.heightFt)} onChange={(heightFt) => update({ heightFt })} tag={<FieldTag dam={dam} field="heightFt" />} />
          <NumField label="Length" suffix="ft" value={dam.lengthFt} {...inputRange("lengthFt", dam.lengthFt)} onChange={(lengthFt) => update({ lengthFt })} tag={<FieldTag dam={dam} field="lengthFt" />} />
          <NumField label="Crest" suffix="ft" value={dam.crestFt} step={0.1} onChange={(crestFt) => update({ crestFt })} tag={
            <>
              <FieldTag dam={dam} field="crestFt" />
              {crestNavdLabel(dam, result.datum).includes("converted") ? (
                <span className="mt-1 block text-xs text-muted" data-testid="crest-converted">
                  {crestNavdLabel(dam, result.datum)}
                </span>
              ) : null}
            </>
          } />
          <NumField label="Streambed (breach bottom)" suffix="ft" value={dam.streambedFt} step={0.1} onChange={(streambedFt) => update({ streambedFt })} tag={<FieldTag dam={dam} field="streambedFt" />} />
          <NumField label="Spillway" suffix="ft" value={dam.spillwayFt} step={0.1} onChange={(spillwayFt) => update({ spillwayFt })} />
          <NumField label="Normal pool" suffix="ft" value={dam.normalPoolFt} step={0.1} onChange={(normalPoolFt) => update({ normalPoolFt })} />
          <NumField label="Storage" suffix="ac-ft" value={dam.storageAcFt} {...inputRange("storageAcFt", dam.storageAcFt)} onChange={(storageAcFt) => update({ storageAcFt })} tag={<FieldTag dam={dam} field="storageAcFt" />} />
          <NumField
            label="Surface area at top of dam"
            suffix="ac, 0 = unknown"
            value={dam.surfaceAreaAc ?? 0}
            step={0.1}
            onChange={(surfaceAreaAc) => update({ surfaceAreaAc })}
            tag={
              <>
                <FieldTag dam={dam} field="surfaceAreaAc" />
                {dam.nidRef?.surfaceAreaNormalAc != null ? (
                  <span className="mt-1 block text-xs text-muted">
                    NID normal-pool area {dam.nidRef.surfaceAreaNormalAc} ac (reference only; not the area at the top of dam)
                  </span>
                ) : null}
              </>
            }
          />
          <NumField label="Drainage" suffix="mi²" value={dam.drainageMi2} step={0.1} onChange={(drainageMi2) => update({ drainageMi2 })} tag={<FieldTag dam={dam} field="drainageMi2" />} />
          <NumField label="DEM crest" suffix="ft NAVD88, from the DEM" value={dam.demCrestFt} step={0.1} onChange={(demCrestFt) => update({ demCrestFt })} />
          <SelectField
            label="Stated datum"
            value={dam.statedDatum}
            options={[...(dam.statedDatum === "" ? [["", "— choose —"] as [string, string]] : []), "Local", "NAVD88", "NGVD29"]}
            onChange={(statedDatum) => update({ statedDatum: statedDatum as DatumName })}
          />
          <NumField label="Latitude" suffix="°" value={dam.lat ?? NaN} step={0.00001} min={-90} max={90} warning={latLonWarning(dam, "lat")} onChange={(lat) => update({ lat })} tag={<FieldTag dam={dam} field="lat" />} />
          <NumField label="Longitude" suffix="°" value={dam.lon ?? NaN} step={0.00001} min={-180} max={180} warning={latLonWarning(dam, "lon")} onChange={(lon) => update({ lon })} tag={<FieldTag dam={dam} field="lon" />} />
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
        <LocationPanel dam={dam} />
      </div>
      <aside className="rounded-md border border-line bg-sand p-4">
        <h3 className="text-lg">Tied to NAVD88</h3>
        <dl className="mt-3 space-y-2 text-sm">
          <Row k="Crest" v={crestNavdLabel(dam, result.datum)} />
          <Row k="Streambed" v={result.datum.streambedNavdFt == null ? "not used" : `${num(result.datum.streambedNavdFt, 1)} ft NAVD88`} />
          <Row k="Spillway" v={result.datum.spillwayNavdFt == null ? "not used" : `${num(result.datum.spillwayNavdFt, 1)} ft NAVD88`} />
          <Row k="Pool" v={result.datum.poolNavdFt == null ? "not used" : `${num(result.datum.poolNavdFt, 1)} ft NAVD88`} />
          <Row k="Offset" v={result.datum.offsetFt == null ? "—" : `${num(result.datum.offsetFt, 2)} ft`} />
        </dl>
        {result.datum.warnings.length > 0 ? (
          <FlagList flags={datumFlags(result)} className="mt-4 space-y-2 text-sm text-amber" testId="datum-flags" />
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
              ["piping", "Piping (default)"],
              ["overtopping", "Overtopping"],
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
  const { strip, outline, ol, against, lookup, plss, nd911, basis } = useFloodAreas(dam, result);
  const view = useRef<HTMLElement>(null);
  const [placing, setPlacing] = useState<PointRole | null>(null);
  const onPlaced = useCallback(() => setPlacing(null), []);
  // Bumped when geometry is set from outside the map (pick automatically), so the map reloads it.
  const [mapRev, setMapRev] = useState(0);
  const [auto, setAuto] = useState<{ busy: boolean; note: string | null; error: string | null }>({ busy: false, note: null, error: null });
  const pickAuto = async () => {
    setAuto({ busy: true, note: null, error: null });
    const id = dam.id;
    const r = await autoBreachPoint(dam);
    if (useDesk.getState().activeId !== id) return setAuto({ busy: false, note: null, error: null });
    if (r.ok) {
      update({ geometry: r.geometry });
      setMapRev((v) => v + 1);
      setAuto({ busy: false, note: r.note, error: null });
    } else setAuto({ busy: false, note: null, error: r.reason });
  };
  const pointBtn = "inline-flex h-11 items-center rounded-sm border px-3 text-sm disabled:opacity-50";
  return (
    <section ref={view} className="space-y-4">
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
          <p className="mt-1 text-xs text-muted">
            The schematic flood is a dated screening aid, not an inundation map. Build the terrain inundation layer below
            (3DEP terrain) for the layer of record; the schematic strip stays only as the last-resort fallback.
          </p>
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
          <ViewExport target={view} kind="flood" dam={dam} result={result} />
        </div>
      </div>
      <div className="rounded-md border border-line bg-card p-4">
        <h3 className="text-lg">Dam line, points, and area of interest</h3>
        <p className="mt-1 text-sm text-muted">
          Draw the dam line and the area of interest, and place the breach and reservoir points: click a button above the
          map and then the spot, or let Crest pick the breach point from the terrain (where the ground at the downstream
          toe is lowest along the dam line, the old channel crossing; with no dam line drawn, one is laid across the
          downhill slope at the record's point). Markers drawn with the map's own marker tool still count in order
          (first = reservoir, second = breach). They are saved on this dam record. A drawn area limits the schematic flood: the valley runs
          downstream from the breach (across the dam at the breach, away from the reservoir; a curved dam uses the
          segment nearest the breach, not the line from end to end), the reach stops where it
          leaves the drawn area, the wet area is clipped to it, and receptors outside it are unresolved (a house that
          would rank higher is still named). The strip is straight down that valley. It does not follow later bends
          in the stream. With no dam line or reservoir point, a drawn area around the breach is cut at its nearest edge, and one that does not contain the breach is not applied.
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-2" data-testid="breach-point-tools">
          <button type="button" className={`${pointBtn} border-water bg-water text-card`} disabled={auto.busy} onClick={() => void pickAuto()}>
            {auto.busy ? "Reading the terrain…" : "Pick breach point automatically"}
          </button>
          {(["breach", "reservoir"] as const).map((r) => (
            <button
              key={r}
              type="button"
              aria-pressed={placing === r}
              className={`${pointBtn} ${placing === r ? "border-amber bg-amber text-card" : "border-line bg-card text-ink"}`}
              onClick={() => setPlacing(placing === r ? null : r)}
            >
              {placing === r ? `Click the map to place the ${r} point (cancel)` : `Click to place ${r} point`}
            </button>
          ))}
        </div>
        {auto.note ? <p className="mt-2 text-sm text-muted">{auto.note}</p> : null}
        <TerrainSource />
        {auto.error ? <p className="mt-2 text-sm text-red-700">{auto.error}</p> : null}
        <div className="mt-3">
          {mounted ? (
            <Suspense fallback={<p className="text-sm text-muted">Loading the map…</p>}>
              <MapDraw
                key={`${dam.id}-${mapRev}`}
                placing={placing}
                onPlaced={onPlaced}
                geometry={dam.geometry}
                lat={dam.lat}
                lon={dam.lon}
                onChange={onGeometry}
                strip={strip.strip}
                stripMessage={strip.reason}
                plss={plss.state === "ready" ? plss.table.rows : null}
                plssMessage={plss.state === "ready" ? null : plss.message}
                nd911={nd911.state === "ready" ? { inside: nd911.table.rows, outside: nd911.table.outside } : null}
                nd911Message={nd911.state === "ready" ? null : nd911.message}
                outline={ol}
                outlineMessage={outline.state === "ready" ? null : outline.message}
              />
            </Suspense>
          ) : (
            <p className="text-sm text-muted">Loading the map…</p>
          )}
        </div>
      </div>
      <OutlinePanel dam={dam} result={result} outline={outline} />
      <PlssPanel lookup={lookup} plss={plss} against={against} basis={basis} />
      <Nd911Panel dam={dam} result={result} lookup={lookup} nd911={nd911} />
      <div className="overflow-hidden rounded-md border border-line">
        <ValleyMap flood={result.flood} />
      </div>
      <div className="grid grid-cols-3 gap-px overflow-hidden rounded-sm bg-line">
        <Stat k="Max depth" v={`${num(result.flood.maxDepthFt, 1)} ft`} />
        <Stat k="Wet area" v={`${num(result.flood.acres, 0)} ac`} />
        {/* 0 means no receptor velocity was estimated (none entered, or none flooded) — show "—", not a false 0.0. */}
        <Stat k="Fastest seed (est.)" v={result.flood.maxVelocityFps ? `${num(result.flood.maxVelocityFps, 1)} ft/s` : "—"} />
      </div>
      <AoiNote flood={result.flood} />
    </section>
  );
}

/** The guided setup's terrain step: the Flood step's terrain build panel, reporting its progress to the step rail. */
function TerrainStep({
  dam,
  result,
  onStatus,
}: {
  dam: DamRecord;
  result: ReturnType<typeof analyze>;
  onStatus: (s: { busy: string | null; error: string | null }) => void;
}) {
  const online = useOnline();
  const outline = useMemo(() => outlineState(dam, result, online), [dam, result, online]);
  return outline.state === "none" ? (
    <div className="rounded-md border border-line bg-card p-4">
      <h3 className="text-lg">Terrain inundation (approximate)</h3>
      <p className="mt-1 text-sm text-muted">{outline.message}</p>
    </div>
  ) : (
    <OutlinePanel dam={dam} result={result} outline={outline} onStatus={onStatus} />
  );
}

const breathe = () => new Promise<void>((r) => setTimeout(r, 0));

/** The IndexedDB error of this dam's last layer save, or null (outline-store.ts). Starts null, so SSR and the first client render agree. */
function useOutlineSaveError(id: string): string | null {
  return useSyncExternalStore(
    onOutlineSaveChange,
    () => outlineSaveError(id),
    () => null,
  );
}

/** "and saved with this dam", or the storage error when IndexedDB refused the layer. */
function SavedNote({ id }: { id: string }) {
  const err = useOutlineSaveError(id);
  if (!err)
    return <>and saved with this dam (the terrain itself is not kept).</>;
  return (
    <>
      but <span className="text-amber" data-testid="outline-not-saved">layer not saved (storage error: {err}). It stays on the desk for this visit only; the browser may be out of storage or in a private window.</span>
    </>
  );
}

/**
 * The terrain outline: built on request from one 3DEP terrain request (about 3.5 MB for a 10 km reach), saved on the
 * dam so it shows offline. The build runs in a Web Worker (inundation-client.ts), so the page stays responsive and
 * Cancel stops it; where no worker can be made it runs in-thread in short chunks, as before.
 */
function OutlinePanel({
  dam,
  result,
  outline,
  onStatus,
}: {
  dam: DamRecord;
  result: ReturnType<typeof analyze>;
  outline: OutlineState;
  /** The guided setup's step rail listens here for the build's progress and errors. */
  onStatus?: (s: { busy: string | null; error: string | null }) => void;
}) {
  const update = useDesk((s) => s.update);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const building = useRef<AbortController | null>(null);
  useEffect(() => onStatus?.({ busy, error }), [busy, error, onStatus]);
  // A build keeps running when the user moves to another step meanwhile; its result is still saved on its own dam
  // (the activeId guard below drops it only if another dam is active by then). Only Cancel stops it.
  if (outline.state === "none") return null;
  const build = async () => {
    const io = outlineInputs(dam, result);
    if (!io.ok) return setError(io.reason);
    const id = dam.id;
    setError(null);
    const ctl = new AbortController();
    building.current = ctl;
    try {
      setBusy("Preparing the terrain request…");
      const r = await buildInundationInWorker(dam, result, { yieldFn: breathe, onStatus: setBusy, signal: ctl.signal });
      if (useDesk.getState().activeId !== id) return;
      if (r.ok) update({ outline: r.cache });
      else setError(r.reason);
    } catch (e) {
      if ((e as Error).message !== CANCELLED) setError((e as Error).message);
    } finally {
      if (building.current === ctl) building.current = null;
      setBusy(null);
    }
  };
  const cancel = () => building.current?.abort();
  const s = outline.state === "ready" ? outline.stats : null;
  const layer = outline.state === "ready" ? outline.layer : "outline";
  const rec = layerOfRecord(dam, result, outline);
  const triage = triageDam(dam, result, outline);
  const routed = outline.state === "ready" ? outline.routed : undefined;
  // The volume soft limit is the outline's (outline.ts); the routed flood keeps its own volume logic.
  const vol = s
    ? volumeCheck(routed ? routed.fillAcFt : s.laidAcFt, routed ? routed.releasedAcFt : result.breach.volumeAcFt, {
        rawLaidAcFt: routed ? null : s.volumeLimit?.rawLaidAcFt,
      })
    : null;
  const limitNote = s && !routed ? volumeLimitNote(s, result.breach.volumeAcFt) : null;
  const volLine = vol ? (
    <>
      {vol.label ? (
        <p className="mt-1 text-sm font-medium text-amber" role="status" data-testid="outline-volume-label">
          <span className="rounded-sm border border-amber px-1.5 py-0.5">{vol.label}</span>{" "}
          <span className="font-normal">
            Under {SMALL_RELEASE_ACFT} ac-ft released: a terrain layer is a poor guide to so little water. Run this dam in the ND Breach Runner and import the run on Results.
          </span>
        </p>
      ) : null}
      <p className={"mt-1 text-sm " + (vol.warn ? "text-amber" : "text-muted")} role={vol.warn ? "alert" : undefined} data-testid="outline-volume-check">
        {vol.text}
      </p>
      {limitNote ? (
        <p className="mt-1 text-xs text-muted" data-testid="outline-volume-limit">
          {limitNote}
        </p>
      ) : null}
    </>
  ) : null;
  const btn = "inline-flex h-10 items-center rounded-sm border border-line bg-paper px-3 text-sm text-ink disabled:opacity-50";
  return (
    <div className="rounded-md border border-line bg-card p-4" data-testid="outline-panel">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-lg">Terrain inundation (approximate)</h3>
        <div className="flex flex-wrap gap-2">
          {outline.state !== "offline" ? (
            <button type="button" onClick={() => void build()} disabled={busy != null} className={btn}>
              {busy ? "Building…" : s ? "Build again" : "Build the inundation layer"}
            </button>
          ) : null}
          {busy ? (
            <button type="button" onClick={cancel} className={btn} data-testid="outline-cancel">
              Cancel
            </button>
          ) : null}
          {dam.outline ? (
            <button type="button" onClick={() => update({ outline: undefined })} disabled={busy != null} className={btn}>
              Remove layer
            </button>
          ) : null}
        </div>
      </div>
      <p className="mt-1 text-sm font-medium" data-testid="outline-label">
        {layer === "routed" ? ROUTED_LABEL : OUTLINE_LABEL}.
      </p>
      <p className={"mt-1 text-sm " + (rec.needsRunner ? "text-amber" : "text-muted")} role={rec.needsRunner ? "alert" : undefined} data-testid="inundation-primary-note">
        {rec.note}
      </p>
      {!dam.resultsOfRecord ? (
        <div className="mt-2 rounded-sm border border-line bg-paper p-3" data-testid="triage">
          <p className={"text-sm font-medium " + (triage.recommendation === "needs-runner" ? "text-amber" : "")}>
            Triage: {triage.label} · {CONFIDENCE_LABEL[triage.confidence]}
          </p>
          <ul className="mt-1 list-disc pl-5 text-sm text-muted">
            {triage.reasons.map((r) => (
              <li key={r}>{r}</li>
            ))}
          </ul>
          <p className="mt-1 text-xs text-muted">
            A screening call for ordering the Runner worklist, not a hazard classification: Crest is a screening stand-in, never the layer of record.
          </p>
        </div>
      ) : null}
      {outline.message ? (
        <p className="mt-1 text-sm text-muted" role="status">
          {outline.message}
        </p>
      ) : null}
      {busy ? (
        <p className="mt-1 text-sm text-muted" role="status">
          {busy}
        </p>
      ) : null}
      {error ? (
        <p className="mt-1 text-sm text-amber" role="alert">
          {error}
        </p>
      ) : null}
      {s && routed ? (
        <>
          <div className="mt-3 grid grid-cols-2 gap-px overflow-hidden rounded-sm bg-line sm:grid-cols-4">
            <Stat k="Flooded area" v={`${num(routed.floodedAc, 0)} ac`} />
            <Stat k="Routed peak out" v={`${num(routed.peakOutCfs, 0)} cfs`} />
            <Stat k="Peak attenuated" v={`${num(routed.routedPeakFellBy * 100, 0)} %`} />
            <Stat k="Channel routed" v={`${num(routed.channelKm, 1)} km`} />
          </div>
          <p className="mt-2 text-sm" data-testid="routed-note">
            The routed flood carries the breach hydrograph down the drainage the terrain traces ({num(routed.channelKm, 1)} km),
            attenuating the {num(routed.peakInCfs, 0)} cfs breach peak to {num(routed.peakOutCfs, 0)} cfs,{" "}
            {routed.method === "spread"
              ? `then pours the water each stretch holds at its own peak onto the ground beside it (${num(routed.fillAcFt, 0)} ac-ft laid down for ${num(routed.releasedAcFt, 0)} ac-ft released, ${num(routed.volumeRatio, 1)}×).`
              : "then fills the water surface onto the ground."}{" "}
            {routed.cappedSections > 0
              ? `${routed.cappedSections} of ${routed.sections} cross-sections could not carry the routed peak within the cut height, so their stage is a floor, not a fit.`
              : `All ${routed.sections} cross-sections carried the routed peak.`}
          </p>
          <p className="mt-1 text-xs text-muted">
            By depth band: {s.bands.map((b) => `${b.label} ${num(b.ac, 0)} ac`).join(" · ")}. The PLSS and 911 lists below are
            against this flood; the dated flood strip stays on the map only as the last-resort fallback.
          </p>
          {routed.reach?.incomplete ? (
            <p className="mt-1 text-sm text-amber" role="alert" data-testid="routed-incomplete">
              Incomplete downstream: after {routed.reach.rounds} terrain extension{routed.reach.rounds === 1 ? "" : "s"} (+
              {num(routed.reach.extendedKm, 1)} km) the routed wave still reaches the terrain's edge with {num(routed.reach.strandedAcFt, 0)} ac-ft
              to carry. The flood runs further than this layer shows; a Runner run is the layer of record there.
            </p>
          ) : null}
          {deepCellsNote(s) ? (
            <p className="mt-1 text-xs text-amber" role="status" data-testid="outline-deep-cells">
              {deepCellsNote(s)}
            </p>
          ) : null}
          {volLine}
          {routed.notes.length ? (
            <p className="mt-1 text-xs text-amber" data-testid="routed-flags">
              {routed.notes.join(" ")}
            </p>
          ) : null}
          <p className="mt-1 text-xs text-muted" data-testid="outline-dem">
            Terrain: {s.dem.width} × {s.dem.height} cells at {num(s.dem.cellM, 0)} m (NAVD88); depth from Muskingum-Cunge
            routing of the breach hydrograph on DEM cross-sections and {routed.method === "spread" ? "a volume spread" : "a terrain fill"}, not a full 2-D hydraulic model. Built{" "}
            {new Date(outline.state === "ready" ? outline.builtAt : "").toLocaleString()} <SavedNote id={dam.id} /> {DEM_ATTRIBUTION}
          </p>
        </>
      ) : s ? (
        <>
          <div className="mt-3 grid grid-cols-2 gap-px overflow-hidden rounded-sm bg-line sm:grid-cols-4">
            <Stat k="Outline area" v={`${num(s.outlineAc, 0)} ac`} />
            <Stat k="Engine wet area" v={`${num(s.engineAc, 0)} ac`} />
            <Stat k="Channel traced" v={`${num(s.channelKm, 1)} km`} />
            <Stat k="Bed, top to end" v={`${num(s.bedTopFt, 0)} → ${num(s.bedEndFt, 0)} ft`} />
          </div>
          <p className="mt-2 text-sm" data-testid="outline-acres-note">
            {outlineAcresNote(s)}
          </p>
          {volLine}
          {traceStartNote(s) ? (
            <p className="mt-1 text-sm" data-testid="outline-trace-start">
              {traceStartNote(s)}
            </p>
          ) : null}
          {channelPitsNote(s) ? (
            <p className="mt-1 text-xs text-muted" data-testid="outline-channel-pits">
              {channelPitsNote(s)}
            </p>
          ) : null}
          {pitsExcludedNote(s) ? (
            <p className="mt-1 text-xs text-muted" data-testid="outline-pits-excluded">
              {pitsExcludedNote(s)}
            </p>
          ) : null}
          {deepCellsNote(s) ? (
            <p className="mt-1 text-xs text-amber" role="status" data-testid="outline-deep-cells">
              {deepCellsNote(s)}
            </p>
          ) : null}
          <p className="mt-1 text-xs text-muted">
            By depth band:{" "}
            {s.bands.map((b) => `${b.label} ${num(b.ac, 0)} ac`).join(" · ")}. The PLSS and 911 lists below are against
            this outline; the dated flood strip stays on the map only as the last-resort fallback.
          </p>
          <p className="mt-1 text-xs text-muted" data-testid="outline-dem">
            Terrain: {s.dem.width} × {s.dem.height} cells at {num(s.dem.cellM, 0)} m ({num(s.dem.bytes / 1e6, 1)} MB, NAVD88),
            {s.dem.missing ? ` ${s.dem.missing} cells missing,` : ""} {s.rings} rings after smoothing; {s.droppedPatches}{" "}
            patches under the minimum dropped, {s.filledHoles} holes filled. Water surface = the traced channel bed (pits
            filled) + the desk depth at that distance downstream; ground must be under it and connected to the channel.
            Built {new Date(outline.state === "ready" ? outline.builtAt : "").toLocaleString()} <SavedNote id={dam.id} /> {DEM_ATTRIBUTION}
          </p>
        </>
      ) : null}
    </div>
  );
}

function PlssPanel({ lookup, plss, against, basis }: { lookup: FloodStrip | null; plss: PlssState; against: string; basis: string }) {
  const update = useDesk((s) => s.update);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (plss.state === "none") return null;
  const lookUp = async () => {
    if (!lookup) return;
    setBusy(true);
    setError(null);
    const id = useDesk.getState().activeId;
    try {
      const p = await fetchPlss(lookup);
      if (useDesk.getState().activeId === id) update({ plss: p });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const t = plss.state === "ready" ? plss.table : null;
  return (
    <div className="rounded-md border border-line bg-card p-4" data-testid="plss-panel">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-lg">PLSS sections the {against === "flood strip" ? "strip" : against} touches</h3>
        {plss.state !== "offline" ? (
          <button
            type="button"
            onClick={() => void lookUp()}
            disabled={busy}
            className="inline-flex h-10 items-center rounded-sm border border-line bg-paper px-3 text-sm text-ink disabled:opacity-50"
          >
            {busy ? "Looking up…" : t ? "Look up again" : "Look up PLSS sections"}
          </button>
        ) : null}
      </div>
      <p className="mt-1 text-xs text-muted" data-testid="plss-basis">Strip: {basis}.</p>
      {plss.message ? <p className="mt-1 text-sm text-muted" role="status">{plss.message}</p> : null}
      {error ? <p className="mt-1 text-sm text-amber" role="alert">{error}</p> : null}
      {t ? (
        <>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-sm" data-testid="plss-table">
              <thead className="text-left text-xs tracking-wide text-muted uppercase">
                <tr>
                  <th className="px-2 py-1">#</th>
                  <th className="px-2 py-1">Section</th>
                  <th className="px-2 py-1">Wet ac</th>
                  <th className="px-2 py-1">Deepest band</th>
                  <th className="px-2 py-1">km downstream</th>
                </tr>
              </thead>
              <tbody>
                {t.rows.map((r) => (
                  <tr key={r.id} className="border-t border-line">
                    <td className="px-2 py-1 tabular-nums">{r.order}</td>
                    <td className="px-2 py-1">{r.label}</td>
                    <td className="px-2 py-1 tabular-nums">{num(r.wetAc, 1)}</td>
                    <td className="px-2 py-1">{r.bandLabel}</td>
                    <td className="px-2 py-1 tabular-nums">
                      {num(r.fromKm, 2)}–{num(r.toKm, 2)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-xs text-muted">
            {t.rows.length} sections, {num(t.listedAc, 1)} of the {against}&apos;s {num(t.stripAc, 1)} ac. Slivers under {SLIVER_AC} ac
            not listed: {t.sliversIgnored}
            {t.uncoveredAc >= SLIVER_AC ? `; ${num(t.uncoveredAc, 1)} ac of the ${against} lies outside every fetched section` : ""}.
            {against === "routed flood"
              ? ` ${ROUTED_LABEL}.`
              : against !== "flood strip"
                ? ` ${OUTLINE_LABEL}.`
                : basis.startsWith("the schematic")
                  ? " Approximate, and a dated last resort: the strip is the schematic desk valley, not a mapped flood extent — build the terrain inundation layer for the current method."
                  : ` Strip from ${basis}.`}{" "}
            Looked up{" "}
            {new Date(t.fetchedAt).toLocaleString()} and saved with this dam.
          </p>
          <p className="mt-1 text-xs text-muted">{PLSS_ATTRIBUTION}</p>
        </>
      ) : null}
    </div>
  );
}

const HAB_OPTIONS: [string, string][] = [
  ["", "Not classified (counted as habitable)"],
  ["yes", "Habitable"],
  ["no", "Not habitable"],
];

/**
 * 911 address points: looked up on request, listed when inside the strip. They never change the screen on their own;
 * "Add for review" makes receptor seeds the user classifies, and "Remove" takes them away again.
 */
function Nd911Panel({
  dam,
  result,
  lookup,
  nd911,
}: {
  dam: DamRecord;
  result: ReturnType<typeof analyze>;
  lookup: FloodStrip | null;
  nd911: Nd911State;
}) {
  const update = useDesk((s) => s.update);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Picked ND dams: look the points up and add the ones inside the strip without a click (once).
  const autoTried = useRef(false);
  useEffect(() => {
    if (!lookup || autoTried.current || !wantsNd911Lookup(dam, nd911)) return;
    autoTried.current = true;
    setBusy(true);
    const id = dam.id;
    fetchNd911(lookup)
      .then((c) => { if (useDesk.getState().activeId === id) update({ nd911: c }); })
      .catch((e) => setError((e as Error).message))
      .finally(() => setBusy(false));
  }, [dam, nd911, lookup, update]);
  useEffect(() => {
    const p = nd911AutoPatch(dam, nd911, result);
    if (p) update(p);
  }, [dam, nd911, result, update]);
  // Pads for the 911 seeds: ground at the point minus the channel bed at its station, from 3DEP (once per seed).
  const padTried = useRef(new Set<string>());
  useEffect(() => {
    const todo = padPoints(dam).filter((p) => !padTried.current.has(p.id));
    if (todo.length === 0) return;
    todo.forEach((p) => padTried.current.add(p.id));
    const id = dam.id;
    lookupPads(dam, result)
      .then((receptors) => {
        // dam switched mid-read: do not write this dam's receptors onto whatever is active now,
        // and drop the tried ids so the lookup runs again when this dam is active.
        if (useDesk.getState().activeId !== id) {
          todo.forEach((p) => padTried.current.delete(p.id));
          return;
        }
        if (receptors) update({ receptors });
      })
      .catch((e) => {
        todo.forEach((p) => padTried.current.delete(p.id));
        setError((e as Error).message);
      });
  }, [dam, result, update]);
  if (nd911.state === "none") return null;
  const lookUp = async () => {
    if (!lookup) return;
    setBusy(true);
    setError(null);
    const id = dam.id;
    try {
      const c = await fetchNd911(lookup);
      if (useDesk.getState().activeId === id) update({ nd911: c });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const t = nd911.state === "ready" ? nd911.table : null;
  const seeds = new Map(dam.receptors.filter((r) => r.source === "nd911").map((r) => [r.id, r]));
  const addRows = (rows: Nd911Row[]) => update({ receptors: addNd911Seeds(dam.receptors, rows, result) });
  const remove = (ids?: string[]) => update({ receptors: removeNd911Seeds(dam.receptors, ids) });
  const classify = (id: string, v: string) =>
    update({
      receptors: dam.receptors.map((r) => (r.id === id ? { ...r, habitable: v === "" ? undefined : v === "yes" } : r)),
    });
  const notAdded = t ? t.rows.filter((r) => !r.added) : [];
  // Pads left estimated because the terrain REQUEST failed (3DEP 502, timeout), as against no terrain at the point.
  const padRetry = padsToRetry(dam.receptors);
  const retryPads = () => {
    padRetry.forEach((r) => padTried.current.delete(r.id));
    update({ receptors: resetFailedPads(dam.receptors) });
  };
  const btn = "inline-flex h-10 items-center rounded-sm border border-line bg-paper px-3 text-sm text-ink disabled:opacity-50";
  const small = "inline-flex h-9 items-center rounded-sm border border-line bg-paper px-2 text-xs text-ink";
  return (
    <div className="rounded-md border border-line bg-card p-4" data-testid="nd911-panel">
      {padRetry.length ? (
        <p className="mb-2 text-sm text-amber" role="alert" data-testid="nd911-pad-retry">
          The terrain request for {padRetry.length} structure{padRetry.length === 1 ? "'s" : "s'"} ground height failed ({padRetry[0].padNote?.replace(/^pad estimated: terrain request failed \((.*?)\);.*$/, "$1")}), so{" "}
          {padRetry.length === 1 ? "it sits" : "they sit"} on the channel bed (pad 0) and the screen holds at Review. This is not "no terrain at the point".{" "}
          <button type="button" className="underline" onClick={retryPads}>
            Look the pads up again
          </button>
        </p>
      ) : null}
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-lg">911 address points {t ? `inside the ${t.against}` : "near the strip"}</h3>
        {nd911.state !== "offline" ? (
          <button type="button" onClick={() => void lookUp()} disabled={busy} className={btn}>
            {busy ? "Looking up…" : t ? "Look up again" : "Look up 911 address points"}
          </button>
        ) : null}
      </div>
      {nd911.message ? (
        <p className="mt-1 text-sm text-muted" role="status">
          {nd911.message}
        </p>
      ) : null}
      {error ? (
        <p className="mt-1 text-sm text-amber" role="alert">
          {error}
        </p>
      ) : null}
      {t ? (
        <>
          <p className="mt-1 text-sm" data-testid="nd911-count">
            <span className="font-medium">{t.rows.length}</span> of {t.total} address points in the looked-up box are inside
            the {t.against}. They do not change the desk screen unless you add them for review.
          </p>
          {t.rows.length > 0 ? (
            <div className="mt-2 flex flex-wrap gap-2">
              <button type="button" className={btn} disabled={notAdded.length === 0} onClick={() => addRows(notAdded)}>
                Add all {notAdded.length || ""} as structures for review
              </button>
              <button type="button" className={btn} disabled={seeds.size === 0} onClick={() => remove()}>
                Remove 911 structures{seeds.size ? ` (${seeds.size})` : ""}
              </button>
            </div>
          ) : null}
          {t.rows.length > 0 ? (
            <div className="mt-3 overflow-x-auto">
              <table className="w-full text-sm" data-testid="nd911-table">
                <thead className="text-left text-xs tracking-wide text-muted uppercase">
                  <tr>
                    <th className="px-2 py-1">#</th>
                    <th className="px-2 py-1">Address</th>
                    <th className="px-2 py-1">Depth band</th>
                    <th className="px-2 py-1">km downstream</th>
                    <th className="px-2 py-1">Place type</th>
                    <th className="px-2 py-1">For review</th>
                  </tr>
                </thead>
                <tbody>
                  {t.rows.map((r) => {
                    const id = seedId(r.nguid, r.lon, r.lat);
                    const seed = seeds.get(id);
                    return (
                      <tr key={id} className="border-t border-line align-top">
                        <td className="px-2 py-1 tabular-nums">{r.order}</td>
                        <td className="px-2 py-1">{r.address}</td>
                        <td className="px-2 py-1">{r.bandLabel}</td>
                        <td className="px-2 py-1 tabular-nums">{num(r.downstreamKm, 2)}</td>
                        <td className="px-2 py-1">{r.placeType || "—"}</td>
                        <td className="px-2 py-1">
                          {seed ? (
                            <div className="flex flex-wrap items-center gap-2">
                              <select
                                aria-label={`Classify ${r.address}`}
                                className="h-9 rounded-sm border border-line bg-paper px-2 text-xs"
                                value={seed.habitable == null ? "" : seed.habitable ? "yes" : "no"}
                                onChange={(e) => classify(id, e.target.value)}
                              >
                                {HAB_OPTIONS.map(([v, l]) => (
                                  <option key={v} value={v}>
                                    {l}
                                  </option>
                                ))}
                              </select>
                              <button type="button" className={small} onClick={() => remove([id])}>
                                Remove
                              </button>
                            </div>
                          ) : (
                            <button type="button" className={small} onClick={() => addRows([r])}>
                              Add as structure
                            </button>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : null}
          <p className="mt-2 text-xs text-muted">
            Inside means within the {t.against}&apos;s depth bands; the band is the one the point falls in. An address point
            marks an address, not a surveyed structure or floor height. Added points become home seeds on the channel
            bed (pad 0 ft, the desk&apos;s deepest case) at their distance downstream, listed under &quot;Needs an
            engineer&apos;s call&quot; until you classify them. Looked up{" "}
            {new Date(t.fetchedAt).toLocaleString()} and saved with this dam.
          </p>
          <p className="mt-1 text-xs text-muted">
            Fields kept: the published address, unit, community, ZIP, county, place type and placement, and the NG911 site
            id. Not kept: {ND911_DROPPED_FIELDS.slice(0, 3).map((d) => d.field).join(", ")} (free text that can name people
            or businesses) and duplicate or record-keeping fields. {ND911_ATTRIBUTION}
          </p>
        </>
      ) : null}
      {!t && seeds.size > 0 ? (
        <div className="mt-2">
          <button type="button" className={btn} onClick={() => remove()}>
            Remove 911 structures ({seeds.size})
          </button>
        </div>
      ) : null}
    </div>
  );
}

function ScreenStep({ dam, result: deskResult }: { dam: DamRecord; result: ReturnType<typeof analyze> }) {
  const update = useDesk((s) => s.update);
  const { result, ref } = useAnalysisOfRecord(dam, deskResult);
  return (
    <section className="space-y-4">
      <div className="rounded-md border border-line bg-card p-4">
        <p className="text-xs tracking-widest text-muted uppercase" data-testid="screen-source">
          {ref ? `Screen on the source of record: ${ref.label}` : "Desk screen"}
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-3">
          <LevelBadge level={result.screen.level} label={result.screen.label} />
          <p className="max-w-lg text-sm text-muted">
            ND rules on the ACER TM 11 depth-velocity charts: a home in the high-danger zone reads High; in the judgment
            zone it reads {result.screen.judgmentAsProbable ? "High (flagged; old option)" : "Medium by rule"}; any wet home reads at
            least Medium (economic loss); roads by AADT and class. Barns and sheds do not count.
            {result.screen.acerLevel ? ` Worst TM 11 zone: ${result.screen.acerLevel}.` : ""}
          </p>
        </div>
        <NidHazardLine dam={dam} />
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
            value={dam.judgmentAsProbable === true ? "high" : "medium"}
            options={[
              ["medium", "Medium by rule (default)"],
              ["high", "Count as probable loss of life (High, old option)"],
            ]}
            onChange={(v) => update({ judgmentAsProbable: v === "high" })}
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

function ReportStep({ dam, result: deskResult }: { dam: DamRecord; result: ReturnType<typeof analyze> }) {
  const { result, ref, label } = useAnalysisOfRecord(dam, deskResult);
  const view = useRef<HTMLElement>(null);
  // The inventory pull dates for the Report's age line, only for a record that came from the inventories.
  const { data: nd } = useNdData(Boolean(dam.nidId || dam.nidRef));
  const bundles = nd ? { nidFetchedUtc: nd.nidSource.fetchedUtc, dwrPulledUtc: nd.dwrSource?.pulledUtc ?? null } : undefined;
  return (
    <section ref={view} className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-xs tracking-widest text-muted uppercase">Read from this desk</p>
          <h2 className="text-2xl">
            {dam.name} <span className="text-muted">({dam.nid})</span>
          </h2>
          <p className="text-sm text-muted">
            {dam.county ? `${dam.county} · ` : ""}
            {result.breach.label} · {result.flood.engineLabel}
          </p>
          <p className="text-sm" data-testid="report-source">
            Source of record: <span className="font-medium">{label}</span>
            {ref
              ? ` (imported ${ref.importedAt.slice(0, 10)}; ${ref.files.join(", ")}). Depths, wet area, velocities and the screen are from this run; breach and hydrograph are the desk's.`
              : " (schematic valley). Import a model run on the Results step to use it instead."}
          </p>
          {ref?.warnings.length ? (
            <ul className="mt-1 list-disc pl-5 text-xs text-amber">
              {ref.warnings.map((w, i) => (
                <li key={`run-${i}`}>{w}</li>
              ))}
            </ul>
          ) : null}
        </div>
        <ViewExport target={view} kind="report" dam={dam} result={result} />
      </div>
      <AoiNote flood={result.flood} />
      <PaperReport dam={dam} result={result} source={label} runWarnings={ref?.warnings ?? []} bundles={bundles} />
      <ExportPanel dam={dam} result={result} desk={deskResult} />
      <PluginPanel dam={dam} result={result} />
      {dam.nidId ? <SourcesNote dam={dam} /> : null}
    </section>
  );
}

const handoff = {
  work: "",
  dem: "",
  model: "lisflood",
  source: "pack",
  log: "The screening chain has not been started.",
  busy: false,
  job: "",
};
let handoffTimer = 0;

type JobPath = "/api/screen" | "/api/model" | "/api/folder" | "/api/layers";

/** The desktop server puts a per-launch token in <meta name="crest-token"> of the page it serves; every /api/ call
 * must send it back (crest-desktop/crest/__main__.py). */
function crestApiHeaders(json = false): Record<string, string> {
  const token =
    typeof document === "undefined"
      ? ""
      : (document.querySelector('meta[name="crest-token"]')?.getAttribute("content") ?? "");
  return { ...(json ? { "Content-Type": "application/json" } : {}), "X-Crest-Token": token };
}

function PluginPanel({ dam, result }: { dam: DamRecord; result: ReturnType<typeof analyze> }) {
  const [, bump] = useState(0);
  useEffect(() => {
    const id = window.setInterval(() => {
      if (handoff.busy) bump((n) => n + 1);
    }, 500);
    return () => window.clearInterval(id);
  }, []);
  const online = useOnline();
  const strip = useMemo(() => floodStrip(dam, result), [dam, result]);
  const plss = useMemo(() => plssState(dam, strip.strip, online), [dam, strip.strip, online]);
  const files = useMemo(() => {
    const out: Record<string, string> = {};
    const root = (dam.nid || dam.id).toLowerCase().replace(/[^a-z0-9]/g, "_").replace(/^_+/, "") || "dam";
    try {
      const pkg = generateLisfloodPackage(dam, result.breach.series, {
        cellSizeM: dam.lisflood?.cellSizeM,
        pointX: dam.lisflood?.pointX,
        pointY: dam.lisflood?.pointY,
      });
      out[`${root}.par`] = pkg.par;
      out[`${root}.bci`] = pkg.bci;
      out[`${root}.bdy`] = pkg.bdy;
    } catch {
      /* the package stays out of the folder when the desk cannot build it */
    }
    if (strip.strip) {
      const table = plss.state === "ready" ? plss.table : null;
      out["flood_strip.geojson"] = exportFloodStripGeoJsonWithPlss(strip.strip, table);
      out["flood_strip.kml"] = exportFloodStripKmlWithPlss(strip.strip, table);
      if (table) out["plss.csv"] = exportPlssCsv(table);
    }
    return out;
  }, [dam, result.breach.series, strip.strip, plss]);

  const start = useCallback(
    async (path: JobPath) => {
      if (handoffTimer) window.clearInterval(handoffTimer);
      handoff.busy = true;
      handoff.log = "Starting…\n";
      bump((n) => n + 1);
      let response: Response;
      try {
        response = await fetch(path, {
          method: "POST",
          headers: crestApiHeaders(true),
          body: JSON.stringify({
            dam,
            analysis: result,
            work: handoff.work,
            dem: handoff.dem,
            model: handoff.model,
            source: handoff.source,
            files: path === "/api/layers" ? undefined : files,
          }),
        });
      } catch {
        // fetch itself rejected: nothing is listening on this origin, so there is no desktop server running.
        handoff.busy = false;
        handoff.log =
          "This page has no Crest server, so QGIS was not started. From the crest-desktop folder run: python run.py";
        bump((n) => n + 1);
        return;
      }
      try {
        if (!response.ok) throw new Error(`The Crest server rejected the job (HTTP ${response.status}).`);
        const job = (await response.json()) as { id?: string };
        if (!job.id) throw new Error("The Crest server did not return a job id.");
        handoff.job = job.id;
        handoffTimer = window.setInterval(async () => {
          try {
            const found = await (await fetch(`/api/job?id=${job.id}`, { headers: crestApiHeaders() })).json();
            handoff.log = String(found.log || "");
            if (found.done) {
              window.clearInterval(handoffTimer);
              handoffTimer = 0;
              handoff.busy = false;
              handoff.job = "";
              bump((n) => n + 1);
            }
          } catch {
            window.clearInterval(handoffTimer);
            handoffTimer = 0;
            handoff.busy = false;
            handoff.job = "";
            handoff.log = "Lost the job log.";
            bump((n) => n + 1);
          }
        }, 1000);
      } catch (e) {
        // A response came back, so a server is there — surface its real error, not the generic "no server" text.
        handoff.busy = false;
        handoff.log = `The Crest server returned an error: ${(e as Error).message}`;
        bump((n) => n + 1);
      }
    },
    [dam, result, files],
  );

  const cancel = useCallback(async () => {
    if (!handoff.job) return;
    try {
      await fetch(`/api/cancel?id=${handoff.job}`, { method: "POST", headers: crestApiHeaders() });
    } catch {
      /* the job log shows whether the run stopped */
    }
  }, []);

  const field = "mt-1 h-10 w-full rounded-sm border border-line bg-paper px-3 text-sm";
  const btn =
    "inline-flex h-10 items-center rounded-sm border border-water bg-water px-3 text-sm text-card disabled:cursor-not-allowed disabled:opacity-50";
  return (
    <div className="rounded-md border border-line bg-card p-4">
      <h3 className="text-lg">Plugins already in QGIS</h3>
      <p className="mt-1 text-sm text-muted">
        Writes the dam folder the plugins read, including the LISFLOOD package and the flood strip. Get layers downloads
        a DEM and streams for this area, or uses the ND offline pack both plugin sets already share. Screening runs tool
        17 and starts nddam:runchain only if the preflight passes. A missing DEM or a preflight FAIL stops there. Cancel
        stops QGIS; CREST_SCREENING_TIMEOUT_S sets a time limit. This is not an official class.
      </p>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <label className="text-xs text-muted">
          Work folder
          <input
            className={field}
            value={handoff.work}
            placeholder="blank = CrestDams in your home folder"
            onChange={(e) => {
              handoff.work = e.target.value;
              bump((n) => n + 1);
            }}
          />
        </label>
        <label className="text-xs text-muted">
          DEM
          <input
            className={field}
            value={handoff.dem}
            placeholder="blank = dem.tif, the pack DEM, or CREST_DEM"
            onChange={(e) => {
              handoff.dem = e.target.value;
              bump((n) => n + 1);
            }}
          />
        </label>
        <label className="text-xs text-muted">
          Flood model
          <select
            className={field}
            value={handoff.model}
            onChange={(e) => {
              handoff.model = e.target.value;
              bump((n) => n + 1);
            }}
          >
            <option value="lisflood">LISFLOOD-FP only</option>
            <option value="anuga">LISFLOOD-FP, then ANUGA</option>
            <option value="hecras">LISFLOOD-FP, then HEC-RAS 2D</option>
            <option value="builtin">Built-in screening engine (still needs a DEM)</option>
          </select>
        </label>
        <label className="text-xs text-muted">
          Layers
          <select
            className={field}
            value={handoff.source}
            onChange={(e) => {
              handoff.source = e.target.value;
              bump((n) => n + 1);
            }}
          >
            <option value="pack">ND offline pack (both plugin sets)</option>
            <option value="download">Download DEM and streams for this area</option>
          </select>
        </label>
      </div>
      <div className="mt-3 flex flex-wrap gap-2">
        <button type="button" className={btn} disabled={handoff.busy} onClick={() => void start("/api/layers")}>
          Get layers
        </button>
        <button type="button" className={btn} disabled={handoff.busy} onClick={() => void start("/api/folder")}>
          Write the dam folder
        </button>
        <button type="button" className={btn} disabled={handoff.busy} onClick={() => void start("/api/screen")}>
          Write folder and run screening
        </button>
        <button type="button" className={btn} disabled={handoff.busy} onClick={() => void start("/api/model")}>
          Run the flood model
        </button>
        {handoff.busy && handoff.job ? (
          <button
            type="button"
            className="inline-flex h-10 items-center rounded-sm border border-line bg-paper px-3 text-sm"
            onClick={() => void cancel()}
          >
            Cancel
          </button>
        ) : null}
      </div>
      <p className="mt-2 text-xs text-muted">
        The flood button runs one dam (ndbreach:rundam, or ndbreach_cli.py when both Point X and Point Y are filled in the
        DEM's coordinates). It does not start ndbreach:batch. The drawn line stays longitude and latitude, so it is
        not sent as DAM_LINES.
      </p>
      <pre className="mt-3 max-h-72 overflow-auto rounded-sm bg-paper p-3 text-xs whitespace-pre-wrap">{handoff.log}</pre>
    </div>
  );
}

function SourcesNote({ dam }: { dam: DamRecord }) {
  const prov = Object.entries(dam.provenance ?? {});
  return (
    <div className="rounded-md border border-line bg-card p-4 text-sm" data-testid="report-sources">
      <h3 className="text-lg">Sources</h3>
      <NidHazardLine dam={dam} />
      <ul className="mt-2 space-y-1">
        {prov.map(([field, p]) =>
          p ? (
            <li key={field}>
              {field}: {String(p.value)} from {p.src} {p.field} ({p.asOf}
              {p.confirmed ? ", confirmed by the user" : ""})
            </li>
          ) : null,
        )}
      </ul>
      {dam.nidRef ? <p className="mt-2 text-xs text-muted">{dam.nidRef.attribution}</p> : null}
      {dam.nidRef?.dwrAttribution ? <p className="mt-1 text-xs text-muted">{dam.nidRef.dwrAttribution}</p> : null}
    </div>
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
      {flood.warnings.length > 0 ? <FlagList flags={floodFlags({ flood })} className="mt-2 space-y-1 text-amber" testId="flood-flags" /> : null}
    </div>
  );
}

function StepNav({ step, setStep, blocked }: { step: StepId; setStep: (s: StepId) => void; blocked: string[] }) {
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
        <span className="flex flex-col items-end gap-1">
          <button
            type="button"
            disabled={blocked.length > 0}
            onClick={() => setStep(next.id)}
            className={`${btn} border-water bg-water text-card disabled:cursor-not-allowed disabled:opacity-50`}
          >
            Next: {next.label}
            <ChevronRight className="size-4" aria-hidden="true" />
          </button>
          {blocked.length > 0 ? <span className="text-xs text-amber">Enter first: {blocked.join(", ")}</span> : null}
        </span>
      ) : null}
    </nav>
  );
}

function ExportPanel({ dam, result, desk }: { dam: DamRecord; result: ReturnType<typeof analyze>; desk: ReturnType<typeof analyze> }) {
  const update = useDesk((s) => s.update);
  const root = (dam.nid || dam.id).toLowerCase().replace(/[^a-z0-9]/g, "_");
  // UTM zone for the dam's longitude, so the grid hint matches the zone the export actually projects into
  // (western ND is 13N, central/eastern 14N) rather than always claiming 14N.
  const utmZone = dam.lon != null && Number.isFinite(dam.lon) ? Math.floor((dam.lon + 180) / 6) + 1 : null;
  const utmLabel = utmZone ? `UTM ${utmZone}N` : "UTM (zone for the dam's longitude)";
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
  // The strip of record (desk strip, or an imported run's raster strip), and the outline when it is built.
  const { online, strip, outline, ol, plss, nd911 } = useFloodAreas(dam, desk);
  // Lists against the outline when it is built (and in its exports); the strip's own exports keep the strip's lists.
  const plssTable = plss.state === "ready" ? plss.table : null;
  const nd911Table = nd911.state === "ready" ? nd911.table : null;
  const stripPlss = useMemo(() => (ol ? plssState(dam, strip.strip, online) : plss), [ol, dam, strip.strip, online, plss]);
  const stripNd911 = useMemo(() => (ol ? nd911State(dam, strip.strip, online) : nd911), [ol, dam, strip.strip, online, nd911]);
  const stripPlssTable = stripPlss.state === "ready" ? stripPlss.table : null;
  const stripNd911Table = stripNd911.state === "ready" ? stripNd911.table : null;
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
          onClick={() => strip.strip && void save(`${root}_flood_strip.geojson`, exportFloodGeoJson(strip.strip, stripPlssTable, stripNd911Table), "application/geo+json")}
        >
          Flood strip GeoJSON
        </button>
        <button
          type="button"
          className={btn}
          disabled={!strip.strip}
          title={strip.reason ?? undefined}
          onClick={() => strip.strip && void save(`${root}_flood_strip.kml`, exportFloodKml(strip.strip, stripPlssTable, stripNd911Table), "application/vnd.google-earth.kml+xml")}
        >
          Flood strip KML
        </button>
        <button
          type="button"
          className={btn}
          disabled={!ol}
          title={ol ? undefined : (outline.message ?? undefined)}
          onClick={() => ol && void save(`${root}_terrain_outline.geojson`, exportFloodGeoJson(ol, plssTable, nd911Table), "application/geo+json")}
        >
          Terrain outline GeoJSON
        </button>
        <button
          type="button"
          className={btn}
          disabled={!ol}
          title={ol ? undefined : (outline.message ?? undefined)}
          onClick={() => ol && void save(`${root}_terrain_outline.kml`, exportFloodKml(ol, plssTable, nd911Table), "application/vnd.google-earth.kml+xml")}
        >
          Terrain outline KML
        </button>
        <button
          type="button"
          className={btn}
          disabled={!plssTable}
          title={plssTable ? undefined : (plss.message ?? undefined)}
          onClick={() => plssTable && void save(`${root}_plss_sections.csv`, exportPlssCsv(plssTable), "text/csv")}
        >
          PLSS sections CSV
        </button>
        <button
          type="button"
          className={btn}
          disabled={!nd911Table || !dam.nd911}
          title={nd911Table ? undefined : (nd911.message ?? undefined)}
          onClick={() => nd911Table && dam.nd911 && void save(`${root}_911_address_points.csv`, exportNd911Csv(nd911Table, dam.nd911), "text/csv")}
        >
          911 address points CSV
        </button>
      </div>
      <p className="mt-2 text-xs text-muted">
        Flood strip (WGS 84): {strip.strip ? (dam.resultsOfRecord && !strip.basis.startsWith("the desk") ? `depth bands from ${strip.basis}.` : `dated last resort — an approximate screening strip, not a mapped flood extent; the schematic desk valley drawn as depth bands${dam.resultsOfRecord ? ` (${strip.basis})` : ""}. Build the terrain inundation layer for the current method.`) : strip.reason}
        {dam.resultsOfRecord ? ` dam.json names the source of record (${dam.resultsOfRecord.label}).` : ""}
      </p>
      <p className="mt-1 text-xs text-muted" data-testid="outline-export-note">
        {outline.state === "ready" && outline.layer === "routed" ? "Routed terrain flood" : "Terrain outline"} (WGS 84):{" "}
        {ol
          ? `${outline.state === "ready" && outline.layer === "routed" ? ROUTED_LABEL : OUTLINE_LABEL}; ${num(ol.properties.outline_area_ac, 1)} ac, depth bands, with the PLSS and 911 lists against it. ${DEM_ATTRIBUTION_SHORT}.`
          : outline.message}
      </p>
      <p className="mt-1 text-xs text-muted">
        PLSS sections:{" "}
        {plssTable
          ? `${plssTable.rows.length} touched${ol ? ` by the terrain outline (in its GeoJSON/KML and the CSV)${stripPlssTable ? `; ${stripPlssTable.rows.length} by the strip (in the strip files)` : ""}` : ", listed in the strip GeoJSON/KML and the CSV"}. ${PLSS_ATTRIBUTION}`
          : plss.message}
      </p>
      <p className="mt-1 text-xs text-muted" data-testid="nd911-export-note">
        911 address points:{" "}
        {nd911Table
          ? `${nd911Table.rows.length} inside the ${nd911Table.against}${ol ? ` (in its GeoJSON/KML and the CSV)${stripNd911Table ? `; ${stripNd911Table.rows.length} inside the flood strip (in the strip files)` : ""}` : ", as points in the strip GeoJSON/KML and in the CSV"}. ${ND911_ATTRIBUTION}`
          : nd911.message}
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
          placeholder={`breach point in ${utmLabel}`}
          onChange={(pointX) => update({ lisflood: { ...dam.lisflood, pointX } })}
        />
        <OptNumField
          label="Point Y (DEM CRS, m)"
          value={dam.lisflood?.pointY}
          placeholder={`breach point in ${utmLabel}`}
          onChange={(pointY) => update({ lisflood: { ...dam.lisflood, pointY } })}
        />
      </div>
      <p className="mt-1 text-xs text-muted">
        Default grid: NAD83 / {utmLabel} at 10 m, point at the drawn breach point (else the record lat/lon). Leave blank for the
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

function TextField({
  label,
  value,
  onChange,
  tag,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  tag?: ReactNode;
}) {
  return (
    <label className="block text-sm">
      <span className="text-muted">{label}</span>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 h-11 w-full rounded-sm border border-line bg-paper px-3 text-ink"
      />
      {tag}
    </label>
  );
}

/**
 * A number input with the desk's own limits on it. `min`/`max` (and `inputMode="decimal"`, the numeric keypad on a
 * phone) come from the caller; `warning` is the field's own out-of-range or clamp text, shown under the box in amber
 * (the engine still clamps, engine.ts bounded(); this just says so where the value is typed). The box itself is
 * marked invalid (aria-invalid) while the warning stands.
 */
function NumField({
  label,
  value,
  onChange,
  suffix,
  step = 1,
  min,
  max,
  warning,
  tag,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  suffix?: string;
  step?: number;
  min?: number;
  max?: number;
  warning?: string | null;
  tag?: ReactNode;
}) {
  return (
    <label className="block text-sm">
      <span className="text-muted">
        {label}
        {suffix ? ` (${suffix})` : ""}
      </span>
      <input
        type="number"
        inputMode="decimal"
        step={step}
        min={min}
        max={max}
        aria-invalid={warning ? true : undefined}
        // A missing value (NaN) shows blank, never 0; clearing the box makes it missing again.
        value={Number.isFinite(value) ? value : ""}
        onChange={(e) => onChange(e.target.value === "" ? Number.NaN : Number(e.target.value))}
        className={"mt-1 h-11 w-full rounded-sm border bg-paper px-3 tabular-nums text-ink " + (warning ? "border-amber" : "border-line")}
      />
      {warning ? (
        <span className="mt-1 block text-xs text-amber" role="status">
          {warning}
        </span>
      ) : null}
      {tag}
    </label>
  );
}

/** The field's own out-of-range text (W-V1), only once a value is typed: a blank field is "missing", said elsewhere. */
function inputRange(field: keyof typeof INPUT_RANGE, v: number): { min: number; max: number; warning: string | null } {
  const r = INPUT_RANGE[field];
  return { min: r.lo, max: r.hi, warning: Number.isFinite(v) ? inputRangeWarning(field, v) : null };
}

/** A latitude or longitude out of its range, or the pair swapped (guided.ts locationProblem), under both fields. */
function latLonWarning(dam: DamRecord, which: "lat" | "lon"): string | null {
  const v = which === "lat" ? dam.lat : dam.lon;
  if (v == null || !Number.isFinite(v)) return null;
  if (which === "lat" && Math.abs(v) > 90) return "Latitude must be between -90 and 90.";
  if (which === "lon" && Math.abs(v) > 180) return "Longitude must be between -180 and 180.";
  const p = locationProblem(dam);
  return p && /swapped/.test(p) ? p : null;
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
