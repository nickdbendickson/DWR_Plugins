/**
 * ND dam picker (County -> Dam) and the NID / DWR panels on the Record step.
 * All data is offline (nd-dams.ts loads the bundles by dynamic import). No network calls in this module.
 */
import { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  applyDwrElevationsPatch,
  confirmText,
  countyOptions,
  damOptionLabel,
  demElevationsPatch,
  DUGOUT_FIELD_HINTS,
  DWR_FIELD_HINTS,
  dwrElevationOffer,
  dwrReference,
  fieldTag,
  heightWarningOnElevations,
  heightWarningOnPick,
  loadNdData,
  type NdDam,
  type NdData,
  nidHazardLine,
  prefillPatch,
  revertPatch,
  searchDams,
  storageFor,
  unlinkPatch,
  dwrAreaPatch,
} from "@/lib/crest/nd-dams";
import { blankDam } from "@/lib/crest/samples";
import { useDesk } from "@/lib/crest/store";
import type { DamRecord } from "@/lib/crest/types";

const MAX_OPTIONS = 60;

/** The bundles, loaded on first use. */
function useNdData(enabled = true): { data: NdData | null; error: string | null } {
  const [data, setData] = useState<NdData | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!enabled) return;
    let live = true;
    loadNdData().then(
      (d) => live && setData(d),
      (e: unknown) => live && setError(`Could not load the ND dam list: ${(e as Error).message}`),
    );
    return () => {
      live = false;
    };
  }, [enabled]);
  return { data, error };
}

