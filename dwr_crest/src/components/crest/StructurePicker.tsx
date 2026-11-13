/**
 * Record step: "Fill from DWR dams / dugouts / unknown structures" (Nick, 2026-11-05). The NID picker above covers the
 * NID main dams; this one lists every other DWR dam, every DWR dugout and the DWR Unknown Structures from the batch
 * screen's structure list (nd_structures.json; an UNK- number DWR has since moved into the Dams layer finds that
 * record, and the few that sit on an NID dam find the NID dam) and
 * makes a new record from one, the same way the batch screen does (batch.ts): height, storage, area and length from
 * DWR; the crest from the terrain at the point (3DEP, or the local DEM when one is chosen) (streambed = crest - H) and the dam line, reservoir and breach point from the
 * terrain, when the terrain service answers; otherwise the DWR numbers only, elevations blank.
 */
import { useEffect, useMemo, useState } from "react";

import { applyDefaults } from "@/lib/crest/settings";
import { sharedNow } from "@/lib/crest/settings-store";
import { KIND_LABEL, aimAlongChannel, deriveRecord, loadStructures, missingData, nearRequest, structureRecord, type Structure } from "@/lib/crest/batch";
import { autoBreachOn } from "@/lib/crest/breach-point";
import { fold, type NdData } from "@/lib/crest/nd-dams";
import { demSourceLabel, fetchDem } from "@/lib/crest/outline";
import { useDesk } from "@/lib/crest/store";

const num = (v: number | null, unit: string) => (v == null ? "—" : `${v.toLocaleString("en-US")} ${unit}`);

