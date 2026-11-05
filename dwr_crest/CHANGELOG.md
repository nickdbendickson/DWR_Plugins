# Changelog

## 2026-11-05

- **Batch screen: many structures at once, including every DWR dam and the DWR dugouts.** A new **Batch** view picks
  structures one by one, by county, by HUC8 watershed or statewide, from the NID main dams (948), the DWR Dams layer
  (8,955 more, all statuses but Removed / Not Built) and the DWR Dugouts layer (1,766), and runs each through the
  desk's own breach, inundation layer, 911 lookup and triage with an auto-derived record (crest from DWR NAVD 88 or
  3DEP at the point, direction from the 3DEP downhill). Results sort as a Runner worklist with a CSV and Open in desk.
  `npm run batch:dams` does the same from the command line, resumable, and reads a local DEM mosaic with `--dem`.
  New bundle `nd_structures.json` (`npm run data:nd-structures`).
- **DWR Unknown Structures in the batch and the DWR fill.** From Nick's DWR export (not on the map service). Most
  (3,611 of 3,770) are already DWR Dams records under a new number, or NID dams: their UNK- number is now an alias that
  finds that record (UNK-2400 is DWR-16431). The other 144 are listed as "DWR unknown structure", status Unknown
  (on by default).
- **Local DEM, DWR fill, map location and breach point on the desk.** The desktop app can use a local DEM file (e.g.
  the statewide 1 m VRT) for all terrain instead of 3DEP, chosen in a file dialog and remembered. The Record step can
  make a new record from any DWR dam or dugout, and a new dam's latitude and longitude can be set by clicking the map.
  The Flood step picks the breach point automatically (lowest downstream toe along the dam line) or by a click on the
  map, and the reservoir point by a click.

## 2026-11-04

- **The web app renders on the server without crashing, and hydrates without tearing the page down.** Crest is a
  TanStack Start (SSR) app. Two things broke a production (`vite preview`) load. The Leaflet maps touch `window` at
  module load, and they were `lazy()` inside `Suspense` with no client guard, so a map in the first render threw
  `ReferenceError: window is not defined` on the server. And the guided-setup step card read `navigator.onLine`
  straight during render, so the server (no `navigator`, treated as online) and a first client render that happened to
  be offline produced different step text and React threw a #418 hydration mismatch, regenerating the whole tree on
  every load. Maps now render inside a `ClientOnly` boundary (`useSyncExternalStore`, so the server and the first
  client pass agree on the fallback), and online status comes from the shared SSR-safe `useOnline` hook (starts
  `true`, updates after mount) instead of an inline `navigator.onLine` read. Verified against the production preview:
  no server `window` error, no #418. The static desktop bundle was already clean.
- **The Results step no longer overflows a phone screen.** Its top `<section>` was a grid with an unconstrained
  (`auto`) column, so its cards sized to their content (573 px) and ran off a 390 px viewport. The column is now
  `minmax(0,1fr)` and the map cell `min-w-0`, so the content wraps to fit; the wide layout is unchanged.
- **crest-desktop fits an 800 px screen.** The pywebview window's `min_size` was `(900, 600)`, wider than an 800 px
  display; it is now `(760, 560)`.

- **Desktop "Run the flood model" without a DEM point works again: Crest sends the Runner's option numbers, not
  their text.** With Point X / Y blank the desk runs `ndbreach:rundam` through `qgis_process`, passing METHOD and TIER
  as the option text. QGIS 3.34's `qgis_process` rejects that text ("Incorrect parameter value for METHOD", then
  TIER), so the button failed on both plugin sets (ND Runner 3.27.5, DamSafety Runner 3.14.5). Crest now reads the
  option list from `qgis_process help ndbreach:rundam` once per run and sends the matching index, so a reordered
  plugin still gets the right option; if help cannot be read it sends the text as before. The `ndbreach_cli.py` path
  (Point X / Y filled in) was not affected. Test: `crest-desktop/tests/test_handoff.py`
  (`test_model_sends_enum_indexes_read_from_help`).

