# DamSafety (DWR) dam suite on a Mac (M2 Pro / Apple Silicon)

Everything installs into your home folder with conda. No sudo, no Homebrew.
Your normal QGIS and its "default" profile are not touched: the suite uses its own
QGIS profile called **damsafety**.

| what | version |
|---|---|
| Miniforge | 26.9.4-1, only if you have no conda (its sha256 is checked before it runs) |
| QGIS | 3.34 LTR (the newest 3.34.x on conda-forge; 3.34.6 when this was written), env `ndqgis`, numpy < 2 |
| ANUGA + gmsh | ANUGA 4.0.1, gmsh 4.15, env `ndanuga` |
| LISFLOOD-FP | 8.3, official source from Zenodo + the Runner's small patch, built on your Mac |
| plugins | the zips in `plugins/` (named `<plugin>_<version>.zip`); the first line of `plugins/SHA256SUMS` names the DWR_Plugins commit they were built from |

The package (`DWR_Mac_Setup`) is the `tools/mac` folder of DWR_Plugins with its `plugins/`
folder filled by `python3 tools/mac/make_plugin_zips.py` (from the checked-out commit: committed
files only). The `--check` option and the end of the install list the installed versions.

## 1. Before you start (once)

Open **Terminal** (Applications ▸ Utilities) and install Apple's command line tools
(needed to compile LISFLOOD-FP):

```bash
xcode-select --install
```

Click *Install* in the window that opens. If it says they are already installed, that is fine.

## 2. Install (about 20–40 minutes, needs internet)

Unzip the package (e.g. into Downloads), then:

```bash
cd ~/Downloads/DWR_Mac_Setup
bash install_dwr_suite_mac.sh
```

It installs Miniforge (if you have no conda), the three conda envs (`ndqgis`, `ndanuga`,
`ndbuild`), installs the plugins into the damsafety profile, downloads LISFLOOD-FP 8.3
(73.9 MB, sha256 checked), patches it with the installed Runner's `patch_lisflood.py` and
builds it into `~/dwr-dam-suite/bin/lisflood`, and ends with a self-check (it runs no
model). The self-check fails when no patched LISFLOOD-FP could be built (unless you gave
`--skip-lisflood`): `run_nickreview.sh` needs one. The log is in `~/dwr-dam-suite/logs/`;
the compiler logs of each build attempt are in `~/dwr-dam-suite/src/lisflood-logs/`.

It is safe to run again: finished steps are skipped. To only check what is installed:

```bash
bash install_dwr_suite_mac.sh --check
```

Useful options: `--skip-lisflood` (don't build LISFLOOD-FP; one already in
`~/dwr-dam-suite/bin/lisflood` is still used), `DS_LISFLOOD=/path/to/lisflood` (use a
patched build you already have; an existing `~/nd-dam-suite/bin/lisflood` that is patched
is reused automatically), `DS_THREADS=8`.

To open QGIS with the suite: double-click **Open DWR QGIS.command** in `~/dwr-dam-suite`
(from Terminal, run that file; it sets the environment the Runner reads, e.g. the gmsh
mesher, before it starts QGIS).

## 3. Copy the data

The runs read the box's data from `~/dwr-dam-suite/NickReview_data/` (read only).

* Absaraka (DWR-2801) only: `absaraka/DATA_TO_COPY_DWR-2801.md` — 10.85 GB, of which
  1.06 GB must be copied and 9.79 GB is USGS LiDAR you can download, plus the 3 Screening
  7b record inputs in `Dams/NickReview/screen_maxstor/`.
* All 42 NickReview dams: `DATA_TO_COPY_nickreview.md` — 32.69 GB, of which 22.90 GB
  must be copied, plus 44 Screening 7b record inputs in `Dams/NickReview/screen_maxstor/`.

Each has a `.tsv` with every file, its size and where it goes (`?`: the screen_maxstor
files were added after the sizes were measured on the box). To download the LiDAR tiles
instead of copying them:

