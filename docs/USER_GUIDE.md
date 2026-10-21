# DamSafety plugins: user guide (changes through 2026-10-20, main b0c2ad5)

This guide covers the behaviour added in PRs #9-#21, #23 and #27, plus #22, #30, #32, #38, #76, #84, #92, #93 and #96. Other merged PRs (#85-#90, #94, #100-#105, …) are listed in CHANGELOG.md only. The labels in quotes are the exact text in the code. For the full list of changes, see CHANGELOG.md.

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

## HEC-RAS compute: no wall-clock limit by default (Runner 3.10.48)
- With `NDBREACH_HECRAS_TIMEOUT_S` unset, empty or `0`, RasUnsteady and each preprocess step run until they finish. Before 3.10.48 it was stopped at max(2 h, 3 × the estimate), capped at 48 h, even with the variable unset.
- A positive value still stops the compute (and each preprocess step) after that many wall-clock seconds, with SIGTERM, then SIGKILL after `NDBREACH_HECRAS_KILL_GRACE_S`. An invalid value is ignored, with a note in the log.
- With no value there is no limit anywhere: the Wine preprocess steps lost their 1,800 s cap too. A Wine dialog waiting for a click (the TCU prompt) then waits until someone deals with it; use `NDBREACH_HECRAS_TCU_HOOK` or a positive value for unattended batches.
- The limit in force ("none" or N s) is in the Runner log, `run_report.json` (`hecras_time_limit`) and the run report's "HEC-RAS wall-clock limit" row.

## Not verified in this guide
- These descriptions come from PR text and source code. None of the window steps were checked in a running QGIS.
- The exact list of fixes in #27 and the details of #11 and #16 are summarized from PR titles and commit headlines only.
- No window steps for #22, #30, #32, #76, #84, #92, #93 or #96 were tried in QGIS. The #93 labels were checked in main's source at d217ec2, and the #92 `WATER_MASK` label and lookup order and the #96 names at b0c2ad5.
- The installed copy of the plugins may lag behind main.
