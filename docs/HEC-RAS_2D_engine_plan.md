# HEC-RAS 2D as a third Runner engine: design and plan

Status: DRAFT design, no code. Applies to DamSafety Runner (`ndbreach`, `TIER`) and Screening tool 7b (`ENGINE`, `core/handoff.py`).
Hard rule: the existing choices (`LISFLOOD-FP only` = `TIER_LISFLOOD`, `LISFLOOD-FP then ANUGA` = `TIER_BOTH`) keep their defaults, code paths and byte-identical results. HEC-RAS is opt-in only.

## 1. Summary

- **Why:** HEC-RAS 2D's subgrid terrain lets a 60–150 ft cell carry hydraulic property tables built from the 1–5 m LiDAR, so channels, road fills and culvert openings show up in the results without ANUGA's fine mesh and long runs.
- **What already exists in `main`:** most of the model writer and the results reader.
  - `core/hecras_model.py::build_model()` writes a full dam-breach project: `.prj/.g01/.u01/.p01/.rasmap`, `GIS/Projection.prj`, `Terrain/<name> Terrain.tif`. The reservoir is a storage area, the dam is an SA/2D connection with the breach, and there's a downstream 2D area, a normal-depth BC, and road culverts, bridges and notches as SA/2D connections inside the 2D area (v3.12.0).
  - `core/hecras.py::read_mesh / to_grids / import_as_reference` read a `.p##.hdf` (max WSE, velocity, arrival) into `Grid`s with h5py.
  - `algorithms_hecras.py` has the export and import tools. Imports are named `HECRAS Depth (Max).tif` and so on.
- **What's missing** is the middle step: turning text files into a runnable `.p##.tmp.hdf`/`.b##`/`.x##`. That means terrain HDF, mesh generation, property tables and the geometry HDF. HEC only ships that step on Windows (Ras.exe / RAS Mapper / RasProcess.exe).
- **Verdict:** the unsteady compute runs natively on Linux, using HEC's official build. **Terrain and mesh preprocessing can't be done natively on Linux.** It needs Windows HEC-RAS, run either natively or under Wine through ras-commander. Writing the terrain HDF or the property tables ourselves with GDAL/h5py isn't viable: it's a proprietary, versioned format.
- **Recommended path:** (1) The engine runs natively on **Windows**, where QGIS + HEC-RAS 6.6 are installed: preprocess with RasProcess/ras-commander, then compute with `Ras.exe`/RasUnsteady. (2) **Linux:** preprocess under Wine (experimental, qualified per HEC version), then compute with the official Linux `RasUnsteady`. (3) **macOS:** not supported. The engine reports "unavailable" and the user can still export the project.

## 2. Findings (sources checked 2026-10-16)

### 2.1 Official Linux build
- HEC's download page lists "HEC-RAS 6.6 Linux", a 213 MB zip that HEC won't support beyond its release notes. 7.0/7.0.1 ship as `HEC-RAS_70x_with_Linux_Setup.exe`. <https://www.hec.usace.army.mil/software/hec-ras/download.aspx>
- Release notes: `RasGeomPreprocess`, `RasUnsteady` and `RasSteady`, built for x64 with Intel oneAPI on RHEL 8, plus a Muncie example. <https://www.hec.usace.army.mil/software/hec-ras/documentation/HEC-RAS_66_Linux_Build_Release_Notes.pdf>
- **Probe on the box** (`/workspace/hecras_probe/Linux_RAS_v66`, from `https://www.hec.usace.army.mil/software/hec-ras/downloads/Linux_RAS_v66.zip`): with `LD_LIBRARY_PATH=libs:libs/mkl:libs/rhel_8`, `ldd` shows no missing libraries. `RasUnsteady` starts and prints "Performing Unsteady Flow Simulation HEC-RAS 6.6 September 2024". There's no `--help`: it treats the argument as an HDF file name. No model was run.
- **Inputs it needs**, from the Muncie scripts: `RasGeomPreprocess <name>.p04.tmp.hdf x04` and `RasUnsteady <name>.p04.tmp.hdf x04`. `wrk_source/` holds `Muncie.p04.tmp.hdf`, `.b04`, `.x04` and `.r04`. Those files are written by **Windows** HEC-RAS when a plan is "computed" (the text-to-HDF compile). The Linux preprocessor only "updates the hydraulic property tables" in an existing `.tmp.hdf`. It can't build a mesh, a terrain or the `.tmp.hdf` from `.g01` text.
- Known issue in 6.6: "Cannot recompute 2D Property Tables – No Terrain is associated with this geometry" when the terrain is outside the project folder. Keep `Terrain/` inside the project. <https://www.hec.usace.army.mil/confluence/rasdocs/raski/6.6>

