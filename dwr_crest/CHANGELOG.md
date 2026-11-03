# Changelog

## 2026-11-03

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
