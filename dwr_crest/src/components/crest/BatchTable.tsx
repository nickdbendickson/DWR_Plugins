/**
 * The batch results table, on its own so the run loop in BatchScreen.tsx can change without touching the rendering.
 * @tanstack/react-table gives sortable columns and a text filter over the rows BatchScreen hands in (already in the
 * worklist order, batch.ts sortRows): with no column sort chosen the rows stay in that order, and "Worklist order"
 * puts it back. The header sticks while the body scrolls, every row's id can be copied in one click, and the
 * narrower columns hide on a phone (the CSV has them all) so the table no longer needs a 760 px floor.
 */
import { useEffect, useMemo, useState } from "react";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getSortedRowModel,
  useReactTable,
  type FilterFn,
  type Row,
  type SortingState,
} from "@tanstack/react-table";

import type { RowData } from "@tanstack/react-table";

import { KIND_LABEL, type BatchRow } from "@/lib/crest/batch";
import { fold } from "@/lib/crest/nd-dams";
import { RECOMMENDATION_ORDER } from "@/lib/crest/triage";

declare module "@tanstack/react-table" {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars -- the library's interface takes these two parameters
  interface ColumnMeta<TData extends RowData, TValue> {
    /** Extra classes on the column's cells (the narrower columns hide below sm/md). */
    className?: string;
    align?: "right";
  }
}

const num = (v: number | null | undefined, d = 0) => (v == null ? "—" : v.toLocaleString("en-US", { maximumFractionDigits: d }));

/** Numbers sort with the missing ones last, whichever direction. */
function nullsLast(a: number | null, b: number | null): number {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  return a - b;
}

/** Triage, screen and status columns sort in their worklist order (needs-runner first), not alphabetically. */
const TRIAGE_RANK = (r: BatchRow) => (r.triage ? RECOMMENDATION_ORDER[r.triage.recommendation] : r.status === "needs-data" ? 90 : 99);
const SCREEN_RANK: Record<string, number> = { High: 0, Medium: 1, Review: 2, Low: 3 };

/** One text box over id, name, county, triage label, screen label and status (fold: case and accents dropped). */
const rowFilter: FilterFn<BatchRow> = (row, _columnId, value) => {
  const q = fold(String(value ?? ""));
  if (!q) return true;
  const r = row.original;
  return fold(`${r.id} ${r.label} ${r.county ?? ""} ${r.triage?.label ?? ""} ${r.screenLabel ?? ""} ${r.status} ${KIND_LABEL[r.kind]}`).includes(q);
};

const col = createColumnHelper<BatchRow>();

/** "Copy id": the structure id to the clipboard, with a two-second "Copied" in place of the button's text. */
function CopyId({ id }: { id: string }) {
  const [done, setDone] = useState(false);
  useEffect(() => {
    if (!done) return;
    const t = setTimeout(() => setDone(false), 2000);
    return () => clearTimeout(t);
  }, [done]);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(id);
      setDone(true);
    } catch {
      // No clipboard (http, or permission refused): select the id text instead so a manual copy is one keystroke.
      const sel = window.getSelection();
      const el = document.querySelector(`[data-batch-id="${CSS.escape(id)}"]`);
      if (sel && el) {
        const range = document.createRange();
        range.selectNodeContents(el);
        sel.removeAllRanges();
        sel.addRange(range);
      }
    }
  };
  return (
    <button type="button" className="ml-1 rounded-sm border border-line bg-paper px-1.5 text-[0.7rem] text-muted hover:text-ink" onClick={() => void copy()} aria-label={`Copy ${id}`} title="Copy the id">
      {done ? "Copied" : "copy"}
    </button>
  );
}