### 2.2 Headless preprocessing options
| Option | Verdict |
|---|---|
| Write terrain HDF and property tables ourselves (GDAL + h5py) | **No.** Proprietary format with pyramids, TIN stitching and version-specific layout. `hecras_model.py` already notes it can't be made outside HEC-RAS. Very risky to maintain. |
| `RasProcess.exe CreateTerrain units=Feet stitch=true prj=… out=… <tif>` (undocumented CLI) | **Yes on Windows**, and under Wine on Linux. ras-commander wraps it as `RasTerrain.create_terrain_hdf()`. GDAL/PROJ environment variables must be cleared, or it silently writes a 1.8 KB stub HDF. <https://github.com/gpt-cmdr/ras-commander/blob/93984c8f/ras_commander/terrain/RasTerrain.py>, <https://github.com/gpt-cmdr/ras-commander/commit/0159686cda8cd6f041138ee6671574a0ac93624b> |
| Mesh + property tables: ras-commander `GeomMesh` (calls RasMapperLib .NET through pythonnet) | **Yes on Windows** with HEC-RAS 6.6 installed. On Linux it needs Wine + .NET in the prefix, and is "qualified, not guaranteed". <https://pypi.org/project/ras-commander/>, <https://rascommander.info/ras/notebooks/511_headless_linux_wine_ras2cng/> |
| `.tmp.hdf/.b##/.x##` compile | Windows `Ras.exe` command line (ras-commander `RasCmdr.compute_plan`, `RasPreprocess.preprocess_plan`). Under Wine on Linux; then the official Linux `RasUnsteady` computes. <https://rascommander.info/ras/development/release-notes/> |
| Stored maps (depth/WSE/velocity TIFs) | Not needed. We rasterise from the HDF ourselves with `hecras.read_mesh/to_grids` onto the Runner grid, so mapping is the same everywhere and there's no RAS Mapper render-mode dependency. (ras2cng: <https://rascommander.info/ras2cng/user-guide/linux-wine-setup/>) |
| "Windows-only prep, Linux compute" split | Works: prep on a Windows PC, copy the folder, compute on Linux. It's a good fallback for the box, but it's a manual step. |

### 2.3 Python tooling
| Package | Licence | Platform | Use |
|---|---|---|---|
| h5py | BSD-3 | all | Already used by `hecras.py`; optional dependency. |
| ras-commander (CLB Engineering) | MIT | Windows full; Linux compute + Wine for RasMapper parts | Terrain, mesh, compute, HDF helpers. Optional, never a hard dependency. <https://github.com/gpt-cmdr/ras-commander> |
| rashdf (FEMA-FFRD) | MIT | all, read-only | Cross-check for our reader. <https://github.com/fema-ffrd/rashdf> |
| pyHMT2D | MIT | all, but its RAS control is Windows COM | Reference only. |
| pythonnet | MIT | needs .NET/Mono | Only for GeomMesh. |

Policy: keep our own pure-numpy reader (`hecras.py`) as the path that's always available. Load ras-commander lazily, only for preprocessing. Pin the version and test it against the HEC-RAS version found.

### 2.4 Licensing and availability
- HEC-RAS is US Government (USACE) software and free to download. Its distribution terms say it's provided without warranty or support. We should **not bundle** HEC binaries in the plugins or the offline pack: users install HEC-RAS themselves, and we detect it. The Linux zip comes with "no support beyond the release notes". Re-check the 7.x Linux installer's terms before recommending it.
- Windows: full, with the HEC-RAS 6.x/7.x installer. Linux: compute natively, preprocessing under Wine (experimental). macOS: none; export only.

## 3. Architecture