export function NdDamPicker({ dam }: { dam: DamRecord }) {
  const { data, error } = useNdData();
  const update = useDesk((s) => s.update);
  const addDam = useDesk((s) => s.addDam);
  const [county, setCounty] = useState("");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [picked, setPicked] = useState<NdDam | null>(null);
  const listId = useId();
  const inputRef = useRef<HTMLInputElement>(null);

  const counties = useMemo(() => (data ? countyOptions(data.dams) : []), [data]);
  const results = useMemo(() => (data ? searchDams(data.dams, query, county) : []), [data, query, county]);
  const shown = results.slice(0, MAX_OPTIONS);
  const confirm = useMemo(() => (picked && data ? prefillPatch(picked, data, dam) : null), [picked, data, dam]);

  const choose = (d: NdDam) => {
    setPicked(d);
    setOpen(false);
    setQuery(d.name ?? d.otherNames ?? d.nid);
  };
  const onKey = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
      setActive((i) => Math.min(i + 1, shown.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter" && open && shown[active]) {
      e.preventDefault();
      choose(shown[active]);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  };

  return (
    <div className="rounded-md border border-line bg-paper p-3">
      <p className="text-sm font-medium">Fill from the NID (North Dakota dams)</p>
      {error ? <p className="mt-1 text-sm text-amber">{error}</p> : null}
      <div className="mt-2 grid gap-3 sm:grid-cols-[200px_1fr]">
        <label className="block text-sm">
          <span className="text-muted">County</span>
          <select
            value={county}
            disabled={!data}
            onChange={(e) => {
              setCounty(e.target.value);
              setActive(0);
            }}
            className="mt-1 h-11 w-full rounded-sm border border-line bg-card px-3 text-ink"
          >
            <option value="">All counties ({data?.dams.length ?? "…"})</option>
            {counties.map((c) => (
              <option key={c.county} value={c.county}>
                {c.label}
              </option>
            ))}
          </select>
        </label>
        <div className="relative text-sm">
          <label htmlFor={`${listId}-input`} className="text-muted">
            Dam (name, other name or NID ID)
          </label>
          <input
            id={`${listId}-input`}
            ref={inputRef}
            role="combobox"
            aria-expanded={open && shown.length > 0}
            aria-controls={`${listId}-list`}
            aria-autocomplete="list"
            aria-activedescendant={open && shown[active] ? `${listId}-opt-${active}` : undefined}
            disabled={!data}
            value={query}
            placeholder={data ? "e.g. Absaraka, swan buff, ND00199, 199" : "Loading the dam list…"}
            onChange={(e) => {
              setQuery(e.target.value);
              setOpen(true);
              setActive(0);
            }}
            onFocus={() => setOpen(true)}
            onBlur={() => setTimeout(() => setOpen(false), 150)}
            onKeyDown={onKey}
            className="mt-1 h-11 w-full rounded-sm border border-line bg-card px-3 text-ink"
          />
          {open && data ? (
            <ul
              id={`${listId}-list`}
              role="listbox"
              aria-label="ND dams"
              className="absolute z-20 mt-1 max-h-72 w-full overflow-y-auto rounded-sm border border-line bg-card shadow"
            >
              {shown.map((d, i) => (
                <li
                  key={d.nid}
                  id={`${listId}-opt-${i}`}
                  role="option"
                  aria-selected={i === active}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    choose(d);
                  }}
                  onMouseEnter={() => setActive(i)}
                  className={"cursor-pointer px-3 py-2 " + (i === active ? "bg-sand" : "")}
                >
                  {damOptionLabel(d, data.dwr.get(d.nid))}
                </li>
              ))}
              {results.length === 0 ? <li className="px-3 py-2 text-muted">No dam matches.</li> : null}
              {results.length > shown.length ? (
                <li className="px-3 py-2 text-muted">{results.length - shown.length} more; type more to narrow.</li>
              ) : null}
            </ul>
          ) : null}
        </div>
      </div>
      {picked && confirm && data ? (
        <div role="alertdialog" aria-label="Fill from the NID" className="mt-3 rounded-sm border border-line bg-card p-3 text-sm">
          <p>{confirmText(confirm.replaced)}</p>
          <p className="mt-1 text-muted">
            {damOptionLabel(picked, data.dwr.get(picked.nid))}. Elevations are not in the NID; you enter them next.
          </p>
          <div className="mt-2 flex flex-wrap gap-2">
            <button
              type="button"
              className="h-10 rounded-sm border border-water bg-water px-3 text-card"
              onClick={() => {
                update(confirm.patch);
                setPicked(null);
              }}
            >
              Apply to this record
            </button>
            <button
              type="button"
              className="h-10 rounded-sm border border-line bg-paper px-3"
              onClick={() => {
                const { patch } = prefillPatch(picked, data);
                const base = blankDam();
                addDam({ ...base, ...patch, id: `nid-${picked.nid.toLowerCase()}-${base.id.slice(5)}` });
                setPicked(null);
              }}
            >
              New record from NID
            </button>
            <button type="button" className="h-10 rounded-sm px-3 text-muted" onClick={() => setPicked(null)}>
              Cancel
            </button>
          </div>
        </div>
      ) : null}
      <p className="mt-2 text-xs text-muted">
        {data ? data.nidSource.attribution : "Dam inventory data: U.S. Army Corps of Engineers, National Inventory of Dams."}
        {data?.dwrSource ? ` ${data.dwrSource.attribution}` : ""}
      </p>
    </div>
  );
}

/** "from NID" / "edited (NID: x)" + revert / "not in NID: enter a value". */
export function FieldTag({ dam, field }: { dam: DamRecord; field: string }) {
  const update = useDesk((s) => s.update);
  const t = fieldTag(dam, field);
  if (!t) return null;
  const cls =
    t.kind === "missing"
      ? "text-red-700"
      : t.kind === "edited"
        ? "text-amber"
        : "text-muted";
  return (
    <span className={`mt-1 flex flex-wrap items-center gap-2 text-xs ${cls}`}>
      {t.text}
      {t.kind === "edited" ? (
        <button type="button" className="underline" onClick={() => update(revertPatch(dam, field))}>
          revert
        </button>
      ) : null}
    </span>
  );
}

export function NidHazardLine({ dam }: { dam: DamRecord }) {
  const line = nidHazardLine(dam.nidHazard);
  if (!line) return null;
  return (
    <p className="mt-2 inline-block rounded-sm border border-line bg-paper px-2 py-1 text-xs text-muted" data-testid="nid-hazard">
      {line}. Not used by the desk screen.
    </p>
  );
}

