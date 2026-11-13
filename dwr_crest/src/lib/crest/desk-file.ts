/**
 * Desk backup and restore (2026-11-13). The desk's records live only in this browser (localStorage, with each dam's
 * terrain layer in IndexedDB), so a cleared browser, another computer or a full storage quota lost them, and a
 * dam.json written from the Report step could not be read back in.
 *
 *   deskFileText   every dam on the desk, whole (inputs, receptors, drawn geometry, saved 911 / PLSS lookups, the
 *                  terrain layer, an imported run's summary), as one JSON file: "Save desk to file".
 *   openDeskFile   reads that file back, or a dam.json: one written by the Report step since this change carries the
 *                  whole record (`crest_record`) and comes back exactly; an older dam.json, or the desktop's
 *                  Write-the-dam-folder dam.json, carries the engineering inputs only and comes back as a new record
 *                  from those, with a note saying what the file did not hold.
 *   mergeIntoDesk  adds the opened dams to the desk without overwriting anything, or replaces the desk with them.
 *
 * A file is data from outside: every record is rebuilt field by field onto a blank record, wrong types are dropped
 * with a note, and nothing in it is run or rendered as markup.
 */
import { blankDam } from "./samples.ts";
import type { BreachMethod, DamRecord, DatumName, EngineName, FailureMode } from "./types.ts";
import { CREST_VERSION } from "./version.ts";

export const DESK_FILE_FORMAT = "crest-desk";
export const DESK_FILE_VERSION = 1;
/** Refuse a file larger than this before parsing it (a desk of 500 dams with terrain layers is far smaller). */
export const DESK_FILE_MAX_BYTES = 400 * 1024 * 1024;

export type DeskFile = {
  format: typeof DESK_FILE_FORMAT;
  formatVersion: number;
  crestVersion: string;
  savedAt: string;
  activeId: string;
  dams: DamRecord[];
};

/** The whole desk as a file. NaN (an outlet level left blank) is written as null, as JSON has no NaN. */
export function deskFileText(dams: DamRecord[], activeId: string, now = new Date()): string {
  const file: DeskFile = {
    format: DESK_FILE_FORMAT,
    formatVersion: DESK_FILE_VERSION,
    crestVersion: CREST_VERSION,
    savedAt: now.toISOString(),
    activeId,
    dams,
  };
  return JSON.stringify(file) + "\n";
}

/** crest-desk-2026-11-13.json (local date). */
export function deskFileName(now = new Date()): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `crest-desk-${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}.json`;
}

export type OpenedFile =
  | {
      ok: true;
      /** "desk": a Save-desk file. "record": a dam.json carrying the whole record. "inputs": a dam.json with inputs only. */
      kind: "desk" | "record" | "inputs";
      dams: DamRecord[];
      activeId?: string;
      savedAt?: string;
      crestVersion?: string;
      notes: string[];
    }
  | { ok: false; reason: string };

type Obj = Record<string, unknown>;
const isObj = (v: unknown): v is Obj => v !== null && typeof v === "object" && !Array.isArray(v);
const num = (v: unknown): number | undefined =>
  typeof v === "number" && Number.isFinite(v) ? v : undefined;
const text = (v: unknown, max = 400): string | undefined =>
  typeof v === "string" ? v.slice(0, max) : undefined;

const FAILURE: FailureMode[] = ["overtopping", "piping"];
const METHOD: BreachMethod[] = ["froehlich", "macdonald", "tr60"];
const ENGINE: EngineName[] = ["lisflood", "anuga", "routing"];

const oneOf =
  <T extends string>(allowed: readonly T[]) =>
  (v: unknown): T | undefined =>
    allowed.includes(v as T) ? (v as T) : undefined;
const bool = (v: unknown) => (typeof v === "boolean" ? v : undefined);
const obj = (v: unknown) => (isObj(v) ? v : undefined);
const longText = (v: unknown) => text(v, 4000);
const shortText = (v: unknown) => text(v);

/**
 * How each field of a record is read from a file. Typed on every key of DamRecord, so a field added to the record
 * does not compile until it is given a reader here (and so cannot be silently left out of a backup). A reader returns
 * undefined for a value it does not accept; the blank record's value then stands.
 */
