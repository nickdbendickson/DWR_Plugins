# Audit of the DWR plugins, compared to the nd_hazard plugins

**Date:** 2026-10-15
**Scope:** `nickdbendickson/DWR_Plugins` (the `DamSafety_*` plugins) vs `nickdbendickson/nd_hazard` (the `nd_*` plugins), both at their `claude/funny-goldberg-dg1x2s` branch (even with `main`).
**Method:** a 74-agent fan-out that mapped every corresponding plugin pair, traced the shared `damsuite/` package, checked whether the documented "release 1.22" fixes landed, and hunted correctness bugs. **Every candidate bug was then handed to an independent skeptic whose job was to refute it by reading the same code** (refute-by-default). 6 candidates were refuted and are excluded; 54 survived (53 confirmed, 1 plausible). Findings are verified by reading call paths end-to-end; the plugins were **not** run against real QGIS/GDAL/LISFLOOD, so a few runtime-dependent items are confirmed by code reading plus the sibling repo's own documented real-world failures (noted where relevant).

---

## 1. Executive summary

The two repos are the **same North Dakota dam-safety QGIS plugin suite**. `nd_hazard` is the forward release line, still `nd_*`-branded; `DWR_Plugins` is the active-development monorepo where the plugins were **rebranded `nd_* → DamSafety_*` and had their version numbers reset downward**. They share a common ancestor and a vendored `damsuite/` package, and have since **diverged in both directions**.

| Role | DWR (`DamSafety_*`) | nd_hazard (`nd_*`) |
|---|---|---|
| Hazard classifier | `DamSafety_Hazard` **2.18.23** | `ND_Dam_Hazard_Helper` **2.32.0** |
| Breach runner | `DamSafety_Runner` **3.10.14** | `nd_breach_runner` **3.22.0** |
| Screening | `DamSafety_Screening` **0.17.22** | `nd_dam_screening` **0.30.0** |
| Data updater | `DamSafety_Data` **1.3.6** | `nd_data_updater` **1.6.0** |
| Suite setup | `DamSafety_Setup` **1.0.7** | *(no counterpart)* |

**The version numbers invert the maturity.** DWR's lower numbers are a reset, not a newer codebase: for every pair, nd_hazard carries more modules and several bug-fixes DWR never received. The hazard-classification **math** is still shared and byte-identical in the critical modules (`damsuite/tm11_curves.py`, `units.py`, `loss.py`, `core/nd_hazard.py`, the ACER TM-11 charts), so **a typical dam usually gets the same hazard class from both** — but DWR can diverge or fail on specific, often-real inputs, and is missing required deliverables and several safety transparency features.

**DWR is genuinely ahead** on: `average` DEM resampling (matches the regression baselines; nd_hazard regressed to `bilinear`, which it says ran floods 3–40 % high), UTF-8/Windows file-encoding hardening, AADT road-traffic joins, automatic 3DEP DEM-refill, `qa_maps`, culvert-guess with inventory burning, the 7b/ANUGA engine hand-off, the `DamSafety_Setup` plugin, and a persistent file-digest cache in `damsuite/chain.py`.

### Fix these first (verified, highest impact)

