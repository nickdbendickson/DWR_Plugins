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

### 3a. The DEM first (do not use "Download a DEM" with USGS 3DEP at 1 m)

Grok's run used **36 USGS 1 m lidar tiles** (10 km × 10 km each, x60–65 × y518–523,
9.79 GB). The Runner's *Download a DEM* box with **USGS 3DEP + 1 m** asks for **729**
pieces: that source is an image service that is cut into 1200 × 1200 px requests
(1.2 km at 1 m), and the default 10 mile buffer is a 32 km square, so 27 × 27 = 729
requests. They are not lidar tiles, it is slow, and the result is a resampled image,
not the tiles Grok used. Cancel it and do one of these:

**Option B (recommended, the same DEM as Grok's run):**

```bash
cd ~/Downloads/DWR_Mac_Setup
bash get_lidar_tiles.sh --dry-run        # lists the 36 tiles (9.79 GB)
bash get_lidar_tiles.sh                  # downloads them; re-run to resume
bash run_nickreview.sh --dam DWR-2801 --stages record   # writes the DEM VRT, runs no model
```

The last line writes the DEM VRT with its paths changed to your Mac:
`~/dwr-dam-suite/Dams/NickReview_mac/work/DWR-2801/dem/DWR-2801_composite_1m_full_native_v2.vrt`.
Use **this** file as the DEM. The copy under `<DATA>/nickreview_rerun_20261023/` still
points at the box's `/home/box/...` folders and will not open correctly in QGIS on the Mac.

**Option A (inside QGIS, for this or any other dam):** Processing ▸ **DamSafety Screening**
▸ Dam screening ▸ **Download DEM (dam, county, or state)**:

| field | value |
|---|---|
| Source | **USGS 3DEP 1 m lidar tiles (native, TNM)** |
| 1 m tiles: box west,south,east,north in degrees | `-97.63262,46.71761,-96.90646,47.15281` (Grok's DWR-2801 model area; or leave empty and give the dam point below) |
| Dam point, longitude,latitude | `-97.390918,46.984382` (only used when the box is empty, with the buffer) |
| Dam ID | `DWR-2801` |
| 1 m tiles: tile folder | e.g. `~/dwr-dam-suite/lidar_1m` (not `lidar_1m_native`: the tool does not recognise tiles get_lidar_tiles.sh put there and would download them again) |
| 1 m tiles: download the listed tiles | **off** the first time: it only lists the tiles and the total |
| 1 m tiles: largest download for one run (GB) | **15** (the default 10 is just too small: 38 tiles ≈ 10.3 GB) |
| Output folder | e.g. `~/dwr-dam-suite/DEM/DWR-2801` |

Run it once: the log lists **38 tiles** for that box (Grok's 36 plus x66y518 and
x66y519). Then tick **1 m tiles: download the listed tiles** and run again (it resumes if
cut off). The DEM is `<Output folder>/DWR-2801_1m_lidar.vrt`. With the dam point and the
default 15 mile buffer instead of the box it is about 32 tiles.
Option A's VRT is the same 1 m lidar but not byte-identical to Grok's composite (that one
also has a 5 m DEM underneath outside the 1 m tiles), so use option B to match Grok's run.

### 3b. "Set the DEM elevation unit (feet or metres)" error

Run breach model's **DEM elevation units** is *Detect from the raster* by default, and in
the plugins this package installs (Runner 3.10.86) that choice always stops with
*"Set the DEM elevation unit (feet or metres). QL2 ASCII grids are feet; a USGS IMG is
usually metres."* Set **DEM elevation units = Meters** (all the DEMs above are metres;
only ND DWR QL2 ASCII grids are feet). The Terminal script already passes Meters.

### 3c. Run breach model

Processing ▸ **DamSafety Runner** ▸ **Run breach model for one dam**.
`<DATA>` = `~/dwr-dam-suite/NickReview_data`.

| field | value |
|---|---|
| Dam | DWR-2801, Swan Buffalo Detention Dam 12, NID ND00199 |
| Dam point | -106343.832, 2666591.505 [EPSG:5070] |
| Height / storage / area | 27.5 ft / 1615.496 ac-ft / **154.5 ac** (DWR max pool area) |
| Top of dam | 1069.117 ft |
| Method / scenario | 0 / 0, baseline on, stop rule off |
| DEM | `~/dwr-dam-suite/Dams/NickReview_mac/work/DWR-2801/dem/DWR-2801_composite_1m_full_native_v2.vrt` (step 3a, option B; or option A's `DWR-2801_1m_lidar.vrt`) |
| DEM elevation units | **Meters** (step 3b) |
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
