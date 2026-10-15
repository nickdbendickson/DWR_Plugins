# DamSafety plugins: user guide (changes through 2026-10-15)

This guide covers the behaviour added in PRs #9-#21, #23 and #27. The labels in quotes are the exact text in the code. For the full list of changes, see CHANGELOG.md.

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

## Tool 7b: engine and culverts (#22)
- **"Runner models"** (`ENGINE`):
  - "Runner default (LISFLOOD-FP only)" (the default)
  - "LISFLOOD-FP for the extent, then ANUGA (final results from ANUGA)"
- **"Runner: force ANUGA (final results from ANUGA even with LISFLOOD-FP only)"** (`FORCE_ANUGA`, default off).
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

## Not verified in this guide
- These descriptions come from PR text and source code. None of the window steps were checked in a running QGIS.
- The exact list of fixes in #27 and the details of #11 and #16 are summarized from PR titles and commit headlines only.
- No window steps for #22, #30 or #32 were tried in QGIS.
- The installed copy of the plugins may lag behind main.
