/**
 * The shared defaults file: the suite-wide starting values every dam tool reads (DamSafety Core 1.2.0
 * `damsuite/shared_defaults.py`, DamIT Core 0.3.0 `damit_core/lib/shared_defaults.py`, and Crest here). One file
 * format, so the file a plugin writes opens in Crest and the other way round:
 *
 *   {"format": "dam-defaults", "format_version": 1, "name": "...", "note": "...", "values": {key: value}}
 *
 * The suite's own values (SUITE_VALUES, a copy of the suite's defaults.json) are merged with YOUR values (only the
 * keys you changed): a key in yours wins, a key you never changed follows the suite. Unknown keys and bad values are
 * left out and reported, never guessed.
 *
 * What Crest does with them: a NEW record starts from the merged values (applyDefaults). A record that exists keeps
 * its own inputs (they belong to the dam, and travel with its dam.json and desk file), and the Breach step and every
 * dam.json say where the record differs from the shared values (assumptions). Crest is a sunny-day screen, so
 * `pmf_breach_volume` is read and kept but never used here.
 *
 * Pure module (no DOM, no fetch).
 */
import { DEFAULT_ROUGHNESS, MANNING_RANGE, roughnessOf } from "./routing.ts";
import type { BreachMethod, DamRecord, FailureMode, StartPool } from "./types.ts";

export const DEFAULTS_FORMAT = "dam-defaults";
export const DEFAULTS_FORMAT_VERSION = 1;
export const DEFAULTS_FILE_NAME = "defaults.json";
/** A defaults file is a few hundred bytes; anything this large is not one. */
export const DEFAULTS_MAX_BYTES = 200_000;

export type SharedKey = "sunny_day_start_pool" | "pmf_breach_volume" | "failure_mode" | "breach_method" | "manning_n";
export type SharedValue = string | number;
export type SharedValues = Partial<Record<SharedKey, SharedValue>>;

type Spec =
  | { kind: "choice"; label: string; choices: [string, string][]; help: string; crest: string }
  | { kind: "number"; label: string; minimum: number; maximum: number; help: string; crest: string };

/** The keys, their choices and ranges: the same as the suite's SCHEMA. `crest` says what Crest does with the key. */
export const SCHEMA: Record<SharedKey, Spec> = {
  sunny_day_start_pool: {
    kind: "choice",
    label: "Sunny-day breach: starting pool",
    choices: [
      ["max_pool", "Maximum pool (top of dam; maximum storage)"],
      ["normal_pool", "Normal pool"],
    ],
    help: "Where the pool stands when a sunny-day (no inflow) breach starts.",
    crest: "A new record starts its breach here. A record without a normal pool elevation uses the top of dam and says so.",
  },
  pmf_breach_volume: {
    kind: "choice",
    label: "PMF breach: reservoir volume in the breach equations",
    choices: [
      ["storage_at_start_pool", "Storage at the pool when the breach starts"],
      ["max_storage", "Maximum storage"],
    ],
    help: "The volume the breach equations take for a PMF or flood-inflow breach.",
    crest: "Not used by Crest (a sunny-day screen). Kept in the file for the Runner.",
  },
  failure_mode: {
    kind: "choice",
    label: "Failure mode",
    choices: [
      ["piping", "Piping"],
      ["overtopping", "Overtopping"],
    ],
    help: "Used by the Froehlich equations.",
    crest: "A new record starts with this failure mode.",
  },
  breach_method: {
    kind: "choice",
    label: "Breach width and time equation",
    choices: [
      ["froehlich2008", "Froehlich (2008)"],
      ["froehlich1995", "Froehlich (1995)"],
      ["mlm1984", "MacDonald & Langridge-Monopolis (1984)"],
      ["vtg1990", "Von Thun & Gillette (1990), erodible"],
      ["tr60", "NRCS TR-60, triangular hydrograph"],
      ["tr60_curvilinear", "NRCS TR-60, curvilinear hydrograph"],
    ],
    help: "The breach parameter equation a new run starts with.",
    crest: "Crest has Froehlich (2008), MacDonald and a TR-60 width. Another choice starts new records on Froehlich (2008).",
  },
  manning_n: {
    kind: "number",
    label: "Manning's n (where there is no n raster or land cover)",
    minimum: MANNING_RANGE.lo,
    maximum: MANNING_RANGE.hi,
    help: "The roughness used where no land cover gives one.",
    crest: "The floodplain n of a new record's routed flood. The channel n (0.035) is set per record.",
  },
};
export const SHARED_KEYS = Object.keys(SCHEMA) as SharedKey[];

