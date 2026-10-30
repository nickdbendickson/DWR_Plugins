import { SAMPLE_DAMS } from "./samples.ts";
import type { DamRecord } from "./types.ts";

/** DWR-2801 / ND00199 as the Breach Runner ran it (Docs & QA r3 harness, run_crest_2801.mjs). */
export function dwr2801(over: Partial<DamRecord> = {}): DamRecord {
  return {
    ...SAMPLE_DAMS[0],
    id: "dwr-2801",
    nid: "ND00199",
    heightFt: 27.5,
    storageAcFt: 1612.59,
    lengthFt: 1700,
    crestFt: 1071.09,
    streambedFt: 1043.59,
    spillwayFt: Number.NaN,
    normalPoolFt: Number.NaN,
    statedDatum: "NAVD88",
    demCrestFt: 1070.0,
    failureMode: "piping",
    method: "froehlich",
    engine: "anuga",
    // Runner inputs only: no surface area unless a test sets it (m = 2), auto reach.
    surfaceAreaAc: undefined,
    reachKm: undefined,
    classOfRecord: undefined,
    sampleNote: undefined,
    ...over,
  };
}
