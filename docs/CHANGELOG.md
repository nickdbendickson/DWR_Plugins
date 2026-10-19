# Changelog

## 2026-10-19
- **HEC-RAS timeouts on the wall clock (Runner 3.10.26):** the RasUnsteady compute timeout (and the Wine preprocess steps, the Ras.exe -c loop and the ANUGA gmsh mesher) used `time.monotonic()` through Python's subprocess timeouts. That clock stops while the VM is paused or forked, so a 5 h timeout fired after 9.7 h of wall time and the SIGKILL lost RasUnsteady's end-of-run series. `core/wallclock.py` measures max(wall, monotonic) elapsed, logs clock jumps, and stops a timed-out process group with SIGTERM, `NDBREACH_HECRAS_KILL_GRACE_S` (default 60 s), then SIGKILL. The timeout is logged to `linux_run/run.log` and the Runner log before any cleanup, and recorded in `run_report.json` as `hecras_timeout`; the partial results HDF is kept as `<name>.p01.partial.hdf`. Runner 3.10.22 is claimed by the open #90 and #92 and 3.10.24 was this PR's first number; merged as 3.10.26 after #101 took 3.10.25.
- **Screening 0.17.27: tool 3 honours the storage choice (Nick's decision, #101).** New `STORAGE_CHOICE` enum on tool 3 (Max storage default / Normal storage / Larger of the two). Max raises the top-of-dam storage to the inventory maximum, Normal keeps the DEM above the normal pool, Larger uses the larger inventory value; a missing or zero value falls back to the other with a warning, neither keeps the DEM estimate. `stor_notes` and the log start with the storage used (no new field, tools 16 / 19 write by position). The window sends the choice to tool 3 only (not tool 1); the chain and tool 17 use the same defaults. 7b and the Runner hand-off do not run tool 3; they read its curve (`cal_total_m3`), which follows the choice.
- **Tests that failed on main d217ec2 (Runner 3.10.25):** the gmsh mesher checks the outer polygon before importing gmsh, so a bad mesh spec is reported as such where gmsh is missing (the only plugin-code change). Test fixes, no product change: the default ANUGA job-file pin is checked with the built-in outline tracer (contourpy traces up to one cell differently); the Word report expects its figures embedded exactly when cairosvg renders; the Runner GUI tests resolve the checkout through a symlink; the real-Qt window test skips in a Python without PyQt5; Screening's qgis_mock gained `pushWarning` (QGIS >= 3.16) and `qgis.utils.loadPlugin` / `startPlugin`; the nd_hazard adapter and popup tests follow the one-signer chart rule, the single Dam safety menu entry, the DamSafety labels and the per-computer Dams folder. Runner 3.10.22 / 3.10.24 are taken by the open #90, #92 and #96.
- **Flagged structures (Runner 3.10.23, Hazard 2.18.38, Screening 0.17.26, damsuite 1.17.2):** a repo-shipped list of records that are not a dam to breach (`damsuite/structure_flags.py`, see [Flagged_structures.md](Flagged_structures.md)). The first entry is DWR-3161 / ND00597 (Sanish Bay WPA Dam 1): the ND-23 road fill over backwater, breach analysis not applicable, hazard Low from the DWR record (the 3.10.16 rebaseline's HIGH was a model artifact). Screening lists it with that note and the Low class instead of a breach hazard (new fields `struct_cls`, `flag_note`). The Runner refuses a single-dam breach run of it unless `ALLOW_FLAGGED_STRUCTURE`, and the batch tools skip it (Status `flagged`, new columns `Structure_Flag` and `Hazard_Of_Record`). Hazard refuses to classify it unless allowed. The Dam suite home shows the flag and Low. Runner 3.10.22 is taken by the open #90 and #92, so this is 3.10.23.

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
