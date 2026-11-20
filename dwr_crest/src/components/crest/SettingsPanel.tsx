/**
 * Settings: the shared defaults every dam tool starts from (settings.ts), opened from the header.
 *
 * "How would you open and change the settings?" (Nick, 2026-11-13): here, or in QGIS (Plugins > Dam safety > Shared
 * defaults), or by editing the file by hand. All three read and write the same defaults.json. In the desktop window
 * Save writes that file; on the web the values are kept in this browser and move as a file (Open / Save settings file).
 */
import { useEffect, useMemo, useRef, useState } from "react";

import { saveText } from "@/lib/crest/save";
import {
  DEFAULTS_FILE_NAME,
  DEFAULTS_MAX_BYTES,
  SCHEMA,
  SHARED_KEYS,
  SUITE_VALUES,
  changedOnly,
  checkValue,
  defaultsFileText,
  labelOf,
  mergeDefaults,
  readDefaultsFile,
  recordDefaults,
  type SharedKey,
  type SharedValues,
} from "@/lib/crest/settings";
import { saveSettings, useSettings } from "@/lib/crest/settings-store";

const btn = "inline-flex h-10 items-center rounded-sm border border-line bg-paper px-3 text-sm text-ink disabled:cursor-not-allowed disabled:opacity-50";
const box = "mt-1 h-11 w-full rounded-sm border bg-paper px-3 text-ink";

