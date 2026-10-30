import { create } from "zustand";
import { persist } from "zustand/middleware";
import { blankDam, SAMPLE_DAMS } from "./samples.ts";
import type { DamRecord, StepId } from "./types.ts";

type DeskState = {
  dams: DamRecord[];
  activeId: string;
  step: StepId;
  setStep: (step: StepId) => void;
  select: (id: string) => void;
  update: (patch: Partial<DamRecord>) => void;
  add: () => void;
  /** Add a ready-made record (e.g. "New record from NID") and select it. */
  addDam: (dam: DamRecord) => void;
  restore: () => void;
};

export const useDesk = create<DeskState>()(
  persist(
    (set, get) => ({
      dams: SAMPLE_DAMS,
      activeId: SAMPLE_DAMS[0].id,
      step: "record",
      setStep: (step) => set({ step }),
      select: (id) => set({ activeId: id, step: "record" }),
      update: (patch) => {
        const { dams, activeId } = get();
        set({
          dams: dams.map((d) => (d.id === activeId ? { ...d, ...patch, id: d.id } : d)),
        });
      },
      add: () => {
        const dam = blankDam();
        set({ dams: [...get().dams, dam], activeId: dam.id, step: "record" });
      },
      addDam: (dam) => set({ dams: [...get().dams, dam], activeId: dam.id, step: "record" }),
      restore: () => set({ dams: SAMPLE_DAMS, activeId: SAMPLE_DAMS[0].id, step: "record" }),
    }),
    // v2 (2026-10-29): the ND00199 sample changed (912 ft / H 28 -> 1072.24 NAVD88 / H 27.50; round 2 moved it to 1071.09, migrated below) and the
    // screen levels changed. A new key makes returning browsers start from the current samples
    // instead of the old crest-desk-v1 records; old data under v1 is left alone, not migrated.
    // version 1 (round 2, 2026-10-29): the ND00199 sample moved to the DWR values + VERTCON, 1071.09 / 1043.59 NAVD88.
    // Same key, so the user's other records stay; an untouched old sample (1072.24 / 1044.74) is updated in place.
    { name: "crest-desk-v2", skipHydration: true, version: 1, migrate: migrateDesk },
  ),
);

/** Persisted desk state from an older version -> current. Only rewrites an unedited ND00199 sample. */
export function migrateDesk(persisted: unknown, version: number): DeskState {
  const state = (persisted ?? {}) as DeskState;
  if (version < 1 && Array.isArray(state.dams)) {
    const now = SAMPLE_DAMS.find((d) => d.id === "nd00199");
    state.dams = state.dams.map((d) =>
      now && d.id === "nd00199" && d.crestFt === 1072.24 && d.streambedFt === 1044.74
        ? { ...d, crestFt: now.crestFt, streambedFt: now.streambedFt, demCrestFt: now.demCrestFt, datumNote: now.datumNote }
        : d,
    );
  }
  return state;
}

export function activeDam(state: DeskState): DamRecord {
  return state.dams.find((d) => d.id === state.activeId) ?? state.dams[0];
}