```bash
bash get_lidar_tiles.sh                              # Absaraka
bash get_lidar_tiles.sh data_files_nickreview.tsv    # all dams
```

On the box, `--list-data` writes both lists again with every size. The box has no installer
(so no `damsafety_env.sh` for `run_nickreview.sh`): run `nickreview_mac.py` itself from a
DWR_Plugins checkout, with the box's QGIS python (it needs GDAL; without it the script stops
and says so). It is a dry run, so it needs no solvers and no plugins, and it lists finished
dams too:

```bash
cd DWR_Plugins/tools/mac
PY=/path/to/the/box/QGIS/python    # the python QGIS runs with on the box
"$PY" -c 'from osgeo import gdal'  # no error: this python has GDAL
"$PY" nickreview_mac.py --list-data data_files_nickreview.tsv \
  --data /home/box/DamSafety --out /tmp/nr_list --dams-file nickreview_dams.txt
"$PY" nickreview_mac.py --list-data absaraka/data_files_DWR-2801.tsv --dam DWR-2801 \
  --data /home/box/DamSafety --out /tmp/nr_list --dams-file nickreview_dams.txt
```

Each writes the `.tsv` and the `DATA_TO_COPY_*.md` next to it (nothing goes to `--out`).

## 4. Run the NickReview dams

```bash
bash run_nickreview.sh --dry-run          # shows what would run; runs and writes nothing
bash run_nickreview.sh --dam DWR-2801     # one dam (do this one first)
bash run_nickreview.sh                    # all 42 dams in nickreview_dams.txt
```

* Uses the same settings as the Grok Bot box runs (`damsuite_settings_nickreview.csv`,
  `absaraka/grok_settings_DWR-2801.json`).
* **Resume:** stop with Ctrl-C any time (the log, the summary and the manifest of the
  dams so far are kept); run the same command again and finished stages are skipped (for a
  stopped `--redo`, run the command it prints, which is the same command without `--redo`:
  the same `--redo` command again redoes everything, see below).
  A stage counts as finished when its `work/<dam>/status/<stage>.ok` is there, ran in the
  dam's mode (see `--no-kits` below) and is newer than the `.ok` of every stage whose
  results it reads (the table below), so a stage that ran again makes the later ones run
  again too, and no stage runs on missing or older results: with `--stages`, a stage they
  read that is not finished runs first. One exception: a rundam run with
  `--allow-no-record` when no record could be written counts as finished although the
  record stage is not; once a record is written (by a later run, when the record's inputs
  are there), rundam is no longer finished and runs again, and hazclass and hazbatch after it.
* **`--redo`** runs finished stages again: the stages in `--stages` (all of them by
  default) that apply to the dam, and every later stage that reads their results. The
  others keep their results. The stages run in the order prepare, structures, precheck,
  record, rundam, hazclass, hazbatch. What runs again with each:

  | `--redo --stages` | also runs again |
  |---|---|
  | prepare | structures, precheck, rundam, hazclass, hazbatch |
  | structures | precheck, rundam, hazclass, hazbatch |
  | precheck (a report) | nothing |
  | record | rundam, hazclass, hazbatch |
  | rundam | hazclass, hazbatch |
  | hazclass | hazbatch |
  | hazbatch | nothing |

  prepare and structures do not apply to DWR-2801, DWR-2865 and DWR-3034 (they use the box
  preparation), so for them `--redo --stages prepare` changes nothing. A prepare step that
  runs again extracts and downloads its inputs (3DHP streams too) again. Only precheck and
  hazbatch can run again without the stages after them: any other stage that runs again
  makes the later stages that read it run again in the next run that includes them.
