# Changelog

## 2026-11-04

- **Per-dam triage: "Clearly trivial: screen only", "Uncertain: run it" or "Needs a Runner run".** Crest is the suite's
  fast first-pass screener, never the layer of record, so each dam now gets an explicit call and a terrain confidence
  instead of only a footprint. Needs a Runner run: a large flood (the flood-size gate), a routed layer that is
  incomplete downstream, or anything flooded in Crest's screen. Clearly trivial: nothing flooded, on a complete footprint
  with high confidence. Everything else is uncertain. The confidence comes from a new extent check saved with the layer
  (`stats.extent`): how far the flood's edge moves with a foot of depth error (Crest's depth bias against the 2-D models
  is about a foot), measured on Crest's routed spread and the DEM it was built on. High at 60 m or less (confined valley
  or closed basin), Medium above it (open terrain: the footprint is a minimum), Low when the footprint is incomplete or
  unchecked; the words match the published accuracy envelope. A terrain-outline dam gets the check from a routed-spread
  probe on the same terrain (the outline stays the layer). On the four-dam baseline: DWR-3034 26 m (High; uncertain only
  because it ends at a road-fill basin a culvert could drain), DWR-2801 127 m (Needs a Runner run, Low: large and
  incomplete), DWR-4031 149 m and DWR-2865 158 m (Uncertain, Medium). The threshold is fit on four dams and is
  provisional. The Flood step shows the call with its reasons, the Dashboard carries it as a flag, and
  `scripts/triage-dams.mjs` triages a list of dam records into a CSV, Runner jobs first. Saved layers built before this
  show "build it again for a confidence".

## 2026-11-03

- **The routed flood grows its terrain downstream on demand (issue #62).** The terrain box was sized from the schematic
  stations before any water was routed, and the drainage trace stopped at the schematic's last station or the box's
  edge; on DWR-2801 59 % of the Runner's flood lay outside the terrain entirely. The desk now grows the box and the
  trace downstream only while the routed wave still reaches the end of the traced channel wet, with water above a floor
  (5 % of the release or 10 ac-ft, the lesser) still travelling, and not stopped in a closed basin: at most four rounds,
  each a quarter of the channel (1–4 km) along the channel's own end direction, on the same cell lattice. A contained
  flood gets the single build it always got, cell for cell (DWR-4031, 0.354 unchanged). When the loop stops with water
  still at the edge the layer says so (an "incomplete downstream" alert with the ac-ft stranded) and a Runner run is the
  layer of record there. Four-dam re-score against LISFLOOD-FP at 0.1 ft: DWR-2801 CSI 0.157 → 0.227 (recall 17 → 31 %,
  +13.5 km, flagged incomplete), DWR-2865 0.299 → 0.295, DWR-3034 0.701 (ends in a basin, not grown), DWR-4031 0.354.
  Saved routed layers ask to be built again.
- **An exhausted spread stretch is held at the cap, not stacked.** A stretch whose pool had risen over every reachable
  cell took the whole rest of its slice as depth: 277 ft of water on the 27.5 ft DWR-2801 and a +3.5 ft depth bias
  against both LISFLOOD-FP and DSS-WISE. The pool is now held at the routed stage + 1 ft (or the level that just reaches
  the last cell), never above 2× the dam height; the surplus is withheld and reported. 2801's deepest cell falls from
  277.6 to 23.4 ft and its depth bias against the Runner from +3.6 to +1.0 ft; no dam's footprint changes.
- **Scoring at a chosen wet depth.** `validate-crest.mjs` and `score-routed.mjs` take `--wet-ft` (default 0.1 ft) and
  the report says which threshold it used. At 0.1 ft the 2-D models disagree with each other on a flat plain by
  thousands of acres of thin sheet (LISFLOOD-FP vs ANUGA on DWR-2801: CSI 0.46); at 1 ft they agree (0.58), so a 1 ft
  score says how a layer does where the water matters.
- **The routed flood is now the volume spread.** For large dams (the flood-size gate's routed side) the desk now pours
  the water each stretch of the routed channel holds at its own peak onto the ground beside it, instead of carrying the
  Manning stage across the floodplain. A closed basin on the channel (for example behind a road fill with no culvert in
  the terrain) has to fill before the flood passes, and ends the flood if it can hold it all. Against the four-dam
  LISFLOOD-FP baseline it beats the terrain outline on every dam (CSI 0.16/0.30/0.70/0.35 vs 0.10/0.23/0.58/0.23) and
  lays down 2–6× the released water, where the old stage fill laid down 31–779×. A saved routed layer built with the
  old stage fill asks to be built again. Small dams still get the terrain outline.
- **A Runner run is the layer of record.** When a dam has an imported ND Breach Runner (LISFLOOD-FP) run, that run is the
  inundation layer of record whatever the flood-size gate picks; the terrain layer stays only as a comparison. A run that
  passes the import checks becomes the source of record on its own (choosing the desk estimate on Results is remembered
  and stops that). The PLSS and 911 lists, the Dashboard depth layer and the Results map follow the run once its rasters
  are loaded. Reason: on large floods the terrain layers can badly under-predict (the outline covered about 0.10× of the
  Runner's flooded area on DWR-2801).
- **Runner worklist.** Results lists the large dams (released volume at or above the gate) whose layer of record is still a
  terrain layer, largest release first, with a CSV download. Those dams get a critical flag on the Dashboard and a warning
  on the Flood step's terrain panel until a run is imported.
- **Guided setup (Style C).** A new first step, "Guided setup": a rail of setup steps (Dam record, Breach, Terrain
  layer, Review results) beside a live map of the current record and flood layer. Each step card shows its state
  (done, active, broken) and a broken step says why on its own card, never in a modal. The terrain build shows its
  progress on the rail, and the map redraws as soon as the layer is saved. At most three flag pills sit bottom-left on
  the map, then "+N more". Every field stays editable (nothing is locked); the forms are the same as the classic steps.
- **Dashboard (Style B).** A map-first results dashboard after Results: independent depth, arrival, structures and
  hillshade layers, a labelled hydrograph peak, a 10-row sortable structure table with class badges, a status bar with
  severity pills (a critical volume flag needs acknowledging), and a compare-with-Runner overlay.

## 2026-11-02

- **Paper report (Style 3).** The Report step's printable sheet: header block, KPI table, notice box, Figure 1
  hydrograph with its peak labelled, signature lines, and the disclaimer on every printed sheet.