export function SettingsPanel({ onClose }: { onClose: () => void }) {
  const yours = useSettings((s) => s.yours);
  const where = useSettings((s) => s.where);
  const filePath = useSettings((s) => s.filePath);
  const problems = useSettings((s) => s.problems);
  // The form holds every key as text, so a half-typed number is not thrown away while it is typed.
  const [draft, setDraft] = useState<Record<SharedKey, string>>(() => asText(mergeDefaults(yours).values));
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fileNotes, setFileNotes] = useState<string[]>([]);
  const fileInput = useRef<HTMLInputElement>(null);
  useEffect(() => setDraft(asText(mergeDefaults(yours).values)), [yours]);

  const checked = useMemo(() => {
    const values: SharedValues = {};
    const bad: Partial<Record<SharedKey, string>> = {};
    for (const k of SHARED_KEYS) {
      const c = checkValue(k, draft[k]);
      if (c.ok) values[k] = c.value;
      else bad[k] = c.problem;
    }
    return { values, bad };
  }, [draft]);
  const invalid = Object.keys(checked.bad).length > 0;
  const mine = changedOnly(checked.values);
  const dirty = JSON.stringify(mine) !== JSON.stringify(changedOnly(yours));
  const merged = mergeDefaults(mine);
  const presetNotes = recordDefaults(merged.values).notes;

  const save = async () => {
    setError(null);
    const problem = await saveSettings(checked.values);
    if (problem) setError(problem);
    else setNote(where === "file" ? `Saved to ${useSettings.getState().filePath ?? "your defaults file"}. The QGIS plugins read the same file.` : "Saved in this browser. New records start from these values.");
  };
  const open = async (file: File) => {
    setError(null);
    setNote(null);
    setFileNotes([]);
    if (file.size > DEFAULTS_MAX_BYTES) return setError(`${file.name} is too large to be a defaults file.`);
    const read = readDefaultsFile(await file.text(), file.name);
    if (!read.ok) return setError(read.reason);
    setDraft(asText(mergeDefaults(read.values).values));
    setFileNotes(read.problems.map((p) => `${p}. Left out.`));
    setNote(`Read ${file.name}. Check the values, then Save.`);
  };

  return (
    <section className="border-b border-line bg-card" aria-label="Settings" data-testid="settings-panel">
      <div className="mx-auto max-w-6xl px-4 py-4">
        <div className="flex flex-wrap items-baseline gap-3">
          <h2 className="text-xl">Settings: shared defaults</h2>
          <button type="button" className="ml-auto h-10 px-2 text-sm text-muted" onClick={onClose} data-testid="settings-close">
            Close
          </button>
        </div>
        <p className="mt-1 max-w-3xl text-sm text-muted">
          The starting values every dam tool reads: Crest and the QGIS plugins. A new record or run starts from them, and each
          record can still be changed on its Breach step. Records you already have keep their own values.{" "}
          {where === "file" ? (
            <span data-testid="settings-where">
              Your values are in <span className="break-all text-ink">{filePath ?? "your defaults file"}</span>, the same file the plugins read.
            </span>
          ) : (
            <span data-testid="settings-where">
              On the web your values are kept in this browser. Use Open settings file and Save settings file to share one defaults.json with the plugins.
            </span>
          )}
        </p>
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          {SHARED_KEYS.map((k) => {
            const spec = SCHEMA[k];
            const bad = checked.bad[k];
            const isMine = !bad && merged.source[k] === "yours";
            return (
              <label key={k} className="block text-sm">
                <span className="text-muted">{spec.label}</span>
                {spec.kind === "choice" ? (
                  <select data-testid={`setting-${k}`} value={draft[k]} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} className={`${box} border-line`}>
                    {spec.choices.map(([id, text]) => (
                      <option key={id} value={id}>
                        {text}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    type="number"
                    inputMode="decimal"
                    step={0.005}
                    min={spec.minimum}
                    max={spec.maximum}
                    data-testid={`setting-${k}`}
                    aria-invalid={bad ? true : undefined}
                    value={draft[k]}
                    onChange={(e) => setDraft({ ...draft, [k]: e.target.value })}
                    className={`${box} tabular-nums ${bad ? "border-amber" : "border-line"}`}
                  />
                )}
                <span className="mt-1 block text-xs text-muted">
                  {isMine ? <span className="text-ink">Yours. The suite value is {labelOf(k, SUITE_VALUES[k])}. </span> : "Suite value. "}
                  {spec.crest}
                </span>
                {bad ? (
                  <span className="mt-1 block text-xs text-amber" role="status">
                    {bad}
                  </span>
                ) : null}
              </label>
            );
          })}
        </div>
        {[...problems, ...fileNotes, ...presetNotes].length ? (
          <ul className="mt-3 list-disc space-y-1 pl-5 text-sm text-amber" data-testid="settings-problems">
            {[...problems, ...fileNotes, ...presetNotes].map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        ) : null}
        <div className="mt-4 flex flex-wrap gap-2">
          <button type="button" className={btn} disabled={invalid || !dirty} onClick={() => void save()} data-testid="settings-save">
            {where === "file" ? "Save to the shared defaults file" : "Save"}
          </button>
          <button type="button" className={btn} onClick={() => setDraft(asText(SUITE_VALUES))} data-testid="settings-reset">
            Back to the suite values
          </button>
          <button type="button" className={btn} onClick={() => fileInput.current?.click()} data-testid="settings-open">
            Open settings file…
          </button>
          <button
            type="button"
            className={btn}
            disabled={invalid}
            onClick={() => void saveText(DEFAULTS_FILE_NAME, defaultsFileText(checked.values), "application/json")}
            data-testid="settings-download"
          >
            Save settings file…
          </button>
          <input
            ref={fileInput}
            type="file"
            accept=".json,application/json"
            className="sr-only"
            data-testid="settings-file"
            onChange={(e) => {
              const f = e.target.files?.[0];
              e.target.value = "";
              if (f) void open(f);
            }}
          />
        </div>
        {error ? (
          <p className="mt-2 text-sm text-amber" role="alert" data-testid="settings-error">
            {error}
          </p>
        ) : note ? (
          <p className="mt-2 text-sm text-muted" role="status" data-testid="settings-note">
            {note}
          </p>
        ) : null}
      </div>
    </section>
  );
}

function asText(values: SharedValues): Record<SharedKey, string> {
  return Object.fromEntries(SHARED_KEYS.map((k) => [k, String(values[k] ?? SUITE_VALUES[k])])) as Record<SharedKey, string>;
}