```
Runner TIER (algorithms.py:490)                    Screening 7b ENGINE (core/handoff.py ENGINE_LABELS)
  0 LISFLOOD-FP only        (default, unchanged)     0 LISFLOOD-FP only (default)
  1 LISFLOOD-FP then ANUGA  (unchanged)               1 LISFLOOD-FP -> ANUGA
  2 LISFLOOD-FP then HEC-RAS 2D  (NEW, opt-in)        2 LISFLOOD-FP -> HEC-RAS 2D (NEW) -> sends TIER 2
```
- New constant `workflow.TIER_HECRAS = "lisflood+hecras"`. It's appended at the end of the enum, so saved indices 0/1 keep their meaning. `handoff.effective_engine()` maps 2 straight through, and the legacy FORCE_ANUGA still maps to 1.
- LISFLOOD still runs first to set the extent, the hydrograph and the arrival reference, just as `TIER_BOTH` does at `workflow.py:1221`. The HEC-RAS stage then replaces the final arrays the way the ANUGA stage does.
- New module `core/hecras_engine.py`:
  - `detect()` returns `{available, platform, ras_exe, linux_unsteady, rasprocess, ras_commander, wine, version, reason}`. Settings/env: `NDBREACH_HECRAS_DIR`, `NDBREACH_HECRAS_LINUX_DIR`, `NDBREACH_WINE_PREFIX`.
  - `write_project(dam_folder, out, settings)` is a thin wrapper over `hecras_model.build_model()`, with options for cell size, breaklines and BC.
  - `preprocess(project)`: terrain HDF, mesh, property tables and the `.tmp.hdf/.b/.x` compile (Windows native, or Wine).
  - `compute(project)`: `Ras.exe -c` on Windows, or Linux `RasUnsteady <p>.tmp.hdf x01` and then rename to `.p01.hdf`. It runs under the existing `solve_gate` (pre-run smoke, timeouts).
  - `results(hdf, like_grid, arrival_m)` calls `hecras.import_as_reference()` and returns the same dict `_final_arrays()` builds, which is written with `FINAL_NAMES` (`Depth (Max).tif`, `Velocity (Max).tif`, `Arrival Time.tif`, `WSE (Max).tif`) on the Runner grid. Hazard therefore reads the same file names, units (ft, ft/s, h after breach initiation) and grid.
- `run_report.json` gets `"engine": "hecras"`, the HEC-RAS version, platform, mesh cell count, terrain resolution and a compute log path. `run_report.md` gets a line for them.

## 4. Pipeline (TIER 2)
1. Run LISFLOOD as now, which gives the extent, the breach hydrograph and the `model_dem()` terrain.
2. **Terrain:** take the *native* conditioned 1–5 m DEM (not the 40 m `model_dem`) clipped to the 2D-area buffer. Apply the culvert burn when `CULVERT_BURN_MATCHED`. Write it as GeoTIFF in the model CRS (ND State Plane ft), then `CreateTerrain` into `Terrain/<name>.hdf`.
3. **Geometry** (already in `build_model`): the reservoir storage area with the elevation-volume curve, the dam SA/2D connection with breach parameters from `breach.py`, and a 2D area perimeter from the LISFLOOD extent plus a buffer.
   - Mesh: base cell 100 ft by default (option 50–200), with **breaklines along NG911 road centrelines** and the dam line (near spacing about 30 ft) and stream centrelines from the culvert trace.
   - Alternative inflow mode: a flow-hydrograph BC on a short line at the existing downstream-side `inflow_xy`, using the Runner's breach hydrograph instead of SA routing. This mode is for exact parity with the LISFLOOD/ANUGA forcing. **The default for comparisons is the hydrograph BC.** The SA/2D breach mode is an option.
4. **Outflow:** normal-depth BC lines wherever the perimeter crosses the LISFLOOD wet edge downstream, with slope from the valley (already done for one BC; extend this to several exits).
5. **Structures:** from `structures.json` and the culvert matches (`source`, span, rise, barrels, shape, inverts from LiDAR). Each one becomes an SA/2D connection inside the 2D area: a weir from the road profile plus a culvert group, as v3.12.0 already writes it. Bridges and notches become weir gaps. Matches without inverts fall back to a breakline only, and the report says so.
6. **Preprocess:** mesh, property tables and the compile into `.p01.tmp.hdf/.b01/.x01`.
7. **Compute** with the platform solver, logging and timeouts handled through solve_gate.
8. **Results:** read the `.p01.hdf`, rasterise it onto the Runner grid, and write `FINAL_NAMES`. Arrival uses the same depth threshold and t0 as LISFLOOD/ANUGA. Keep `hecras/` in the dam folder for review in RAS Mapper.

