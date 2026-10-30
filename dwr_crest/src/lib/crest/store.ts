import { create } from "zustand";
import { persist } from "zustand/middleware";
import { blankDam, SAMPLE_DAMS } from "./samples";
import type { DamRecord, StepId } from "./types";

type DeskState = {
  dams: DamRecord[];
  activeId: string;
  step: StepId;
  setStep: (step: StepId) => void;
  select: (id: string) => void;
  update: (patch: Partial<DamRecord>) => void;
  add: () => void;
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
      restore: () => set({ dams: SAMPLE_DAMS, activeId: SAMPLE_DAMS[0].id, step: "record" }),
    }),
    // v2 (2026-10-29): the ND00199 sample changed (912 ft / H 28 -> 1072.24 NAVD88 / H 27.50) and the
    // screen levels changed. A new key makes returning browsers start from the current samples
    // instead of the old crest-desk-v1 records; old data under v1 is left alone, not migrated.
    { name: "crest-desk-v2", skipHydration: true },
  ),
);

export function activeDam(state: DeskState): DamRecord {
  return state.dams.find((d) => d.id === state.activeId) ?? state.dams[0];
}