const READ: { [K in keyof Required<DamRecord>]: (v: unknown) => unknown } = {
  id: () => undefined, // set by recordFromFile after its own check
  name: shortText,
  nid: shortText,
  county: shortText,
  year: num,
  heightFt: num,
  lengthFt: num,
  crestFt: num,
  streambedFt: num,
  // Outlet levels: blank (NaN) is a real state, written to JSON as null.
  spillwayFt: (v) => num(v) ?? Number.NaN,
  normalPoolFt: (v) => num(v) ?? Number.NaN,
  storageAcFt: num,
  drainageMi2: num,
  statedDatum: oneOf<DatumName | "">(["NAVD88", "NGVD29", "Local", ""]),
  demCrestFt: num,
  failureMode: oneOf(FAILURE),
  method: oneOf(METHOD),
  engine: oneOf(ENGINE),
  lat: num,
  lon: num,
  datumOffsetFt: num,
  datumNote: longText,
  classOfRecord: longText,
  sampleNote: longText,
  // Structured parts are kept as they are when they are objects (the engine and the views guard their insides).
  lisflood: obj,
  surfaceAreaAc: num,
  reachKm: num,
  receptors: () => undefined, // filtered by recordFromFile
  geometry: obj,
  plss: obj,
  resultsOfRecord: obj,
  sourceOfRecordChoice: oneOf(["desk"] as const),
  nd911: obj,
  outline: obj,
  judgmentAsProbable: bool,
  receptorSource: oneOf(["nd911"] as const),
  nd911AutoAdded: bool,
  nidId: longText,
  nidHazard: oneOf(["Low", "Significant", "High", "Undetermined"] as const),
  provenance: obj,
  nidRef: obj,
};

/** Fields every record has: a present but unusable value in the file is worth a note. */
const REQUIRED: (keyof DamRecord)[] = [
  "name",
  "nid",
  "county",
  "year",
  "heightFt",
  "lengthFt",
  "crestFt",
  "streambedFt",
  "storageAcFt",
  "drainageMi2",
  "demCrestFt",
  "statedDatum",
  "failureMode",
  "method",
  "engine",
];

/**
 * A record from a file, rebuilt onto a blank one. Returns null when it is not a record at all (no id or name).
 * `notes` gets a line for each field that was dropped.
 */
export function recordFromFile(raw: unknown, notes: string[] = []): DamRecord | null {
  if (!isObj(raw)) return null;
  const id = text(raw.id, 80);
  const name = text(raw.name);
  if (!id || !/^[A-Za-z0-9._:-]+$/.test(id) || name === undefined) return null;
  const d = { ...blankDam(), id, receptors: [] } as DamRecord;
  const out = d as unknown as Obj;
  for (const k of Object.keys(READ) as (keyof DamRecord)[]) {
    if (!Object.hasOwn(raw, k)) continue;
    const v = READ[k](raw[k]);
    if (v !== undefined) out[k] = v;
    else if (REQUIRED.includes(k) && raw[k] !== null)
      notes.push(`${name || id}: "${k}" was not a usable value and was left at the default.`);
  }
  if (Array.isArray(raw.receptors)) {
    const kept = raw.receptors.filter(
      (r) => isObj(r) && typeof r.id === "string" && typeof r.kind === "string",
    );
    if (kept.length !== raw.receptors.length)
      notes.push(
        `${name || id}: ${raw.receptors.length - kept.length} receptor(s) had no id or kind and were dropped.`,
      );
    d.receptors = kept as DamRecord["receptors"];
  }
  return d;
}

const first = (o: Obj, ...keys: string[]): unknown => {
  for (const k of keys) if (o[k] !== undefined && o[k] !== null) return o[k];
  return undefined;
};

/**
 * A new record from a dam.json that holds the engineering inputs only: the Report step's dam.json before this
 * change, or the desktop's dam-folder dam.json (top_of_dam_ft, dam_length_ft, ...). Crest and streambed in these
 * files are already tied to NAVD88 unless the file says otherwise.
 */
