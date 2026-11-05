import { create } from "zustand";
import { createJSONStorage, persist, type StateStorage } from "zustand/middleware";
import { deleteOutline, loadOutlines, outlineSync, withoutOutline } from "./outline-store.ts";
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

/**
 * localStorage, read lazily, with a full store never thrown into the desk. zustand persist writes on every set(), so a
 * QuotaExceededError there came out of update() itself and the desk showed it as the build's error (Safari's text,
 * "The quota has been exceeded."). The heavy inundation layers no longer go here (outline-store.ts); if the light
 * records still don't fit, the edit stands in this tab and only the save is skipped.
 */
export const deskStorage: StateStorage = {
  getItem: (name) => {
    try {
      return globalThis.localStorage?.getItem(name) ?? null;
    } catch {
      return null;
    }
  },
  setItem: (name, value) => {
    try {
      globalThis.localStorage?.setItem(name, value);
    } catch (e) {
      console.warn(`Crest: could not save the desk in this browser (${(e as Error).message}).`);
    }
  },
  removeItem: (name) => {
    try {
      globalThis.localStorage?.removeItem(name);
    } catch {
      /* nothing to remove */
    }
  },
};

/** What localStorage keeps: the records without their inundation layers (those go to IndexedDB, outline-store.ts). */
export function persistedDesk(s: Pick<DeskState, "dams" | "activeId" | "step">) {
  return { dams: s.dams.map(withoutOutline), activeId: s.activeId, step: s.step };
}

export const useDesk = create<DeskState>()(
  persist(
    (set, get) => ({
      dams: SAMPLE_DAMS,
      activeId: SAMPLE_DAMS[0].id,
      step: "setup",
      setStep: (step) => set({ step }),
      select: (id) => set({ activeId: id, step: "setup" }),
      update: (patch) => {
        const { dams, activeId } = get();
        set({
          dams: dams.map((d) => (d.id === activeId ? applyPatch(d, patch) : d)),
        });
      },
      add: () => {
        const dam = blankDam();
        set({ dams: [...get().dams, dam], activeId: dam.id, step: "setup" });
      },
      addDam: (dam) => set({ dams: [...get().dams, dam], activeId: dam.id, step: "setup" }),
      restore: () => set({ dams: SAMPLE_DAMS, activeId: SAMPLE_DAMS[0].id, step: "setup" }),
    }),
    // v2 (2026-10-29): the ND00199 sample changed (912 ft / H 28 -> 1072.24 NAVD88 / H 27.50; round 2 moved it to 1071.09, migrated below) and the
    // screen levels changed. A new key makes returning browsers start from the current samples
    // instead of the old crest-desk-v1 records; old data under v1 is left alone, not migrated.
    // version 1 (round 2, 2026-10-29): the ND00199 sample moved to the DWR values + VERTCON, 1071.09 / 1043.59 NAVD88.
    // Same key, so the user's other records stay; an untouched old sample (1072.24 / 1044.74) is updated in place.
    // version 2 (full test 2026-10-30): the ND00199 sample's class of record no longer claims "TM11 Significant
    // (DWR-2801 benchmark)"; an unedited old text is updated in place.
    // 2026-11-05: the records only (persistedDesk); each dam's inundation layer is kept in IndexedDB (hydrateDesk).
    {
      name: "crest-desk-v2",
      skipHydration: true,
      version: 2,
      migrate: migrateDesk,
      storage: createJSONStorage(() => deskStorage),
      partialize: (s) => persistedDesk(s) as unknown as DeskState,
    },
  ),
);

let hydrating: Promise<void> | null = null;

/**
 * Load the saved desk: the records from localStorage, then each dam's inundation layer from IndexedDB, and from then
 * on keep IndexedDB in step with the layers. A layer saved inside the records by an older Crest is kept and moved
 * across (the next save writes the records without it). Runs once per page.
 */
export function hydrateDesk(): Promise<void> {
  if (hydrating) return hydrating;
  hydrating = (async () => {
    await useDesk.persist.rehydrate();
    const kept = await loadOutlines();
    const dams = useDesk.getState().dams;
    const ids = new Set(dams.map((d) => d.id));
    if (dams.some((d) => !d.outline && kept.has(d.id)))
      useDesk.setState({ dams: dams.map((d) => (!d.outline && kept.has(d.id) ? { ...d, outline: kept.get(d.id) } : d)) });
    for (const id of kept.keys()) if (!ids.has(id)) void deleteOutline(id);
    // Seeded with what IndexedDB already holds, so those layers aren't written straight back.
    const sync = outlineSync(undefined, new Map([...kept].filter(([id]) => ids.has(id))));
    sync(useDesk.getState().dams);
    useDesk.subscribe((s, prev) => {
      if (s.dams !== prev.dams) sync(s.dams);
    });
  })();
  return hydrating;
}

/**
 * Apply a desk edit to a record. A crest or streambed typed over the stored one drops the stored datum note (e.g. "Crest converted
 * from NGVD29 …"): the note described the old number, and left in place it kept riding on the record's datum_rule and
 * datum_note after the retype (DWR-4031 QA, 2026-11-03). Everything else is a plain merge.
 */
export function applyPatch(d: DamRecord, patch: Partial<DamRecord>): DamRecord {
  const next = { ...d, ...patch, id: d.id };
  // The crest and the streambed are both elevations the note's conversion was applied to, so retyping either clears it.
  const retyped = (["crestFt", "streambedFt"] as const).some((f) => f in patch && patch[f] !== d[f]);
  if (retyped && !("datumNote" in patch)) delete next.datumNote;
  return next;
}

const OLD_ND00199_CLASS = "ND MEDIUM / TM11 Significant (DWR-2801 benchmark)";

/** Persisted desk state from an older version -> current. Only rewrites an unedited ND00199 sample. */
export function migrateDesk(persisted: unknown, version: number): DeskState {
  // A corrupted or non-object persisted value (a primitive, null, or an array) must not be cast blindly to
  // DeskState: the migration below and activeDam() both read state.dams, so a bad value would crash on load
  // rather than fall back to the samples. Start fresh from the current samples when the stored shape is wrong.
  const defaults = (): DeskState => ({ ...({} as DeskState), dams: SAMPLE_DAMS, activeId: SAMPLE_DAMS[0].id, step: "setup" });
  if (persisted === null || typeof persisted !== "object" || Array.isArray(persisted)) return defaults();
  const state = persisted as DeskState;
  if (!Array.isArray(state.dams)) return defaults();
  if (version < 1 && Array.isArray(state.dams)) {
    const now = SAMPLE_DAMS.find((d) => d.id === "nd00199");
    state.dams = state.dams.map((d) =>
      now && d.id === "nd00199" && d.crestFt === 1072.24 && d.streambedFt === 1044.74
        ? { ...d, crestFt: now.crestFt, streambedFt: now.streambedFt, demCrestFt: now.demCrestFt, datumNote: now.datumNote }
        : d,
    );
  }
  if (version < 2 && Array.isArray(state.dams)) {
    const now = SAMPLE_DAMS.find((d) => d.id === "nd00199");
    state.dams = state.dams.map((d) =>
      now && d.id === "nd00199" && d.classOfRecord === OLD_ND00199_CLASS
        ? { ...d, classOfRecord: now.classOfRecord, sampleNote: now.sampleNote }
        : d,
    );
  }
  return state;
}

export function activeDam(state: DeskState): DamRecord {
  return state.dams.find((d) => d.id === state.activeId) ?? state.dams[0];
}