- **The routing reads the terrain: a Muskingum-Cunge-Todini wave in defined valleys, a frozen Muskingum-Cunge wave on
  flat plains and tiny pools.** The frozen-coefficient Muskingum-Cunge translates a wave down a flat, wide reach about
  right but attenuates it too little in a cut valley, where the real wave slows and flattens as it fills the overbank.
  The variable-parameter Muskingum-Cunge-Todini scheme (Todini 2007) recomputes the celerity and width each step at the
  local flow and captures that attenuation, but it over-damps a shallow overbank on a plain, where the celerity
  collapses and the wave bogs. The router now chooses per reach, not per dam: a reach steeper than 0.0015 whose section
  carries the peak within its banks routes with MCT, the rest stay on frozen MC. One dam can be confined upstream and a
  plain downstream and gets both. The MCT coefficients are the Courant/cell-Reynolds form, which sum to one every step,
  so the scheme conserves mass (no manufactured volume, unlike naive variable-parameter MC). Four-dam re-score against
  LISFLOOD-FP (CSI at 0.1 ft / 1 ft): DWR-2801 0.255 / 0.191 → 0.255 / 0.192 (flat plain, held on MC), DWR-2865 0.318 /
  0.282 → 0.327 / 0.303 (incised), DWR-3034 0.694 / 0.653 → 0.765 / 0.714 (confined), DWR-4031 0.369 / 0.159 → 0.362 /
  0.165 (tiny pool, held on MC). The confinement trigger is insensitive from 0.0005 to 0.002 on all four dams. Saved
  routed layers ask to be built again.
- **The spread pours each stretch on the ground beside it, not on a stripe due north or south of it.** Each stretch of
  the routed channel pours its water on the cells nearest it. "Nearest" was counted in grid steps (a 4-neighbour
  search), and by that count a point off a slanting channel is equally near a long run of it: whole grid columns went
  to one channel cell, so on the flat DWR-2801 plain the flood came out as a comb of north-south stripes up to 3 km off
  the channel, deep where a stripe crossed a low and dry between. Nearest is now the straight-line distance. Four-dam
  re-score against LISFLOOD-FP (CSI at 0.1 ft / 1 ft): DWR-2801 0.227 → 0.255 / 0.175 → 0.191, DWR-2865 0.295 → 0.318 /
  0.311 → 0.282, DWR-3034 0.701 → 0.694 / 0.633 → 0.653, DWR-4031 0.354 → 0.369 / 0.101 → 0.159. Saved routed layers ask
  to be built again.
- **Water that tops a saddle fills the low behind it lowest ground first.** Every cell behind a saddle shares its spill
  height; the pour laid the overflow over them in raster order (north row first) and solved its level with cells above
  it counted at negative depth. It now fills the low from its lowest cell and holds the slice exactly.
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
- **Published accuracy envelope (`docs/accuracy-envelope.md`).** A trust map of where a Crest footprint can screen and
  where it needs a Runner run, scored on all four baselines at 0.1 ft and 1 ft (1 ft is the headline: the 2-D
  references agree with each other there). Confined valley / basin (DWR-3034) high, CSI 0.71 at 1 ft; incised valley
  (2865) medium, 0.30; tiny pool (4031) extent only, 0.17 at 1 ft; flat multi-channel plain (2801) low, 0.19, run the
  Runner. An *incomplete downstream* alert on the routed layer means the dam needs a Runner run. The 0.35 CSI floor is
  retired: LISFLOOD-FP, ANUGA and DSS-WISE agree with each other on 2801 only at 0.34–0.49 at 0.1 ft. 2801 against
  five references (LISFLOOD-FP 20/10/5 m, ANUGA, DSS-WISE): CSI 0.19–0.25 at 1 ft (median 0.21). Scored on 34524e0.

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