export function recordFromDamJson(j: Obj, notes: string[]): DamRecord | null {
  const name = text(first(j, "name", "dam_name"));
  const height = num(first(j, "height_ft"));
  if (name === undefined || height === undefined) return null;
  const d = { ...blankDam(), receptors: [] } as DamRecord;
  const damId = text(first(j, "dam_id", "nid_id", "id"), 80);
  d.name = name;
  d.nid = damId ?? "NEW";
  d.heightFt = height;
  const set = (
    k:
      | "storageAcFt"
      | "lengthFt"
      | "crestFt"
      | "streambedFt"
      | "surfaceAreaAc"
      | "lat"
      | "lon"
      | "normalPoolFt"
      | "spillwayFt",
    ...keys: string[]
  ) => {
    const v = num(first(j, ...keys));
    if (v !== undefined) d[k] = v;
    return v !== undefined;
  };
  set("storageAcFt", "storage_acft");
  set("lengthFt", "length_ft", "dam_length_ft");
  set("surfaceAreaAc", "surface_area_top_ac", "surface_area_ac");
  set("lat", "lat");
  set("lon", "lon");
  set("normalPoolFt", "normal_pool_ft");
  set("spillwayFt", "spillway_ft");
  const crest = set("crestFt", "crest_ft", "top_of_dam_ft");
  const bed = set("streambedFt", "streambed_ft");
  if (crest && !bed) d.streambedFt = d.crestFt - d.heightFt;
  if (!crest)
    notes.push(
      `${name}: the file has no crest elevation, so the blank record's was kept. Enter it on the Record step.`,
    );
  const datum = String(first(j, "crest_datum", "top_of_dam_datum") ?? "NAVD88");
  d.statedDatum = /NGVD/i.test(datum) ? "NGVD29" : /local/i.test(datum) ? "Local" : "NAVD88";
  if (d.statedDatum === "NAVD88" && crest) d.demCrestFt = d.crestFt;
  if (METHOD.includes(j.method as BreachMethod)) d.method = j.method as BreachMethod;
  if (FAILURE.includes(j.failure_mode as FailureMode))
    d.failureMode = j.failure_mode as FailureMode;
  const county = text(j.county);
  if (county) d.county = county;
  const note = text(j.datum_note, 4000);
  if (note) d.datumNote = note;
  const nid = text(j.nid_id, 80);
  if (nid) d.nidId = nid;
  if (isObj(j.provenance)) d.provenance = j.provenance as DamRecord["provenance"];
  notes.push(
    `${name}: this dam.json holds the engineering inputs only. Receptors, drawn lines and areas, the terrain layer and saved 911 / PLSS lookups were not in the file; add or rebuild them on the desk.`,
  );
  return d;
}

/** Read a Save-desk file or a dam.json. Never throws. */
export function openDeskFile(textIn: string): OpenedFile {
  if (textIn.length > DESK_FILE_MAX_BYTES)
    return { ok: false, reason: "The file is too large to be a Crest desk file." };
  let j: unknown;
  try {
    j = JSON.parse(textIn);
  } catch (e) {
    return {
      ok: false,
      reason: `This is not a JSON file Crest can read (${(e as Error).message}).`,
    };
  }
  if (!isObj(j)) return { ok: false, reason: "This file is not a Crest desk file or a dam.json." };
  const notes: string[] = [];

  if (j.format === DESK_FILE_FORMAT) {
    const v = num(j.formatVersion) ?? 0;
    if (v > DESK_FILE_VERSION)
      return {
        ok: false,
        reason: `This desk file was saved by a newer Crest (file format ${v}; this Crest reads up to ${DESK_FILE_VERSION}). Update Crest and open it again.`,
      };
    if (!Array.isArray(j.dams)) return { ok: false, reason: "This desk file has no dams in it." };
    const dams: DamRecord[] = [];
    const seen = new Set<string>();
    for (const raw of j.dams) {
      const d = recordFromFile(raw, notes);
      if (!d) notes.push("One entry was not a dam record and was skipped.");
      else if (seen.has(d.id))
        notes.push(`${d.name}: a second record with the same id was skipped.`);
      else {
        seen.add(d.id);
        dams.push(d);
      }
    }
    if (!dams.length)
      return { ok: false, reason: "This desk file has no dam records Crest can read." };
    const activeId = text(j.activeId, 80);
    return {
      ok: true,
      kind: "desk",
      dams,
      activeId: activeId && seen.has(activeId) ? activeId : dams[0].id,
      savedAt: text(j.savedAt, 40),
      crestVersion: text(j.crestVersion, 80),
      notes,
    };
  }

  if (isObj(j.crest_record)) {
    const d = recordFromFile(j.crest_record, notes);
    if (d)
      return {
        ok: true,
        kind: "record",
        dams: [d],
        activeId: d.id,
        crestVersion: text(j.crest_version, 80),
        notes,
      };
    notes.push(
      "The record stored in this dam.json could not be read, so its inputs were used instead.",
    );
  }
  const d = recordFromDamJson(j, notes);
  if (d)
    return {
      ok: true,
      kind: "inputs",
      dams: [d],
      activeId: d.id,
      crestVersion: text(j.crest_version, 80),
      notes,
    };
  return {
    ok: false,
    reason: "This file is not a Crest desk file or a dam.json (it has no dam name and height).",
  };
}

