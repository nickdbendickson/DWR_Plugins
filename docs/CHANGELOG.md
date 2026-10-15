# Changelog

## 2026-10-15 (later)
- **#22 (merged 06:12Z), Screening 7b:** new options are passed to `ndbreach:rundam`:
  - `ENGINE` ("Runner models") → TIER
  - `FORCE_ANUGA` → FORCE_ANUGA
  - `REFINE_NEAR` → REFINE_NEAR
  - `CULVERTS` (line layer) → STRUCTURES
  - `C_KIND`/`C_WIDTH`/`C_HEIGHT`/`C_BARRELS`/`C_SHAPE`/`C_NAME` → `S_*`

  Only values that differ from the Runner defaults are sent, so a default 7b run is unchanged.
- **#32 (merged 10:26Z), cross-platform fixes:**
  - Text files are now read and written as UTF-8.
  - CSVs are written with `newline=""`.
  - On Windows, the conda env python is found at `<env>\python.exe`, which fixes ANUGA env auto-detect.
  - Subprocess output is decoded with `errors="replace"`.
  - No behaviour change on Linux/macOS. The vendored `damsuite/` copies are not yet changed.
- **#30 (merged 10:34Z), Runner:** culvert guesses can be sized from the NDDOT/NBI inventory (CULVERT_INVENTORY, CULVERT_MATCH_M 75 m, CULVERT_BURN_MATCHED). See USER_GUIDE.md.

Merged pull requests, 2026-10-14 to 2026-10-15 (main at e2acb76). Merge times are UTC.

## Runner (DamSafety_Runner)
- **#9 (10-14):** Brought back `NDBREACH_DEM_RESAMPLE` with this precedence: tool parameter, then environment variable, then default. Added the `DEM_RESAMPLE` setting. The method used is recorded in `run_manifest.json` (`extra.dem_resample`).
- **#10 (10-14):** **The DEM resample default changed from bilinear to average** (`core/gdal_io.DEFAULT_RESAMPLE`). The precedence stays the same. Bilinear gave flooded areas 3-40% too high compared with the baselines. To keep the old behaviour, set `NDBREACH_DEM_RESAMPLE=bilinear` or pick bilinear in the setting.
- **#11 (10-14):** Security, integrity and performance fixes from the 2026-10-14 review.
- **#14 (10-15):** **The inflow point is now the lowest qualifying cell** (at least half the dam height down) within the usual radius, instead of the nearest one (`core/terrain.find_inflow_point`). The 400 m fallback still uses the nearest cell. This changes results for dams where the nearest and lowest cells differ; see RESULTS_LOG.md. The same PR also regenerated the stale `damsuite/FILES.json`, isolated the tests from `NDSUITE_SETTINGS`, and fixed the test fakes.
- **#20 (10-15):** **Structures crash fix.** `ndbreach:rundam` no longer raises "truth value of an array ... is ambiguous" when it's given a STRUCTURES (culvert/bridge) layer. The same `or []` pattern was also fixed in `culvert_guess` and in the HEC-RAS structure writers.
- **#21 (10-15):** **Per-run QA maps.** Each dam folder now gets `qa_inflow_map.png` and `qa_depth_map.png`. The new `QA_MAPS` setting has the options Off / Inflow map only / Inflow map and depth map, and the default is both maps. A QA-map error never fails a run. Details are recorded under `qa_maps` in `run_manifest.json`.
- **#23 (10-15) + #27 (10-15):** **New tool: Spillway adequacy** (`spillwayadequacy`), also added to nd_breach_runner. It runs a no-breach routing of each inflow hydrograph against REG_05.2026 residual freeboard. #27 carries the review fixes (start pool, inflow units, ...).

## Screening (DamSafety_Screening)
- **#13 (10-15):** **Shallow-basin storage guard** (ported from nd_ 0.28.3). In tool 3, a mapped water body whose depth from bed to crest is under 0.5 × the dam height gets the full-height inventory curve instead, with a warning. In the hand-off, no AREA_AC is sent when the curve spans under 0.5 × the height, so the Runner uses m = 2.
- **#14:** Tool 7b now sends the package's `structure_points` instead of the structure polygons when no structures layer is named.
- **#15 (10-15):** **Tool 7b treats unknown-like classes as no class.** These values count as no class (any letter case): "", NoData, No data, NULL, None, nan, Unknown, Undetermined, Not determined, Undet, TBD, To be determined, N/A, NA, ?, Unclassified, Not classified, Pending. High, Medium and Low are matched in any letter case. A new mode, "High, Medium, Low and unknown (all classified dams)", was added as option 5; the existing mode numbers 0-4 are unchanged.
- **#19 (10-15):** Fixed test failures on QGIS 3.34 (numpy trapz, test_runner import).

## Shared suite (damsuite, all plugins)
- **#12 (10-15):** **Safe settings saves.** Saving now re-reads the file and merges in only this window's changes, refuses to overwrite a file it couldn't read (unless "Save as" replaces it), skips the write when nothing changed, writes atomically, keeps a `.bak` copy, and uses a `.lock` file (a stale lock is taken over after 30 s; a save gives up after waiting 10 s).
- **#16 (10-15):** First steps to simplify the plugin windows ("your values first", one home, Data plugin fixes).
- **#17 (10-15):** `os.scandir` iterators are now closed (no ResourceWarning).
- **#18 (10-15):** **Standard work-folder layout** (`damsuite/layout.py`). See USER_GUIDE.md.
