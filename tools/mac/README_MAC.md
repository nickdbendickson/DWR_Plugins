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
files were added after the sizes were measured on the box; `run_nickreview.sh --list-data`
there writes both lists again with every size). To download the LiDAR tiles instead of
copying them:

```bash
bash get_lidar_tiles.sh                              # Absaraka
bash get_lidar_tiles.sh data_files_nickreview.tsv    # all dams
```

## 4. Run the NickReview dams

```bash
bash run_nickreview.sh --dry-run          # shows what would run; runs and writes nothing
bash run_nickreview.sh --dam DWR-2801     # one dam (do this one first)
bash run_nickreview.sh                    # all 42 dams in nickreview_dams.txt
```

* Uses the same settings as the Grok Bot box runs (`damsuite_settings_nickreview.csv`,
  `absaraka/grok_settings_DWR-2801.json`).
* **Resume:** stop with Ctrl-C any time (the log, the summary and the manifest of the
  dams so far are kept); run the same command again and finished stages are skipped.
  `--redo` runs the stages in `--stages` (all of them by default) and every stage after
  them again, and its prepare step extracts and downloads its inputs (3DHP streams too)
  again; earlier stages keep their results.
* rundam waits for the Screening 7b record (`dams/<dam>/dam.json`, from
  `Dams/NickReview/screen_maxstor`), as on the box: without it the Runner would use its
  default crest length. `--allow-no-record` runs it anyway and says so in the log.
* `--threads N` (default 8; an M2 Pro has 6 or 8 performance cores). Threads do not change the results.
* `--data DIR` / `--out DIR` if your folders are elsewhere (spaces in paths are fine).
* Results: `~/dwr-dam-suite/Dams/NickReview_mac/dams/<dam>/`; a log per dam in
  `work/<dam>/run_<dam>.log`; a manifest per run with the versions, the settings and the
  sha256 of every file the dam's steps read (each rundam and hazclass input, every file of
  a `;;` list, the DEM VRTs and the DEM tiles that touch the dam's area, the streams and
  the other structures inputs, the Screening 7b inputs, the LISFLOOD-FP binary).
* The Mac is kept awake while it runs (`caffeinate`).

**Walkthrough:** Absaraka step by step, including the DEM and every QGIS dialog value:
`absaraka/RUN-ABSARAKA.md` (in the unzipped package folder). Do that dam first.

## 5. Three things that trip people up in QGIS

* **1 m DEM: "729 tiles".** The Runner dialog's *Download a DEM* box with *USGS 3DEP* at
  *1 m* fetches an image service in 1.2 km pieces: the default 10 mile buffer is
  27 × 27 = 729 requests, not lidar tiles (in Runner 3.10.86 that box has no native-tile
  source). For a 1 m DEM use `get_lidar_tiles.sh` (the tiles the box used), or the
  Screening plugin's Processing tool *Download DEM (dam, county, or state)* with Source
  **USGS 3DEP 1 m lidar tiles (native, TNM)** (10 km tiles, about 25–40 for a dam; it
  lists them before downloading). Details in `absaraka/RUN-ABSARAKA.md`, step 3a.
* **"Set the DEM elevation unit (feet or metres)".** In *Run breach model for one dam*,
  set **DEM elevation units = Meters** (USGS 1 m lidar, 3DEP downloads and ND DWR .img are
  metres; only ND DWR QL2 ASCII grids are feet). In Runner 3.10.86 *Detect from the
  raster* always stops with that message; Meters is right for every DEM here either way.
* The box's VRTs under `NickReview_data` point at the box's `/workspace/...` and
  `/home/box/...` folders; use the copies the run script writes to `work/<dam>/dem/`
  (`bash run_nickreview.sh --dam <dam> --stages record` writes them, and the Screening 7b
  record, without running a model).

## Good to know

* The box runs used patched LISFLOOD-FP **8.2**; the Mac uses patched **8.3**.
* LISFLOOD-FP 8.3 has no Metal (GPU) support, so it runs on the CPU. HEC-RAS (Tier 2)
  does not run on macOS.
* Leave Hazard's *offline pack* setting empty (the run script passes the pack itself).
* DWR-3161 (flagged road-fill structure) and DWR-4593 (breached) are not in the list.
* If USGS 3DHP streams can't be downloaded (or the service answers an error or no
  flowlines), the script falls back to the NHD flowlines. Every later run of that dam says
  so in its log and manifest, and `--redo --stages prepare` tries 3DHP again (`--offline`
  always uses NHD).
* OSM road crossings are only in the 2026-10-21 offline pack, which is in the data list.

## Folders it creates

`~/miniforge3` (if no conda), `~/dwr-dam-suite` (LISFLOOD, env file, logs, run folders),
and the QGIS profile `~/Library/Application Support/QGIS/QGIS3/profiles/damsafety`.