/** The suite's values: a copy of DamSafety_Core/defaults.json (the same values as damit_core/data/defaults.json). */
export const SUITE_VALUES: Required<SharedValues> = {
  sunny_day_start_pool: "max_pool",
  pmf_breach_volume: "storage_at_start_pool",
  failure_mode: "piping",
  breach_method: "froehlich2008",
  manning_n: 0.06,
};

const same = (a: SharedValue | undefined, b: SharedValue | undefined) =>
  typeof a === "number" || typeof b === "number" ? Math.abs(Number(a) - Number(b)) < 1e-12 : a === b;

/** The typed value, or why it cannot be used (the suite's check()). A choice's label is accepted as well as its id. */
export function checkValue(key: string, value: unknown): { ok: true; value: SharedValue } | { ok: false; problem: string } {
  const spec = (SCHEMA as Record<string, Spec | undefined>)[key];
  if (!spec || !Object.hasOwn(SCHEMA, key)) return { ok: false, problem: `unknown key '${key}'` };
  if (spec.kind === "choice") {
    const s = value == null ? "" : String(value).trim();
    const hit = spec.choices.find(([id, label]) => id === s || label.toLowerCase() === s.toLowerCase());
    return hit ? { ok: true, value: hit[0] } : { ok: false, problem: `'${s}' is not one of ${spec.choices.map((c) => c[0]).join(", ")}` };
  }
  const v = typeof value === "number" ? value : typeof value === "string" && value.trim() !== "" ? Number(value) : Number.NaN;
  if (!Number.isFinite(v)) return { ok: false, problem: `'${String(value)}' is not a number` };
  if (v < spec.minimum || v > spec.maximum) return { ok: false, problem: `${v} is outside ${spec.minimum} - ${spec.maximum}` };
  return { ok: true, value: v };
}

export function labelOf(key: SharedKey, value: SharedValue | null | undefined): string {
  if (value == null) return "not used";
  const spec = SCHEMA[key];
  if (spec.kind === "choice") return spec.choices.find(([id]) => id === value)?.[1] ?? String(value);
  return String(value);
}

export type ReadDefaults = { ok: true; values: SharedValues; problems: string[]; name: string | null } | { ok: false; reason: string };

/** Read one defaults file's text. Unknown keys and bad values are reported and left out; a file that is not a
 * defaults file at all (or was written by a newer format) is refused with the reason. */
export function readDefaultsFile(text: string, fileName = DEFAULTS_FILE_NAME): ReadDefaults {
  if (text.length > DEFAULTS_MAX_BYTES) return { ok: false, reason: `${fileName} is too large to be a defaults file.` };
  let data: unknown;
  try {
    data = JSON.parse(text.charCodeAt(0) === 0xfeff ? text.slice(1) : text);
  } catch (e) {
    return { ok: false, reason: `${fileName} is not valid JSON (${(e as Error).message}).` };
  }
  if (data === null || typeof data !== "object" || Array.isArray(data)) return { ok: false, reason: `${fileName} has no 'values' object.` };
  const d = data as Record<string, unknown>;
  const values = d.values ?? {};
  if (values === null || typeof values !== "object" || Array.isArray(values)) return { ok: false, reason: `${fileName} has no 'values' object.` };
  if (d.format != null && d.format !== DEFAULTS_FORMAT)
    return { ok: false, reason: `${fileName} is not a defaults file (format ${JSON.stringify(d.format)}, expected "${DEFAULTS_FORMAT}").` };
  if (d.format == null && !("values" in d)) return { ok: false, reason: `${fileName} is not a defaults file (no "format" and no "values").` };
  if (typeof d.format_version === "number" && Number.isInteger(d.format_version) && d.format_version > DEFAULTS_FORMAT_VERSION)
    return { ok: false, reason: `${fileName} was written in defaults format ${d.format_version} by a newer tool; this Crest reads format ${DEFAULTS_FORMAT_VERSION}. Update Crest.` };
  const out: SharedValues = {};
  const problems: string[] = [];
  for (const [k, v] of Object.entries(values as Record<string, unknown>)) {
    const c = checkValue(k, v);
    if (c.ok) out[k as SharedKey] = c.value;
    else problems.push(`${fileName}: ${c.problem} (${k})`);
  }
  return { ok: true, values: out, problems, name: typeof d.name === "string" ? d.name : null };
}

