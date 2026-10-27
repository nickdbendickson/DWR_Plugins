# Absaraka Dam (DWR-2801) on the Mac — same settings as Grok Bot

The settings below are the ones Grok Bot used for the DWR-2801 NickReview rerun
(DWR_Plugins main cc5892e, Runner 3.10.83). The machine-readable copy is
`grok_settings_DWR-2801.json`. This package installs newer plugins: the zips in its
`plugins/` folder (the first line of `plugins/SHA256SUMS` names their commit). These notes
were written against main a83ad04 (Runner 3.10.86, Screening 0.17.49).

## 1. Get the data (once)

Put the files listed in `DATA_TO_COPY_DWR-2801.md` under
`~/dwr-dam-suite/NickReview_data/` (keep the sub-folders):

* 1.06 GB to copy from the box (run kit, culverts, box-made DEMs, NHD waterbodies)
* the 3 Screening 7b record inputs in `Dams/NickReview/screen_maxstor/` (`8_hazard.gpkg`,
  `4_breach_points.gpkg`, `3_stage_storage/curve_DWR-2801.csv`): the run writes the dam's
  Screening 7b `dam.json` from them before the breach model, as the box did
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
versions, the settings and the sha256 of every file the steps read: each rundam and
hazclass input (every file of a `;;` list), the DEM VRTs and the 1 m tiles that touch the
dam's area, the box kit's `rundam.json` and `hazclass.json`, the Screening 7b inputs, the
dam's `dam.json` as rundam read it, the suite settings CSV and the LISFLOOD-FP binary.
Run it again and finished stages are skipped (a stage that ran again makes the later stages
that read it run again). `--redo` runs finished stages again; with `--stages`, the named
stages that apply and every later stage that reads their results (for this dam the stages
are precheck, record, rundam, hazclass, hazbatch; prepare and structures do not apply, the
box prepared it, so `--redo --stages prepare` changes nothing). For example
`--redo --stages rundam` runs rundam, hazclass and hazbatch again, and
`--redo --stages precheck` only the precheck (a report no later stage reads). Before it
runs anything, `--redo` sets aside the `status/<stage>.ok` of those stages (renamed to
`<stage>.ok.redo_<time>`) and lists them. If it stops (Ctrl-C or a failure), finish it
with the command it prints ("to finish, run the same command WITHOUT --redo: ..."), run
as it is: it is the same command without `--redo`, and its `--stages` also lists the
stages that read the ones you named (for `--redo --stages rundam` it has
`--stages rundam,hazclass,hazbatch`; a hand-typed `--stages rundam` would leave hazclass
and hazbatch not done). That run does only what is not finished yet. The same `--redo`
command again would redo all of those stages again.

## 3. Or in QGIS (Runner dialog)

### 3a. The DEM first (do not use "Download a DEM" with USGS 3DEP at 1 m)

Grok's run used **36 USGS 1 m lidar tiles** (10 km × 10 km each, x60–65 × y518–523,
9.79 GB). The Runner's *Download a DEM* box with **USGS 3DEP + 1 m** asks for **729**
pieces: that source is an image service that is cut into 1200 × 1200 px requests
(1.2 km at 1 m), and the default 10 mile buffer is a 32 km square, so 27 × 27 = 729
requests. They are not lidar tiles, it is slow, and the result is a resampled image,
not the tiles Grok used. Cancel it and do one of these:

**Option B (recommended, the same DEM as Grok's run):** with the step 1 data in place,

```bash
cd ~/Downloads/DWR_Mac_Setup
bash get_lidar_tiles.sh --dry-run        # lists the 36 tiles and their total, 9.79 GB
bash get_lidar_tiles.sh                  # downloads them; re-run to resume
bash run_nickreview.sh --dam DWR-2801 --stages record   # writes the DEM VRT, runs no model
```

The last line writes the DEM VRT with its paths changed to your Mac (and the dam's
Screening 7b `dam.json`):
`/Users/<you>/dwr-dam-suite/Dams/NickReview_mac/work/DWR-2801/dem/DWR-2801_composite_1m_full_native_v2.vrt`
(your user name for `<you>`). Use **this** file as the DEM. The copy under
`<DATA>/nickreview_rerun_20261023/` still points at the box's `/workspace/...` and
`/home/box/...` folders and will not open correctly in QGIS on the Mac.

**Option A (inside QGIS, for this or any other dam):** the Screening plugin's tool (the
Source list of the Runner dialog's *Download a DEM* section has no native-tile source in
Runner 3.10.86, but its *Download DEM…* button opens this same tool, where you can switch
Source): Processing ▸ **DamSafety Screening** ▸ Dam screening ▸ **Download DEM (dam,
county, or state)**. Its folder fields do not expand `~`: type the full path
(`/Users/<you>/...`, with your user name for `<you>`) or pick the folder with the **…** button.

