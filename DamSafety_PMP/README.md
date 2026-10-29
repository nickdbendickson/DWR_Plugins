# DamSafety_PMP (ND PMP Tool, prototype v0.4.0): INTERNAL / EXPERIMENTAL

> **LICENSING / PERMISSION: READ FIRST**
> - This plugin ports Applied Weather Associates' (AWA) ND PMP Evaluation Tool (2021) from the North Dakota statewide PMP study.
> - The study's final report says it "has been prepared for the exclusive use of the North Dakota State Water Commission (Client)" and that it may not be relied on by any other party without AWA's written consent.
> - The tool zip carries no license.
> - **Do not redistribute this plugin, its outputs or the AWA data outside the team, and do not publish it to the QGIS plugin repository, without written permission from the ND State Water Commission (SWC/DWR) and AWA.**
>
> **Not validated against ArcGIS.** Results are from an independent port: see "Not validated against ArcGIS / known gaps" below. Do not use them for design or regulatory submittals until they have been checked against the ArcGIS tool.

## In this repository
- **Folder:** `DamSafety_PMP/`, named to match the other DamSafety_* plugins. The **Processing provider id stays `ndpmp`**, and the **QgsSettings keys stay `nd_pmp/...`** (`nd_pmp/data_home`, `nd_pmp/whitebox_tools`, `nd_pmp/hms_home`). That keeps existing models, scripts and saved settings working. The plugin uses only relative imports, so the folder name doesn't matter to the code.
- **Data is NOT in the repo.** The six AWA File Geodatabases, about 700 MB, stay outside it. Download `pmp_evaluation_tool.zip` from the ND Department of Water Resources (formerly SWC) PMP study page, "Probable Maximum Precipitation Study": https://www.dwr.nd.gov/info_edu/reports_and_publications/probable_maximum_precipitation_study/ (direct link: `.../probable_maximum_precipitation_study/pmp_evaluation_tool.zip`). Extract it anywhere, and choose the extracted `PMP_Evaluation_Tool/` folder (it holds `Input/*.gdb`) as **"PMP_Evaluation_Tool data folder"**. The choice is remembered in `nd_pmp/data_home`. Never commit the data or outputs (see `.gitignore`).
- **Optional external tools:**
  - WhiteboxTools, for pothole storage: set the tool setting or `WBT_PATH`.
  - HEC-HMS 4.13, for `export_hms` runs: set the tool setting or `ND_PMP_HMS_HOME`.

  The plugin has no built-in paths for either.

### Hydrograph CSV contract (read by DamSafety_Runner's Spillway adequacy tool)
`ndpmp:inflow_hydrograph` writes one CSV per storm into `<out>/hydrographs/`:
```
# header lines start with '#'
time_hr,flow_cfs,precip_in,excess_in
```
- **Column order is `time_hr, flow_cfs, precip_in, excess_in`** (v0.3.0). v0.2.0 wrote `time_hr,precip_in,excess_in,flow_cfs`.
- `time_hr` is the end of each timestep, starting at Δt, with no t = 0 row. `flow_cfs` is total inflow.
- DamSafety_Runner's `spillwayadequacy` reads the columns **by header name** (`time_hr`, `flow_cfs`), skips `#` lines, and adds a t = 0 zero row.
- Older Runner code (`read_csv_series`) reads the **first two numeric columns**. Keep `time_hr, flow_cfs` first.
- `tests/test_light.py` checks this order.

### Tests
- `python tests/run_tests.py`: light tests only. They need no AWA data, no HEC-HMS and no QGIS (just GDAL + numpy): hand calculations, the DAD lookup, and the hydrograph CSV column order.
- `python tests/run_tests.py --all`: also runs the data-dependent scripts from the current working folder.
  - A script whose inputs are missing prints `SKIP` (exit 77).
  - Inputs: `ND_PMP_DATA_HOME` (the PMP_Evaluation_Tool folder), the Medina test inputs (`medina_pmp/`, `pothole_ws/`, `NOAA_Dist_C.xlsx`, `dem3dep.tif`), and `ND_PMP_HMS_HOME`.
  - `validate_pothole.py` runs its synthetic-cone part whenever WhiteboxTools is found.
- Use a conda/QGIS Python with GDAL ≥ 3.7. `run_tests.py` sets `PROJ_DATA` from the interpreter's prefix.
- `tests/test_streamstats.py` (offline, saved StreamStats responses for DWR 2801 and DWR 4031, no QGIS) is in the light set. `tests/test_streamstats_dialog.py` opens the dialog offscreen with real QGIS and a mocked client (`QT_QPA_PLATFORM=offscreen`; run it with `python -c "import os, sys, pytest; rc = pytest.main([...]); os._exit(int(rc))"`, as CI does). Set `PMP_SHOTS=<folder>` to save screenshots.

## Get basin data from StreamStats… (v0.4.0)

**Plugins > DamSafety PMP > Get basin data from StreamStats…** fills the basin inputs for **Inflow hydrograph** from USGS StreamStats and shows a read-only sanity check for the PMF.