/** Only usable keys and values (a stored copy may be from an older Crest, or hand-edited). */
export function cleanValues(raw: unknown): SharedValues {
  const out: SharedValues = {};
  if (raw === null || typeof raw !== "object" || Array.isArray(raw)) return out;
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    const c = checkValue(k, v);
    if (c.ok) out[k as SharedKey] = c.value;
  }
  return out;
}

export type Shared = { values: Required<SharedValues>; source: Record<SharedKey, "suite" | "yours"> };

/** The suite's values with yours over them, and where each one came from. */
export function mergeDefaults(yours: SharedValues, suite: Required<SharedValues> = SUITE_VALUES): Shared {
  const values = { ...suite } as Required<SharedValues>;
  const source = {} as Record<SharedKey, "suite" | "yours">;
  for (const k of SHARED_KEYS) {
    const mine = yours[k];
    const changed = mine != null && !same(mine, suite[k]);
    if (changed) values[k] = mine as never;
    source[k] = changed ? "yours" : "suite";
  }
  return { values, source };
}

/** Your values that differ from the suite's: what your defaults file holds, so an unchanged key follows suite updates. */
export function changedOnly(values: SharedValues, suite: Required<SharedValues> = SUITE_VALUES): SharedValues {
  const out: SharedValues = {};
  for (const k of SHARED_KEYS) {
    const v = values[k];
    if (v != null && !same(v, suite[k])) out[k] = v;
  }
  return out;
}

/** The text of YOUR defaults file (the suite's save_user layout): only the keys that differ from the suite. */
export function defaultsFileText(yours: SharedValues, now = new Date()): string {
  const keep = changedOnly(yours);
  const pad = (n: number) => String(n).padStart(2, "0");
  const saved = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  return (
    JSON.stringify(
      {
        format: DEFAULTS_FORMAT,
        format_version: DEFAULTS_FORMAT_VERSION,
        name: "Your defaults",
        note: "Your changes to the suite defaults. A key not listed here follows the suite file. Edit by hand, in QGIS (Plugins > Dam safety > Shared defaults) or in Crest (Settings).",
        saved,
        values: keep,
      },
      null,
      1,
    ) + "\n"
  );
}

// ---- shared values <-> a Crest record -------------------------------------------------------------------------

const METHOD_FROM_SHARED: Record<string, BreachMethod | undefined> = { froehlich2008: "froehlich", mlm1984: "macdonald", tr60: "tr60", tr60_curvilinear: "tr60" };
const METHOD_TO_SHARED: Record<BreachMethod, string> = { froehlich: "froehlich2008", macdonald: "mlm1984", tr60: "tr60" };
const POOL_FROM_SHARED: Record<string, StartPool> = { max_pool: "top", normal_pool: "normal" };

export type RecordDefaults = {
  startPool: StartPool;
  failureMode: FailureMode;
  method: BreachMethod;
  nFloodplain: number;
  /** Where a shared value could not be taken as it stands (a breach equation Crest does not have). */
  notes: string[];
};

/** What a new Crest record starts from, given the shared values. */
export function recordDefaults(values: Required<SharedValues>): RecordDefaults {
  const notes: string[] = [];
  const shared = String(values.breach_method);
  let method = METHOD_FROM_SHARED[shared];
  if (!method) {
    method = "froehlich";
    notes.push(`The shared defaults ask for ${labelOf("breach_method", shared)}, which Crest does not have. New records start on Froehlich (2008).`);
  } else if (shared === "tr60" || shared === "tr60_curvilinear") {
    notes.push("The shared defaults ask for an NRCS TR-60 hydrograph. Crest's TR-60 is a breach width from the dam height, routed through the same weir as the other methods.");
  }
  return {
    startPool: POOL_FROM_SHARED[String(values.sunny_day_start_pool)] ?? "top",
    failureMode: values.failure_mode === "overtopping" ? "overtopping" : "piping",
    method,
    nFloodplain: Number(values.manning_n),
    notes,
  };
}