export function BatchTable({
  rows,
  canOpen,
  onOpen,
  openLabel = "Open in desk",
}: {
  /** Rows in worklist order (sortRows); the table keeps that order until a column sort is chosen. */
  rows: BatchRow[];
  /** Whether "Open in desk" is offered for a row (its finished record is still in memory). */
  canOpen: (id: string) => boolean;
  onOpen: (id: string) => void;
  openLabel?: string;
}) {
  const [sorting, setSorting] = useState<SortingState>([]);
  const [filter, setFilter] = useState("");

  const columns = useMemo(
    () => [
      col.accessor("label", {
        id: "structure",
        header: "Structure",
        sortingFn: (a: Row<BatchRow>, b: Row<BatchRow>) => a.original.label.localeCompare(b.original.label, "en", { numeric: true }),
        cell: (c) => {
          const r = c.row.original;
          return (
            <>
              <span className="font-medium">{r.label}</span>
              <span className="block text-xs text-muted">
                <span data-batch-id={r.id}>{r.id}</span>
                <CopyId id={r.id} />
                {" · "}
                {KIND_LABEL[r.kind]}
                {r.crestFrom ? ` · crest from ${r.crestFrom}` : ""}
              </span>
            </>
          );
        },
      }),
      col.accessor((r) => r.county ?? "", {
        id: "county",
        header: "County",
        sortDescFirst: false,
        cell: (c) => c.row.original.county ?? "—",
        meta: { className: "hidden sm:table-cell" },
      }),
      col.accessor(TRIAGE_RANK, {
        id: "triage",
        header: "Triage",
        sortDescFirst: false,
        sortingFn: (a: Row<BatchRow>, b: Row<BatchRow>) => TRIAGE_RANK(a.original) - TRIAGE_RANK(b.original),
        cell: (c) => {
          const r = c.row.original;
          return (
            <>
              {r.triage ? (
                <>
                  <span className={r.triage.recommendation === "needs-runner" ? "font-semibold text-amber" : ""}>{r.triage.label}</span>
                  <span className="block text-xs text-muted">{r.triage.confidence} confidence</span>
                </>
              ) : (
                <span className="text-muted">{r.status === "needs-data" ? "Needs data" : "Failed"}</span>
              )}
              {r.reason ? <span className="block text-xs text-muted">{r.reason}</span> : null}
            </>
          );
        },
      }),
      col.accessor((r) => (r.screen ? SCREEN_RANK[r.screen] ?? 9 : 9), {
        id: "screen",
        header: "Screen",
        sortDescFirst: false,
        cell: (c) => c.row.original.screenLabel ?? "—",
        meta: { className: "hidden md:table-cell" },
      }),
      col.accessor("releasedAcFt", {
        id: "released",
        header: "Released ac-ft",
        sortingFn: (a: Row<BatchRow>, b: Row<BatchRow>) => nullsLast(a.original.releasedAcFt, b.original.releasedAcFt),
        cell: (c) => num(c.row.original.releasedAcFt),
        meta: { className: "text-right hidden md:table-cell", align: "right" },
      }),
      col.accessor("floodedAc", {
        id: "flooded",
        header: "Flooded ac",
        sortingFn: (a: Row<BatchRow>, b: Row<BatchRow>) => nullsLast(a.original.floodedAc, b.original.floodedAc),
        cell: (c) => num(c.row.original.floodedAc),
        meta: { className: "text-right", align: "right" },
      }),
      col.accessor("structuresFlooded", {
        id: "homes",
        header: "Homes wet / in reach",
        sortingFn: (a: Row<BatchRow>, b: Row<BatchRow>) => nullsLast(a.original.structuresFlooded, b.original.structuresFlooded),
        cell: (c) => {
          const r = c.row.original;
          return r.structuresFlooded == null ? "—" : `${r.structuresFlooded} / ${r.structuresInside ?? "?"}`;
        },
        meta: { className: "text-right", align: "right" },
      }),
      col.display({
        id: "open",
        header: () => <span className="sr-only">Open</span>,
        cell: (c) =>
          canOpen(c.row.original.id) ? (
            <button type="button" className="inline-flex h-9 items-center rounded-sm border border-line bg-paper px-2 text-xs" onClick={() => onOpen(c.row.original.id)}>
              {openLabel}
            </button>
          ) : null,
        enableSorting: false,
      }),
    ],
    [canOpen, onOpen, openLabel],
  );

  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting, globalFilter: filter },
    onSortingChange: setSorting,
    onGlobalFilterChange: setFilter,
    globalFilterFn: rowFilter,
    getRowId: (r) => r.id,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  });

  const shown = table.getRowModel().rows;
  return (
    <div className="mt-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <label className="flex min-w-0 flex-1 items-center gap-2 sm:max-w-sm">
          <span className="sr-only">Filter rows</span>
          <input
            type="search"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Filter: triage, county or id"
            className="h-10 w-full rounded-sm border border-line bg-paper px-3"
            data-testid="batch-filter"
          />
        </label>
        <span className="text-muted" aria-live="polite">
          {shown.length === rows.length ? `${rows.length.toLocaleString("en-US")} rows` : `${shown.length.toLocaleString("en-US")} of ${rows.length.toLocaleString("en-US")} rows`}
        </span>
        {sorting.length ? (
          <button type="button" className="text-water underline" onClick={() => setSorting([])}>
            Worklist order
          </button>
        ) : null}
      </div>
      <div className="mt-2 max-h-[70vh] max-w-full overflow-auto">
        <table className="w-full text-left text-sm" data-testid="batch-results">
          <thead className="sticky top-0 z-10 bg-card text-xs text-muted">
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id} className="border-b border-line">
                {hg.headers.map((h) => {
                  const meta: { className?: string; align?: "right" } = h.column.columnDef.meta ?? {};
                  const sorted = h.column.getIsSorted();
                  return (
                    <th
                      key={h.id}
                      scope="col"
                      className={`py-2 pr-2 align-bottom ${meta.className ?? ""}`}
                      aria-sort={sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : h.column.getCanSort() ? "none" : undefined}
                    >
                      {h.isPlaceholder ? null : h.column.getCanSort() ? (
                        <button
                          type="button"
                          className={`inline-flex items-center gap-1 font-medium ${meta.align === "right" ? "flex-row-reverse" : ""}`}
                          onClick={h.column.getToggleSortingHandler()}
                          title="Sort by this column; click again to reverse, a third time for the worklist order"
                        >
                          {flexRender(h.column.columnDef.header, h.getContext())}
                          <span aria-hidden="true" className="w-3 text-[0.65rem]">
                            {sorted === "asc" ? "▲" : sorted === "desc" ? "▼" : ""}
                          </span>
                        </button>
                      ) : (
                        flexRender(h.column.columnDef.header, h.getContext())
                      )}
                    </th>
                  );
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {shown.map((row) => (
              <tr key={row.id} className="border-b border-line align-top">
                {row.getVisibleCells().map((cell) => {
                  const meta: { className?: string } = cell.column.columnDef.meta ?? {};
                  return (
                    <td key={cell.id} className={`py-2 pr-2 ${meta.className ?? ""}`}>
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  );
                })}
              </tr>
            ))}
            {shown.length === 0 ? (
              <tr>
                <td colSpan={columns.length} className="py-3 text-muted">
                  No row matches “{filter}”.
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  );
}