1. **Outlet point.** Click **Pick on map…** and click the creek just below the dam, or choose a point layer and a dam and click **Use the dam point**. A dam's mapped point is often on the embankment or in the reservoir, off the stream line. If StreamStats can't snap it, the window says *"Not on the stream network … Move the point onto the channel just below the dam"* and nothing is filled.
2. **Get basin data.** StreamStats outlines the basin. The window loads it as a layer (`<name>_watershed.geojson`) and fills:
   - **Drainage area** (DRNAREA).
   - **Non-contributing area, suggested**: ISOLAKESUM, the area draining to isolated lakes and potholes. This is only a starting value; you decide the non-contributing area (or run `pothole_storage`).
   - **Tc / lag**: Kirpich (1940) from the longest flow path and its 10-85 slope (lag = 0.6 Tc), or the NRCS lag equation with **your own CN**. The window says which method was used. **CN is never taken from StreamStats.**
3. **Sanity check (read-only).** The ND regression peaks for 50% to 0.2% AEP, with ~90% ranges from each equation's average standard error, and the Crippen & Bue (1977) maximum-flood envelope.
4. **Open Inflow hydrograph…** opens the tool with the area, non-contributing area, Tc, lag and the StreamStats basin file filled (CN left as you set it). After the run, the tool compares the PMF peak with the 0.2% AEP peak and the envelope and writes `streamstats_pmf_check.json` next to the hydrographs. These are plain-language warnings; they never stop the run. **Open PMP tool…** opens `ndpmp:pmp` with the basin layer.

The run runs in the background with progress and **Cancel**. Files go to the chosen folder: `<name>_watershed.geojson`, `<name>_streamstats_basin.json` (the values) and `<name>_streamstats_provenance.json` (service versions, query time, region, point and snapped point, every request with the sha256 of its answer, and the citations). Answers are cached in `<folder>/streamstats_cache/` by sha256 of the request; a cached file whose hash no longer matches is refused.

**Source.** Williams-Sether, T., 2015, Regional regression equations to estimate peak-flow frequency at sites in North Dakota using data through 2009: U.S. Geological Survey Scientific Investigations Report 2015-5096, 12 p., https://doi.org/10.3133/sir20155096 (StreamStats ND, peak-flow region C equations, average standard errors 51-83%). Envelope: Crippen, J.R., and Bue, C.D., 1977, Maximum floodflows in the conterminous United States: U.S. Geological Survey Water-Supply Paper 1887, https://doi.org/10.3133/wsp1887.

**Limits.**
- The regression peaks are for **rural, unregulated** basins with characteristics inside the ranges used in SIR 2015-5096. The window warns when StreamStats flags a value outside them. They are not a PMF; they are a check on the order of magnitude.
- The Crippen & Bue envelope is the largest observed US floods for the area, also not a PMF. A PMF well below the 0.2% AEP peak, or well above the envelope, deserves a second look; neither is proof of an error.
- Pothole country: the drainage area is the whole outlined basin. How much of it contributes is your call.
- North Dakota points only (the window checks the box 45.9-49.05 N, 104.1-96.5 W).

**Services (Jan 2026 migration).** StreamStats moved to new open-source services (application 4.30.0, Dec 2025), and USGS switched the legacy `streamstatsservices` off on 2026-01-30; USGS notes results may differ slightly from the old services. This tool uses the new `ss-delineate` and `ss-hydro` services (versions recorded in the provenance file; tested with ss-delineate 1.2.0 and ss-hydro 1.4.0) and NSS for the regression equations. If USGS changes them again, the window says "StreamStats failed" with the reason; saved runs and the cache still work.