/** A new record with the shared values on it. Values equal to Crest's built-in ones leave the record untouched. */
export function applyDefaults(dam: DamRecord, values: Required<SharedValues>): DamRecord {
  const d = recordDefaults(values);
  const next: DamRecord = { ...dam, failureMode: d.failureMode, method: d.method };
  if (d.startPool === "top") delete next.startPool;
  else next.startPool = d.startPool;
  const nChannel = dam.roughness?.nChannel;
  if (same(d.nFloodplain, DEFAULT_ROUGHNESS.nFloodplain)) {
    if (nChannel != null) next.roughness = { nChannel };
    else delete next.roughness;
  } else next.roughness = { ...(nChannel != null ? { nChannel } : {}), nFloodplain: d.nFloodplain };
  return next;
}

/** What this record uses, in the shared file's own keys and ids (null = this run does not use the key). */
export function usedValues(dam: DamRecord, usedStartPool: StartPool = dam.startPool ?? "top"): Record<SharedKey, SharedValue | null> {
  return {
    sunny_day_start_pool: usedStartPool === "normal" ? "normal_pool" : usedStartPool === "elevation" ? "entered_elevation" : "max_pool",
    pmf_breach_volume: null,
    // MacDonald & Langridge-Monopolis has no failure-mode term.
    failure_mode: dam.method === "macdonald" ? null : dam.failureMode,
    breach_method: METHOD_TO_SHARED[dam.method] ?? String(dam.method),
    manning_n: roughnessOf(dam.roughness).nFloodplain,
  };
}

export type Assumptions = {
  used: Record<string, SharedValue | null>;
  differs: Record<string, { used: SharedValue; shared: SharedValue }>;
  shared_values: Record<string, SharedValue>;
  shared_source: Record<string, "suite" | "yours">;
  tool: string;
  version: string;
  when: string;
  note: string;
};

/**
 * The "assumptions" entry a run writes into dam.json (the suite's assumptions()): what this record used and where
 * that differs from the shared values. `usedStartPool` is the pool the engine really used (Analysis.breach.startPool),
 * which is the top of dam when the record's own choice could not be used.
 */
export function assumptions(dam: DamRecord, shared: Shared, version: string, usedStartPool?: StartPool, now = new Date()): Assumptions {
  const used = usedValues(dam, usedStartPool);
  const differs: Assumptions["differs"] = {};
  for (const k of SHARED_KEYS) {
    const u = used[k];
    const s = shared.values[k];
    if (u != null && s != null && !same(u, s)) differs[k] = { used: u, shared: s };
  }
  const notes: string[] = [];
  const nCh = roughnessOf(dam.roughness).nChannel;
  if (nCh !== DEFAULT_ROUGHNESS.nChannel) notes.push(`Channel Manning's n ${nCh} (Crest built-in ${DEFAULT_ROUGHNESS.nChannel}).`);
  if (used.sunny_day_start_pool === "entered_elevation") notes.push("The breach starts at an elevation entered on the record (starting_pool_ft).");
  return {
    used,
    differs,
    shared_values: { ...shared.values },
    shared_source: { ...shared.source },
    tool: "Crest",
    version,
    when: now.toISOString().slice(0, 19),
    note: notes.join(" "),
  };
}

/** Plain sentences for the Breach step and the report: where this record differs from the shared values. */
export function differsText(entry: Pick<Assumptions, "differs">): string[] {
  return Object.keys(entry.differs)
    .sort()
    .map((k) => {
      const d = entry.differs[k];
      const key = k as SharedKey;
      const used = d.used === "entered_elevation" ? "an entered elevation" : labelOf(key, d.used);
      return `${SCHEMA[key].label}: ${used} on this record; the shared defaults say ${labelOf(key, d.shared)}.`;
    });
}
