/**
 * The warning catalogue (2026-11-08). Until now only the two NID height checks had codes (W-H1, W-H2 in nd-dams.ts);
 * every other flag the engine, the inundation build and the picker raise was free text, rendered with the text as its
 * React key (two equal texts collided) and impossible to point at in a report or a bug. Each warning now carries a
 * stable code and a severity beside its text. The text is unchanged everywhere: `warningText()` hands consumers and
 * tests that still expect strings exactly what they had, and no threshold or wording moved.
 *
 *   code      where it is raised                         severity
 *   W-D1      engine.ts datum rules (DEM tie, VERTCON,   warn (stop when the crest could not be tied: the screen is
 *             local band, streambed replaced)            held at Review)
 *   W-V1      engine.ts bounded() (an input outside the  warn (clamped to the desk range) / info (a missing input
 *             desk range) and inundation.ts volumeCheck  replaced by the placeholder, the volume band)
 *   W-R1      reach.ts / inundation.ts: the routed       warn
 *             flood still reaches the terrain's edge
 *             with water to carry (reach incomplete)
 *   W-E1      inundation.ts probeExtent failed, so the   info
 *             layer has no extent check
 *   W-P1      engine.ts: a 911 pad kept at 0 because the stop (the screen is held at Review)
 *             terrain for it could not be read
 *   W-S1      outline.ts outlineState: the saved layer   info
 *             is stale (inputs changed, old trace)
 *   W-H1/H2   nd-dams.ts NID height checks               warn
 *
 * The README lists the codes under "Warning codes".
 */

export type WarningSeverity = "info" | "warn" | "stop";

export type Warning = {
  /** Catalogue code, stable across releases (W-D1, W-V1, …). */
  code: WarningCode;
  severity: WarningSeverity;
  /** The sentence shown to the user: exactly the free text the producers wrote before the catalogue. */
  text: string;
};

export type WarningCode = "W-D1" | "W-V1" | "W-R1" | "W-E1" | "W-P1" | "W-S1" | "W-H1" | "W-H2";

export const WARNING_CATALOGUE: Record<WarningCode, { title: string; severity: WarningSeverity }> = {
  "W-D1": { title: "Datum: an elevation could not be tied to NAVD 88 as stated", severity: "warn" },
  "W-V1": { title: "Volume or input band: a value is outside the desk range or the layer's volume band", severity: "warn" },
  "W-R1": { title: "Reach incomplete: the routed flood still reaches the terrain's edge", severity: "warn" },
  "W-E1": { title: "Extent probe failed: the saved layer has no extent check", severity: "info" },
  "W-P1": { title: "Pad terrain failed: a 911 structure sits on the channel bed (pad 0)", severity: "stop" },
  "W-S1": { title: "Layer stale: the saved terrain layer no longer matches the record", severity: "info" },
  "W-H1": { title: "NID height looks structural, not the breach head", severity: "warn" },
  "W-H2": { title: "H disagrees with crest minus breach bottom", severity: "warn" },
};

/** Make a warning with the catalogue's default severity (override it where the producer knows better). */
export function warning(code: WarningCode, text: string, severity?: WarningSeverity): Warning {
  return { code, severity: severity ?? WARNING_CATALOGUE[code].severity, text };
}

/** The text of a warning, or the string itself: the adapter that keeps string consumers and tests working. */
export function warningText(w: Warning | string): string {
  return typeof w === "string" ? w : w.text;
}

/** The texts of a list of warnings (or strings), in order. */
export function warningTexts(ws: readonly (Warning | string)[]): string[] {
  return ws.map(warningText);
}

/** A stable React key for a warning in a list: its code and position, never its text (equal texts collided). */
export function warningKey(w: Warning | string, i: number): string {
  return typeof w === "string" ? `w-${i}` : `${w.code}-${i}`;
}

/**
 * Classify a free-text warning the engine or the inundation build produced, by the sentence it starts with. The
 * producers are being converted one by one; until each one emits a Warning itself, this puts the right code on the
 * text it already writes (and a text no rule matches is W-V1 info, the catch-all). Pure: the text is never changed.
 */
export function classifyWarning(text: string, origin: "datum" | "flood" | "layer" = "datum"): Warning {
  const t = text;
  if (/^No DEM tie\b/.test(t)) return warning("W-D1", t, "stop");
  if (origin === "datum" && /^(Entered NGVD29 offset|NGVD29 offset unknown|Stated datum Local|DEM crest .* differs|Streambed was in the 0–300 ft band|.* ties to .* from the DEM crest|.* is in the 0–300 ft band on a real-datum record)/.test(t))
    return warning("W-D1", t);
  if (/ is outside the desk range /.test(t)) return warning("W-V1", t, "warn");
  if (/ is missing\. Using .* as a placeholder\.$/.test(t)) return warning("W-V1", t, "info");
  if (/^Volume: /.test(t)) return warning("W-V1", t, / Check against a model run\./.test(t) ? "warn" : "info");
  if (/^Pad estimated for /.test(t)) return warning("W-P1", t);
  if (/still reaches the edge|Treat the layer as incomplete|Incomplete downstream|^The schematic flood is still .* deep/.test(t)) return warning("W-R1", t);
  if (/extent probe|no extent check/i.test(t)) return warning("W-E1", t);
  if (/^(The desk inputs changed since|This terrain layer was built before|This routed flood was built with the old stage fill)/.test(t)) return warning("W-S1", t);
  return warning("W-V1", t, origin === "flood" ? "warn" : "info");
}

/**
 * The flags behind a `warnings` list: the engine's own `flags` when they are the same list (same length, same texts),
 * else the texts classified. A test or a caller that spreads an analysis and replaces `warnings` (the dashboard
 * tests blank them) leaves the old `flags` behind, so `warnings` stays the authority.
 */
function flagsFor(warnings: string[], flags: Warning[] | undefined, origin: "datum" | "flood"): Warning[] {
  if (flags && flags.length === warnings.length && flags.every((w, i) => w.text === warnings[i])) return flags;
  return warnings.map((t) => classifyWarning(t, origin));
}

/** The datum flags of an analysis, with their codes. */
export function datumFlags(a: { datum: { warnings: string[]; flags?: Warning[] } }): Warning[] {
  return flagsFor(a.datum.warnings, a.datum.flags, "datum");
}

/** The flood-step flags of an analysis, the same way. */
export function floodFlags(a: { flood: { warnings: string[]; flags?: Warning[] } }): Warning[] {
  return flagsFor(a.flood.warnings, a.flood.flags, "flood");
}

/** "W-D1 · text", the form the Report, the CSVs and the dashboard pills use when they show a flag as one string. */
export function warningLine(w: Warning): string {
  return `${w.code} · ${w.text}`;
}