**Using the USGS services.** StreamStats is a free public USGS service with no key. USGS asks for no more than 4 requests at once; this tool sends **one at a time, at least 2 s apart**, identified as `DamSafety_PMP/<version> (QGIS plugin; North Dakota dam safety; …)`, and reuses the cache, so asking again for the same point sends nothing. Requests go through QGIS networking (`QgsBlockingNetworkRequest`), so they follow the **proxy, authentication and SSL settings in Settings > Options > Network**, and the QGIS network timeout applies. If StreamStats cannot be reached, the tool says so in plain words and points to those proxy settings. (QGIS sends its own User-Agent, `Mozilla/5.0 QGIS/…`; the tool's identification goes in the `X-Client` header.) Outside QGIS (scripts, tests) the client falls back to Python's `urllib`, which follows the `HTTPS_PROXY` / `HTTP_PROXY` environment variables instead. Results are provisional USGS products; check them before use.

---

# ND PMP Tool – QGIS prototype (INTERNAL USE ONLY)

This is a prototype port of the **main PMP script** (`ND_PMP_Tool.py`, Applied Weather Associates, v1, 2021) from the North Dakota statewide PMP study.
It runs as a QGIS Processing algorithm. **It has not been validated against ArcGIS output. Do not use it for design or regulatory work.** Do not publish or redistribute it. The study report limits reliance on its methods to NDSWC without AWA's written consent.

## Identity
| | |
|---|---|
| Plugin folder / module | `DamSafety_PMP` in this repo (was `nd_pmp`) |
| Plugin name (metadata) | ND PMP Tool (prototype), version 0.1.0, experimental |
| Processing provider id | `ndpmp` ("ND PMP (prototype)") |
| Algorithm ids | `ndpmp:pmp` ("PMP Evaluation (main tool)"), `ndpmp:inflow_hydrograph`, `ndpmp:pothole_storage` (v0.2.0) |
| Data-folder setting | QgsSettings key `nd_pmp/data_home` (remembered after each run) |

## Layout
- `core/`: pure Python 3 (GDAL/OGR ≥ 3.7 and numpy, plus matplotlib for the chart only). It has no QGIS imports.
  - `pmp.py`: grid selection, area, DAD lookup, storm maximization, basin/subbasin averages, rasters
  - `temporal.py`: temporal distributions, hyetograph CSVs and the summary CSV
- `algorithm.py`, `provider.py`, `plugin.py`: the thin PyQGIS layer
- `tests/run_core.py`, `tests/verify.py`: the scripts used for the checks below

## Install
1. Download the SWC zip and extract it. The data folder is `PMP_Evaluation_Tool/` and must contain `Input/*.gdb`. Reading FileGDB **rasters** isn't needed here. FileGDB vectors and tables are read with GDAL's built-in OpenFileGDB driver.
2. QGIS ≥ 3.28, including QGIS 4 (tested: QGIS 3.44.7 with GDAL 3.10.2; QGIS 3.34; QGIS 4.2.3 / Qt 6 with GDAL 3.13, all from conda-forge on Linux). Copy `nd_pmp/` into `<profile>/python/plugins/`, or use Plugins → Install from ZIP with `nd_pmp.zip`. Then enable it.
3. Run it from Processing Toolbox → ND PMP (prototype) → PMP Evaluation.

Headless example:
```
QGIS_PLUGINPATH=/path/to/parent qgis_process plugins enable nd_pmp
qgis_process run ndpmp:pmp -- BASIN=basin.shp DATA_HOME=/data/PMP_Evaluation_Tool OUTPUT_FOLDER=out SUBBASIN_FIELD=SUB_ID
```
Parameters:
- `BASIN` (polygon)
- `DATA_HOME`
- `OUTPUT_FOLDER`
- `LOCAL_DURATIONS` / `GENERAL_DURATIONS` / `COOL_DURATIONS`: comma-separated hours; blank skips that storm type. The defaults are the original's runTemporal lists.
- `USE_BASIN_AREA`, `CUSTOM_AREA`
- `WEIGHTED` (default true)
- `SUBBASIN_FIELD`
- `DD_CHART`
- `TEMPORAL`
- `WRITE_RASTERS`

## Outputs (`<out>/`)
- `<StormType>/PMP_<basin>_<N>sqmi.gpkg`: layer `<StormType>_PMP_Points_<basin>_<N>sqmi` with fields `ID`, `PMP_xx` (in), `STORM_xx` (controlling SPAS id, or `STORM` if no storm transposes)
- `<StormType>/<L|G|C>_<xx>_<basin>_<N>sqmi.tif`: 0.025° Float32 GeoTIFF, EPSG:4326, nodata −9999
- `<StormType>/CSV_<basin>_<N>sqmi/<StormType>_PMP_Basin_Average_<N>sqmi.csv` (and `_Subbasin_Average_` if a subbasin field is given)
- `<StormType>/<basin>_<StormType>_Depth_Duration_Chart.png`
- **`<out>/hyetographs/<storm_id>.csv`**: one file per storm type × duration × temporal distribution. `storm_id = <general|local|coolseason>_<dd>hr_<distribution name, non-alphanumerics→_>`, for example `general_24hr_GS_24HR_GENERAL_SYNTHETIC`, `local_06hr_SPAS_1177_1_6_hr`.
  ```
  # storm_type: general | local | cool-season
  # duration_hr: <int>
  # timestep_hr: <float>           (0.083333 = 5 min, 0.25 = 15 min, 1 = hourly controlling-storm patterns)
  # area_mi2: <float>              (area used for the PMP analysis)
  # basin_avg_depth_in: <float>    (basin-average PMP for this duration)
  # temporal_distribution: <original field name / pattern name>
  # snowmelt_in:                   (always blank – snowmelt tool not ported)
  # source: ... source_table=<Non_Storm_Data.gdb table or 'computed'>
  time_hr,incr_depth_in,cum_depth_in      (time_hr = end of timestep; first row = first timestep, no t=0 row)
  ```
  Patterns produced, following the original's call conditions:
  - Local: 2-hr (`LS_02HR_DISTRIBUTION`); 6-hr (10th/90th percentile, synthetic); 6-hr 5-min critically stacked; 06-hr controlling-storm patterns
  - General / Cool-season: 24/48/72-hr (general and hybrid × 10th/90th/synthetic); 24-hr 15-min critically stacked; 06-hr and 24-hr controlling-storm patterns
  - Controlling-storm patterns are produced only for controlling storms that appear in `CONTROLLING_STORM_TEMPORAL_DISTRIBUTIONS_06/24`. Any that are missing are logged.
- **`<out>/basin_avg_depth_by_duration.csv`**: columns `duration_hr,depth_in,storm_type,controlling_storm`. There is one row per storm type × duration. `controlling_storm` is the storm that controls the most grid points for that duration (ties go alphabetically). This is a definition chosen for the prototype; the original has no such field.

## Logic mapping (ported faithfully, including quirks)
- Points: Vector_Grid cells that INTERSECT the basin, then Grid_Points with their centre in those cells.
- Area: planar area in the basin CRS. Geographic CRS is projected to ESRI:102039. Uses the original constants. Rounding goes 3 dp → 1 dp → 2 dp. The original's `== "Foot" or "Foot_US"` bug treats every non-metre unit as feet; this is kept, with a warning.
- DAD lookup: linear interpolation between bracketing areas. Below the smallest area it extrapolates from the first segment. Above the largest it uses `x1/area*y1`, as in the original.
- PMP: `round(round(DAD,3)·TAF,1)` where `TRANS==1`; the first storm reaching the max wins (strict `>`). Storm lists come from the Local/General/CoolSeason feature datasets.
- Basin average:
  - Weighted: weight = clipped cell area in the Vector_Grid CRS (degrees²), via the ID join; np.average; round 2.
  - Unweighted: arithmetic mean.
  - With subbasins, points are restricted to cells intersecting each subbasin.
- Python 2 `round()` (half away from zero) is emulated (`py2round`).
- Temporal: line-by-line port of temporalDistLS2/LS6/_24hr/_48hr/_72hr, the controlling-storm functions and temporalCritStacked.

## Test results (Linux, GDAL 3.10, QGIS 3.44.7 via qgis_process)
I used two **synthetic** test basins I created (they are not real watersheds; see `tests/`):
- `small_geo`: a single polygon near Bismarck, EPSG:4326
- `two_sub_utm`: two subbasins near Jamestown, EPSG:32614

| | small_geo | two_sub_utm |
|---|---|---|
| Area used (sq mi) | 103.4 (OGR geodesic 103.441) | 387.1 (geodesic 387.376) |
| Grid points | 68 | 218 |
| Local PMP 1/6/24 h (in) | 9.49 / 16.8 / 18.31 | 5.89 / 13.3 / 16.25 |
| General PMP 6/24/72 h | 12.9 / 18.02 / 18.82 | 12.1 / 15.7 / 17.7 |
| Cool-season PMP 6/24/72 h | 7.65 / 9.77 / 11.97 | 7.64 / 9.58 / 11.73 |
| Run time (core) | ~7 s | ~10 s |

Checks:
- An independent per-point recompute (np.interp on the DAD tables plus a brute-force max over storms) matched the core with max abs diff 0.0 for 6 basin/type/duration cases. It shares the readers and the rounding helper, so it isn't fully independent.
- Grid_Points Id equals the containing Vector_Grid cell Id for all 68,277 points, which confirms the original's join assumption.
- 96 hyetographs:
  - every file ends at its duration
  - Σ increments = final cumulative
  - no negative increments
  - final depth equals the basin-average PMP to within 0.001 in for durations ≤ 24 h, and exactly for the 48/72-h synthetic patterns
- Raster cells equal point values (218/218).
- The `qgis_process` run gave a summary CSV and hyetographs identical to the pure-core run.

## Not validated against ArcGIS / known gaps
- **I made no comparison with ArcGIS output.** ArcGIS wasn't available and no reference outputs were provided. Areas where results may differ:
  - SelectLayerByLocation edge cases (basins touching cell edges)
  - ArcGIS Clip areas
  - FeatureToRaster cell snapping (here, cell centres sit on the grid points)
  - planar vs. ArcGIS area for projected basins
  - Py2 float formatting
- No `Temporal_Distribution_Check_*` PASS/FAIL table (checkTemporal not ported).
- Output is GeoPackage/GeoTIFF/CSV, not FileGDB. No Excel output.
- Critically stacked: depths are looked up by duration name; the original looks them up by field position, which is equivalent only for the default duration lists. The original's `pop(max)` branch would crash; here it raises a logged error instead. The test basins didn't trigger it.
- Spatial Distribution and Snowmelt tools are not ported, so `snowmelt_in` is always blank.
- The subbasin ID field is used as text. Multi-feature basins are dissolved for the main analysis, as ArcGIS selection by a multi-feature layer would do.
- Only Linux/conda QGIS was tested; Windows/OSGeo4W was not.


---
# v0.2.0 additions

## `ndpmp:inflow_hydrograph` (core: `core/hydro.py`, wrapper: `algorithm_hydro.py`)
This algorithm reads the hyetograph CSVs written by `ndpmp:pmp`, or any CSV with the same `#` header and the columns `time_hr,incr_depth_in,cum_depth_in` on a uniform timestep. It writes one inflow hydrograph per storm.
- **Losses:**
  - `LOSS=0`: SCS CN. Parameters: `CN`; `AMC3` converts AMC II→III with CN3 = 23·CN/(10+0.13·CN); `IA_RATIO` defaults to 0.2.
  - `LOSS=1`: initial + constant (`INITIAL_IN`, `CONSTANT_IN_HR`).
- **Transform:**
  - `TRANSFORM=0`: SCS dimensionless UH, with ordinates from the NEH-630 Ch.16 table.
    - Inputs: `LAG_HR`, or `TC_HR` (lag = 0.6·Tc). Tp = Δt/2 + lag.
    - Peak: qp = `PRF`·A/Tp (default 484). For non-484 PRF the time base is scaled by 484/PRF so volume stays at 1 in.
    - `PEAKING_PCT`: PMF peaking adjustment. Raises the UH peak by p% by dividing Tp by (1+p/100); volume is preserved.
    - Ordinates are sampled at the end of each Δt, as HEC-HMS does, then renormalised to exactly 1 in. Before renormalising they are +0.1–0.2 % high.
  - `TRANSFORM=1`: Clark (`TC_HR`, `CLARK_R_HR`). Uses the HEC-HMS synthetic time–area curve and linear-reservoir routing.
- **Baseflow:** constant `BASEFLOW_CFS`, or recession Q0·k^(t/24) (`RECESSION_K` per day).
- **Area:** comes from the hyetograph header `area_mi2` unless `AREA_MI2` > 0. `NONCONTRIB_MI2` is subtracted. A typical value is `noncontributing_area_mi2` from `ndpmp:pothole_storage`, but treat that with care; see the limitations below.
- **Outputs:**
  - `<out>/hydrographs/<storm_id>.csv` (storm_id = the hyetograph file name):
    ```
    # storm_id: ...            # storm_type: ...        # duration_hr: ...     # temporal_distribution: ...
    # peak_cfs: ...            (total flow incl. baseflow)
    # time_to_peak_hr: ...
    # runoff_volume_in: ...    (DIRECT runoff only, over contributing area; excludes baseflow)
    # runoff_volume_acft: ...
    # area_mi2 / contrib_area_mi2 / dt_hr / loss / transform / baseflow
    time_hr,flow_cfs,precip_in,excess_in      (v0.3.0 order; v0.2.0 was time_hr,precip_in,excess_in,flow_cfs)
    ```
    - `time_hr` is the end of each Δt, starting at Δt.
    - Rows continue past the end of the storm until the UH recedes. After the storm, `precip_in` and `excess_in` are 0.
    - The spillway runner should read `time_hr,flow_cfs` and skip lines starting with `#`.
  - `<out>/inflow_hydrograph_summary.csv`, sorted by peak, descending. Columns: `storm_id,storm_type,duration_hr,temporal_distribution,area_mi2,contrib_area_mi2,precip_in,excess_in,runoff_volume_in,runoff_volume_acft,peak_cfs,time_to_peak_hr,loss,transform,baseflow,hydrograph_csv`

### Validation (`tests/validate_hydro.py` → `validate_hydro.json`)
- **CN hand calculation** (P = 10 in, CN 80): 7.520833 in, identical to the hand result. AMC III of CN 80 = 90.196, identical.
- **SCS UH hand calculation** (A = 10 mi², lag = 3 h, Δt = 1 h):
  - qp = 484·10/3.5 = 1382.86 cfs/in.
  - The ordinates at t = 1–5 h (244.96, 837.62, 1333.47, 1333.47, 1039.12) match the hand interpolation of the DUH table exactly.
  - Raw UH volume = 1.00105 in before renormalising.
- **Volume conservation:** on a real nd_pmp hyetograph (GS 24-h synthetic, 103.4 mi²), runoff volume from ∑Q·Δt equals excess depth to 6 dp for all four configurations tested: SCS / Clark+initial-constant / AMC III + 25 % peaking / PRF 300.
- **Published HEC-HMS example** ("HEC-HMS Example for Typical NRCS Applications", HMS 4.10):
  - Setup: 100-yr storm, 8.4 in, NOAA Atlas 14 Type C 24-h pattern (from the example's NOAA_Dist_C.xlsx), Δt = 5 min, CN with Ia = 0.2S, SCS UH PRF 484.
  - Subbasin peaks, ours vs HMS:

    | Subbasin | Ours (cfs) | HEC-HMS (cfs) |
    |---|---|---|
    | Area 1 | 2143 | 2149 |
    | Area 2 | 1956 | 1962 |
    | Area 3 | 1647 | 1652 |
    | Area 4 | 1889 | 1894 |
    | Area 5 | 1858 | 1861 |

  - Ours are consistently 0.16–0.31 % low. The likely cause is our renormalisation of the UH to exactly 1 in, plus how the percentage curve is interpolated to 5 minutes. Reaches/routing weren't modelled.
- **Not validated:** the Clark UH against HMS (only internal volume conservation was checked), and the recession baseflow.

## `ndpmp:pothole_storage` (core: `core/pothole.py`, wrapper: `algorithm_pothole.py`)
This needs the **WhiteboxTools** CLI binary (tested with v2.4.0). Install it with **one** of:
- `conda install -c conda-forge whitebox_tools` (what I used, in the QGIS conda env)
- `pip install whitebox`, then run `python -c "import whitebox; whitebox.WhiteboxTools()"` once so it downloads the binary
- set the env var `WBT_PATH`, or the algorithm parameter `WBT_EXE` (remembered in QgsSettings `nd_pmp/whitebox_tools`)

Inputs:
- `DEM`: projected CRS required
- `BASIN`: polygon
- `Z_TO_M`: DEM z units to metres
- `MIN_DEPTH_M`: default 0.05
- `MIN_AREA_M2`: default 100
- `OUTPUT_FOLDER`

Method:
1. Clip the DEM to the basin. The basin boundary acts as the spill boundary.
2. Run WhiteboxTools FillDepressions; depth = filled − DEM.
3. Clump the depression cells (8-connected) and drop small or shallow ones.
4. For each pothole compute area, max/mean depth, volume and spill elevation (the filled surface).
5. Contributing area: run FillDepressions(fix_flats) → D8Pointer → D8FlowAccumulation. The outlet is the exit cell with the largest accumulation. A cell contributes if its D8 path reaches the outlet without entering any pothole cell.

Outputs in `<out>/`:
- `potholes.gpkg`, layer `potholes` (MultiPolygon, DEM CRS). Fields: `pothole_id, area_m2, area_ac, max_depth_m, mean_depth_m, volume_m3, volume_acft, spill_elev (DEM z units), n_cells`.
- `potholes.csv`: same fields, no geometry.
- `pothole_summary.csv` (`metric,value`): `basin_area_mi2, basin_area_ac, cell_size_m, n_potholes, pothole_area_ac, total_storage_acft, total_storage_in_over_basin, max_pothole_depth_m, contributing_area_mi2, contributing_fraction, noncontributing_area_mi2, contributing_any_exit_mi2, outlet_x, outlet_y, outlet_accum_cells, min_depth_m, min_area_m2, dem`
- `contributing_area.tif`: 1 = contributes, 0 = does not, 255 = outside the basin.
- `depression_depth_m.tif`

### Tests (`tests/validate_pothole.py`, results in `validate_pothole.json`, `pothole_ws.json`)
- **Synthetic analytic cone** (1 m grid, radius 100 m, depth 2 m, level rim at 100 m):
  - volume 20,943.96 m³ vs analytic πr²h/3 = 20,943.95 m³
  - area 31,428 m² vs 31,416 m²
  - max depth 1.986 m vs 2.0 m (cell-centre sampling)
  - spill elevation 100.0
- **Real DEM:** USGS 3DEP 1/3″, fetched with the public ImageServer `exportImage` call (bbox −99.36,46.86 to −99.30,46.90; reprojected to UTM 14N at about 9.18 m). This is pothole-prairie terrain on the Missouri Coteau near Medina, ND.
- **Test basin:** a 3.317 mi² watershed I delineated from that DEM with WhiteboxTools. The pour point is the highest-accumulation cell, so this is not an official HUC or NHD basin. Results at the default thresholds:
  - 198 potholes covering 546 ac
  - storage 1,652 ac-ft, equal to 9.34 in over the basin
  - max depth 3.53 m
  - contributing area to the outlet 0.0005 mi² (≈0.01 %)
  - with thresholds 0.3 m / 1000 m²: 64 potholes, 1,580 ac-ft (8.93 in), contributing 0.0014 mi²
- **Box basin** (6.13 mi² rectangle, not a watershed): storage 2,632 ac-ft (8.05 in). Varying the thresholds over 0.05–0.5 m and 100–4047 m² gives 2,632–2,222 ac-ft.

### Limitations
- **The 3DEP lidar DEM shows ponded water as a flat surface,** so storage below the water level at the time of the lidar flight is not counted. The storage is only what lies above the water surface up to the spill point.
- **The contributing-area definition is strict.** Any depression on the flow path, however small, makes everything upstream non-contributing, so in Coteau terrain it comes out near 0 %. It is a fill-and-spill "no depression on path" estimate, not a dynamic one: it does not account for potholes overflowing once they are full, as they would in a PMF. Do **not** subtract it blindly as `NONCONTRIB_MI2` for PMF work.
- **Single-outlet assumption.** The synthetic radial test drains to many edges, and only 23 % reached the single "main outlet" (86 % reached some exit).
- **Run-to-run variation:** that synthetic contributing fraction was 0.218 in one run and 0.234 in another, from WhiteboxTools flat resolution. The storage numbers were stable.
- **WhiteboxTools 2.4.0 occasionally panics while writing GeoTIFF output.** The wrapper retries up to 4 times.
- I did not compare against ArcGIS, HEC-HMS or any published depression-storage study.

# v0.3.0 additions (internal prototype – not for publication)

Algorithm IDs: `ndpmp:pmp`, `ndpmp:inflow_hydrograph`, `ndpmp:pothole_storage`, **new** `ndpmp:spatial_distribution`,
`ndpmp:snowmelt`, `ndpmp:export_hms`.

**BREAKING:** hydrograph CSVs are now `time_hr,flow_cfs,precip_in,excess_in`. DamSafety_Runner's
`read_csv_series` reads the first two numeric columns, so the v0.2.0 order would have been read as precipitation.

## Fill-and-spill cascade (`core/cascade.py`)
`ndpmp:pothole_storage` now also writes `pothole_network.json`, also exposed as output `NETWORK_JSON`. It contains:
`{cell_m2, basin_m2, direct_to_outlet_m2, to_other_exit_m2, potholes:[{id, volume_m3, area_m2, catchment_m2, target}]}`.
- `target` is a pothole id, `0` for the basin outlet, or `-1` for another exit (the basin edge or NoData).
- It is found by walking D8 from the pothole until the walk reaches a different pothole or an exit.
- `catchment_m2` is the first-hit catchment.

`ndpmp:inflow_hydrograph` takes these parameters:
- `POTHOLE_MODE`: 0 none, 1 cascade, 2 abstraction.
- `POTHOLE_NETWORK`: the JSON file.
- `POTHOLE_INITIAL_FULL`: 0–1.
- `POTHOLE_STORAGE_IN`: abstraction-depth override.

The two modes:
- **Cascade:** for each time step, excess falls on every catchment and fills its pothole. Potholes are processed in topological order, and any overflow spills to the target pothole. Spill is instantaneous within the step, with no routing between potholes. The outlet excess, averaged over the basin, then goes through the UH.
- **Abstraction:** the first *capacity* inches of cumulative excess are removed.

Cascade mode writes `<out>/pothole_cascade/<storm>.csv`:
- Header lines: `# storm_id`, `# pothole_mode`, `# basin_area_mi2`, `# pothole_capacity_acft`.
- Columns: `time_hr,excess_in,outlet_excess_in,storage_loss_in,stored_acft,contributing_area_mi2,contributing_fraction,noncontributing_area_mi2,n_full`.

The summary CSV gains these columns: `pothole_mode, excess_before_potholes_in, pothole_capacity_in, pothole_stored_end_in, pothole_min_contrib_fraction, pothole_max_contrib_fraction`.
When potholes are on, `excess_in` is the excess after pothole storage.

**Medina test** (real 3DEP 1/3″ DEM, 3.3 mi² watershed). Settings: CN 80, SCS lag 2 h, Medina PMP synthetic hyetographs. Results are in `tests/test_cascade.py` and `cascade_results.json`.
Pothole capacity is 9.339 in (1643.6 ac-ft). Almost all of it is one terminal tree, pothole 3, which drains to the outlet.

| storm (P in) | none: excess / peak | abstraction | cascade | cascade, 50 % full |
|---|---|---|---|---|
| Local 6 h (22.03) | 19.29 in / 9196 cfs | 9.95 / 5278 | 9.95 / 5269 | 14.62 / 7450 |
| General 72 h (20.80) | 18.07 / 3176 | 8.74 / 2089 | 8.74 / 2080 | 13.41 / 3041 |
| Cool-season 72 h (12.63) | 10.06 / 1587 | 0.72 / 251 | 0.74 / 249 | 5.39 / 1198 |

General 72 h cascade details:
- Mass balance: 18.0741 in of excess equals 8.7354 in at the outlet plus 9.3387 in stored, to 4 decimal places.
- Contributing fraction: 0.18 % up to 34.5 h (direct-to-outlet cells only), above 50 % from 34.75 h, and 100 % at the end.

Cascade and abstraction agree closely here because one tree dominates. They will differ in basins with many independent pothole trees.

## `ndpmp:spatial_distribution` (`core/spatial.py`, port of Spatial_Distribution_v1_0.py)
Input is the `<Type>_PMP_Points_<basin>_<area>` layer from `ndpmp:pmp`. The tool works as follows:
- It shifts the SPAS total-storm raster so that its centre (taken from Storm_List Lon/Lat) sits on the basin centroid or on the given point.
- It samples the raster bilinearly at the points. NoData becomes 0, and those zeros stay in the mean, as in the original.
- It flattens the values as x^(1/5) and computes SAF = value / mean.
- It sets `PMP_xx = mean(PMP_xx) × SAF`.

Default patterns are the same as the original: LS 1209/1427/1699, GS 1527/1583, CS 1698/1733.

Outputs:
- `<out>/<LS|GS|CS>_PMP_SpatialDist_<SPAS>_<name>/` containing a points `.gpkg` with `PMP_*`, `<SPAS_ID>` (flattened value) and `SAF_<id>` fields, plus one GeoTIFF per duration at 0.025°.
- `spatial_distribution_summary.csv`.

Tests:
- Medina (6 points): SAF ranges 0.996–1.004.
- two_sub_utm cool-season (218 points): SAF 0.968–1.023 for SPAS_1698 and 0.907–1.055 for SPAS_1733. The PMP_24 mean of 9.578 is preserved; the maximum is 10.10.
- Not compared against ArcGIS output, because no reference output is available.

## `ndpmp:snowmelt` (`core/snowmelt.py`, port of Snowmelt_Runoff_Tool.py)
This is a degree-day melt of `SWE.gdb/ND_Final_100yr_SWE_<mmdd>` using `Temperature.gdb/Ta_ave_Daymet_1981_2019_<mmdd>`:
- Ta is multiplied by 1.075 on day 2 and on day rainDur+3, and by 1.15 on days 3 to rainDur+2.
- DD = max(Ta − 32, 0).
- Melt = DD × Cm, capped by the starting SWE.
- The basin mask uses cell centres, with an all-touched fallback for basins smaller than a cell.

Outputs:
- `Basin_Ave_<basin>_<mm_dd>.csv` with columns `Day,Ta,DegreeDays,Cm,SWE,Melt,MeltAccum,TaFactor`. Values are rounded to 2 decimals as in the original, and SWE is the value before that day's melt.
- `Sample_Point_*.csv`.
- Optional daily GeoTIFFs.

If `HYETOGRAPH_FOLDER` is given, the **rain-window melt** (the sum of Melt over days 3 to rainDur+2) is written into `# snowmelt_in:` of the cool-season hyetographs. Only that header line changes.
The original tool does not define which melt value goes with the PMP; this choice is ours and should be reviewed.
One defect in the original was not reproduced: it advances the date with `datetime.timedelta`, but only `timedelta` is imported.

Medina test (Mar 15 – Apr 15, rain 3 d, Cm 0.06; 2 grid cells):
- Starting SWE is 5.41 in.
- Total melt is 5.41 in, so the pack is gone by Apr 15.
- Rain-window melt (Mar 17–19) is 0.34 in. This value was written to 21 cool-season hyetographs in the test copy.

## `ndpmp:export_hms` (`core/hms.py`, `algorithm_hms.py`)
It takes the same parameters as `inflow_hydrograph`, plus `PROJECT`, `RUN` and `HMS_HOME`.

What it writes:
- A HEC-HMS 4.13 project: `.hms`, `Basin_<p>.basin` (one subbasin draining to the sink `Outlet`), and one `M_*.met` / `C_*.control` / `R_*` run per hyetograph.
- `.gage` entries: External DSS, `/NDPMP/<storm>/PRECIP-INC/01JAN2000/<dt>/PMP/`.
- `run_hms.py` (Jython). It writes the precipitation to `<p>.dss` (DSS7), computes all runs, and writes `hms_peaks.csv` with columns `storm_id,run,hms_peak_cfs,hms_time_to_peak_hr,hms_volume_in,status`.

Run it with `hec-hms.sh -s run_hms.py`, or set `RUN=true`. `hms_volume_in` is currently left blank.

Mapped methods:
- SCS CN, with Initial Abstraction = Ia ratio × S.
- Initial+Constant.
- SCS UH (STANDARD, or PRFnnn) or Clark (Specified).

Not mapped (a warning is printed):
- Baseflow other than 0; HMS baseflow is set to None.
- Peaking %.
- Cascade.

Pothole **abstraction** is exported as Simple Surface storage. This is **not equivalent**: HMS fills surface storage from precipitation before the loss method.

HEC-HMS 4.13 Linux (with its bundled JRE) was used on the build machine; set its folder in the tool or with `ND_PMP_HMS_HOME`. Peak comparison, HMS vs ndpmp (Medina hyetographs; `tests/test_hms.py`):

| case | Local 6 h | General 72 h | Cool-season 72 h |
|---|---|---|---|
| SCS CN 80, lag 2 h | 9215.1 vs 9196.5 (−0.20 %) | 3180.0 vs 3176.5 (−0.11 %) | 1588.9 vs 1587.3 (−0.10 %) |
| Init 1.0 / 0.1 in/h, Clark Tc 3 R 2 | 8102.0 vs 8062.6 (−0.49 %) | 2914.4 vs 2903.3 (−0.38 %) | 1410.8 vs 1405.6 (−0.37 %) |
| SCS + pothole abstraction | 5190 vs 5278 (+1.7 %) | 2071 vs 2089 (+0.9 %) | 45 vs 251 (+457 %, different ordering) |

Times to peak are identical in the first two cases.
HMS logs a non-fatal "Premature end of file" XML parse error on start-up; it did not affect the results.

## DamSafety_Runner integration (read-only)
`tests/e2e_damsafety.py` imports `DamSafety_Runner.core.flood` and `core.breach` without modifying them. It runs:
- `FloodInputs.from_files(<our hydrograph CSV>, spillway_rating.csv, normal_pool_ft=20, crest_length_ft=800)`
- `route_flood(DamData(...), "overtopping", None, fi)`

The dam is **hypothetical**: 30 ft high, top of dam 30 ft, 1500 ac-ft, 120 ac, a 100-ft weir spillway at 20 ft (C 3.0), and an 800-ft crest.
Freeboard = top of dam − `peak_pool_ft_no_breach`.

| inflow | inflow peak read | peak pool | no-breach outflow | freeboard |
|---|---|---|---|---|
| Local 6 h, no potholes | 9196 | 28.81 ft | 7850 cfs | 1.19 ft |
| General 72 h, no potholes | 3176 | 24.62 | 2979 | 5.38 |
| Local 6 h, cascade | 5269 | 25.93 | 4333 | 4.07 |
| General 72 h, cascade | 2080 | 23.44 | 1916 | 6.56 |

The `failed=True` / breach rows come from their default `trigger="peak"`, which breaches at the peak pool. That is not an overtopping result.
