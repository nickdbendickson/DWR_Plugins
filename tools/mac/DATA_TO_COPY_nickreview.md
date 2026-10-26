# Data to copy for all 42 NickReview dams

Put every file at `~/dwr-dam-suite/NickReview_data/<copy_to>` (same sub-folders). The scripts only read this folder.

- **1109 files, 32.69 GB** in total.
- 9.79 GB of that is USGS 3DEP 1 m LiDAR that can be downloaded instead of copied; the rest (22.90 GB) must be copied from the box.
  To download them: `bash get_lidar_tiles.sh data_files_nickreview.tsv` (all 36 URLs and sizes checked against USGS on 2026-10-26).
- The full per-file list (size, path, source) is `data_files_nickreview.tsv`.

| folder | files | size | source |
|---|---:|---:|---|
| `DEM/lidar_1m_native` | 36 | 9.79 GB | live download possible (USGS 3DEP 1 m, The National Map) or copy |
| `nickreview_rerun_20261023/tiles/DWR-3034` | 23 | 5.79 GB | copy (box-made 1 m tiles) |
| `DEM/NickReview_runner_1m` | 875 | 5.46 GB | copy (box-made DEM: 3DEP averaged/clipped) |
| `nickreview_rerun_20261023/tiles/DWR-2865` | 24 | 5.35 GB | copy (box-made 1 m tiles) |
| `DEM/NickReview_runner_10m` | 40 | 2.65 GB | copy (box-made DEM: 3DEP averaged/clipped) |
| `offline_packs_20261021/hazard_pack` | 1 | 1.95 GB | offline pack 20261021 (copy; read only, Hazard offline_pack setting stays empty) |
| `ND_Dam_Suite_Pack/hydro` | 3 | 0.65 GB | ND_Dam_Suite_Pack (copy, or point --map at your own copy) |
| `ND_Dam_Suite_Pack/roads` | 5 | 0.29 GB | ND_Dam_Suite_Pack (copy, or point --map at your own copy) |
| `DEM/Cass_runner_10m` | 1 | 0.25 GB | copy (box-made DEM: 3DEP averaged/clipped) |
| `ND_Dam_Suite_Pack/structures` | 1 | 0.24 GB | ND_Dam_Suite_Pack (copy, or point --map at your own copy) |
| `DEM/Eddy_runner_10m` | 1 | 0.20 GB | copy (box-made DEM: 3DEP averaged/clipped) |
| `nickreview_rerun_20261023/dwr3034` | 17 | 32.7 MB | copy (box run kit: settings / DEM VRT) |
| `nickreview_rerun_20261023/dwr2801` | 13 | 10.3 MB | copy (box run kit: settings / DEM VRT) |
| `ND_Dam_Suite_Pack/dwr` | 1 | 7.3 MB | ND_Dam_Suite_Pack (copy, or point --map at your own copy) |
| `nickreview_rerun_20261023/dwr2865` | 17 | 6.2 MB | copy (box run kit: settings / DEM VRT) |
| `culvert_sources/research` | 1 | 5.3 MB | copy (box-made from NDDOT / NTAD downloads) |
| `ND_Dam_Suite_Pack/railroads` | 1 | 1.8 MB | ND_Dam_Suite_Pack (copy, or point --map at your own copy) |
| `culvert_precheck_20261024/dwr2801` | 1 | 0.3 MB | copy (box culvert fix: invfix2 + NBI 9122221) |
| `ND_Dam_Suite_Pack/lifelines` | 2 | 0.2 MB | ND_Dam_Suite_Pack (copy, or point --map at your own copy) |
| `Dams/NickReview` | 42 | 0.2 MB | copy (NickReview screening record) |
| `culvert_sources/nddot_bridge_inventory.gpkg` | 1 | 0.1 MB | copy (box-made from NDDOT / NTAD downloads) |
| `culvert_sources/ntad_nbi.gpkg` | 1 | 0.1 MB | copy (box-made from NDDOT / NTAD downloads) |
| `nickreview_rerun_20261023/dwr2801_maincc5892e` | 2 | 0.0 MB | copy (box run kit: settings / DEM VRT) |
