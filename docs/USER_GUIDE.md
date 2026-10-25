# DamSafety plugins: user guide (changes through 2026-10-21, main 034e9d7)

This guide covers the behaviour added in PRs #9-#21, #23 and #27, plus #22, #30, #32, #38, #76, #84, #92, #93, #96, #131 (arrival depth), #132 (ANUGA threads), #137 (HEC-RAS time limit) and #126 (zone colours). Other merged PRs (#85-#90, #94, #100-#105, …) are listed in CHANGELOG.md only. The labels in quotes are the exact text in the code. For the full list of changes, see CHANGELOG.md.

## Standard work-folder layout (#18)
The Screening window, run_chain, the 7b hand-off and Setup's Dams-folder picker all create the same empty folders:

```
<work>/2_terrain 3_stage_storage 5_hydrographs 6_triage_envelopes 7_simulations
       9_sensitivity 10_reports 11_benchmark _downloads _logs Dams/ README.txt _layout.json
```
- Tool outputs go into fixed files, for example `1_dams.gpkg`, `3_dams_storage.gpkg`, `7b_runner_results.gpkg` and `8_hazard.gpkg`.
- Tool 7b writes dam folders into `<work>/Dams`.
- Creating the layout never deletes, moves or overwrites anything. It creates nothing inside `Dams/`, because every folder there is read as a dam.
- It refuses to run inside ND_Dam_Suite_Pack, a Hazard reproduction folder, a single dam's folder, a drive root or the home folder.
- An empty step folder doesn't count as a finished step.

