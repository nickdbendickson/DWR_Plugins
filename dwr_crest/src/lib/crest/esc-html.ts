// Escape the HTML metacharacters &, <, > in text that will be inserted as innerHTML
// (e.g. Leaflet bindTooltip/bindPopup, which assign string content via node.innerHTML).
// Legitimate display text is unchanged; any embedded markup is shown literally instead
// of being parsed, so a file-provided dam/receptor name cannot inject script.
export const escHtml = (t: string): string =>
  t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
