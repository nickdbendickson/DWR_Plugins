# Absaraka Dam (DWR-2801) on the Mac — same settings as Grok Bot

The settings below are the ones Grok Bot used for the DWR-2801 NickReview rerun
(DWR_Plugins main cc5892e, Runner 3.10.83). The machine-readable copy is
`grok_settings_DWR-2801.json`. This package installs the newer plugins from main
a83ad04 (Runner 3.10.86).

## 1. Get the data (once)

Put the files listed in `DATA_TO_COPY_DWR-2801.md` under
`~/dwr-dam-suite/NickReview_data/` (keep the sub-folders):

* 1.06 GB to copy from the box (run kit, culverts, box-made DEMs, NHD waterbodies)
* 9.79 GB of USGS 1 m LiDAR tiles: copy them, or download them with
  `bash get_lidar_tiles.sh` (in the package folder; resumes and checks every size)

## 2. Run it from Terminal (recommended: exactly Grok's inputs)

```bash
cd ~/Downloads/DWR_Mac_Setup          # the unzipped package
bash run_nickreview.sh --dam DWR-2801 --dry-run   # check paths; runs nothing
bash run_nickreview.sh --dam DWR-2801             # the real run (hours)
```

Results go to `~/dwr-dam-suite/Dams/NickReview_mac/dams/DWR-2801/`. The dam's log is
`.../NickReview_mac/work/DWR-2801/run_DWR-2801.log`, the whole run's log is in
`.../NickReview_mac/logs/`, and `.../NickReview_mac/manifest_<time>.json` records the
versions, settings and the sha256 of every input file.
Run it again and finished stages are skipped; `--redo` runs them again.

## 3. Or in QGIS (Runner dialog)

Open **Open DWR QGIS.command** in `~/dwr-dam-suite`, then
Processing ▸ DamSafety ▸ **Run dam**. `<DATA>` = `~/dwr-dam-suite/NickReview_data`.

| field | value |
|---|---|
| Dam | DWR-2801, Swan Buffalo Detention Dam 12, NID ND00199 |
| Dam point | -106343.832, 2666591.505 [EPSG:5070] |
| Height / storage / area | 27.5 ft / 1615.496 ac-ft / **154.5 ac** (DWR max pool area) |
| Top of dam | 1069.117 ft |
| Method / scenario | 0 / 0, baseline on, stop rule off |
| DEM | `<DATA>/nickreview_rerun_20261023/dwr2801/DWR-2801_composite_1m_full_native_v2.vrt`, Z in meters |
| Structures | `<DATA>/culvert_precheck_20261024/dwr2801/structures_2801_with9122221_invfix2.gpkg`, layer `runner_culverts` |
| Structure fields | kind, span_ft, rise_ft, barrels, shape, invert_up, invert_dn, name |
| Culvert inventory | `nddot_centerline_pipes.gpkg`, `nddot_bridge_inventory.gpkg`, `ntad_nbi.gpkg` (in `<DATA>/culvert_sources`), match 75 m, burn matched off |
| Receptors | `pack_NSI_structures_2801`, `pack_ND_roads_NG911_2801`, `pack_NARN_rail_lines_2801` (in `<DATA>/nickreview_rerun_20261023/dwr2801`) |
| Water mask | `<DATA>/ND_Dam_Suite_Pack/hydro/NHD_waterbodies.gpkg` |
| Engine | Tier 1, **force ANUGA on**, cell 30 m, Manning 0.06 |
| Reach | length 16 mi, half width 6 mi, step 3 mi, max extent 20 mi, max runs 1 |
| Arrival / edge tolerance | 0.1 ft / 1 ft |
| Duration | 0 (auto), max 72 h |
| Work CRS / ANUGA buffer | EPSG:5070 / 2000 m |
| Threads | 8 (the box used 6; threads do not change the results) |
| Keep files / products | on / on |

Then **Hazard class** (DAM_FOLDER = the DWR-2801 results folder): lethality 0,
wet 0.1 ft, unknown AADT 1, unknown trains 1, passenger codes A,B,C, with the
`pack_*_2801.gpkg` layers listed in `grok_settings_DWR-2801.json`. Then **Hazard
batch**: downloads on, report on, EAP off, 1 worker, 90 min timeout. Leave
Hazard's offline_pack setting **empty**.

Also set (the Terminal script does this for you) `NDBREACH_ANUGA_MESHER=gmsh` and
`NDBREACH_DEM_RESAMPLE=average`.

## What changed from the older Absaraka reference

* area 154.5 ac (was 111.643), storage 1615.496 ac-ft (seed had 1615.344)
* DEM: the full native 1 m v2 VRT (all 412 culvert ends on native 1 m)
* structures: invfix2 (NBI 9122221 as a 2-barrel 2.97 × 2.01 m box; invert fixes on fid 257/263/280)
* water mask: NHD waterbodies; threads 8

Not possible on a Mac: Tier 2 / HEC-RAS, and Metal GPU (LISFLOOD-FP 8.3 has none).
After the run, first check the maximum velocity / time step at NBI 9122221.