## Runner settings
- **DEM resampling onto the model grid** (`DEM_RESAMPLE`): "Automatic" uses the `NDBREACH_DEM_RESAMPLE` environment variable, or **average** if that isn't set. An explicit setting wins over the environment variable. The method used is written to `run_manifest.json`.
- **QA maps** (`QA_MAPS`, "QA maps written into the dam folder"): "Off", "Inflow map only", or "Inflow map and depth map" (the default). On the command line, use `qa_maps: off | inflow | inflow+depth`. Check `qa_inflow_map.png` after every run to see where the inflow point landed relative to the dam.
- **Inflow point (#14):** the lowest qualifying cell within the usual radius. The QA inflow map shows the point, the search radius and the inflow elevation against the top of dam.
- **Inflow side check (#84, Runner 3.10.18+):** the inflow point is always picked on the downstream side of the dam. Check `inflow_point.side_check` in `run_report.json`. If the trace from the inflow runs back over the dam point, or the inflow is behind the dam or on the upstream side of the crest, the first warning reads **"CHECK THE DAM POINT"**. Fix the dam point or give the dam line before trusting the run.
- **Road-fill warning (#84):** if a road from the receptor road layer is within 30 m of the dam point and no DWR dam line (`DAM_LINES`) was given, the Runner warns that the embankment in the DEM may be the road's fill (`dam_point_checks` in `run_report.json`).

## Arrival depth: 0.1 ft by default (#131; Runner 3.10.53, Hazard 2.18.52)
**What it is.** The arrival depth is how deep the water has to get at a spot before the Runner says the flood has "arrived" there. The arrival time in `Arrival Time.tif` is the first time the depth goes above it, in hours after the breach starts. Since Runner 3.10.53 the default is **0.1 ft**. Before, it was **1 ft**.

**Why it changed.** At 1 ft, any place where the flood stayed shallower than 1 ft got no arrival time at all. Most of the flooded houses in the DWR-2801 sample (9 of 10, 0.07-0.65 ft deep) had a blank arrival time. ND Dam Safety Standards 5.4(c) asks for an arrival time at each critical feature, and Section 5.2.2 counts a house flooded "to any degree". 0.1 ft is the same "any depth" the hazard tools already use, so now every flooded house gets an arrival time.

**What the arrival depth controls in the Runner.** It is more than the arrival map. The same number is used for:
- **Arrival times:** `Arrival Time.tif` and the arrival lines and zones (`Arrival Zones.geojson`, the EAP preview), and `latest_arrival_hr` in `run_report.json`.
- **"Flooded" in the run summary:** `flooded_acres_over_arrival_depth`, `receptors_flooded` (structures, road and rail flooded), the velocity at flooded structures, roads and rail, the per-pass flood extent, and `Flood Extent.geojson`.
- **The in-run LISFLOOD-FP vs ANUGA comparison** in `run_report.json` (`comparison`: areas and the fit index F) is counted at this depth.
- **The run's clock.** When the run length is automatic, the Runner runs LISFLOOD-FP again 1.5 times longer if the latest arrival falls in the last 10% of the run (the flood is "still moving"). Shallow edges arrive late, so at 0.1 ft this happens more often. Some runs will simulate longer than they did at 1 ft. A longer run can also spread further.
- It is part of the run key, so a 0.1 ft run never resumes an older 1 ft checkpoint.

**What it does not change.** Depth, velocity and water surface are worked out the same way. The 1 ft edge tolerance (`EDGE_TOL_FT`), which decides when the model area grows, is unchanged. The fixed-depth areas `flooded_acres_ge_0_1ft`, `flooded_acres_ge_1ft` and `flooded_acres_ge_2ft` don't depend on it. Neither do the ND hazard class or the ACER TM 11 class: Hazard and the Runner's ND hazard tool decide "flooded" with their own 0.1 ft "any depth", not with the arrival depth.

**Where to set it.**
- *Run breach model for one dam* (also *Run breach models for a layer of dams (batch)* and *Cascade failure of dams in a chain*): **"Arrival depth (ft): arrival time is when the depth first exceeds this; also the depth counted as flooded in the run summary …"** (`ARRIVAL_FT`).
- *Import HEC-RAS 2D results as rasters*: "Arrival depth (ft; …)" (`ARRIVAL_FT`), default 0.1.
- *Flood polygons and EAP map from a dam's rasters*: "Flood extent and arrival zones: depth at least (ft; …)" (`ARRIVAL_FT`), default 0.1.
- Command line (`ndbreach_cli.py`): `arrival_depth_ft` in the run configuration. The template now has 0.1.
- The value a run used is in `run_report.json` under `settings` → `arrival_depth_ft`. Check it before you compare two runs.
- To get the old behaviour, set it to 1. Hazard's own HEC-RAS import ("Arrival depth (ft)") and the Screening tools already used 0.1 ft and are unchanged.

**What Hazard does with it (Hazard 2.18.52).** Hazard doesn't set an arrival depth for Runner results. It reads the arrival raster the Runner wrote. It uses the arrival time for:
- `First_Arrival_hr` in the structures, roads and rail tables, the notification list and the map book;
- the EAP arrival bands and contours;
- the loss-of-life estimate (Graham), where a blank arrival time counts as "no warning";
- the evacuation timing, where a deep-enough road with no arrival time counts as closed from the start.

When a flooded structure still has no arrival time, its Notes now say why: the arrival raster has no value at its wet cells, and the Runner's default was 1 ft before Runner 3.10.53, so re-run an older result.

**Comparing with older runs.** Every Runner result made before Runner 3.10.53 (main before 732f597) used 1 ft, unless someone set it. After a re-run at 0.1 ft, expect:
- arrival times at places that had none (water 0.1-1 ft deep), and earlier arrival times everywhere else, because the water reaches 0.1 ft before it reaches 1 ft;
- a later `latest_arrival_hr`, and a different median arrival;
- a bigger `flooded_acres_over_arrival_depth` (it now equals the ≥0.1 ft area), more `receptors_flooded`, and different velocities at receptors;
- in Hazard, filled-in `First_Arrival_hr` values, different loss-of-life estimates (shallow houses that had "no warning" now get a warning time; deep houses get less warning) and earlier road closures in the evacuation timing.

Compare like with like. Use the fixed-depth areas (`flooded_acres_ge_0_1ft`, `_ge_1ft`, `_ge_2ft`), max depth and the hazard class across the change. Don't compare arrival times, `flooded_acres_over_arrival_depth`, receptor counts or the in-run fit index between a 1 ft run and a 0.1 ft run. Re-classifying an old result with the new Hazard doesn't fill in its arrival times: you need a Runner re-run for that. RESULTS_LOG.md lists which logged baselines came before the change.

## Settings saves (#12)
Several windows can share one settings file safely. A window that opens without changing anything doesn't rewrite the file. If a save reports that the file can't be read, fix or restore it from `<name>.bak` (or use "Save as" to replace it on purpose) instead of deleting it. A leftover `<name>.lock` from a crashed QGIS clears itself after 30 s.

## Shallow-basin storage guard (#13)
If tool 3 warns that a basin is shallow (bed-to-crest depth under 0.5 × dam height), the mapped water body isn't the reservoir, and the inventory curve (storage ∝ depth²) is used instead. The hand-off notes (`rn_inputs`) record the reason when AREA_AC is withheld.

## How-to: force undetermined dams through tool 7b
In tool 7b, the "Dams to send" (`WHICH`) parameter has these options:
0. "High and Medium from tool 8 (hz_class), and dams with no class ..." (the default; unknown-like classes are included as no class)
1. "High only (hz_class)"
2. "Borderline (hz_border = yes) or class changed vs the existing class (hz_vs_existing)"
3. **"All dams"**: sends every dam and doesn't need tool-8 fields.
4. **"Selected features only"**: select the dams on the map first. This needs the dams to be a map layer, not a file path, and fails if nothing is selected.
5. "High, Medium, Low and unknown (all classified dams)": skips any other text, such as "Excluded".

To push dams with an "Undetermined" or "Unknown" class through, either use option 0 or 5, which treat those values as no class, or select them and use "Selected features only". If the dams layer has no `hz_class` field, options 0, 1, 2 and 5 stop with "Choose 'All dams' or 'Selected features only'."

## How-to: point the Runner at a screener run's dams
Tool 7b writes one folder per dam into `<screening work folder>/Dams`. In the Runner, set **"Dams folder"** (`DAMS_FOLDER`, "Dams folder (each dam gets a folder named after its ID)") to that same `Dams` folder. Use the folder itself, not the work folder above it and not a single dam's folder. The suite home window ("Dams folder:") lists each dam's state from `dam.json`.

## How-to: offline pack (Hazard)
On the Hazard window's data tab, the offline section has a **"Pack:"** field. Use "Browse..." to pick `nd_offline_pack.zip` or its unpacked folder, or "Clear" to remove it; the "Status:" line describes the pack. Build the pack once on a computer that's online, either with "Build offline pack..." or by running `build_nd_pack.py`. The setting belongs to the computer, not the project, and is stored in QGIS3.ini under `[ND_Dam_Hazard_Helper] offline_pack`. Never write into ND_Dam_Suite_Pack or into the pack folders.

## Spillway adequacy tool (#23, #27)
"Spillway adequacy (pass the Inflow Design Flood?)" is in the Runner's Processing tools. It needs:
- `INFLOWS`: a folder of hydrograph CSVs, such as an nd_pmp output folder or its `hydrographs/` folder. Columns are read by header name (`time_hr`, `flow_cfs`), and plain 2-column files also work.
- `HAZARD`: the hazard class.
- `TOP_FT`: the post-settlement top of dam, not counting the roadway.
- `SPILLWAY_CSV`: the spillway rating (elevation in ft, discharge in cfs).

Optional inputs:
- `CREST_LENGTH`.
- `START_FT`: the starting pool. If empty, it's the crest of the spillway rating.
- `STAGE_STORAGE_CSV`, or a power law built from `STORAGE_TOP`, `AREA_TOP` and `HEIGHT_FT`.
- `WIND_FT`.
- Advanced: `FREEBOARD_FT`, `OUTLETS` and `SPILLWAY_GATE`.
- `BISECT`: finds the largest multiple of each inflow that still passes, up to 2×.

How it works:
- Each storm is routed with **no breach**.
- A storm passes when the peak pool stays at or below the top of dam minus the residual freeboard. That freeboard is max(1 ft, wind) for high and medium hazard, 3 ft for low hazard, or the override if you set one.
- Outputs are `adequacy.csv`, `adequacy.md` (the verdict, the governing storm, and notes on the Table 6.1 IDF, §6.4 IDA and the 24-hr 500-yr storm) and `governing_pool.png`.
- The tool doesn't use the Runner's breach "failed" flag. Note that the main Runner's default peak-pool trigger assumes the dam breaches.

## Tool 7b: engine and culverts (#22, #38; Screening 0.17.22+)
- **"Runner models"** (`ENGINE`):
  - "LISFLOOD-FP only (Runner default; culverts / bridges not modelled)" (the default)
  - "LISFLOOD-FP for the extent, then ANUGA (final results from ANUGA; = the Runner's 'force ANUGA')"
- `FORCE_ANUGA`: since #38 it is hidden and folded into the engine choice. An old `FORCE_ANUGA = True` (from saved settings, models or history) selects "LISFLOOD-FP then ANUGA".
- **"Runner: near-dam refine with the FV1 solver"** (`REFINE_NEAR`, default off).
- **"Culverts and bridges for the Runner (lines, upstream end first; used by ANUGA)"** (`CULVERTS`, optional): pick the layer in the window's Inputs. Optional field parameters (`C_KIND` ... `C_NAME`) name its kind, width, height, barrels, shape and name fields. If a field is left empty, the Runner matches its own synonyms.
- In the Screening window, these are on the Settings tab as runner_tier, runner_force_anuga and runner_refine, and they're saved in the settings file.

## Culvert matching (#30, merged 10:34Z)
Labels below are from main (checked at 18ae1f5). These are advanced Runner parameters:
- **`CULVERT_INVENTORY`:** "Culvert guesses: inventory GeoPackages that size them, separated by ;; (NDDOT bridge inventory, NDDOT Center Line Pipes, NTAD NBI; NBI item 43B = 19 is a culvert)". To use Center Line Pipes, download the FileGDB and convert it first (`ogr2ogr -f GPKG clp.gpkg centerlinepipes.gdb`).
- **`CULVERT_MATCH_M`** (default **75 m**): a guessed road-crossing culvert takes the size of the nearest inventory culvert within this distance:
  - span → width, rise → height, plus barrels and shape (an arch becomes a box)
  - NDDOT wins over NBI
  - bridges never size a guess
  - a guess this close to a STRUCTURES line is dropped
- **`CULVERT_BURN_MATCHED`** (default **off**): "Culvert guesses: cut the inventory-matched ones into the DEM (unmatched guesses are never cut)". The burn uses inverts read from the LiDAR DEM (`invert_up_m`/`invert_dn_m` in the report).
- **Output:** the run report gains a `culvert_matches` table (name, source, match_m, size, barrels, shape, burned, inverts). `culvert_candidates.csv` gains `barrels` and `match_m`.

## ANUGA culvert inverts and enquiry points (Runner 3.10.71)
- **Inverts on the ground ANUGA uses.** With the fine terrain bed (`ANUGA_ELEVATION` fine, the default when the terrain is finer than the model grid), a culvert end with no invert of its own gets the **lowest fine ground within max(apron, 5 m)** of the end. Before 3.10.71 it was read from the 30 m model DEM, which blurs the road embankment into the cells at the ends (DWR-2801: a median 0.69 m, up to 2.3 m, above the ground), so the culvert passed little until the water was that deep.
- No fine ground near an end (no data there): the model DEM value is kept, with a note.
- **Inverts you give are kept** (structure layer fields or the inventory, after the LiDAR check). Only one more than **0.3 m above** that lowest ground is lowered to ground + 0.3 m, with a **WARNING** that names both values. Check the inventory elevation when you see it.
- With the model-grid bed (`ANUGA_ELEVATION` model_grid) the inverts are as before.
- **Enquiry points.** ANUGA reads the head for each culvert end at an enquiry point. It is now one triangle edge (9.9 m with the default 25 m² culvert-end triangles) past the end's inlet area, instead of at its edge, where it fell inside a triangle the culvert drains or fills. Where the triangles at an end are bigger than that (for example a road line crossing the culvert's refined area), it goes 0.5 m past the furthest of them, with a note. It stays at the edge (with a note) when the point would leave the mesh, or with an ANUGA build whose culvert operator has no `enquiry_gap`.
- **Where to see it:** `run_report.json["anuga_culvert_inverts"]` (per culvert: inverts, source per end `given` / `given_capped` / `fine_grid` / `model_dem`, the fine ground, the old model-DEM value, the search radius, the enquiry gap and whether each enquiry point is still in an inlet triangle), the summary line `anuga_culverts` and the manifest (`extra.anuga_culverts`). HEC-RAS packages built from the ANUGA run take the same inverts.

## Culvert pre-check (Runner 3.10.72)
Before a dam's run, see which culvert inverts the run will change, read-only and in seconds (no model):

    python DamSafety_Runner/scripts/ndbreach_cli.py culvert-precheck --dam-folder DAMDIR --out OUTDIR
    python DamSafety_Runner/scripts/ndbreach_cli.py culvert-precheck --structures S.gpkg \
        --fine-dem DEM.vrt [--coverage NATIVE_1M.vrt FILL.vrt] [--dem-z meters] --dam ID --out OUTDIR

- Use a Python that has GDAL, e.g. QGIS's own.
- A dam folder gives the inputs through `rundam.json` (DEM, DEM_Z, STRUCTURES, CELL), else through `structures*.gpkg` and `*composite*.vrt`.
- **HIGH**: the given invert is more than 0.3 m above the lowest fine ground within max(apron, 5 m). The run lowers it to ground + 0.3 m.
- **FALLBACK**: there is no fine-grid data near that end, so the run uses the 30 m model DEM.
- **SKIPPED**: the Runner leaves the culvert out of ANUGA.
- The given inverts are checked first against the LiDAR (`reconcile_inverts`), as in a run. Use `--no-reconcile` to skip that step.
- `on_top_source` says whether an end lies on a composite VRT's first source (native 1 m) or on its fill.
- The output is `culvert_precheck_<dam>.md`, `.csv` and `.json`. With several dams there is also `culvert_precheck_index.json`.
- The suggested invert is the lowest fine ground. It is not surveyed.

## Cross-platform (#32)
- The plugins now read and write text as UTF-8 on every OS, so files with names, °, or – move cleanly between Windows and Linux.
- CSVs no longer get blank rows on Windows.
- ANUGA conda env auto-detect now works on Windows (`<env>\python.exe`).
- Known gap: the vendored `damsuite/` copies weren't changed.

## Flagged structures (#93; Runner 3.10.23, Hazard 2.18.38, Screening 0.17.26)
Some records in the dam inventory aren't dams to breach. The plugins carry a short, version-controlled list of them in `damsuite/structure_flags.py`; see `docs/Flagged_structures.md` for the evidence. The first entry is **DWR-3161 / ND00597 (Sanish Bay WPA Dam 1)**: the ND-23 road fill over backwater from Lake Sakakawea (Sanish Bay), hazard **Low (DWR record)**, breach analysis not applicable. Lookups accept `DWR-3161`, `3161`, `ND00597` and `NID-ND00597`.
- **Screening:** tool 1 adds `struct_cls` and `flag_note`. Tools 5, 6, 7 and 12 skip the breach (`flagged`), 7b never sends the dam to the Runner, and tool 8 sets `hz_class` = Low with `hz_conf` = `flagged`. Screening has no override.
- **Runner:** single-dam, sensitivity, nested and probabilistic breach runs stop with the reason. Batch runs skip the dam with Status `flagged` and add two columns at the end of `Batch_Breach_Runs.csv`: `Structure_Flag` and `Hazard_Of_Record`.
- **Hazard:** classifying the dam stops with the reason, written to the run log.
- **Dam suite home:** shows class Low, next action "none" and the flag text. An older class (such as the 10-18 re-baseline's High) is labelled as not used.
- **To run it anyway, for information only:** tick the advanced **"Run a flagged structure anyway"** (`ALLOW_FLAGGED_STRUCTURE`), use `ndbreach_cli.py run --allow-flagged-structure`, or set the environment variable **`ALLOW_FLAGGED_STRUCTURE=1`**. The log and run report then start with "FLAGGED STRUCTURE (run allowed …)", and the hazard of record stays Low.
- Nothing writes the list. To add or change an entry, open a PR.

## HEC-RAS TIER 2 inflow (#76)
- `NDBREACH_HECRAS_INFLOW_MODE` = `auto` (default), `hydrograph` or `breach`. `auto` uses the inflow hydrograph when there's a usable inflow point (the run's, or one walked downstream), and otherwise builds a breach model at the dam. The site checks still stop a breach model that doesn't fit the site; `ALLOW_BAD_SITE=1` overrides them.
- An inflow point on the wrong side of the dam isn't used. `auto` stops with a "likely dam-point / downstream-side problem" error instead of switching to a breach model.
- The report, polygons and EAP preview come from the HEC-RAS rasters. The LISFLOOD-FP figures are kept in a "Screening pass (LISFLOOD-FP)" section.

## Run-report metrics and the water-body mask (#92, Runner 3.10.27)
Reporting only: the model, the rasters and the flood polygons don't change. Field list: [Run_report_metrics.md](Run_report_metrics.md).
- `run_report.json` summary adds flooded area at ≥0.1, ≥1 and ≥2 ft (`flooded_acres_ge_0_1ft`, `flooded_acres_ge_1ft`, `flooded_acres_ge_2ft`), `velocity_p95_fps` / `velocity_p99_fps` over wet cells, and the depth and location of the max-velocity cell (`max_velocity_depth_ft`, `max_velocity_xy`), plus velocity at flooded structures and overtopped roads and rail. New batch-CSV columns go at the end.
- **Quote P99 or receptor velocity, not the max.** At DWR-3161 the 19.4 ft/s max sat in one cell 0.15 ft deep.
- **Water-body mask, opt-in in practice.** Advanced parameter "Water-body mask for the run report only (polygons of existing lakes and ponds; …)" (`WATER_MASK`), or the `NDBREACH_WATERBODY_MASK` environment variable. If both are empty, the Runner looks for the dam folder's `waterbodies` layer, then `hydro/3DHP_waterbodies.gpkg`, any `hydro/3DHP_*waterbod*.gpkg`, then `hydro/NHD_waterbodies.gpkg` in the suite pack or suite data folder. `off` turns it off. ND_Dam_Suite_Pack has no waterbody file, so on this box point `WATER_MASK` at `/home/box/DamSafety/Data/3DHP/3DHP_ND_waterbodies.gpkg` (USGS 3DHP, NHD-sourced, 2023) or at a polygon you draw.
- The mask adds `*_masked` figures next to the unmasked ones and writes `Water Body Mask.geojson`. Near Lake Sakakawea, check the masked area first: the whole lake is one polygon.

## HEC-RAS timeouts on the wall clock (#96, Runner 3.10.26)
- VM pauses now count against `NDBREACH_HECRAS_TIMEOUT_S`. (Since 3.10.48 there is no compute limit unless it is set; see below.)
- On a timeout, look for `[ndbreach] TIMEOUT` in `linux_run/run.log`, `linux_run/timeout.json`, and `hecras_timeout` in `run_report.json`. These are written before RasUnsteady is stopped.
- Partial results are kept as `<name>.p01.partial.hdf` and are never used as results.
- `NDBREACH_HECRAS_KILL_GRACE_S` (default 60 s) sets the wait between SIGTERM and SIGKILL.

## HEC-RAS: no time limit unless you set one (#137, Runner 3.10.48)
**What changed.** Before Runner 3.10.48 the Runner stopped a HEC-RAS 2D run after a set wall-clock time even when you hadn't asked for a limit. It stopped the compute (RasUnsteady) at 3 times its own estimate, at least 2 h and at most 48 h, and each Wine preprocess step (terrain.exe, mesh.exe, drv.exe, Ras.exe -c) after 1,800 s. Long runs were killed before they finished. Now:

| `NDBREACH_HECRAS_TIMEOUT_S` | What happens |
|---|---|
| Not set, empty, `0`, or `none` / `off` / `no` / `unlimited` | **No time limit anywhere.** The compute and every preprocess step run until they finish. |
| A positive number of seconds | That limit applies to the compute **and** to each preprocess step separately. When it's reached, the Runner sends SIGTERM, waits `NDBREACH_HECRAS_KILL_GRACE_S` (default 60 s), then sends SIGKILL. Partial results are kept as `<name>.p01.partial.hdf` and are never used. |
| Anything else (text, a negative number, `inf`, `nan`) | Ignored, with a note in the log; no limit. |

The run-time estimate is still worked out and written to the log, but it no longer stops anything. The limit is measured on the wall clock, so time while the computer is paused counts.

**Caution: a Wine dialog can now wait forever.** HEC-RAS's Ras.exe shows HEC's Terms and Conditions (TCU) dialog, and Wine doesn't remember that it was accepted. The Runner never clicks it for you. With no limit set, nothing stops a run that sits on that dialog. The only guard is your own TCU hook: point `NDBREACH_HECRAS_TCU_HOOK` at a script that accepts the dialog (for example with xdotool) if you have accepted HEC's terms. For unattended or overnight batches, also set a positive `NDBREACH_HECRAS_TIMEOUT_S` large enough for the longest run. Then a stuck step is stopped instead of holding the queue.

**Known issue on current main.** With no limit set, if Ras.exe -c exits without writing its output, the error message can itself fail (a Python formatting error), and the real HEC-RAS error is hidden. If a HEC-RAS run fails right after "Ras.exe -c" with a `TypeError`, look in the plan's Compute Messages for the real cause.

**Where to see the limit a run used.**
- The Runner log: "HEC-RAS preprocess: wall-clock limit per step none [NDBREACH_HECRAS_TIMEOUT_S unset; no limit]" and "HEC-RAS compute: … wall-clock limit none […]".
- `run_report.json` → `hecras_time_limit` (`limit_s`, `limit`, `how`, `estimate_s`, and `applies_to`: "the RasUnsteady compute and each Wine preprocess step"). It's also in `_hecras/hecras_stage.json`.
- `run_report.md`: the "HEC-RAS wall-clock limit" row.

Results don't change: the limit only decides whether a run is allowed to finish. The gmsh mesh-points timeout (600 s, #127) is separate and unchanged.

## ANUGA threads (#132, Runner 3.10.47)
**What it does.** ANUGA can split its work across several processor cores (threads). Before Runner 3.10.47, leaving the threads setting at 0 meant ANUGA ran on **one** core, so long ANUGA runs (for example the refined 72 h and 20x20 Absaraka runs) were slower than they had to be. LISFLOOD-FP was not affected; it already used all cores. From 3.10.47 the Runner picks a real thread count for ANUGA:

| Threads setting | ANUGA uses |
|---|---|
| A number above 0 | That number, exactly (no upper limit; it also applies to LISFLOOD-FP) |
| 0 (the default) and the `NDBREACH_ANUGA_THREADS` environment variable is a number above 0 | `NDBREACH_ANUGA_THREADS` |
| 0 and no usable `NDBREACH_ANUGA_THREADS` | The number of CPUs minus 2, but at most 4 and at least 1. On the 8-CPU box that is 4; on a 2-, 3- or 1-CPU machine it is 1; on a 5-CPU machine it is 3. |

"CPUs" means the cores QGIS is allowed to use. The automatic choice stops at 4 on purpose, so a model run leaves room for other work on a shared computer.

**Where the setting is.**
- The **"Threads (0 = the provider setting, then automatic: …)"** parameter (`THREADS`) in the Advanced section of *Run breach model for one dam* and the other run tools. The self-test and verification tools have the same parameter.
- If that is 0, the Runner uses the computer-wide Processing setting **"Threads for the models (0 = automatic: LISFLOOD-FP uses all cores; ANUGA uses NDBREACH_ANUGA_THREADS, else CPUs - 2, at most 4)"**. It's under Settings > Options > Processing > Providers > DamSafety Runner, and is stored as `NDBREACH_THREADS`.
- On the command line (`ndbreach_cli.py`): `--threads N`, or `threads:` in the run configuration. The `--threads` help still says "0 = all", which is now true for LISFLOOD-FP only.
- The Runner window and dam wizard have no threads field of their own; they use the tool parameter and the Processing setting above.

**How to give ANUGA a different count without changing the setting.** Leave the threads setting at 0 and set `NDBREACH_ANUGA_THREADS` before you start QGIS or the command-line run. For example, use `export NDBREACH_ANUGA_THREADS=6` on Linux or macOS, or `set NDBREACH_ANUGA_THREADS=6` in the Windows command prompt that launches QGIS. This changes ANUGA only; LISFLOOD-FP still uses all cores.
- A value of 0, a negative number or text (such as `lots`) is ignored. The automatic count is used instead, and the report says the value was ignored.
- A decimal is cut down to a whole number: `2.7` gives 2.
- Unlike the checkpoint variables, there is no short form: `ANUGA_THREADS` without `NDBREACH_` is not read.
- `OMP_NUM_THREADS` no longer controls ANUGA, because the Runner always sets it for the ANUGA process. Use `NDBREACH_ANUGA_THREADS` instead.

**Where to see what a run used.**
- **The run log** (the tool's Log tab in QGIS, or `logs/runner.log` in Grok Bot's queue runs) has the line `ANUGA threads: 4 (automatic: min(8 CPUs - 2, 4)).` and, at the end, `ANUGA finished in … (N triangles, 4 thread(s))`.
- **`_breach_model/anuga/anuga.log`** has `ANUGA OpenMP threads: 4 (asked for 4)`. This is the count ANUGA itself reports.
- **`_breach_model/run_report.json`** has an `anuga_threads` entry:
  - `requested`: the threads setting the run was given (0 = automatic);
  - `used`: the count the Runner gave ANUGA;
  - `rule`: why, such as `automatic: min(8 CPUs - 2, 4)`, `NDBREACH_ANUGA_THREADS=6` or `set (2)`;
  - `anuga_reported`: the count ANUGA reported back;
  - `note`: only when something went wrong. For example, an ANUGA build without multi-thread support runs on one thread, and the run still finishes.
- **`run_report.md`** has an "ANUGA threads" row in the run table.
- A run that reused an earlier ANUGA result has no new `anuga_threads` entry.

**Tip: the thread count doesn't change the results.** On real ANUGA, 1 and 4 threads gave the same depth, velocity, arrival time and water surface, to the last digit. This was tested on 589- and 87,749-triangle domains. A run stopped at 1 thread and resumed from its checkpoint at 4 threads also matched. Only the time changes: 4 threads were about 2.6x faster on the 87,749-triangle test (62 s to 24 s), and about 2x on a 262,000-triangle benchmark. So you can compare a new run with an older single-thread run without allowing for the thread count. Any difference comes from other changes in the code or settings, not from the threads. Compare wall-clock times only between runs with the same `used` count. Windows and macOS installs were not tested; check `anuga_reported` there, or run `anuga_benchmark_omp --threads 1,4` in the ANUGA environment.

## ANUGA gmsh far-field sizing (Runner 3.10.69)
**What it does.** With `ANUGA_MESHER=gmsh` the Runner builds the ANUGA mesh with gmsh: small triangles along the roads, embankments and the flood channel, growing to large ones (20,000 m²) away from them. Three optional settings let the far field be coarser, so ANUGA has fewer triangles to compute. **With none of them set, the mesh is exactly what it was before 3.10.69** (a test pins the mesh input and every gmsh size field to main's), so earlier runs, and reruns that reuse an earlier run's mesh settings, are unchanged.

| Environment variable (or `anuga_gmsh` key) | Default (unset) | What it changes |
|---|---|---|
| `NDBREACH_ANUGA_GMSH_GRADE_DIST_M` (`grade_dist_m`) | unset = 1,500 m from lines, 2,500 m from the channel, 600 m from culvert ends | The distance over which triangles grow from the line size to the far-field size. Setting it to D uses D from lines, 2 × D from the channel and 0.6 × D from culvert ends (500 → 500 / 1,000 / 300 m). A shorter distance = fewer triangles. |
| `NDBREACH_ANUGA_GMSH_LINE_MIN_AREA_M2` (`line_min_area_m2`) | unset = 100 m² | The smallest intended triangle along roads and embankments (200 m² is the suggested coarser value). The quality gate scales with it (0.45 × the value). Culvert ends keep their own size when `culvert_area_m2` is set. |
| `NDBREACH_ANUGA_GMSH_MAX_AREA_M2` (`max_area_m2`) | 20,000 m² | The far-field triangle size (the setting existed; the variable is new). |
| `NDBREACH_ANUGA_GMSH_REFINE_FOOTPRINT` (`refine_footprint`) | off | `wet`: refine only inside the area LISFLOOD-FP flooded (plus a pad), so roads and embankments far from the water don't get small triangles. Culvert ends are always refined. `off` turns it off. |
| `NDBREACH_ANUGA_GMSH_REFINE_PAD_M` (`refine_footprint_pad_m`) | 200 m | The pad around the wet area for `wet`. |

**How to set them.** Set the variables before you start QGIS or the command-line run (for example `export NDBREACH_ANUGA_GMSH_GRADE_DIST_M=500` and `export NDBREACH_ANUGA_GMSH_LINE_MIN_AREA_M2=200`), or put the keys in a run's `anuga_gmsh` settings. A variable that is set wins over the same key. An empty variable counts as unset.

**Bad values.** A value that isn't a positive number (such as `500m`, `0` or `-5`), a footprint other than `wet` / `off`, or a distance so short that the channel's distance is no longer above its 250 m inner distance (`grade_dist_m` of 125 m or less) stops the gmsh mesh with a message naming the setting and the variable, before gmsh runs. These settings are only read by the gmsh mesher; the default ANUGA mesher ignores them.

**Where to see what a run used.** The run log has a `gmsh sizing: lines 200 m2, far field 20000 m2, grading 500 / 1000 / 300 m (lines / channel / points)` line (and the wet-footprint size when it is on). The ANUGA mesh note adds `Sizing: …`. `run_report.json` → `anuga_mesh` → `sizing` (and `gmsh.sizing`) holds the values used, the footprint's area and how many km of lines were left outside it. The settings themselves are under `anuga_gmsh`. None of this appears when no option is set.

**Estimates, not yet checked for accuracy.** On the benchmark meshes, grading 500 m alone was estimated at about 1.45× faster; lines 200 m² alone at 31 % fewer triangles; and a 1,000 m wet-area buffer + lines 200 m² + grading 500 m at about 2.3× faster (56 % fewer triangles). The flood results still need to be compared with the default mesh (DWR-2851) before these are used for production runs.

## ANUGA time-step diagnostic (Runner 3.10.70)
**What it does.** ANUGA takes many small internal steps between the saved yield steps; the size of each step (dt) is set by the triangle where water crosses fastest relative to the triangle's size. When an ANUGA run is slow, the diagnostic tells you why: how many internal steps each yield step took, how big they were, which triangle set the step and whether a culvert or the breach inlet is next to it. **It is off by default, and it never changes the results**: it only reads ANUGA's numbers (tested bit for bit on real ANUGA, on and off).

**Turning it on (the next run; it can't be attached to an ANUGA run that is already going).**

| Setting | Default | Meaning |
|---|---|---|
| `NDBREACH_ANUGA_DT_DIAG` | unset (off) | `1` turns it on, `0` turns it off (wins over the run setting). |
| `NDBREACH_ANUGA_DT_DIAG_EVERY` | 1 | A row every N yield steps (only when it is on). |
| run setting `anuga_dt_diag` | 0 | The same in a run's settings: 0 = off, N = a row every N yield steps, `on` = 1. |

For example `export NDBREACH_ANUGA_DT_DIAG=1` before starting QGIS, the command-line run or the queue. The run's settings key (the result folder name) doesn't change, and a run stopped and resumed from its checkpoint keeps its file: rows after the checkpoint's time are dropped and the resumed run appends.

**What it writes.** `_breach_model/anuga/timestep_diag.csv`, one row per sample:
- `utc`, `t_s` (model time), `model_s` and `wall_s` since the last row, `model_s_per_wall_s`;
- `steps` (internal steps since the last row) and `steps_total` (since this ANUGA process started);
- `dt_mean_s`, `dt_min_s`, `dt_max_s` (over those steps), `dt_last_s` (the last step, cut to land on the yield time) and `dt_cfl_last_s` (the last step's own CFL step);
- the triangle that limited the last step: `tri`, `x`, `y` (model CRS), `lon`, `lat`, `area_m2`, `radius_m`, `depth_m`, `speed_mps`, `wave_speed_mps`, `dt_tri_s`;
- `near_op`, `near_op_type`, `near_op_dist_m`: the breach inlet or culvert nearest the triangle, when it is within max(3 × the triangle's inradius, 50 m).

At the end, `timestep_diag_summary.txt`, the log and `run_report.json` → `anuga_dt_diag` list the triangles that limited the step most often, with their share of the rows, the internal steps and the wall time.

**The wall clock on every progress line (always on).** Each ANUGA progress line now ends with `utc=2026-10-24T09:54:53Z steps=358 dsteps=107 wall_s=0.1 rate=2.89e+03`: the time, the internal steps so far and since the last yield step, the wall seconds since the last yield step and the model seconds per wall second. A long `wall_s` with a normal `dsteps` means the computer paused (a host freeze); a large `dsteps` means ANUGA really took many small steps. The start of the line is unchanged, so the progress bar reads it as before.

## Is a slow ANUGA run frozen or really slow? (Runner 3.10.73)
`core/anuga_slowcheck.py` looks at a running (or stopped) ANUGA run **without touching it**: it only reads the log, the checkpoints and `/proc`, and needs nothing but python3.

    python3 DamSafety_Runner/core/anuga_slowcheck.py <run folder> [--pid N] [--state /tmp/<dam>.slowcheck.json] [--json]

The run folder can be the dam's run folder, its `_breach_model/anuga` folder or `anuga.log`. Without `--pid` it finds the `anuga_worker.py` working in that folder. `--window N` (yield steps, default 48) or `--window 30m` sets how much of the log is the baseline; `--sample-s` (default 1 s) measures the worker's CPU right now. `--state FILE` keeps a small snapshot between calls (keep it outside the run folder); a watch routine should pass it every time.

It prints one line, for example `VERDICT=host_freeze confidence=high last_t_s=203100 last_line_age_s=11 freeze_total_s=38070 state_freeze_s=1000 cores_now=3.55 dsteps_trend=1 ...`, and exits with:

| Exit | Verdict | Meaning |
|---|---|---|
| 0 | `normal` | Advancing as expected (`freeze_total_s` still says how long the box was frozen since the worker started). |
| 1 | `finished` | `PROGRESS 100 done`, or `anuga_result.npz` written after this launch. |
| 2 | – | Bad arguments. |
| 3 | `insufficient_data` | No `anuga.log`, or fewer than 2 yield-step lines. |
| 4 | `host_freeze` | The computer was paused: wall time passed without uptime (since the last `--state` call, or a yield with a long `wall_s` and normal `dsteps`, or a stall the freezes since the start can explain, confidence low). The model is not slower. |
| 5 | `timestep_collapse` | ANUGA really works harder: recent `dsteps` >= 2x the window median, or CPU seconds per model second since the last call >= 2x the run's, or (confidence low/medium) a stall longer than any freeze since the start could explain while the worker computes. Turn on the time-step diagnostic to see the triangle (`lim_tri=`, `lim_near_op=`). |
| 6 | `stalled_unknown` | No new line for 3x the expected yield time (at least 120 s) and the worker is not using CPU. |
| 7 | `not_running` | No live worker for the run and it did not finish. |

**How it tells them apart.** A host freeze stops the box's uptime clock and the worker's CPU time but not the wall clock. Wall seconds since the worker started minus its uptime age is the time the box was frozen (`freeze_total_s`); between two calls, wall gap minus uptime gap (`state_freeze_s`). CPU seconds per model second does not change with a freeze, so a rise in it (`cost_trend`) is a real slowdown. Logs from before 3.10.70 (no `dsteps`) are judged from the checkpoint times, the log's time, uptime and CPU, with confidence medium or low.

## HEC-RAS: stack limit, OpenMP thread stacks, structure lines on cell faces, connection names (Runner 3.10.75)
- **Stack limit.** On Linux the Runner starts RasUnsteady with the stack limit raised (unlimited, or the hard limit if your system does not allow unlimited); a large 2D area (DWR-2801, 421k cells) crashed under the default 8 MB with exit 174. The compute log line `HEC-RAS compute: stack limit for RasUnsteady ...` says what was set. If it says the hard limit and RasUnsteady still exits 174, raise the hard limit (`ulimit -Hs`, `/etc/security/limits.conf`) for the account that runs it.
- **OpenMP thread stacks.** RasUnsteady's Intel OpenMP worker threads have their own stacks (4 MB by default), which `ulimit -s` does not change; DWR-2801 crashed there (exit 174 at PROGRESS 0.50) with the main stack unlimited. The Runner sets `KMP_STACKSIZE=1g` and `OMP_STACKSIZE=1g` for RasUnsteady (Linux, under Wine and on Windows). A value you set in the environment yourself is kept; `NDBREACH_HECRAS_OMP_STACKSIZE` (e.g. `2g`) changes the default, `off` sets neither. The compute log line `HEC-RAS compute: OpenMP thread stacks for RasUnsteady ...` and `run_report.json` (`hecras_time_limit.omp_stacksize`) show what was used. Each OpenMP thread can reserve that much address space (it is reserved, not used, until needed).
- **Structure lines on cell faces.** Each culvert / bridge / weir connection line now gets cell faces along all of its length, also when it is shorter than the cell; the cells near short lines are a little smaller (on DWR-2801 well under 1 % more cells). The build checks how much of each line lies on the faces HEC-RAS will make: under 90 % is a warning in the log and `STRUCTURES_README.md` ("Connection lines on cell faces"); a line with none (under 1 %) stops the build with the list of lines, since HEC-RAS stops on it later ("The SA 2D Connection at ... has an error with the connection"). Set the HEC-RAS option `structure_align_check` to `warn` to write the package anyway. Two connection lines close together can still flag each other; move or drop one.
- **Connection names.** HEC-RAS refuses some characters in a connection name (`/` in `OSM way/254643464`, `,`, `=` ...) and keeps 16 characters. They become `_`, names are cut to 16 characters and made unique (`OSM way_254643 2`), and `STRUCTURES_README.md` lists each renamed connection with its original name ("Renamed for HEC-RAS").
- A HEC-RAS package built before 3.10.75 keeps its old geometry: rebuild it to get the new faces and names.

## Hazard zone colours match the ND Dam Safety Standards (#126, Hazard 2.18.46, Runner 3.10.51, Screening 0.17.34)
**What changed.** Every ACER TM 11 danger-zone graphic now uses the colours of the ND Dam Safety Standards, Policy REG_05.2026 (effective 04/01/2026), Figure 5.1 (houses, p. 21) and Figure 5.2 (mobile homes, p. 22): [the policy document](https://www.dwr.nd.gov/reclink/4dcgi/GetPolicyDocument/16). The standard names no colours, so they were sampled from the published figures.

| Zone (legend label) | Colour now | Map styles before #126 | Review charts before #126 |
|---|---|---|---|
| HIGH DANGER ZONE | #EF9897 (light red) | #F44336 | #f4b6b0 |
| JUDGMENT ZONE | #F9F4A3 (pale yellow) | #FFEB3B (legend said "JUDGEMENT ZONE") | peach #fbe7b5 |
| LOW DANGER ZONE | #C4E0AD (light green) | #4CAF50 | nearly white #e8f3e8 |

The old map styles were drawn at 75 % opacity, and the Hazard chart sign-off overlay left the low zone white (unfilled); it's now filled.

- **Where you see them:** the zone rasters (`Hazard_Foundations.tif`, `Hazard_MobileHomes.tif`, `Hazard_Vehicles.tif`, style `acer_zones.qml`), the zone points (`acer_zone_points.qml`), the Hazard chart sign-off overlay, and the Runner / Screening review charts and their legends. The QML styles are the same in Hazard, Runner and Screening.
- **What didn't change:** the depth-velocity breakpoints, the classes and the stored field values (`HIGH` / `JUDGMENT` / `LOW`). The dam hazard-class colours (High / Medium / Low) aren't ACER zones and are unchanged.
- `tools/ci_checks.py` ("zone colours (REG_05.2026)") checks every copy of the colours, so a style that drifts from the standard fails the checks.

**Tip: let the basemap show through the zone layers.** The zone styles are now fully opaque (opacity 100 %; it was 75 %, which lightened the colours away from the figures). Over aerial imagery that hides the buildings and roads under a zone. To see them:
- **Zone rasters:** *Layer Properties > Transparency > Global opacity*, or the opacity slider in the *Layer Styling* panel. About 60-70 % keeps the zones readable and shows the imagery.
- **Zone points and other vector layers:** *Layer Properties > Symbology > Layer Rendering > Opacity*.
- This only changes how the layer is drawn in your project, not the files or the class.
- Lower opacity lightens the colours, so they no longer match Figure 5.1 / 5.2 exactly. For a figure that a reviewer will compare with the standard, set the zone layer back to 100 % (or right-click the layer, *Styles > Default*, to reload the plugin style).

## Not verified in this guide
- These descriptions come from PR text and source code. None of the window steps were checked in a running QGIS.
- The exact list of fixes in #27 and the details of #11 and #16 are summarized from PR titles and commit headlines only.
- No window steps for #22, #30, #32, #76, #84, #92, #93 or #96 were tried in QGIS. The #93 labels were checked in main's source at d217ec2, and the #92 `WATER_MASK` label and lookup order and the #96 names at b0c2ad5.
- The #131 (arrival depth) and #137 (HEC-RAS time limit) names, labels, defaults and rules were checked in main's source at 732f597. No window was opened in QGIS, and no model was run.
- The #132 (ANUGA threads) names, labels and rules were checked in main's source at caaf850 and against the PR's real-ANUGA check (`/workspace/anuga_threads_pr/real_check.log`). The Processing Options path was not opened in QGIS.
- The #126 zone colours, labels and opacity were checked in main's source at 034e9d7 (`DamSafety_Hazard/helpers.py`, `styles/acer_zones.qml`, `styles/acer_zone_points.qml`). The QGIS menu paths in the transparency tip are the standard QGIS 3.34 ones; they weren't opened in a running QGIS for this guide.
- The installed copy of the plugins may lag behind main.