| field | value |
|---|---|
| Source | **USGS 3DEP 1 m lidar tiles (native, TNM)** |
| 1 m tiles: box west,south,east,north in degrees | `-97.63262,46.71761,-96.90646,47.15281` (Grok's DWR-2801 model area; or leave empty and give the dam point below) |
| Dam point, longitude,latitude | `-97.390918,46.984382` (only used when the box is empty, with the buffer) |
| Dam ID | `DWR-2801` |
| 1 m tiles: tile folder | e.g. `/Users/<you>/dwr-dam-suite/lidar_1m` (not `lidar_1m_native`: the tool only counts tiles listed in its own `lidar_1m_manifest.json`, so it would download the get_lidar_tiles.sh tiles again) |
| 1 m tiles: download the listed tiles | **off** the first time: it only lists the tiles and the total |
| 1 m tiles: largest download for one run (GB) | the default **10** is enough: the tool counts GB as 1024³ bytes, and the 38 tiles are about 9.3–9.7 of those (10.0–10.4 GB in the decimal GB of the data lists) |
| Output folder | e.g. `/Users/<you>/dwr-dam-suite/DEM/DWR-2801` |

Run it once: the log lists **38 tiles** for that box (Grok's 36 plus x66y518 and
x66y519). Two projects publish x66y518 and x66y519 (ND_3DEPProcessing_D22, published
2024, and MN_RedRiver_D23, published 2026); the tool keeps the one with the newest
collection and names it in the log. Then tick
**1 m tiles: download the listed tiles** and run again (it resumes if cut off). The DEM is
`<Output folder>/DWR-2801_1m_lidar.vrt`. With the dam point and the default 15 mile buffer
instead of the box it is about 32 tiles (x59–x64, a different set).
Option A's VRT is the same 1 m lidar where its tiles are the same ones, but not
byte-identical to Grok's composite (that one also has a 5 m DEM underneath outside the 1 m tiles), so use
option B to match Grok's run.

### 3b. "Set the DEM elevation unit (feet or metres)" error

Run breach model's **DEM elevation units** is *Detect from the raster* by default, and in
Runner 3.10.86 (the Runner these notes were written against) that choice always stops with
*"Set the DEM elevation unit (feet or metres). QL2 ASCII grids are feet; a USGS IMG is
usually metres."* Set **DEM elevation units = Meters** (all the DEMs above are metres;
only ND DWR QL2 ASCII grids are feet; Meters is right for them in any Runner version). The
Terminal script already passes Meters.

### 3c. Run breach model

Processing ▸ **DamSafety Runner** ▸ **Run breach model for one dam**.
`<DATA>` = `/Users/<you>/dwr-dam-suite/NickReview_data` (with your user name for `<you>`).
The path fields of the QGIS tools below do not expand `~`: type full paths like these, or
pick the file or folder with the **…** button.