/** Banners, the DWR card, the NID facts box and "unlink NID" for a record filled from the NID. */
export function NidRecordPanel({ dam }: { dam: DamRecord }) {
  const { data } = useNdData(Boolean(dam.nidId));
  const update = useDesk((s) => s.update);
  const [checked, setChecked] = useState(false);
  const [demCrest, setDemCrest] = useState("");
  const [demBed, setDemBed] = useState("");
  if (!dam.nidId) return null;
  const nd = data?.byNid.get(dam.nidId);
  const dwr = data?.dwr.get(dam.nidId);
  const dwrAsOf = data?.dwrSource?.pulledUtc.slice(0, 10) ?? "";
  const elevMissing = !Number.isFinite(dam.crestFt) || !Number.isFinite(dam.streambedFt) || !dam.statedDatum;
  const offer = dwrElevationOffer(dwr);
  const dugout = data?.dugouts.get(dam.nidId);
  const w1 = nd ? heightWarningOnPick(nd, dwr) : null;
  const w2 = heightWarningOnElevations(dam);
  const storage = nd ? storageFor(nd) : null;
  const ref = nd && dwr ? dwrReference(nd, dwr, dam.heightFt) : null;
  const r = dam.nidRef;
  const fmt = (v: number | null | undefined, u = "") => (v == null ? "—" : `${v.toLocaleString("en-US")}${u}`);
  const demOk = demCrest !== "" && demBed !== "" && Number.isFinite(Number(demCrest)) && Number.isFinite(Number(demBed));

  return (
    <div className="space-y-3">
      {elevMissing ? (
        <div className="rounded-sm border border-amber bg-amber/10 p-3 text-sm" role="status" data-testid="elev-banner">
          <p className="font-medium text-amber">Elevations are not in the NID.</p>
          <p className="mt-1">
            Enter the crest (top of dam) and breach bottom and choose their datum before going on. Next stays off until
            they are in; the engine shows Review (elevations missing) and runs no flood.
          </p>
          {offer.ok ? (
            <p className="mt-1 text-muted">The DWR Dams file has NAVD 88 elevations for this dam ({offer.sourceNote}); see the DWR card below.</p>
          ) : null}
          <div className="mt-2 grid gap-2 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
            <label className="block">
              <span className="text-muted">Crest typed from a DEM (ft NAVD88)</span>
              <input
                type="number"
                step={0.01}
                value={demCrest}
                onChange={(e) => setDemCrest(e.target.value)}
                className="mt-1 h-10 w-full rounded-sm border border-line bg-paper px-2 tabular-nums"
              />
            </label>
            <label className="block">
              <span className="text-muted">Breach bottom typed from a DEM (ft NAVD88)</span>
              <input
                type="number"
                step={0.01}
                value={demBed}
                onChange={(e) => setDemBed(e.target.value)}
                className="mt-1 h-10 w-full rounded-sm border border-line bg-paper px-2 tabular-nums"
              />
            </label>
            <button
              type="button"
              disabled={!demOk}
              onClick={() => update(demElevationsPatch(dam, Number(demCrest), Number(demBed)))}
              className="h-10 rounded-sm border border-line bg-card px-3 disabled:opacity-50"
            >
              Use typed DEM values
            </button>
          </div>
          <p className="mt-1 text-xs text-muted">
            Values you read off a DEM and type here; Crest does not sample the DEM, and the DEM crest check is left as is.
            Or type them in the Crest, Streambed and datum fields below.
          </p>
        </div>
      ) : null}

      {nd?.countyNote ? (
        <p className="rounded-sm border border-line bg-paper px-2 py-1 text-xs" data-testid="county-note">
          ⓘ {nd.countyNote}.{dwr?.county ? ` The DWR Dams file also lists ${dwr.county} County.` : ""}
        </p>
      ) : null}
      {storage?.warning ? <p className="text-sm text-amber">{storage.warning}</p> : null}
      {storage?.rangeNote ? <p className="text-sm text-amber">{storage.rangeNote}</p> : null}
      {[w1, w2].filter(Boolean).map((w) => (
        <div key={w!.id} className="rounded-sm border border-line bg-sand p-2 text-sm">
          <span className="font-medium">{w!.id}:</span> {w!.text}
          {w!.useHeightFt != null && w!.useHeightFt !== dam.heightFt ? (
            <button type="button" className="ml-2 underline" onClick={() => update({ heightFt: w!.useHeightFt })}>
              Use {w!.id === "W-H1" ? "dam height" : "crest minus breach bottom"} {w!.useHeightFt} ft
            </button>
          ) : null}
        </div>
      ))}

      {dwr && data?.dwrSource ? (
        <div className="rounded-md border border-line bg-card p-3 text-sm" data-testid="dwr-card">
          <p className="font-medium">
            ND DWR Dams file <span className="text-muted">(structure {dwr.dwrId}, pulled {dwrAsOf})</span>
          </p>
          <p className="mt-1 text-xs text-muted">{data.dwrSource.disclaimer}</p>
          <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1">
            <dt className="text-muted" title={DWR_FIELD_HINTS.datum}>Datum</dt>
            <dd>{ref?.datumLabel}</dd>
            <dt className="text-muted" title={DWR_FIELD_HINTS.topElevFt}>Top of dam (inferred)</dt>
            <dd>{fmt(dwr.topElevFt, " ft")}</dd>
            <dt className="text-muted" title={DWR_FIELD_HINTS.streambedElevFt}>Streambed (inferred)</dt>
            <dd>{fmt(dwr.streambedElevFt, " ft")}</dd>
            <dt className="text-muted" title={DWR_FIELD_HINTS.heightFt}>DWR height (inferred)</dt>
            <dd>{fmt(dwr.heightFt, " ft")}</dd>
            <dt className="text-muted">Top − streambed (H check only)</dt>
            <dd>{fmt(ref?.heightCheckFt, " ft")}</dd>
            <dt className="text-muted" title={DWR_FIELD_HINTS.maxPoolStorageAcFt}>Max-pool storage (inferred)</dt>
            <dd>{fmt(dwr.maxPoolStorageAcFt, " ac-ft")}</dd>
            <dt className="text-muted" title={DWR_FIELD_HINTS.auxSpillwayElevFt}>Aux spillway (inferred)</dt>
            <dd>{fmt(dwr.auxSpillwayElevFt, " ft")}</dd>
            <dt className="text-muted" title={DWR_FIELD_HINTS.normalPoolElevFt}>Normal pool (inferred)</dt>
            <dd>{fmt(dwr.normalPoolElevFt, " ft")}</dd>
            <dt className="text-muted" title={DWR_FIELD_HINTS.maxPoolAreaAc}>Max-pool area (inferred)</dt>
            <dd>
              {fmt(dwr.maxPoolAreaAc, " ac")}
              {dwr.maxPoolAreaAc != null && dam.surfaceAreaAc !== dwr.maxPoolAreaAc ? (
                <button type="button" className="ml-2 underline" onClick={() => update(dwrAreaPatch(dam, dwr, dwrAsOf))}>
                  Use
                </button>
              ) : null}
            </dd>
            {dwr.county ? (
              <>
                <dt className="text-muted">DWR county</dt>
                <dd>{dwr.county}</dd>
              </>
            ) : null}
          </dl>
          {ref?.heldNote ? <p className="mt-2 text-amber">{ref.heldNote}</p> : null}
          {ref?.heightCheckNote ? <p className="mt-2 text-amber">{ref.heightCheckNote}</p> : null}
          {ref?.storageFlag ? <p className="mt-1 text-amber">{ref.storageFlag}</p> : null}
          {offer.ok ? (
            <div className="mt-2 rounded-sm bg-sand p-2">
              <p data-testid="dwr-offer">
                Crest {offer.crestNavdFt} ft, breach bottom {offer.streambedNavdFt} ft NAVD88, {offer.sourceNote}
                {offer.shiftFt != null ? ` (DWR stated ${offer.statedCrestFt} / ${offer.statedStreambedFt} ft NGVD29)` : ""}.
              </p>
              <label className="mt-1 flex items-center gap-2">
                <input type="checkbox" checked={checked} onChange={(e) => setChecked(e.target.checked)} />
                I checked these against the dam file.
              </label>
              <button
                type="button"
                disabled={!checked}
                onClick={() => update(applyDwrElevationsPatch(dam, dwr, dwrAsOf))}
                className="mt-1 h-10 rounded-sm border border-line bg-card px-3 disabled:opacity-50"
              >
                Use DWR elevations (checked against the dam file)
              </button>
            </div>
          ) : (
            <p className="mt-2 text-muted">{offer.reason}</p>
          )}
          <p className="mt-2 text-xs text-muted">{data.dwrSource.attribution}</p>
        </div>
      ) : null}

      {dugout ? (
        <div className="rounded-md border border-line bg-sand/60 p-3 text-sm text-muted" data-testid="dwr-dugout">
          <p className="font-medium">
            DWR dugout record (reference only) <span>(structure {dugout.dwrId ?? "—"})</span>
          </p>
          <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1">
            <dt title={DUGOUT_FIELD_HINTS.maxDepthFt}>Depth</dt>
            <dd>{fmt(dugout.maxDepthFt, " ft")}</dd>
            <dt title={DUGOUT_FIELD_HINTS.maxHeightFt}>Height</dt>
            <dd>{fmt(dugout.maxHeightFt, " ft")}</dd>
            <dt title={DUGOUT_FIELD_HINTS.freeboardFt}>Freeboard</dt>
            <dd>{fmt(dugout.freeboardFt, " ft")}</dd>
            <dt title={DUGOUT_FIELD_HINTS.maxSurfaceAreaAc}>Surface area</dt>
            <dd>{fmt(dugout.maxSurfaceAreaAc, " ac")}</dd>
            <dt title={DUGOUT_FIELD_HINTS.maxStorageAcFt}>Storage</dt>
            <dd>{fmt(dugout.maxStorageAcFt, " ac-ft")}</dd>
          </dl>
          <p className="mt-1 text-xs">Not used by the engine and never fills a field.</p>
        </div>
      ) : null}

      {r ? (
        <details className="rounded-md border border-line bg-card p-3 text-sm" data-testid="nid-facts">
          <summary className="cursor-pointer font-medium">NID facts (reference, not used by the engine)</summary>
          <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1">
            {(
              [
                ["River", r.river],
                ["City", r.city],
                ["Purpose", r.purpose],
                ["Dam type", r.damType],
                ["NID Height", fmt(r.nidHeightFt, " ft")],
                ["Dam height", fmt(r.damHeightFt, " ft")],
                ["Structural height", fmt(r.structuralHeightFt, " ft")],
                ["Hydraulic height", fmt(r.hydraulicHeightFt, " ft")],
                ["Max storage", fmt(r.maxStorageAcFt, " ac-ft")],
                ["Normal storage", fmt(r.normalStorageAcFt, " ac-ft")],
                ["Normal-pool surface area (not filled)", fmt(r.surfaceAreaNormalAc, " ac")],
                ["NID updated", r.nidUpdated],
                ["Last inspection", r.lastInspection],
                ["EAP", r.eap],
                ["NID downloaded", r.downloaded],
              ] as [string, string | null][]
            ).map(([k, v]) => (
              <div key={k} className="contents">
                <dt className="text-muted">{k}</dt>
                <dd>{v ?? "—"}</dd>
              </div>
            ))}
          </dl>
          <p className="mt-2 text-xs text-muted">{r.attribution}</p>
        </details>
      ) : null}
      <button type="button" className="text-sm text-muted underline" onClick={() => update(unlinkPatch())}>
        Unlink NID (keep the values, drop the tags and NID reference)
      </button>
    </div>
  );
}

/** About text: what Crest is and where its dam data come from. */
export function AboutData() {
  const [open, setOpen] = useState(false);
  const { data } = useNdData(open);
  return (
    <details className="mt-8 text-xs text-muted" onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary className="cursor-pointer">About Crest and its data sources</summary>
      <p className="mt-2">
        Crest is a desk screen for dam-breach studies. It does not assign an official hazard class. The ND dam list is an
        offline copy refreshed only on request.
      </p>
      <p className="mt-1">{data?.nidSource.attribution ?? "Dam inventory data: U.S. Army Corps of Engineers, National Inventory of Dams."}</p>
      {data?.dwrSource ? <p className="mt-1">{data.dwrSource.attribution}</p> : null}
      {data?.dwrSource ? <p className="mt-1">{data.dwrSource.disclaimer}</p> : null}
    </details>
  );
}