* **How `--redo` works, and a stopped `--redo`:** before the first dam runs, `--redo`
  sets aside the `status/<stage>.ok` of every stage it runs again, for every dam of the
  command (renamed to `<stage>.ok.redo_<time>`, kept), and lists them once at the start.
  `--redo --dry-run` prints that list and changes nothing. Those stages are then not
  finished, so a `--redo` that stopped (Ctrl-C, or a failure) is finished by **the same
  command without `--redo`**: it does only the stages that are still not finished, and
  nothing that is finished. The run prints that command when it stops and at the end
  ("to finish, run the same command WITHOUT --redo: ..."); its `--stages` lists every
  stage the `--redo` runs again (a run without `--redo` runs only the stages it names).
  Running the same `--redo` command again instead redoes all of its stages again, for
  every dam, also the ones it already redid: that is what `--redo` means.
* A failed precheck (a report no other stage reads) and, with `--allow-no-record`, a
  Screening 7b record that could not be written do not stop anything: the summary and the
  manifest list them as not fatal, and the next run of that dam tries them again (in a
  `--redo`, the printed finish command lists them in its `--stages`).
* `--no-kits` (prepare DWR-2801, DWR-2865 and DWR-3034 here instead of using the box
  preparation) on a dam that finished with its box preparation, or the reverse, does not
  run it: `status/*.ok` records the mode each stage ran in, and the two are never mixed.
  Add `--redo` to run every stage of that dam again in the new mode.
* rundam waits for the Screening 7b record (`dams/<dam>/dam.json`, from
  `Dams/NickReview/screen_maxstor`), as on the box: without it the Runner would use its
  default crest length. `--allow-no-record` runs it anyway, on the record an earlier run
  wrote in `dams/<dam>/dam.json` if there is one (the Runner reads that file), and says
  which in the log and the manifest; once a record is written, that rundam runs again (see
  Resume above).
* `--threads N` (default 8; an M2 Pro has 6 or 8 performance cores). Threads do not change the results.
* `--data DIR` / `--out DIR` if your folders are elsewhere (spaces in paths are fine).
* Results: `~/dwr-dam-suite/Dams/NickReview_mac/dams/<dam>/`; a log per dam in
  `work/<dam>/run_<dam>.log`; a manifest per run with the versions, the settings and the
  sha256 of every file the dam's steps read (each rundam and hazclass input, every file of
  a `;;` list, the DEM VRTs and the DEM tiles that touch the dam's area, the streams and
  the other structures inputs, the suite pack layers or offline pack the extracts were cut
  from and `DWR_dams.gpkg` (prepared dams), the box kit's JSON files, the Screening 7b
  inputs, the dam's `dam.json` as rundam read it, the suite settings CSV the plugins read
  (`damsuite_settings_nickreview.csv` in the results folder), the LISFLOOD-FP binary).
* The Mac is kept awake while it runs (`caffeinate`).

**Walkthrough:** Absaraka step by step, including the DEM and every QGIS dialog value:
`absaraka/RUN-ABSARAKA.md` (in the unzipped package folder). Do that dam first.

## 5. Three things that trip people up in QGIS

* **1 m DEM: "729 tiles".** The Runner dialog's *Download a DEM* section with *USGS 3DEP*
  at *1 m* fetches an image service in 1.2 km pieces: the default 10 mile buffer is
  27 × 27 = 729 requests, not lidar tiles (in Runner 3.10.86 that section's Source list has
  no native-tile source). For a 1 m DEM use `get_lidar_tiles.sh` (the tiles the box used),
  or the Screening plugin's Processing tool *Download DEM (dam, county, or state)* with
  Source **USGS 3DEP 1 m lidar tiles (native, TNM)** (10 km tiles, about 25–40 for a dam;
  it lists them before downloading). The section's *Download DEM…* button opens that same
  Screening tool, so you can also switch its Source there before you run it. Details in
  `absaraka/RUN-ABSARAKA.md`, step 3a.
* **"Set the DEM elevation unit (feet or metres)".** In *Run breach model for one dam*,
  set **DEM elevation units = Meters** (USGS 1 m lidar, 3DEP downloads and ND DWR .img are
  metres; only ND DWR QL2 ASCII grids are feet). In Runner 3.10.86 *Detect from the
  raster* always stops with that message; Meters is right for every DEM here either way.