/** JSON with object keys sorted, so two records compare equal whatever order their fields were set in. */
function canonical(v: unknown): string {
  return JSON.stringify(v, (_k, val) =>
    isObj(val)
      ? Object.fromEntries(
          Object.keys(val)
            .sort()
            .map((k) => [k, val[k]]),
        )
      : val,
  );
}
const same = (a: DamRecord, b: DamRecord) => canonical(a) === canonical(b);

export type MergeResult = {
  dams: DamRecord[];
  /** Id to select afterwards. */
  activeId: string;
  added: number;
  /** Already on the desk, identical: left alone. */
  unchanged: number;
  /** Same id as a dam on the desk but different content: added beside it under a new id. */
  copied: number;
};

/**
 * "add": every opened dam joins the desk and nothing on the desk is changed. A dam whose id is already on the desk is
 * skipped when it is identical, and otherwise added as a second record (new id, name ending "(from file)"), so an
 * opened file can never overwrite work. "replace": the desk becomes exactly the file's dams.
 */
export function mergeIntoDesk(
  existing: DamRecord[],
  opened: DamRecord[],
  mode: "add" | "replace",
  wantActive?: string,
  makeId: () => string = () => blankDam().id,
): MergeResult {
  if (mode === "replace") {
    const activeId = opened.find((d) => d.id === wantActive)?.id ?? opened[0].id;
    return { dams: opened, activeId, added: opened.length, unchanged: 0, copied: 0 };
  }
  const dams = [...existing];
  const byId = new Map(existing.map((d) => [d.id, d]));
  let added = 0;
  let unchanged = 0;
  let copied = 0;
  let activeId = "";
  for (const d of opened) {
    const here = byId.get(d.id);
    let landed = d;
    if (!here) {
      dams.push(d);
      added++;
    } else if (same(here, d)) {
      unchanged++;
      landed = here;
    } else {
      let id = makeId();
      while (byId.has(id)) id = makeId();
      landed = { ...d, id, name: `${d.name} (from file)` };
      dams.push(landed);
      copied++;
    }
    byId.set(landed.id, landed);
    if (d.id === wantActive || !activeId) activeId = landed.id;
  }
  return { dams, activeId, added, unchanged, copied };
}

/** One sentence for the note over the desk after a file is opened. */
export function mergeSummary(
  file: Extract<OpenedFile, { ok: true }>,
  m: MergeResult,
  mode: "add" | "replace",
): string {
  const n = (k: number, w: string) => `${k} ${w}${k === 1 ? "" : "s"}`;
  if (mode === "replace") return `The desk now holds the ${n(m.added, "dam")} from the file.`;
  const parts = [];
  if (m.added) parts.push(`${n(m.added, "dam")} added`);
  if (m.copied)
    parts.push(
      `${n(m.copied, "dam")} added as a second copy, because a different record with the same id is already on the desk`,
    );
  if (m.unchanged) parts.push(`${n(m.unchanged, "dam")} already on the desk, unchanged`);
  const what = file.kind === "desk" ? "Desk file opened" : "dam.json opened";
  return `${what}: ${parts.join("; ")}.`;
}