1. **Two GeoPackage writes crash/no-op on every real QGIS work folder** — Screening tool 18 "Merge dams and ponds" (`core/merge.py:61`) and the Review tab "Move breach point" (`core/review.py:185`) `INSERT`/`UPDATE` with plain `sqlite3` without disabling the GDAL rtree spatial index, so they raise `no such function: ST_IsEmpty` on any QGIS/GDAL-written GeoPackage. The tests pass only because their fixtures are hand-written with no index. **nd_hazard fixed this in 0.29.0 (`core/gpkg_sqlite.disable_rtree`); DWR dropped it.**
2. **Screening default truncates floods and under-classifies dams** — `TSRC` "Terrain for larger clips" defaults to `0` (Cached clips only) (`algorithms/run_breach_sim.py:365`, `core/workflow.py:141`). A flood that reaches the step-2 clip edge is cut off and the dam under-classified (the changelog's own example: Medium/0 buildings vs High/27 buildings/86 roads). **nd_hazard's 0.28.0 fix defaults to following the terrain source; DWR did not take it.**
3. **`collapse_dwellings` under-counts life loss** — buildings that merely share one 911/street address (mobile-home parks, apartment complexes, multi-home farmsteads — routine in rural ND) have their occupants zeroed (`structures.py:2090/2162`), under-counting TM-11 lives-in-jeopardy and PAR/loss-of-life and dropping the TM-11 band (e.g. High→Significant). **nd_hazard removed this call in 2.16.0 with an explicit audit note; DWR kept it.**
4. **HEC-RAS export: 35× too-large inflow** — `core/hecras.py` writes the project as "SI Units" but embeds the breach hydrograph in **cfs**, so HEC-RAS reads cfs numbers as m³/s (≈35.3× overstatement); coordinates are also written in DEM-CRS feet (`core/hecras.py:485–504`). **nd_hazard replaced this skeleton with `core/hecras_model.py`, which derives units from the CRS; DWR ships `hecras_model.py` but never wires it up.**
5. **Unit/CRS elevation bugs** — a metre DEM in a feet CRS is silently scaled as feet (~3.28× elevation error → grossly wrong flood, `Runner core/gdal_io.py:58`); local-LiDAR screening tiles get no vertical-unit conversion (`Screening algorithms/terrain_cache.py:93`); consequences road/rail length is ~3.28× high on a feet-CRS folder (`Runner core/impacts.py:53`). **nd_hazard has the unit cross-check and `z_unit_scale`; DWR's terrain/IO refactor dropped them.**
6. **County DEM download still uses the hand-typed bounding-box table** — the exact "release 1.22" audit bug #5: 48 of 53 county boxes are smaller than the county, so dams in the uncovered strip get missing terrain (`Screening core/dem_download.py:25`). **nd_hazard builds the boxes from `nd_counties.box()`; DWR left the fix on the table (`box()` exists but is unused).**

---

## 2. The relationship, precisely

### 2.1 Rename map and provenance
DWR's own `README.md` documents it: `DWR_Plugins` formerly contained the `nd_*` plugins (as `nd_claude_1.23/` at Hazard 2.31.0 / Runner 3.21.0 / Screening 0.29.0, and `nd_installed/` at Updater 1.4.1). Those were **removed and replaced** by the rebranded `DamSafety_*` plugins on a reset version line, and archived on branch `archive/nd-plugins` (tag `archive-nd-plugins-20261015`). The top-level `nd_breach_runner/` and `nd_dam_screening/` folders that remain in DWR are **test-only import-alias stubs** (each re-executes the `DamSafety_*` `__init__.py` so tests can import by installed name) — **not real plugins, and they must not ship in a plugin zip.**

### 2.2 How far apart the code is (shared files that differ / only-in-one)

| Pair | identical | differ | only in DWR | only in nd_hazard |
|---|---:|---:|---:|---:|
| Hazard | 80 | 85 | 12 | 25 |
| Runner | 69 | 83 | 28 | 40 |
| Screening | 92 | 76 | 30 | 34 |
| Data | 3 | 9 | 2 | **81** |

`DamSafety_Data` is the extreme case: **14 files vs nd_data_updater's 93.** It was stripped to a thin downloader — no vendored `damsuite/`, no `ndpack/` pack-builder, no `packmove`/`servicecheck`/`runreport`/`pack_dialog`/`osm_layers`/`packsource`, and **no tests at all**.

### 2.3 The shared `damsuite/` package (deterministic)
- **nd_hazard:** all four vendored copies (Hazard, Runner, Screening, Updater) are **byte-identical** — its README's "byte-identical in each" claim holds.
- **DWR:** Runner and Screening copies are byte-identical; Hazard's differs only by one extra module (`pack_convert.py`) and its `FILES.json` manifest; `DamSafety_Setup` carries a 3-file partial. `DamSafety_Data` has **no** `damsuite/` at all.
- **Across repos** the shared package genuinely diverged on `chain.py`, `offline_pack.py`, `solvegate.py`, `versions.py`, `__init__.py`, `FILES.json`. DWR added 5–6 modules nd_hazard lacks (`health.py`, `layout.py`, `servers.py`, `suite_config.py`, `suite_fill.py`, `pack_convert.py`).
- DWR labels `SUITE_VERSION = "1.17.1"` while carrying **more** modules than nd_hazard's `"1.18.0"`, so the version string no longer identifies package contents.
- **Packaging regression:** nd_hazard collapsed Hazard's top-level `offline_pack.py`/`suitegpkg.py` into 11-line shims that re-export from `damsuite/` (one canonical copy). DWR kept the **full, older** top-level copies (973/804 lines) alongside the newer `damsuite/` copies (1046/881 lines); both load as separate modules, and the plugin's data paths bind to the **stale** top-level copies, which (unlike `damsuite/` and unlike all of nd_hazard) cannot read a format-2 GeoPackage pack or write an R*Tree index.

---

## 3. Verified bugs (the audit)

Consolidated from 54 verified findings (duplicates surfaced by different reviewers merged). **"Regression"** = nd_hazard's sibling does not have the bug (DWR lost a fix). File paths are under the named DWR plugin.

### 3.1 High severity

| # | Plugin | File:line | Issue | Regression? |
|---|---|---|---|:--:|
| H1 | Screening | `core/merge.py:61` | Tool 18 merge `INSERT`s into a GeoPackage without disabling the GDAL rtree → `no such function: ST_IsEmpty` on every real QGIS-written `.gpkg`; dams+ponds can never be merged on real data. | ✅ (0.29.0) |
| H2 | Screening | `core/review.py:185` | Review "Move breach point" `UPDATE`s the geometry column with plain `sqlite3`, same rtree failure → the move is logged/stamped as done but silently never applied; downstream tools use the stale point. | ✅ (0.29.0) |
| H3 | Screening | `algorithms/run_breach_sim.py:365` | **Default** "Terrain for larger clips" = Cached-clips-only → flood truncated at the step-2 clip edge and the dam under-classified (unsafe default; DWR even prints a `CLIP_EDGE_WARNING` yet defaults to the truncating mode). | ✅ (0.28.0) |
| H4 | Hazard | `structures.py:2090` & `:2162` | `collapse_dwellings()` zeroes `persons` on every habitable building sharing one 911/street address → under-counts TM-11 lives-in-jeopardy and PAR/loss-of-life, drops the TM-11 band (High→Significant). ND primary class is preserved (highest-zone member kept), but life-loss outputs are wrong. | ✅ (2.16.0) |
| H5 | Hazard | `download.py:290` | `parallel_map` returns worker-thread `QgsVectorLayer`s; on QGIS 3.34 their CRS yields invalid (short-circuited) transforms in the main thread, so downloaded **roads/rail/lifelines fall outside the study area and silently empty out**, under-classifying the dam. The shipped `damsuite/transformguard.py` is never installed in `__init__.py`, and none of nd_hazard's `_adopt_results`/`_warm_transforms`/`_repair_transforms`/`prime_ellipsoids` mitigations exist. | ✅ |
| H6 | Runner | `core/refine.py:221` | Near-dam FV1 refine merges fine-grid depth/WSE/arrival into the **final** near-dam rasters with no sanity check; the FV1 free upstream edge can import reservoir water and inflate depths (the sibling documents this blowing a fine box to 6.5e10 m³ vs 9.7e6 m³ injected). The inflated depth drives the hazard class. No `check_fine`, no closed edges, no short fine clock. | ✅ (3.11–3.12) |
| H7 | Runner | `core/hecras.py:500` | HEC-RAS export declares "SI Units" but writes the breach inflow in **cfs** → ~35.3× too-large inflow; 2D polygon/breakline/BC coords written in DEM-CRS (feet) units too. The README never warns of the unit trap. | ✅ |
| H8 | Runner | `core/gdal_io.py:58` | A metre DEM carried in a feet CRS is silently scaled as feet → ~3.28× elevation error → grossly wrong flood. nd_hazard cross-checks the median elevation against ND ground range and warns/corrects. | ✅ |
| H9 | Screening | `algorithms/terrain_cache.py:93` | Local-LiDAR path warps/mosaics tiles with no vertical-unit conversion and no multi-CRS handling → feet-tagged DEM tiles treated as metres. | ✅ |
| H10 | Screening | `core/breach_dir.py:99` | `_steepest` calls `np.isfinite(z0)` on `dem_fn` output that is `None` outside the cached tile → `TypeError`; unhandled in the per-dam loop, so **one edge-case dam aborts the whole tool-4 batch** (realistic on flat ND terrain / no-waterbody / modest clip). nd_hazard guards `None`. | ✅ |
| H11 | Screening | `core/breach_dir.py:154` | Downstream side is judged only at the dam **midpoint**, and the waterbody vote overrides terrain when they disagree → reversed flow direction (whole flood on the wrong side) on long dams. | — |
| H12 | Screening | `core/dem_download.py:25` | County DEM download still uses the hand-typed `COUNTIES` bbox table ("release 1.22" audit #5): 48/53 boxes smaller than the county → missing terrain / step-7 FAIL for dams in the strip. `nd_counties.box()` exists but is unused. | ✅ |
| H13 | Screening | `algorithms/selftest.py:131` | Self-test `shutil.rmtree(ignore_errors=True)` can delete a **user-supplied** WORK folder when `KEEP` is off (the default). *(Pre-existing — unfixed in both repos; still a real hazard.)* | — |
| H14 | Data | `http_qgis.py:32`, `plugin.py:8` | Unscoped PyQt/QGIS enums (`QNetworkRequest.AlwaysNetwork`, `QgsMapLayerType`, `QgsTask.CanCancel`, …) crash on Qt6, yet `metadata.txt` declares `supportsQt6=True`. Sibling `DamSafety_Hazard` has a `compat.py` shim; `DamSafety_Data` has none. | ✅ (1.4.1) |

### 3.2 Medium severity

| # | Plugin | File:line | Issue | Regression? |
|---|---|---|---|:--:|
| M1 | Hazard | `classification.py:50` | `classify_nd` returns `cls='Not classified'` for an empty inventory, but that value is not in `ND_RANK` → `KeyError` in `_raise()`. | — |
| M2 | Hazard | `classification.py:157` / `:44` | No `structs_evaluated` flag: a run whose structure layer is missing/failed reports the habitable-structure criteria as a definitive "not met" and presents a complete Low/Medium class, instead of "not evaluated — class is a minimum and may be higher." | ✅ (2.19.0) |
| M3 | Hazard | `structures.py:1095` | Missing the v2.20.1 parcel-site/911 habitability model → NSI-2026 outbuildings that inherit a parcel's RES record are counted as habitable dwellings (inflates counts, can push borderline dams to High). | ✅ (2.20.1) |
| M4 | Hazard | `damsuite/pack_convert.py:41` | `convert_pack` runs `ogr.Open()`+`CopyLayer` on a format-1 pack whose features are zlib-compressed GeoJSON blobs → written output is wrong. *(Orphan copy; the imported Setup copy differs.)* | — |
| M5 | Runner | `core/lisflood.py:307` | No flood engine out of the box: `exe` defaults to `''` and `_run()` raises "executable not found"; no bundled binary and no built-in fallback, though help promises "auto-detected." nd_hazard bundles LISFLOOD-FP binaries + a numpy engine. | ✅ |
| M6 | Runner | `core/workflow.py:618` | Top-of-dam estimated as the single highest DEM cell (`nanmax`) within 30 m → can latch onto a nearby hill/road spike. | — |
| M7 | Runner | `core/workflow.py:1212` | A DEM-estimated top-of-dam is written to `dam.json` without a "DEM estimate" datum → later runs treat the guess as a surveyed crest. | ✅ |
| M8 | Runner | `core/workflow.py:1346` | A second run into the same dam folder leaves the previous scenario's Baseline/Incremental rasters (only `STALE_ON_RERUN` classification files are cleared). | — |
| M9 | Runner | `core/dem_refill.py:90` | 3DEP hole-refill hard-codes EPSG:5070 but the grid is in the working CRS → the refill silently does nothing off-5070. | — |
| M10 | Runner | `core/dem_refill.py:46` | Automatic 3DEP refill fetches over raw `urllib`, bypassing the QGIS proxy/SSL → silently fails behind a proxy and the run stops. | — |
| M11 | Runner | `core/impacts.py:53` | Consequences reports flooded road/rail length ~3.28× too high on a feet-CRS dam folder (missing the `unit_m` scaling nd_hazard has). | ✅ |
| M12 | Runner | `core/hecras.py:497` | HEC-RAS export hard-caps the plan simulation window to 13 h for every run → longer breach floods truncated. | ✅ |
| M13 | Runner | `algorithms_ndhazard.py:793` | AADT join search radius treated as metres but distances are in grid units → on a feet-CRS folder the ~30 m radius shrinks (roads mis-joined). | — |
| M14 | Runner | `plugin.py:58` | `initGui` no longer creates the "Dam suite home…" menu action (same in Hazard) → `run_home` is unreachable. | ✅ |
| M15 | Runner | `algorithms.py:583` | The LISFLOOD-FP path set through Setup/suite settings (`ndsuite/lisflood_exe`) is never read by the Runner, which reads a different key → configured engine path ignored. | ✅ |
| M16 | Runner | `algorithms.py:1079` | Crest-line breach placement is only **partially** the nd_hazard fix: the dangerous 0.5-default and whole-box bugs are gone, but DWR picks the nearest-midpoint line within 500 m with **no DWR-structure-id match**, so it can still place the breach on a neighbouring dam's line. | partial |
| M17 | Screening | `algorithms/hazard_classification.py:253` | Primary bld/roads/rail field names matched **case-sensitively** → interstate/AADT/passenger not read when case differs, silently classifying every overtopped road by assumption (over-conservative). nd_hazard uses case-insensitive `pick()`. | ✅ (0.18.0) |
| M18 | Screening | `core/storage.py:218` | Storage-leak guard no longer extends the curve to the crest for known-height dams → the stage-storage curve truncates at spill level, under-stating storage. | ✅ |
| M19 | Screening | `algorithms/estimate_storage.py:232` | An assumed quadratic inventory curve is reported as high-confidence "inventory" instead of "low," mislabeling a guessed stage-storage curve. | ✅ |
| M20 | Screening | `algorithms/find_ponds.py:233` | `_dem_window` reuses the per-dam clip whenever it exists and covers the extent, without checking it is as fine as the analysis cell → a rerun reads a pond from a coarser clip and reports ~half the storage. | ✅ (0.21.0) |
| M21 | Data | `core.py:660` / `:662` | `address_points` (default-ON) and `overture` re-download the entire FeatureServer on every check (no `remote_version`, no `min_check_interval`). | ✅ |
| M22 | Data | `core.py:649` | `address_points` and `overture` set no `layer_globs`, so "Add North Dakota Layers to Project" silently never loads them. | — |
| M23 | Data | `core.py:436` / `:439` | Lock reverted to 6 h mtime-staleness: a crash mid-update blocks **all** updates for up to 6 hours (nd_hazard clears a lock whose PID is gone). | ✅ (1.4.0/1.5.0) |
| M24 | Setup | `plugin.py:37` | The Setup plugin's entire suite-configuration surface (status dock + setup dialog) is never wired into the GUI. | — |

### 3.3 Low severity / plausible

- **L1** `Runner core/lisflood.py:140` — `raise_crest` iterates every model cell in a pure-Python double loop, re-run on each LISFLOOD pass (slow on large domains).
- **L2** `Screening core/review.py:220` — `move_breach` has no maximum-distance guard; a mis-typed or wrong-CRS coordinate moves the breach point arbitrarily far (nd_hazard has `MAX_MOVE_M=5000`). *(regression)*
- **L3** `Data sources.py:11` — `NSI_GENERATOR_ZIP_SHA256` is a hardcoded, **enforced** hash of GitHub's codeload archive; a GitHub recompress breaks the `nsi_generator` source. Off by default. *(nd_hazard logs but does not enforce it.)*
- **P1 (plausible)** `Data core.py:710` — `ArcGISFeatureSource` paging can loop and grow the NDJSON unbounded if a FeatureServer ignores `resultOffset` while still setting `exceededTransferLimit`.

### 3.4 Excluded (refuted on verification)
6 candidates were refuted by the adversarial pass and are not listed above, to avoid false positives.

---

## 4. What DWR is missing vs nd_hazard (parity gaps)

These are capabilities present in nd_hazard and absent in DWR — not bugs, but losses a DWR user feels.

### Hazard (13 gaps)
- **[high]** Incomplete-classification flagging (`structs_evaluated`, 2.19.0) — see M2.
- **[high]** Parcel-site/911 habitability model (2.20.1) — see M3.
- **[med]** Depth-velocity hazard spreadsheet export (`dvsheet.py` + `templates/Hazard_Spreadsheet.xlsx`) and the two DV charts in the PDF report — the engineer must now hand-transcribe.
- **[med]** Plain-language result card (`resultcard.py`) — the one-page owner/EM explanation; DWR's report opens on the criteria tables.
- **[med]** "What would change the class" table (`whatif.py`).
- **[med]** Lifeline-route detail — dry detour length + second-facility reachability (`lifelineroutes.py`).
- **[med]** Single-page EAP map + georeferenced GeoPDF for field use (DWR has only the multi-page map book).
- **[med]** File-drop-in offline-pack layers (`packfiles.py`) — the DWR dam inventory and HIFLD facility layers have no public service and cannot enter a DWR-built pack.
- **[med]** "Suite data and servers" window + "Get data for this dam" tool (`suite_data.py`, `getdamdata`) — relocated to the suite-fill/local-folder approach; live per-dam download UI is gone.
- **[low]** Owner/county letter drafts (`letters.py`, `letterwindow.py`); field-check sheet workflow (`fieldcheck.py`); preflight run-button reasons (`preflight.py`); headless chain Processing algorithms for the Model Designer / `qgis_process` batch (`chain_algorithms.py`).

### Runner (12 gaps)
- **[high]** Built-in flood engine + bundled LISFLOOD-FP binaries (zero-install run) — see M5.
- **[high]** FV1 near-dam-refine sanity checks (`check_fine`, closed upstream edges, late-inflow detection) — see H6.
- **[med]** Defensible, sign-off-gated loss of life (`lifeloss.py`, `fatality_rates.json`); DEM unit cross-check + coarse-DEM crest handling (see H8); Screening→Runner hand-off of cell size/method/scenario (a 20 m screen silently re-runs at 10 m); road/rail crossing-closure table for EAPs (`crossings.py`); cascade-check (is a downstream dam inside this flood?, `cascadecheck.py`); tool-input autofill from the dam folder + "Get data for this dam"; the HEC-RAS 2D model builder (`hecras_model.py` ships in DWR but is **orphaned/never registered** — see H7).
- **[low]** Embankment-gap reporting (`gaps.py`); resumable dam queue + `run_finished()` (`batchqueue.py`, `runblock.py`); compare-two-runs (`compareruns.py`) and one-click ANUGA setup (`anuga_setup.py`).

### Screening (11 gaps)
- **[high]** Two loading cases in one run with worse-governs (`core/scenarios.py`) — **a dam whose sunny-day class is higher is under-classified by default.**
- **[med]** Breach-parameter sensitivity row (`breach_band.py`); arrival-time contours + per-structure warning time (`arrival.py`); Runner hand-off grid/breach/scenario in `dam.json`; pond inventory-storage cross-check (`find_ponds` WB_STOR); statewide Priority tab + Excel export; systematic US-unit twin fields on result tables (`us_units.py`); QGIS network-proxy support for 3DEP/LiDAR.
- **[low]** County-run resume manifest (`resume.py`); on-screen "class is a floor, not an answer" incomplete-run flags (`incomplete.py`); runner-vs-screener comparison sheet (`comparison.py`).

### Data (8 gaps — the stripped-down plugin)
- **[high]** Pack change-report + stale-dam detection + data-vintage table (`runreport.py`) — **after a data refresh, a DWR user is never told which already-classified dams are now out of date.**
- **[high]** File-drop-in ingest of DWR dam layers + HIFLD lifelines into the pack (`ndpack/packfiles.py`).
- **[med]** Service pre-flight probe (`servicecheck.py`); verified USB pack transport with checksums (`packmove.py`); OSM pack-layer builder from the Geofabrik `.pbf` (`osm_layers.py`, incl. campgrounds/mobile-home parks — life-loss-relevant); pack-management window (`pack_dialog.py`); pid-aware stale-lock recovery (see M23).
- **[low]** The entire automated test suite (64 tests), `qtcompat.py` Qt6 shim, and QGIS-proxy for the stdlib HTTP fallback.

---

## 5. Where DWR is ahead

- **`average` DEM resampling by default** (`core/gdal_io.py`, `DEM_RESAMPLE` choice) — matches the regression baselines; nd_hazard reverted to `bilinear`, which DWR's own comment says ran floods 3–40 % high. This is a **result-affecting divergence in DWR's favour.**
- **UTF-8 / Windows encoding hardening** across report/CSV/JSON writes (Runner and `damsuite/solvegate.py`, `offline_pack.py`) — nd_hazard dropped `encoding=` on most `open()` calls and risks `UnicodeEncodeError` on a non-ASCII dam name.
- **New data sources in Data:** Overture Maps buildings and ND 911 NENA address points (neither in nd_hazard; but see the re-download bug M21).
- **AADT road-traffic join** in the hazard tool; **automatic 3DEP DEM-refill**; **culvert_guess** with inventory matching + optional DEM burning; **QA maps**; **solve_gate**; **min_cells cut-line lengthening** (better notch geometry on coarse cells).
- **7b/ANUGA engine hand-off** (Screening→Runner) and the **`DamSafety_Setup`** plugin.
- **Tool-7b unknown-class normalization + "mode 5"** and the **shallow-basin storage guard** in its `(crest−pool)+max_depth` form — both appear **ahead of** nd_hazard's screening.
- A **persistent on-disk file-digest cache** in `damsuite/chain.py` (algorithm unchanged, so digests still match).
- Safer Data default: `auto_check=False` (no quiet auto-download until the engineer opts in).

---

## 6. Did the "release 1.22" fixes land in DWR?

Most of the documented fixes **did** carry over: `average` DEM-resample default and precedence, lowest-qualifying inflow cell, the structures "truth value of an array is ambiguous" fix, the shallow-basin guard, tool-7b unknown-class handling + option 5, safe/atomic/locked settings saves (`damsuite/settings.py` is byte-identical to nd_hazard's), closed `os.scandir` iterators, QA maps, and spillway adequacy.

**Not carried over (genuine DWR regressions):** the GeoPackage rtree/`ST_IsEmpty` write fix (H1/H2) and the county-box fix (H12).

**Moot in DWR (the target code was removed in the rebrand):** the dam-queue `_Feedback.pushInfo` crash and the cancelled-dam-rerun fix (no queue in DWR), and the Updater pack-builder fixes (`DamSafety_Data` has no pack-builder).

**Still unfixed in BOTH repos (pre-existing, not DWR-specific):** the self-test folder deletion (H13), `preview()` mutating the caller's dam, and stale baseline rasters on rerun (M8).

---

## 7. Metadata / packaging hygiene

- `DamSafety_Runner/metadata.txt` `author=Your Name` and `DamSafety_Screening/metadata.txt` `author=` (empty) are placeholders; `DamSafety_Data` lists `author=ND Data Updater`. Only Hazard and Setup name the real author.
- `DamSafety_Data/metadata.txt` declares `supportsQt6=True` / `qgisMaximumVersion=4.99`, but the code is **not** Qt6-safe (H14); the repo README targets QGIS 3.34 LTR (Qt5). `DamSafety_Data/README.md` is also an un-rebranded copy ("ND Data Updater", "nd_data_updater.zip", "your Mac").
- `damsuite/FILES.json` differs between Hazard and Runner/Screening within DWR (Hazard's lists the extra `pack_convert.py`); the self-check passes in all three, but the manifest is no longer uniform.
- `damsuite/health.py` and the Hazard `damsuite/pack_convert.py` are shipped but never imported (dead weight).
- Confirm the test-only `nd_breach_runner/` and `nd_dam_screening/` alias stubs are excluded from every built plugin zip.

---

## 8. Recommended order of work

1. **H1/H2** — add a `gpkg_sqlite.disable_rtree` helper (port from nd_hazard) and call it before the raw `INSERT`/`UPDATE` in `merge.py` and `review.py`. Add a test fixture written by GDAL (with an index), not hand-rolled.
2. **H3** — change the `TSRC` / `larger_clips` default to follow the terrain source (nd_hazard's 0.28.0 behaviour).
3. **H7 / H8 / H9 / M11 / M13** — the unit/CRS family: either wire up `hecras_model.py` (CRS-derived units) or convert cfs→m³/s and emit metric coords in `core/hecras.py`; port the DEM unit cross-check into `gdal_io.py`; add vertical-unit scaling to the Screening local-LiDAR path and to `impacts.py`.
4. **H4** — port nd_hazard's removal of the `collapse_dwellings` call (keep the spatial-dedup in `build_inventory`).
5. **H5** — install the shipped `damsuite/transformguard.py` in `__init__.py` and port the layer-adoption/transform-repair machinery into `parallel_map`.
6. **H6** — port `check_fine` + closed upstream edges + short fine clock into `core/refine.py`.
7. **H10/H12** — add the `None` guard in `_steepest`; switch county boxes to `nd_counties.box()`.
8. **Then** the medium set, prioritizing the unsafe-direction classification items (M2, M17, M18, M20) and the Data regressions (M21, M23).
9. **Hygiene:** fix the placeholder `author=` fields; either make `DamSafety_Data` Qt6-safe or set `supportsQt6=False`; rebrand `DamSafety_Data/README.md`; drop or wire the orphaned `damsuite/health.py`/`pack_convert.py`; decide whether to finish the shim refactor for Hazard's top-level `offline_pack.py`/`suitegpkg.py` (so they stop reading the stale copy).

---

## 9. Method, confidence, and caveats

- **Fan-out:** 6 "map" agents (one per plugin pair + the shared `damsuite/` + an audit-fix tracer) and 8 bug-hunting agents across the DWR subsystems; every candidate finding was then verified by an independent agent instructed to **refute it by default**. 74 agents total, 0 errors.
- **Confidence:** 53 of 54 survivors are "confirmed" (a concrete, reachable call path traced end-to-end); 1 is "plausible" (P1). 6 candidates were refuted and dropped.
- **Caveats:** the plugins were analysed statically — they were **not** executed against real QGIS, GDAL, or LISFLOOD-FP. Runtime-dependent items (H5 worker-thread transforms, H6 FV1 free-edge inflation, H8 feet/metre DEM) are confirmed by reading the code paths **and** by the sibling repo's own comments documenting the same failures on real data (Elm River Dam 1, the `gpkg_sqlite` docstring, the `transformguard` note). Severity reflects dam-safety impact (a wrong hazard class or life-loss number is weighted highest).
- This audit covers the `DamSafety_*` plugins as checked out on `claude/funny-goldberg-dg1x2s` (even with `main`). Line numbers are from that revision.
