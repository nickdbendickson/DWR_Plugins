/**
 * One CSV cell writer for every CSV the desk exports (the batch worklist, the PLSS and 911 lists, the Runner worklist,
 * the triage list, the paper report's structure table).
 *
 * Quoting is RFC 4180: a cell with a comma, a quote, a CR or an LF is wrapped in quotes with its quotes doubled.
 *
 * Formula neutralisation: a cell whose text starts with "=", "+", "-" or "@" (also after a leading tab or CR) is read
 * by Excel, LibreOffice and Sheets as a FORMULA, not text, when the CSV is opened. The 911 address fields, the DWR
 * status and purpose text and the triage reasons are third-party text the desk does not control, so such a cell gets
 * a leading apostrophe (the spreadsheets' own "treat as text" prefix) and is quoted. A number is never touched: a
 * -0.4 ft depth bias, a negative offset or a formatted "-97.325185" longitude is a number to the spreadsheet too, so
 * only a string that is NOT a plain number is prefixed.
 */
const FORMULA_START = /^[\t\r]*[=+\-@]/;
const PLAIN_NUMBER = /^[\t\r]*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;

/** A CSV cell: null/undefined empty, numbers as is, strings quoted as needed and never read as a formula. */
export function csvCell(v: unknown): string {
  if (v == null) return "";
  if (typeof v === "number") return Number.isFinite(v) ? String(v) : "";
  let s = String(v);
  if (FORMULA_START.test(s) && !PLAIN_NUMBER.test(s)) s = `'${s}`;
  return /[",\n\r]/.test(s) || s.startsWith("'") ? `"${s.replace(/"/g, '""')}"` : s;
}

/** A whole row: cells joined by commas. */
export function csvRow(cells: readonly unknown[]): string {
  return cells.map(csvCell).join(",");
}