export function StructurePicker() {
  const addDam = useDesk((s) => s.addDam);
  const setStep = useDesk((s) => s.setStep);
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<{ nd: NdData; structures: Structure[] } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState<"all" | "dam" | "dugout" | "unknown">("all");
  const [picked, setPicked] = useState<Structure | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    if (!open || data) return;
    loadStructures()
      .then(setData)
      .catch((e) => setErr((e as Error).message));
  }, [open, data]);

  const list = useMemo(() => (data?.structures ?? []).filter((s) => (s.kind !== "nid" || s.alias) && (kind === "all" || s.kind === kind || (kind === "unknown" && s.alias))), [data, kind]);
  const matches = useMemo(() => {
    const q = fold(query);
    if (!q) return [];
    return list
      .filter((s) => (s.kind === "nid" ? fold(s.alias ?? "").includes(q) : fold(`${s.label} ${s.county ?? ""} ${s.huc8 ?? ""}`).includes(q)))
      .slice(0, 30);
  }, [list, query]);

  const make = async (s: Structure) => {
    setBusy(true);
    setNote(null);
    const id = `${s.id.toLowerCase()}-${Date.now().toString(36)}`;
    const shared = sharedNow().values;
    let made = applyDefaults(structureRecord(s, data?.nd, id), shared);
    let msg = "New record from DWR. Elevations are blank: enter the crest and breach bottom, or read them from the terrain.";
    if (!missingData(s)) {
      try {
        const g = await fetchDem(nearRequest(s.lon!, s.lat!, 0.6, 5));
        const r = deriveRecord(s, g, data?.nd, demSourceLabel() === "3DEP" ? "3DEP" : "local DEM");
        if (r.ok) {
          made = applyDefaults({ ...r.dam, id }, shared);
          // Same two-pass aim as the batch: the synthesized line down the traced channel.
          made = (await aimAlongChannel(made, r.derived.u, (q) => fetchDem(q)).catch(() => ({ dam: made }))).dam;
          const b = autoBreachOn(made, g);
          if (b.ok) made = { ...made, geometry: b.geometry };
          msg = `New record from DWR. ${made.sampleNote ?? ""}`;
        }
      } catch (e) {
        msg = `New record from DWR numbers only (the terrain service did not answer: ${(e as Error).message}). Enter the crest and breach bottom.`;
      }
    } else msg = `New record from DWR. ${missingData(s)} Enter it before the screen can run.`;
    addDam(made);
    // addDam opens the guided setup; stay on the record so the note and any blank fields are in view.
    setStep("record");
    setBusy(false);
    setNote(msg);
  };

  const btn = "inline-flex h-11 items-center rounded-sm border border-line bg-paper px-3 text-sm text-ink disabled:opacity-50";
  return (
    <div className="mt-3 rounded-md border border-line p-3" data-testid="structure-picker">
      <button type="button" className={btn} aria-expanded={open} onClick={() => setOpen(!open)}>
        {open ? "Close DWR dams, dugouts and unknown structures" : "Fill from DWR dams, dugouts, unknown structures"}
      </button>
      {open ? (
        <div className="mt-3 space-y-2">
          {err ? <p className="text-sm text-red-700">Could not load the DWR lists: {err}</p> : null}
          {!data && !err ? <p className="text-sm text-muted">Loading the DWR dam, dugout and unknown structure lists…</p> : null}
          {data ? (
            <>
              <div className="flex flex-wrap items-end gap-2">
                <label className="block min-w-0 flex-1 text-sm">
                  <span className="text-muted">DWR, DUG or UNK number, NID ID, county or watershed</span>
                  <input
                    className="mt-1 h-11 w-full rounded-sm border border-line bg-paper px-3"
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    placeholder="e.g. DWR-4031, DUG-130, UNK-2400, Ward"
                  />
                </label>
                <label className="block text-sm">
                  <span className="text-muted">Show</span>
                  <select className="mt-1 block h-11 rounded-sm border border-line bg-paper px-2" value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
                    <option value="all">All DWR structures</option>
                    <option value="dam">DWR dams</option>
                    <option value="dugout">DWR dugouts</option>
                    <option value="unknown">DWR unknown structures</option>
                  </select>
                </label>
              </div>
              {matches.length ? (
                <ul className="max-h-56 overflow-y-auto rounded-sm border border-line">
                  {matches.map((s) => (
                    <li key={s.id} className="border-b border-line last:border-b-0">
                      <button type="button" className={`block min-h-11 w-full px-3 py-1 text-left text-sm ${picked?.id === s.id ? "bg-sand" : ""}`} onClick={() => setPicked(s)}>
                        <span className="font-medium">{s.label}</span>{" "}
                        <span className="text-muted">
                          · {KIND_LABEL[s.kind]} · {s.county ?? "—"} · {s.status}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              ) : query ? (
                <p className="text-sm text-muted">No DWR dam, dugout or unknown structure matches.</p>
              ) : null}
              {picked ? (
                <div className="rounded-sm bg-sand p-3 text-sm">
                  <p className="font-medium">
                    {picked.label} · {KIND_LABEL[picked.kind]} · {picked.status}
                  </p>
                  <p className="text-muted">
                    {picked.county ?? "—"} County · {picked.huc8 ?? "watershed —"} · H {num(picked.heightFt, "ft")} · V {num(picked.storageAcFt, "ac-ft")} · area{" "}
                    {num(picked.areaAc, "ac")} · length {num(picked.lengthFt, "ft")} · DWR hazard {picked.hazard ?? "—"} (as reported, not used)
                  </p>
                  {missingData(picked) ? <p className="mt-1 text-amber">{missingData(picked)}</p> : null}
                  <button type="button" className={`${btn} mt-2`} disabled={busy} onClick={() => void make(picked)}>
                    {busy ? "Reading the terrain…" : "New record from DWR"}
                  </button>
                </div>
              ) : null}
              <p className="text-xs text-muted">
                Most DWR Unknown Structures are now in the DWR Dams layer under a new number: their UNK- number finds that record (shown in
                brackets). The rest are listed as unknown structures. DWR numbers and locations only; no owner or dam names.
              </p>
            </>
          ) : null}
          {note ? <p className="text-sm text-muted">{note}</p> : null}
        </div>
      ) : null}
    </div>
  );
}