* The box's VRTs under `NickReview_data` point at the box's `/workspace/...` and
  `/home/box/...` folders; use the copies the run script writes to `work/<dam>/dem/`
  (`bash run_nickreview.sh --dam <dam> --stages record` writes them, and the Screening 7b
  record, without running a model).

## 6. The Finder over the whole state

`run_finder_statewide.sh` runs DamSafety Finder's *Fills across streams and draws* over all of
North Dakota from the statewide 1 m terrain VRT on the SSD, in 10 km tiles, and merges the
tiles into one scored GeoPackage with a ranked list of the unlisted dam candidates and a short
list of the top 2,000 with map links. The step-by-step is `RUN-FINDER-STATEWIDE.md`; the short form:

```bash
bash run_finder_statewide.sh --dry-run     # tile count, time / memory / disk estimate; writes nothing
bash run_finder_statewide.sh --limit 4     # a trial: 4 tiles and the merge
bash run_finder_statewide.sh               # the whole state; the same command goes on after a stop
```

* **Reads only:** the terrain VRT (`--dem`, default `/Volumes/Extreme SSD/ND_DEM_1m/ND_1m_26914.vrt`),
  the ND data pack in the suite data folder (`--data`, default `$NDSUITE_DATA`, else
  `~/ND_Suite_Data`) and the installed plugin (the damsafety profile; `--plugins FOLDER` or
  `--repo CHECKOUT` for another copy; Finder 0.7.0, or 0.6.0 at the least). **Writes only** into `--out`
  (default `~/ND_Finder_Statewide`); it refuses an `--out` inside the data folder, the pack, the
  terrain folder or the plugins folder, also when the path is spelled in another case (a Mac's
  disk treats `~/nd_suite_data` and `~/ND_Suite_Data` as one folder). It downloads nothing.
* **Tiles:** 10 km squares on a fixed lattice in EPSG:5070 that touch and do not overlap, about
  1,950 for the state. `finder.run_embankments` works in 5 km squares (Finder 0.7.0: the
  squares of a fixed lattice in EPSG:5070, each searched whole; 0.6.x: from the corner of the
  box it is given), reads 3 km of terrain beyond each square and keeps a fill only when its
  crest point is in the square. A 10 km tile is exactly four of those squares with either
  Finder, so every square is the one a single statewide call would use: no fill is dropped at
  a seam and none is found twice. The engine's two cross-square rules (one fill per pool; no
  two fills within 60 m) are applied across tile seams in the merge, with the engine's own
  functions. The header of `run_finder_statewide.py` has the full reasoning and the two edge
  cases.
* **Time and memory (measured on the 4-core cloud test box, NOT on a Mac):** a tile full of
  terrain takes about 4.5 minutes of processor time on one core (780 CPU-seconds for 9 tiles
  over 300 km2 of terrain; one full tile alone: 175 s) and 1.2 GB of memory at its peak. For the
  state that is about 130 processor-hours: **about a day and a half with the default 4 tiles at
  a time, and about 5 GB of memory**, if a Mac core is as fast as a test-box core. A tile at
  the edge of the terrain is many times quicker than a full one, so every estimate counts a
  tile by its share of terrain; `--dry-run` uses this computer's own figure only once 3 tiles
  **full of terrain** are finished and says which figure it used, and the time left printed
  during a run is marked a first guess until a few full tiles are done. Disk: about 2 GB in
  `--out` for the state.
* **Resume:** each finished tile is a folder `tiles/<tile>/` with a `done.json`; a tile without
  it runs again. Ctrl-C stops cleanly; a killed run leaves nothing half-written in `tiles/`. A
  failed tile is tried once more, then logged with the reason (`failed/<tile>.log`, removed
  when the tile later finishes) and left for the next run; the other tiles go on. Tiles with
  no terrain are found from the VRT's list of source files and a 64 x 64 sample of an
  overview, and are not run; a tile whose terrain file cannot be read is counted apart.
  `--merge-only` reads the finished tiles only, so it works with the SSD unplugged.
