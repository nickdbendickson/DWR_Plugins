import { useState } from "react";
import { FolderOpen, Save } from "lucide-react";

import {
  DESK_FILE_MAX_BYTES,
  deskFileName,
  deskFileText,
  mergeIntoDesk,
  mergeSummary,
  openDeskFile,
  type OpenedFile,
} from "@/lib/crest/desk-file";
import { saveText } from "@/lib/crest/save";
import { useDesk } from "@/lib/crest/store";

type Opened = Extract<OpenedFile, { ok: true }>;

/**
 * "Save desk to file" and "Open desk file or dam.json" (desk-file.ts). The records live only in this browser, so this
 * is the desk's backup, and the way to move work to another computer or between Crest.html, the desktop app and the
 * web app. Opening a file never overwrites a dam on the desk unless "Replace the desk" is chosen.
 */
export function DeskFiles({ onNote }: { onNote: (note: string) => void }) {
  const [pending, setPending] = useState<{ file: Opened; name: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const link = "inline-flex h-11 items-center gap-2 text-left text-sm text-muted hover:text-ink";

  async function save() {
    const { dams, activeId } = useDesk.getState();
    setError(null);
    const r = await saveText(deskFileName(), deskFileText(dams, activeId), "application/json");
    if (r.status === "error") setError(`The desk file was not saved: ${r.message}`);
    else if (r.status === "saved")
      onNote(
        `Desk saved to a file: ${dams.length} dam${dams.length === 1 ? "" : "s"}${r.path ? ` (${r.path})` : ""}. Keep it somewhere safe; open it again with "Open desk file or dam.json".`,
      );
  }

  async function pick(files: FileList | null) {
    const f = files?.[0];
    if (!f) return;
    setError(null);
    setPending(null);
    if (f.size > DESK_FILE_MAX_BYTES)
      return setError(`${f.name} is too large to be a Crest desk file.`);
    setBusy(true);
    try {
      const opened = openDeskFile(await f.text());
      if (!opened.ok) return setError(`${f.name}: ${opened.reason}`);
      // One dam (a dam.json) just joins the desk; a whole desk file asks first, since it can replace the desk.
      if (opened.kind === "desk") setPending({ file: opened, name: f.name });
      else apply(opened, "add");
    } catch (e) {
      setError(`${f.name} could not be read (${(e as Error).message}).`);
    } finally {
      setBusy(false);
    }
  }

  function apply(file: Opened, mode: "add" | "replace") {
    const { dams, setDams } = useDesk.getState();
    const m = mergeIntoDesk(dams, file.dams, mode, file.activeId);
    setDams(m.dams, m.activeId);
    setPending(null);
    onNote([mergeSummary(file, m, mode), ...file.notes].join(" "));
  }

  const onDesk = useDesk((s) => s.dams.length);
  return (
    <div className="space-y-1" data-testid="desk-files">
      <button type="button" className={link} onClick={() => void save()} data-testid="desk-save">
        <Save className="size-4 shrink-0" aria-hidden="true" />
        Save desk to file
      </button>
      <label className={link + " cursor-pointer" + (busy ? " pointer-events-none opacity-50" : "")}>
        <FolderOpen className="size-4 shrink-0" aria-hidden="true" />
        Open desk file or dam.json
        <input
          type="file"
          accept=".json,application/json"
          className="sr-only"
          data-testid="desk-open"
          disabled={busy}
          onChange={(e) => {
            void pick(e.target.files);
            e.target.value = "";
          }}
        />
      </label>
      {error ? (
        <p className="text-sm text-amber" role="alert" data-testid="desk-file-error">
          {error}
        </p>
      ) : null}
      {pending ? (
        <div
          className="space-y-2 rounded-sm border border-line bg-sand p-3 text-sm"
          role="dialog"
          aria-label="Open desk file"
          data-testid="desk-file-choice"
        >
          <p>
            <span className="font-medium">{pending.name}</span> holds {pending.file.dams.length} dam
            {pending.file.dams.length === 1 ? "" : "s"}
            {pending.file.savedAt ? `, saved ${pending.file.savedAt.slice(0, 10)}` : ""}.
          </p>
          <button
            type="button"
            className="block h-10 w-full rounded-sm border border-water bg-water px-3 text-card"
            onClick={() => apply(pending.file, "add")}
            data-testid="desk-file-add"
          >
            Add to this desk
          </button>
          <button
            type="button"
            className="block h-10 w-full rounded-sm border border-line bg-card px-3"
            onClick={() => apply(pending.file, "replace")}
            data-testid="desk-file-replace"
          >
            Replace this desk ({onDesk} dam{onDesk === 1 ? "" : "s"} removed)
          </button>
          <button type="button" className="text-muted underline" onClick={() => setPending(null)}>
            Cancel
          </button>
          <p className="text-xs text-muted">
            Adding never changes a dam already on the desk. Replacing removes every dam now on it;
            save the desk first if unsure.
          </p>
        </div>
      ) : null}
    </div>
  );
}