## 5. Blockers and workarounds
| # | Blocker | Workaround |
|---|---|---|
| B1 | Terrain HDF only from RAS Mapper/RasProcess (Windows) | RasProcess CreateTerrain, native or under Wine. No home-made HDF. |
| B2 | Mesh + property tables + `.tmp.hdf` compile only from Windows .NET/Ras.exe | ras-commander GeomMesh + RasPreprocess on Windows or Wine. Fallback: a "Prepare on Windows" export that the Linux compute then runs. |
| B3 | Linux build unsupported, 6.6 only, RHEL 8 | Detect it, smoke test it (solve_gate) and report the version. Wine prep must use the **same** HEC-RAS version as the Linux solver. |
| B4 | RAS text format drift across 6.x/7.x | `PROGRAM_VERSION` is pinned to 6.6 and 6.10; refuse unknown versions with a clear message. |
| B5 | Mesh quality failures (small faces near breaklines) | GeomMesh auto-fix loop; report the cell count and fixes made. |
| B6 | QGIS Python lacks h5py/pythonnet | Optional; detect() says how to install them; DamSafety Setup can install them later. |
| B7 | macOS | Unavailable; export only. |

## 6. Risks
- Results differ from LISFLOOD/ANUGA because of the physics (diffusion wave vs SWE, subgrid). That's expected, so set tolerances (§7) before trusting the class.
- Wine fragility and an undocumented RasProcess CLI. Keep it optional and gated on a qualification fixture.
- Run time and disk: the terrain HDF for a 1 m DEM over tens of square miles can be several GB. Clip it to the buffer and allow 2–3 m resampling.
- Concurrency: one heavy model at a time on the box (Grok Bot's rule). The HEC-RAS stage counts as heavy.
- Defaults must stay untouched. That's guarded by tests that assert `TIER` default 0, the enum order, and byte-identical LISFLOOD outputs.

## 7. Test plan
- **Unit (PR 1, light):** `detect()` with fake directories and env vars; the TIER/ENGINE enum order and defaults; `effective_engine(2)`; `write_project()` on a synthetic 20×20 DEM, checking keywords and CRLF; `results()` on a **tiny synthetic `.p01.hdf`** written with h5py (4 cells, 3 times), checking depth/WSE/velocity/arrival rasters, units, grid and names; "HEC-RAS unavailable" leaves TIER 0/1 untouched.
- **Integration (Grok Bot, heavy):** (1) the Linux Muncie example from the zip with `run_unsteady.sh`, as a solver smoke test; (2) Absaraka with TIER 0, 1 and 2, comparing flooded acres, max depth, arrival at the hazard receptors, and hazard class with `benchmark.compare_fields`; (3) DWR-3161 with culvert matches burned and not burned; (4) a road-overtopping case with a structure connection.
- **Acceptance:** the hazard class matches ANUGA on Absaraka. Flooded area within ±10% and max depth within ±1 ft of ANUGA. The run is at least 3× faster than ANUGA. TIER 0/1 outputs stay byte-identical to `main`.

## 8. Staged PRs
1. **PR 1 (minimal, off by default):** `core/hecras_engine.py` with `detect()`, `write_project()` (wrapping build_model, adding the hydrograph-BC inflow mode) and `results()`; unit tests with the synthetic HDF. **No** TIER option exposed yet, and no change to workflow.py.
2. **PR 2:** preprocessing adapters (Windows native RasProcess/ras-commander; Wine on Linux), a `hecras_prepare` Processing tool that only prepares a project, and the Linux `RasUnsteady` compute adapter behind solve_gate.
3. **PR 3:** `TIER_HECRAS` in workflow and the Runner enum (index 2), 7b `ENGINE` index 2 with an availability check like `ANUGA_MISSING`, report fields, help text and version bumps.
4. **PR 4:** road breaklines, multiple normal-depth exits, structure connections from culvert matches, and tuning from the Absaraka comparison.
5. **PR 5:** docs, Setup installer support (h5py, optional ras-commander) and a qualification fixture.

## 9. What Grok Bot needs to run (after PR 2/3)
- On the box: `/workspace/hecras_probe/Linux_RAS_v66` (already downloaded; set `NDBREACH_HECRAS_LINUX_DIR`).
- For preprocessing on Linux: Wine 8+ in a 64-bit prefix, with HEC-RAS 6.6 **Windows** installed into it and ras-commander + pythonnet. Or a project prepared on a Windows PC.
- h5py in the Runner's Python.
- Runs: the Muncie smoke test first, then Absaraka TIER 0/1/2, then DWR-3161.