| field | value |
|---|---|
| Dam ID (NID ID) | `DWR-2801` (the DWR ID, not the NID ID: the Runner finds the dam's folder in the Dams folder by it) |
| Dam name | Swan Buffalo Detention Dam 12 |
| NID ID (advanced) | `ND00199` |
| Dams folder | `/Users/<you>/dwr-dam-suite/absaraka-run/Dams` (the installer sets it as the suite's Dams folder; the results go to its `DWR-2801` folder, not to the Terminal run's). Its `DWR-2801/dam.json` is the NickReview screening record plus `dam_length_ft` = 1,700 ft (the NID dam length) |
| Crest length (ft) | the `dam_length_ft` of the dam's Screening 7b record, the crest length the box and Terminal runs use: `grep -m1 dam_length_ft ~/dwr-dam-suite/Dams/NickReview_mac/dams/DWR-2801/dam.json`. Option B's `--stages record` line writes that record (after option A, run that line once; it runs no model). Left empty, the Runner takes the Dams folder's 1,700 ft (its log says "Crest length (ft) from dam.json"); with no crest length anywhere it uses 400 ft |
| Dam point | -106343.832, 2666591.505 [EPSG:5070] |
| Height / storage / area | 27.5 ft / 1615.496 ac-ft / **154.5 ac** (DWR max pool area) |
| Top of dam | 1069.117 ft |
| Breach (ND 5.1.1(d)) / Scenario | **Piping failure, Froehlich (2008) parameters, routed** / **Sunny day: no inflow, breach at the top of dam (ND 5.1.1)** (the first entry of each list: `grok_settings_DWR-2801.json` gives them as 0 / 0). *Flood scenario: also run without the breach ...* on; *Class-stop rule* (advanced) off |
| DEM | `/Users/<you>/dwr-dam-suite/Dams/NickReview_mac/work/DWR-2801/dem/DWR-2801_composite_1m_full_native_v2.vrt` (step 3a, option B; or option A's `DWR-2801_1m_lidar.vrt`) |
| DEM elevation units | **Meters** (step 3b) |
| Structures | `<DATA>/culvert_precheck_20261024/dwr2801/structures_2801_with9122221_invfix2.gpkg`, layer `runner_culverts` |
| Structure fields | kind, span_ft, rise_ft, barrels, shape, invert_up, invert_dn, name |
| Culvert inventory | **one text field** (Advanced parameters: *Culvert guesses: inventory GeoPackages that size them, separated by ;;*) holding the three files `nddot_centerline_pipes.gpkg` (in `research/`), `nddot_bridge_inventory.gpkg` and `ntad_nbi.gpkg` of `<DATA>/culvert_sources/` as **absolute paths** (`/Users/<you>/...`; a `~` path is not found) separated by **`;;`** (not commas): paste the line the command below the table prints. Then match 75 m, burn matched off |
| Receptors | `pack_NSI_structures_2801`, `pack_ND_roads_NG911_2801`, `pack_NARN_rail_lines_2801` (in `<DATA>/nickreview_rerun_20261023/dwr2801`) |
| Water mask | `<DATA>/ND_Dam_Suite_Pack/hydro/NHD_waterbodies.gpkg` |
| Engine | Models **LISFLOOD-FP for the extent, then ANUGA (final results from ANUGA)**, **Force ANUGA** on, cell 30 m, Manning 0.06 |
| Reach | length 16 mi, half width 6 mi, step 3 mi, max extent 20 mi, max runs 1 |
| Arrival / edge tolerance | 0.1 ft / 1 ft |
| Duration | 0 (auto), max 72 h |
| Work CRS / ANUGA buffer | EPSG:5070 / 2000 m |
| Threads | 8 (the box used 6; threads do not change the results) |
| Keep files / products | on / on |

The *Culvert inventory* text, exactly (run this in Terminal and paste what it prints):

```bash
D="$HOME/dwr-dam-suite/NickReview_data/culvert_sources"
echo "$D/research/nddot_centerline_pipes.gpkg;;$D/nddot_bridge_inventory.gpkg;;$D/ntad_nbi.gpkg"
```

It prints one line of this form (with your user name for `<you>`):
`/Users/<you>/dwr-dam-suite/NickReview_data/culvert_sources/research/nddot_centerline_pipes.gpkg;;/Users/<you>/dwr-dam-suite/NickReview_data/culvert_sources/nddot_bridge_inventory.gpkg;;/Users/<you>/dwr-dam-suite/NickReview_data/culvert_sources/ntad_nbi.gpkg`.
A path the Runner cannot open is skipped with only a note in the log ("Culvert inventory
... not read"), and the culvert guesses are then sized differently from the box run.

Then **Hazard class**: Processing ▸ **DamSafety Runner** ▸ Data preparation and tools ▸
**ND hazard classification (Section 5.2, ACER TM 11 charts)**, with
*Dam folder with 'Depth (Max).tif' and 'Velocity (Max).tif'* = the DWR-2801 results folder
(`/Users/<you>/dwr-dam-suite/absaraka-run/Dams/DWR-2801`),
*Potentially lethal to occupants of habitable structures* = **2D model: ACER TM 11
depth-velocity danger charts (ND Fig. 5.1/5.2)**, *'Any depth': inundated or overtopped
from a depth of* = 0.1 ft, and in Advanced parameters *Class of an overtopped road with no
AADT* = **Medium**, *Class of an overtopped railroad with no trains a day* = **Medium** and
*Railroads: passenger field values ...* = `A,B,C` (all of these are the defaults;
`grok_settings_DWR-2801.json` gives the three lists by the place of the entry, counted from
0: LETHALITY 0 is the charts, UNKNOWN_AADT 1 and UNKNOWN_TRAINS 1 are Medium of Low, Medium,
High), with the `pack_*_2801.gpkg` layers listed in `grok_settings_DWR-2801.json`. Three of
its fields (Advanced parameters) are **text fields**, not layer pickers: *More road GeoPackages*
(ROADS_MORE), *Traffic (AADT) GeoPackages* (AADT_LAYERS) and *More lifeline GeoPackages*
(LIFELINES_MORE). Like the culvert inventory, they need **absolute paths**
(`/Users/<you>/...`; a `~` path is not found) separated by **`;;`**. Run this in Terminal
and paste each value line into the field named on the line above it:

```bash
K="$HOME/dwr-dam-suite/NickReview_data/nickreview_rerun_20261023/dwr2801"
echo "More road GeoPackages (ROADS_MORE):"
echo "$K/pack_ND_county_roads_2801.gpkg"
echo "Traffic (AADT) GeoPackages (AADT_LAYERS):"
echo "$K/pack_HPMS_ND_2024_2801.gpkg;;$K/pack_NDDOT_traffic_counts_2801.gpkg"
echo "More lifeline GeoPackages (LIFELINES_MORE):"
echo "$K/pack_lifeline_nd_basic_care_2801.gpkg"
```

It prints the three values in this form (with your user name for `<you>`):
`/Users/<you>/dwr-dam-suite/NickReview_data/nickreview_rerun_20261023/dwr2801/pack_ND_county_roads_2801.gpkg`,
`/Users/<you>/dwr-dam-suite/NickReview_data/nickreview_rerun_20261023/dwr2801/pack_HPMS_ND_2024_2801.gpkg;;/Users/<you>/dwr-dam-suite/NickReview_data/nickreview_rerun_20261023/dwr2801/pack_NDDOT_traffic_counts_2801.gpkg`
and
`/Users/<you>/dwr-dam-suite/NickReview_data/nickreview_rerun_20261023/dwr2801/pack_lifeline_nd_basic_care_2801.gpkg`.
A path that cannot be opened does not stop the run: it is skipped with a message in the
log ("Could not open extra Roads file ..." or "could not read the AADT layers"), and the
class can then differ from the box run (roads without an AADT, for example, then get the
*Class of an overtopped road with no AADT*, Medium).

Then **Hazard batch**: Processing ▸ **DamSafety Hazard** ▸ Dam hazard classification ▸
**Batch hazard classification (folder of dams)**. It classifies every dam folder directly
inside its *Folder containing the dam folders*, so give it a folder that holds only a link
to the DWR-2801 results folder, as the Terminal run does (make it once in Terminal):

```bash
mkdir -p ~/dwr-dam-suite/absaraka-run/hazbatch
ln -s ~/dwr-dam-suite/absaraka-run/Dams/DWR-2801 ~/dwr-dam-suite/absaraka-run/hazbatch/DWR-2801
```

| field | value |
|---|---|
| Folder containing the dam folders | `/Users/<you>/dwr-dam-suite/absaraka-run/hazbatch` |
| Use the internet downloads (...) | on |
| PDF report per dam | on |
| EAP outputs per dam (...) | **off** |
| Parallel worker processes (...) | 1 |
| Worker processes: time limit per dam (minutes, 0 = none) | **90** (the default is 60) |

Leave Hazard's offline_pack setting **empty**.

Also set (the Terminal script does this for you) `NDBREACH_ANUGA_MESHER=gmsh` and
`NDBREACH_DEM_RESAMPLE=average`.

## What changed from the older Absaraka reference

* area 154.5 ac (was 111.643), storage 1615.496 ac-ft (seed had 1615.344)
* DEM: the full native 1 m v2 VRT (all 412 culvert ends on native 1 m)
* structures: invfix2 (NBI 9122221 as a 2-barrel 2.97 × 2.01 m box; invert fixes on fid 257/263/280)
* water mask: NHD waterbodies; threads 8

Not possible on a Mac: HEC-RAS (Models *LISFLOOD-FP then HEC-RAS 2D*), and Metal GPU
(LISFLOOD-FP 8.3 has none).
After the run, first check the maximum velocity / time step at NBI 9122221.
