# DWR-3161 / NID ND00597 (Sanish Bay WPA Dam 1): record check, 2026-10-19
Read-only. Nothing outside this folder was written.
Copied into the repo for the flagged-structure entry (docs/Flagged_structures.md). Not copied: cop30_clip.tif and usgs_img.png (size) and the scratch scripts plot_map.py, storage.py, transects.py (they read box-local paths).

Files
- DWR-3161_record_check_map.png: imagery map, hillshade map, N-S profile through the ND-23 culvert
- pack_records.txt: DWR_dams, DWR_dam_lines and NID_dams rows from ND_Dam_Suite_Pack (read only)
- nid_api_inventory.json: https://nid.sec.usace.army.mil/api/dams/ND00597/inventory (fetched 2026-10-19)
- nbi_0023048401.txt: NBI record for the ND-23 culvert at the dam (pack roads/NBI_bridges.gpkg)
- transects.json / transects.py: N-S profiles (3DEP 10 m DEM vs Copernicus GLO-30)
- storage_south_10m.json / storage.py: elevation-area-storage south of ND-23 from the 10 m DEM, with ND-23 (HPMS) as a wall
- cop30_clip.tif: Copernicus GLO-30 DSM clip (AWS copernicus-dem-30m, EGM2008)
- usgs_img.png: USGS USGSImageryOnly export, 1.4 km box centred on the dam point, EPSG:5070
- plot_map.py: the map script (qgis334 env, PROJ_DATA=/usr/share/proj)

Key results (south pool, 10 m DEM 1966 source, NAVD88; volume above the DEM surface)
elev ft : area ac / storage af
1844.0 : 22.8 / 53 ; 1844.5 : 24.4 / 65 ; 1848.0 : 33.9 / 167 ; 1849.5 : 37.4 / 220
1854.0 : 47.4 / 411 ; 1860.0 : 68.4 / 749 ; 1868.6 : 215.7 / 2,056 ; 1870.1 : 255.0 / 2,407 ; 1872.0 : 355.3 / 3,007
Datum: NGVD29 -> NAVD88 = +0.459 m (+1.51 ft) at the dam (NOAA NCAT/VERTCON 3.0).
GEOID18 N = -18.051 m (NGS API); EGM2008 N about -17.76 m (PROJ), so Copernicus EGM2008 heights are about 1 ft below NAVD88 (inferred, +/-2 ft).
