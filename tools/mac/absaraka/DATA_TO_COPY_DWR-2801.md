# Data to copy for Absaraka (DWR-2801)

Put every file at `~/dwr-dam-suite/NickReview_data/<copy_to>` (same sub-folders). The scripts only read this folder.

- **117 files, 10.85 GB** in total.
- 9.79 GB of that is USGS 3DEP 1 m LiDAR that can be downloaded instead of copied; the rest (1.06 GB) must be copied from the box.
  To download them: `bash get_lidar_tiles.sh` (all 36 URLs and sizes checked against USGS on 2026-10-26).
- The full per-file list (size, path, source) is `data_files_DWR-2801.tsv`.
- Plus **3 Screening 7b record inputs** in `Dams/NickReview/screen_maxstor/` (`8_hazard.gpkg`, `4_breach_points.gpkg` and the `3_stage_storage/curve_<ID>.csv`; a dam without a curve on the box has none to copy). They were added to the lists after the sizes were measured, so their size is `?` and they are not in the totals above; on the box, `nickreview_mac.py --list-data` writes both lists again with every size (the command is in README_MAC.md, section 3).

| folder | files | size | source |
|---|---:|---:|---|
| `DEM/lidar_1m_native` | 36 | 9.79 GB | live download possible (USGS 3DEP 1 m, The National Map) or copy |
| `DEM/NickReview_runner_1m` | 57 | 0.41 GB | copy (box-made DEM: 3DEP averaged/clipped) |
| `ND_Dam_Suite_Pack/hydro` | 1 | 0.29 GB | ND_Dam_Suite_Pack (copy, or point --map at your own copy) |
| `DEM/Cass_runner_10m` | 1 | 0.25 GB | copy (box-made DEM: 3DEP averaged/clipped) |
| `DEM/NickReview_runner_10m` | 2 | 0.09 GB | copy (box-made DEM: 3DEP averaged/clipped) |
| `nickreview_rerun_20261023/dwr2801` | 13 | 10.3 MB | copy (box run kit: settings / DEM VRT) |
| `culvert_sources/research` | 1 | 5.3 MB | copy (box-made from NDDOT / NTAD downloads) |
| `culvert_precheck_20261024/dwr2801` | 1 | 0.3 MB | copy (box culvert fix: invfix2 + NBI 9122221) |
| `culvert_sources/nddot_bridge_inventory.gpkg` | 1 | 0.1 MB | copy (box-made from NDDOT / NTAD downloads) |
| `culvert_sources/ntad_nbi.gpkg` | 1 | 0.1 MB | copy (box-made from NDDOT / NTAD downloads) |
| `Dams/NickReview` | 1 | 0.0 MB | copy (NickReview screening record) |
| `nickreview_rerun_20261023/dwr2801_maincc5892e` | 2 | 0.0 MB | copy (box run kit: settings / DEM VRT) |
| `Dams/NickReview/screen_maxstor` | 3 | ? | copy (NickReview max-storage screening: Screening 7b record input) |
