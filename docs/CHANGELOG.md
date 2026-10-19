# Changelog

## 2026-10-19
- **Flagged structures (Runner 3.10.23, Hazard 2.18.38, Screening 0.17.26, damsuite 1.17.2):** a repo-shipped list of records that are not a dam to breach (`damsuite/structure_flags.py`, see [Flagged_structures.md](Flagged_structures.md)). The first entry is DWR-3161 / ND00597 (Sanish Bay WPA Dam 1): the ND-23 road fill over backwater, breach analysis not applicable, hazard Low from the DWR record (the 3.10.16 rebaseline's HIGH was a model artifact). Screening lists it with that note and the Low class instead of a breach hazard (new fields `struct_cls`, `flag_note`). The Runner refuses a single-dam breach run of it unless `ALLOW_FLAGGED_STRUCTURE`, and the batch tools skip it (Status `flagged`, new columns `Structure_Flag` and `Hazard_Of_Record`). Hazard refuses to classify it unless allowed. The Dam suite home shows the flag and Low. Runner 3.10.22 is taken by the open #90 and #92, so this is 3.10.23.

## Pending review (open, not merged; checked 2026-10-19 06:40Z)
- **#92, Runner 3.10.22 (closes #91): run-report metrics and an optional water-body mask.** Report only: the model, the rasters and the flood polygons don't change, and existing fields and CSV columns keep their names and values.
  - New `run_report.json` summary fields: flooded area at ≥0.1, ≥1 and ≥2 ft (`flooded_acres_ge_0_1ft`, `_ge_1ft`, `_ge_2ft`), P95/P99 velocity over cells ≥0.1 ft deep, the depth and x/y of the max-velocity cell, and velocity at flooded structures, roads and rail. They also go into the md/HTML/DOCX reports, the batch CSV (new columns at the end), the sensitivity and probabilistic tables, and the `qa_depth_map.png` title.
  - **Water-body mask, opt-in.** Set the advanced `WATER_MASK` parameter (or `NDBREACH_WATERBODY_MASK`), or let it find the dam folder's `waterbodies` layer or `hydro/3DHP_waterbodies.gpkg` / `hydro/NHD_waterbodies.gpkg` in the suite pack or suite data folder. `off` turns it off. It adds `*_masked` figures next to the unmasked ones and writes `Water Body Mask.geojson`.
  - Data: the ND 3DHP waterbodies are on the box at `/home/box/DamSafety/Data/3DHP/3DHP_ND_waterbodies.gpkg` (188,320 polygons, all NHD-sourced, 2023 feature dates; `hydro/3DHP_waterbodies.gpkg` there is a link for the suite-data-folder lookup). ND_Dam_Suite_Pack doesn't have the file. At DWR-3161, all of Lake Sakakawea is one polygon about 140 m from the dam, so a mask would remove a large area. Keep it opt-in.
  - Version: 3.10.22 is also claimed by open #90, and #93 took 3.10.23, so #92 will need a re-bump before merging.
- **#96, Runner 3.10.24: HEC-RAS timeouts on the wall clock.** A HEC-RAS run started 10-18 19:04Z was killed at 10-19 04:43Z, after 34,749 s of wall time against a 5 h (18,099 s) timeout, because Python's timeouts use a clock that stops while the VM is paused.
  - New `core/wallclock.py`: the deadline counts wall time (never less than monotonic time) and logs clock jumps over 60 s.
  - On a timeout, the Runner writes `[ndbreach] TIMEOUT` to `linux_run/run.log` and an ERROR to its log **before** stopping RasUnsteady. It sends SIGTERM, waits `NDBREACH_HECRAS_KILL_GRACE_S` (default 60 s), then SIGKILL.
  - The half-written results are kept as `<name>.p01.partial.hdf` (never used as results) and recorded in `linux_run/timeout.json`, `_hecras/hecras_stage.json` (`status: timeout`) and `run_report.json` (`hecras_timeout`).
  - The Wine preprocess steps, `ras_compile` and the gmsh mesher use the same clock. `NDBREACH_HECRAS_TIMEOUT_S` and the timeout formula are unchanged.
  - Tradeoff: VM pause time now counts against the timeout.

## 2026-10-18
- **#76 (merged 12:09Z), Runner, HEC-RAS TIER 2:** the report, polygons and EAP preview are rebuilt from the HEC-RAS rasters (the LISFLOOD-FP figures are kept under `screening_pass_lisflood`). A rejected inflow point is walked downstream. New `NDBREACH_HECRAS_INFLOW_MODE` = `auto` (default) / `hydrograph` / `breach`; `auto` falls back to a breach model when there's no usable inflow point. An inflow point on the wrong side of the dam isn't used: `auto` stops with a "likely dam-point / downstream-side problem" error. The compute timeout scales with the model (max(2 h, 3 × estimate), 48 h cap; `NDBREACH_HECRAS_TIMEOUT_S` overrides).
- **#84 (merged 12:11Z), Runner 3.10.18: breach inflow on the downstream side.** The crest fallback and the 8 ft fallback keep every inflow pick on the downstream side. New `run_report.json` `inflow_point.side_check` gives a "CHECK THE DAM POINT" warning, and a road within 30 m of the dam point gives a road-fill warning (`dam_point_checks`) unless `DAM_LINES` was given. At DWR-3161, the inflow moved 242 m north to ground at 1,839.45 ft. See RESULTS_LOG.md.

## New plugin
- **#60 (merged 10-16), DamSafety_PMP 0.3.0 (internal, experimental):** the nd_pmp prototype is added as `DamSafety_PMP/`. Provider id `ndpmp` and settings keys `nd_pmp/*` are unchanged. The AWA data isn't included. Machine-specific paths were removed. Light tests are in `tests/run_tests.py`, and data-dependent tests skip when their inputs are missing. The OGR memory driver now falls back to "Memory" on GDAL < 3.11. Licensing: for SWC's exclusive use; get permission from SWC and AWA before redistributing.

## Repository
- **#45 (merged 10-15 14:16Z):** Removed the old nd_ plugins (`nd_claude_1.23/`, `nd_installed/`). They are archived on branch `archive/nd-plugins` and tag `archive-nd-plugins-20261015` (18ae1f5). The top-level `nd_breach_runner/` and `nd_dam_screening/` test aliases are kept.

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
- **#38 (merged 14:15Z), Screening 7b follow-ups (Screening 0.17.22):** `ENGINE` is the one Runner engine choice. `FORCE_ANUGA` is folded in and hidden; an old `FORCE_ANUGA = True` still selects "LISFLOOD-FP then ANUGA". The engine is shown on the Run tab and in `rn_engine`, there's a warning when culverts are ignored, and an early check fails if ANUGA is missing.
- **#30 (merged 10:34Z), Runner:** culvert guesses can be sized from the NDDOT/NBI inventory (CULVERT_INVENTORY, CULVERT_MATCH_M 75 m, CULVERT_BURN_MATCHED). See USER_GUIDE.md.

Merged pull requests, 2026-10-14 to 2026-10-15 (main at e2acb76). Merge times are UTC.

## Runner (DamSafety_Runner)
- **#9 (10-14):** Brought back `NDBREACH_DEM_RESAMPLE` with this precedence: tool parameter, then environment variable, then default. Added the `DEM_RESAMPLE` setting. The method used is recorded in `run_manifest.json` (`extra.dem_resample`).
- **#10 (10-14):** **The DEM resample default changed from bilinear to average** (`core/gdal_io.DEFAULT_RESAMPLE`). The precedence stays the same. Bilinear gave flooded areas 3-40% too high compared with the baselines. To keep the old behaviour, set `NDBREACH_DEM_RESAMPLE=bilinear` or pick bilinear in the setting.
- **#11 (10-14):** Security, integrity and performance fixes from the 2026-10-14 review.
- **#14 (10-15):** **The inflow point is now the lowest qualifying cell** (at least half the dam height down) within the usual radius, instead of the nearest one (`core/terrain.find_inflow_point`). The 400 m fallback still uses the nearest cell. This changes results for dams where the nearest and lowest cells differ; see RESULTS_LOG.md. The same PR also regenerated the stale `damsuite/FILES.json`, isolated the tests from `NDSUITE_SETTINGS`, and fixed the test fakes.
- **#20 (10-15):** **Structures crash fix.** `ndbreach:rundam` no longer raises "truth value of an array ... is ambiguous" when it's given a STRUCTURES (culvert/bridge) layer. The same `or []` pattern was also fixed in `culvert_guess` and in the HEC-RAS structure writers.
- **#21 (10-15):** **Per-run QA maps.** Each dam folder now gets `qa_inflow_map.png` and `qa_depth_map.png`. The new `QA_MAPS` setting has the options Off / Inflow map only / Inflow map and depth map, and the default is both maps. A QA-map error never fails a run. Details are recorded under `qa_maps` in `run_manifest.json`.
- **#23 (10-15) + #27 (10-15):** **New tool: Spillway adequacy** (`spillwayadequacy`), also added to nd_breach_runner (the old nd_ copy, now archived; see Repository above). It runs a no-breach routing of each inflow hydrograph against REG_05.2026 residual freeboard. #27 carries the review fixes (start pool, inflow units, ...).

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
