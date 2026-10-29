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
    { name: "crest-desk-v1", skipHydration: true },
  ),
);

export function activeDam(state: DeskState): DamRecord {
  return state.dams.find((d) => d.id === state.activeId) ?? state.dams[0];
}