* **Three flags the script adds:** `in_state` (a fill outside North Dakota: the outline is
  `--state-outline FILE`, else the pack's county layer when it has one, else a simple built-in
  outline with a 600 m allowance), `at_data_edge` (a fill at the edge of the terrain data,
  where NoData acts as a wall and makes a false dam. Finder 0.7.0 decides it itself: a fill
  within 150 m of missing terrain whose pool reaches the missing terrain has the kind `at the
  edge of the terrain data` and no score, and `at_data_edge` is `yes` for exactly those;
  `edge_ft` is the Finder's own distance. For tiles made by 0.6.x, which has no such test, the
  script measures the distance on the DEM and marks every fill within `--edge-m`, 150 m) and
  `excluded` (a fill inside a
  polygon of `--exclude-polygons FILE`: the hook for ground such as mine spoil or sand hills;
  the script holds no such map itself). All three kinds stay in the `embankments` layer and are
  left out of the ranked lists.
* **Results:** `ND_finder_fills.gpkg` (`embankments`, `candidates`, `spillways`, `ridges`,
  `run_info`), `ND_finder_candidates_ranked.csv` (every candidate: not listed, dam or dry
  embankment, in the state, not at the data edge, not excluded; highest score first, scored
  with the plugin's `models/nd_fill_score_trees_2026-11.json`; about 300,000 rows for the
  state), `ND_finder_candidates_top.csv` (the first 2,000 of them, `--top N`, with a link to
  the satellite picture at each: the one to open) and `ND_finder_summary.txt` (which also says
  how many candidates score 0.5 or more, and which Finder version made the run).
* **Finder versions and ids:** one result holds the fills of one Finder version; tiles made by
  another version are kept, not run again and not merged (`--redo` runs them again). `find_id`
  is a code made from the crest point. With **Finder 0.7.0** the ids are the same in every run
  over the same terrain, whatever the box. On 0.6.x the crest point moves by a few metres with
  the box of a run, and no id carries over from one Finder version to another, so marks made
  on a 0.6 run (a county run from the toolbox, the cloud test tiles) are matched to newer fills
  **by location**, not by id.

## Good to know

* The box runs used patched LISFLOOD-FP **8.2**; the Mac uses patched **8.3**.
* LISFLOOD-FP 8.3 has no Metal (GPU) support, so it runs on the CPU. HEC-RAS (the Runner's
  *LISFLOOD-FP then HEC-RAS 2D* model) does not run on macOS.
* Leave Hazard's *offline pack* setting empty (the run script passes the pack itself).
* DWR-3161 (flagged road-fill structure) and DWR-4593 (breached) are not in the list.
* If USGS 3DHP streams can't be downloaded (or the service answers an error or no
  flowlines), the script falls back to the NHD flowlines. Every later run of that dam says
  so in its log and manifest (also a run that skips it as finished), with the command that
  tries 3DHP again for that dam: run the printed command as it is (it is
  `--redo --stages prepare --dam <ID>` with the other options of the run that printed it,
  `--no-kits` for DWR-2801, DWR-2865 or DWR-3034 prepared here, and without `--offline`);
  then every later stage of it runs again too (structures, precheck, rundam, hazclass,
  hazbatch: hours). With `--offline` it uses NHD again.
* In QGIS, set *Crest length* as `absaraka/RUN-ABSARAKA.md` step 3c says: an empty crest
  length with no `dam_length_ft` in the Dams folder's `dam.json` is the Runner's 400 ft
  default, not the length the box runs used.
* OSM road crossings are only in the 2026-10-21 offline pack, which is in the data list.

## Folders it creates

`~/miniforge3` (if no conda), `~/dwr-dam-suite` (LISFLOOD, env file, logs, run folders),
`~/ND_Finder_Statewide` (only when you run `run_finder_statewide.sh`),
and the QGIS profile `~/Library/Application Support/QGIS/QGIS3/profiles/damsafety`.
