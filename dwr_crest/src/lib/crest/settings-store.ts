/**
 * The desk's copy of the shared defaults (settings.ts): YOUR values, merged over the suite's.
 *
 * Desktop window: your values are the shared defaults FILE the QGIS plugins read (crest-desktop defaults.py, through
 * the pywebview bridge), read when the desk opens and written back when you save in Settings. One file, every tool.
 * Web desk: a browser cannot read a file by path, so your values are kept in this browser (localStorage) and move in
 * and out as a file: "Open settings file" reads a defaults.json, "Save settings file" writes one.
 */
import { create } from "zustand";
import { changedOnly, cleanValues, defaultsFileText, mergeDefaults, readDefaultsFile, type Shared, type SharedValues } from "./settings.ts";

export const SETTINGS_KEY = "crest-settings-v1";

type FileReply = { path?: string; text?: string | null; saved?: string; error?: string };
type DefaultsApi = { get_defaults: () => Promise<FileReply>; save_defaults: (text: string) => Promise<FileReply> };

function defaultsApi(): DefaultsApi | undefined {
  if (typeof window === "undefined") return undefined;
  const api = (window as unknown as { pywebview?: { api?: Partial<DefaultsApi> } }).pywebview?.api;
  return api && typeof api.get_defaults === "function" && typeof api.save_defaults === "function" ? (api as DefaultsApi) : undefined;
}

export type SettingsState = {
  /** Your values that differ from the suite's (what your defaults file holds). */
  yours: SharedValues;
  /** Where `yours` lives: the shared file on this computer (desktop), or this browser. */
  where: "file" | "browser";
  /** Desktop: the path of your defaults file. */
  filePath: string | null;
  /** Keys or values that were left out of the file last read, and read or write errors. */
  problems: string[];
};

export const useSettings = create<SettingsState>(() => ({ yours: {}, where: "browser", filePath: null, problems: [] }));

/** The merged values a new record starts from, and where each came from. */
export function sharedNow(): Shared {
  return mergeDefaults(useSettings.getState().yours);
}

function readStored(): SharedValues {
  try {
    const raw = globalThis.localStorage?.getItem(SETTINGS_KEY);
    return raw ? cleanValues((JSON.parse(raw) as { yours?: unknown }).yours) : {};
  } catch {
    return {};
  }
}

function store(yours: SharedValues): string | null {
  try {
    globalThis.localStorage?.setItem(SETTINGS_KEY, JSON.stringify({ yours }));
    return null;
  } catch (e) {
    return `These settings could not be saved in this browser (${(e as Error).message}). They apply until the page is closed.`;
  }
}

let loading: Promise<void> | null = null;

/** Load your values once: from the shared file in the desktop window, else from this browser. */
export function loadSettings(): Promise<void> {
  if (loading) return loading;
  loading = (async () => {
    const api = defaultsApi();
    if (!api) {
      useSettings.setState({ yours: readStored(), where: "browser", filePath: null, problems: [] });
      return;
    }
    try {
      const r = await api.get_defaults();
      if (r.error) {
        useSettings.setState({ yours: {}, where: "file", filePath: r.path ?? null, problems: [`${r.error} The suite's values are used.`] });
        return;
      }
      if (r.text == null) {
        useSettings.setState({ yours: {}, where: "file", filePath: r.path ?? null, problems: [] });
        return;
      }
      const read = readDefaultsFile(r.text, "Your defaults file");
      useSettings.setState(
        read.ok
          ? { yours: changedOnly(read.values), where: "file", filePath: r.path ?? null, problems: read.problems }
          : { yours: {}, where: "file", filePath: r.path ?? null, problems: [`${read.reason} The suite's values are used.`] },
      );
    } catch (e) {
      useSettings.setState({ yours: readStored(), where: "browser", filePath: null, problems: [`The shared defaults file could not be reached (${(e as Error).message}).`] });
    }
  })();
  return loading;
}

/**
 * Keep new values: written to your shared defaults file in the desktop window, to this browser on the web.
 * Returns the error text, or null when saved.
 */
export async function saveSettings(values: SharedValues): Promise<string | null> {
  const yours = changedOnly(cleanValues(values));
  const api = defaultsApi();
  if (api && useSettings.getState().where === "file") {
    try {
      const r = await api.save_defaults(defaultsFileText(yours));
      if (r.error) return r.error;
      useSettings.setState({ yours, filePath: r.saved ?? useSettings.getState().filePath, problems: [] });
      return null;
    } catch (e) {
      return `The shared defaults file could not be written (${(e as Error).message}).`;
    }
  }
  const problem = store(yours);
  useSettings.setState({ yours, problems: problem ? [problem] : [] });
  return problem;
}

/** Test-only: forget the loaded settings. */
export function resetSettings(): void {
  loading = null;
  useSettings.setState({ yours: {}, where: "browser", filePath: null, problems: [] });
}
