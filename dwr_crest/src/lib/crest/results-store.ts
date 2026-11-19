/** Imported runs for this session (rasters live in the results worker; only summaries and samples here). Not persisted. */
import { create } from "zustand";
import type { RunSummary } from "./raster-engine.ts";
import type { FloodStrip } from "./floodstrip.ts";
import type { ReceptorSample, ResultSource } from "./types.ts";

export type RunState = {
  key: string;
  damId: string;
  source: ResultSource;
  status: "loading" | "ready" | "error";
  progress?: string;
  error?: string;
  inputs: string[];
  summary?: RunSummary;
  samples?: ReceptorSample[];
  /** Context / points / strip the summary and samples were computed for. */
  ctxKey?: string;
  pointsKey?: string;
  strip?: FloodStrip | null;
  stripKey?: string;
  importedAt?: string;
};

type ResultsState = {
  runs: Record<string, RunState>;
  patch: (key: string, p: Partial<RunState> & Pick<RunState, "damId" | "source">) => void;
  remove: (key: string) => void;
};

export const runKey = (damId: string, source: ResultSource) => `${damId}:${source}`;

export const useResults = create<ResultsState>()((set) => ({
  runs: {},
  patch: (key, p) =>
    set((s) => {
      const base: RunState = s.runs[key] ?? { key, damId: p.damId, source: p.source, status: "loading", inputs: [] };
      return { runs: { ...s.runs, [key]: { ...base, ...p } } };
    }),
  remove: (key) =>
    set((s) => {
      const runs = { ...s.runs };
      delete runs[key];
      return { runs };
    }),
}));
