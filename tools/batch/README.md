# Batch runs: many dams, headless

Four scripts that run dams through `dam_chain.py` one after another or side by side, and a
watcher that checks each finished dam by rule. They lived in zip files until 2026-11-13; this
folder is now the copy to use, and `tools/mac/use_local_terrain.sh` looks here first.

| File | What it does |
|---|---|
| `run_lisflood_44.py` | The 44 dams in `dams_44.csv` through the full ND chain (Get data, Screening, Runner with LISFLOOD-FP, Hazard classification), profile `nd`. |
| `run_anuga_culverts_44.py` | The same 44 through the DamSafety Runner only, LISFLOOD-FP for the extent and ANUGA for the final results, with culverts cut into the terrain, profile `damsafety`. |
| `run_settings_study_44.py` | The 44 dams through the whole DamSafety chain once per model setting (two cell sizes, two engines), and one page that says for which dams the class depends on the setting. |
| `run_county.py` | Every DWR dam or dugout in one county, several, or the State, in either of those two modes. |
| `batch_watch.py` | Watches a run folder and writes findings as dams finish. Rules only unless you ask for the model. |
| `get_offline_pack.py` | Gets the offline pack without opening QGIS: the whole State, the area of the dams you name, or a pack built before from its zip files. The run scripts call it when they find no pack. |
| `collect_results_44.py` | Packs what a finished 44-dam batch left behind (four result rasters, `dam.json`, reports per dam) into small zip files to hand on. Reads only. |
| `batch_common.py` | What the three share: finding `dam_chain.py`, reading a dam's state, the data files, the watcher. |
| `dams_44.csv` | The 44 dams: DWR id, name, position, height, storage, length, NID id, hazard class (DWR Dams layer, 2026-11-06). |
| `tests/` | Every script run against a stand-in `dam_chain.py`. `python3 -m unittest discover -s tools/batch/tests` |

They call `~/local-ai/dam_chain.py`, which the suite installer sets up; point `DAMCHAIN_PY` at it
if it is somewhere else. `--dry-run` on any of them prints the commands and creates nothing.

## Data that is not in this folder

Three files are inputs, not code, and are **not in the repository**: they are copies of the
State's dam records (the full DWR record, about 8 MB together).

| File | Needed by | Comes from |
|---|---|---|
| `dams_44.gpkg` | `run_anuga_culverts_44.py`, `run_settings_study_44.py` | the batch-44-dams package |
| `nd_dams_dugouts.csv` | `run_county.py` | the county-batch package |
| `nd_dams_dugouts.gpkg` | `run_county.py --mode anuga` | the county-batch package |

The scripts look next to themselves, then in `$BATCH_DATA_DIR`, then in the folders those
packages unpack to under your home, Downloads and Documents. `--dams` and `--data` name a file
directly. When one is missing the script stops and lists where it looked.

## The 44 dams, LISFLOOD-FP

    python3 run_lisflood_44.py --dry-run
    python3 run_lisflood_44.py --watch
    python3 run_lisflood_44.py --parallel 4 --only DWR-2801,DWR-3034

Dam folders go in `~/ND_Dams/DWR-xxxx`. It reads the offline pack in
`~/ND_Suite_Data/ND_Dam_Suite_Pack`, and builds one first when there is none (next section).

| Setting | Value | Why |
|---|---|---|
| Solver | `SOLVER=1` (ACC) | FV2 / MWDG2 velocities are not validated for hazard calls. |
| Model | `TIER=0` | LISFLOOD-FP only. |
| Cell | `CELL=10` | Coarser over-spreads on flat plains (`--cell`). |
| Crest burn | `CREST_BURN=true` | Keeps road and rail embankments from the 1 m DEM. |
| Finer grid | `GRID_REFINE=true` | Re-runs at 10 m if a coarse run over-spreads. |
| 5 m check | `GRID_CHECK_CELL=0` | Off: hours per dam. `--check-5m` turns it on. |
| Near-dam refine | `REFINE_NEAR=false` | Off: it zeroes velocity. |
| Threads | `THREADS=1` | Dams side by side (`--parallel`) beat threads. |

They are passed explicitly, so a saved QGIS default or `DAMCHAIN_BREACH_PARAM` cannot change
them. A dam that finished is skipped next time; `--redo` runs it again.

**The result of a dam is read from its own `_chain_nd/state.json`**, not from the chain's exit
code, and the script exits 1 if any dam failed. Results: `batch44_lisflood_summary.csv` in the
dams folder. With `--parallel` above 1 trust that file over `Chain_Summary_ND.csv`, which two
dams finishing together can each rewrite.

`--dem FILE` models on that DEM or VRT instead of the terrain Get data downloads. It goes in as
the Runner's `DEM` parameter together with `OVERWRITE=true` for Get data, without which a dam
folder from an earlier run keeps the terrain it recorded. The model grid takes the DEM's CRS.
`tools/mac/use_local_terrain.sh` checks the VRT first.

## The offline pack, without QGIS

    python3 get_offline_pack.py --check
    python3 get_offline_pack.py
    python3 get_offline_pack.py --dams dams_44.csv --only DWR-3034,DWR-4031
    python3 get_offline_pack.py --bbox=-101.2,46.8,-100.8,47.2
    python3 get_offline_pack.py --zip part1.zip part2.zip part3.zip

The plugins read their map layers from `<data folder>/ND_Dam_Suite_Pack`. The pack builder in
DamSafety Data (and the one in the ND Data Updater) needs no QGIS; this script finds it, in
this checkout or in a QGIS profile, and runs it with a Python that has GDAL (the `ndqgis` env).
With no area it builds the whole State: hours and a few gigabytes. With `--dams` it builds one
box that takes in 20 miles around each dam named, which is more than the 15 miles Get data
reads; dams that take in most of the State are built as the whole State. A build that stops is
picked up by the same command, and a layer whose server does not answer is listed and left
out: `--update` fetches what is missing. The work folder beside the pack
(`ND_Dam_Suite_Pack_build`) holds national downloads and is several hundred megabytes even for
a small area; it can be deleted once the pack is built.

`--zip` unpacks a pack that was built before, from files or https links, and only when every
entry belongs to an `ND_Dam_Suite_Pack` folder and a manifest is among them.

**A pack that is there is never replaced or written into by this script.** Asked for an area
the pack does not take in, it says so and stops.

`run_lisflood_44.py` and `run_county.py` (LISFLOOD-FP mode) call it before their first dam when
the data folder has no pack, for the area of the dams about to run, and go on with the run
whether or not the build worked. `--no-get-pack`, or `BATCH_GET_PACK=0` in the environment,
leaves that out.

**What a run without a pack comes to** (tried in QGIS 3.34 with the ND 1.30 plugins on DWR-3034,
2026-11-13): Get data wrote twelve layers around the dam from the servers, terrain and land
cover among them, and Screening then stopped at its check on "DWR dam lines" and "Water
bodies". The DWR dam lines have no server to come from: they are in the pack, or in your own
DWR files in the data folder's `dwr/`. The NSI structure points and the lifeline facilities
also come only from the pack. So the pack is not optional for the full chain, which is why the
run scripts build it.

## Handing on the results

    python3 collect_results_44.py --list
    python3 collect_results_44.py --dams-folder ~/ND_Dams
    python3 collect_results_44.py --only DWR-3034,DWR-4031 --out ~/Desktop/two_dams

A dam folder holds gigabytes. Scoring another tool against the Runner needs little of it, so
this takes per dam the four result rasters (`Depth (Max).tif`, `Velocity (Max).tif`,
`Arrival Time.tif`, `WSE (Max).tif`), `dam.json`, and the small text files under
`_breach_model/`, `_screening_model/` and `_chain_nd/`; and for the batch its summary CSVs and
`BATCH_FINDINGS.md`. It writes `batch44_results_<date>_partN.zip` to `~/Desktop/batch44_results`,
each about 150 MB at most (`--max-mb`), a dam never split across two, with a `MANIFEST.csv` in
every part: status, hazard class, cell, rasters found, rasters missing. A dam that failed or
never ran is listed too. Nothing in the dams folder is changed.

## The 44 dams, ANUGA with culverts

    python3 run_anuga_culverts_44.py --get-clp --check-only
    python3 run_anuga_culverts_44.py --only DWR-2801 --watch

The DamSafety Runner finds culverts itself: every place a road crosses a channel gets one, and
a crossing within 75 m of an inventory culvert takes the inventory size (NDDOT Center Line
Pipes with `--get-clp`; others with `--culvert-inventory`). Matched culverts are cut into the
DEM at their inventory size; unmatched ones as **guessed, unverified** 1.2 x 0.9 m boxes, each
named `guess:` in the report. Screening, Hazard and HEC-RAS are skipped. ANUGA needs the
`ndanuga` env and takes hours per dam; `--parallel N` runs N groups, each in its own `partN`
folder. Results: `~/DamSafety/Dams44_ANUGA/<dam>/`.

## Do the settings change the class?

    python3 run_settings_study_44.py --dry-run
    python3 run_settings_study_44.py --only DWR-2801,DWR-3034
    python3 run_settings_study_44.py --parallel 4
    python3 run_settings_study_44.py --variants all --parallel 4
    python3 run_settings_study_44.py --report-only

Each dam goes through the whole DamSafety chain (Screening, Runner, Hazard classification,
profile `damsafety`) once per setting, each setting in its own folder under
`~/DamSafety/Settings_Study_44/`. It needs `dams_44.gpkg` (above) and a DEM that covers the dams
(`--dem`, or the statewide VRT in the suite data folder).

| Setting | Engine | Cell | Also |
|---|---|---|---|
| `lf10` | LISFLOOD-FP | 10 m | The recommended settings. Every other one is compared to it. |
| `lf20` | LISFLOOD-FP | 20 m | `GRID_REFINE=false`: with it on, a 20 m run that over-spreads is re-run at 10 m and the comparison is gone. |
| `an10` | ANUGA for the final results, on LISFLOOD-FP's extent | 10 m | Triangles up to 100 m² near the dam, 1,000 m² elsewhere (the Runner's defaults). |
| `an20` | ANUGA, the same way | 20 m | Triangles up to 400 m² and 4,000 m². `GRID_REFINE=false`. |

The default is `lf10,lf20`: hours for the 44 dams. ANUGA takes hours **per dam**, so `--variants
all` is days. Try a few dams with `--only` first. A dam that finished under a setting is skipped
next time, so the study can be stopped and started again and the ANUGA settings added later;
the report always covers every setting that has been run.

Results in the study folder:

| File | What it is |
|---|---|
| `Settings_Study.html` | One page. How many dams keep their class under every setting, what moves it (cell size or engine, and which way), then the dams whose class changes, the dams that cannot be judged yet (a setting failed or has not run), and the rest. |
| `Settings_Study.csv` | One row per dam: per setting the class, the TM11 class, flooded structures, people in them, the deepest of them, the life-loss index, minutes, and the message of a failed run. |
| `<setting>/<dam>/` | Each run, complete, as the chain leaves it. |

A class that changes with the settings is not an error in either run. It marks a dam whose class
rests on something the model does not pin down. Nothing here decides a class. The script exits 1
while any dam is missing a result under a setting that was asked for.

**Not run here.** The script and its report are tested against a stand-in chain
(`tests/test_settings_study.py`). The study itself needs the offline pack, the terrain and, for
the ANUGA settings, the `ndanuga` env, so no real result exists yet.

## A county

    python3 run_county.py --list
    python3 run_county.py --county Cass --kind dams --dry-run
    python3 run_county.py --county "Grand Forks,Walsh" --kind dugouts --parallel 3
    python3 run_county.py --county Cass --kind dams --mode anuga --get-clp

Dams are those with status Intact unless `--status` says otherwise. In `lisflood` mode
Screening fills a missing height or storage from the DEM; `anuga` mode needs both and lists
the structures it skipped in `skipped_no_height_storage.csv`. Output:
`~/ND_County_Runs/<County>/<kind>/` or `<kind>_ANUGA/`.

## The watcher

    python3 batch_watch.py ~/ND_Dams
    python3 batch_watch.py ~/ND_Dams --once
    python3 batch_watch.py ~/ND_Dams --once --with-model

Every two minutes it reads new WARNING / ERROR / FAILED lines in the chain and batch logs and
checks each finished dam by rule: volume error over 1 %, the flood reaching DEM gaps, the
domain cut off with the edge still moving, the over-spread advisory, unverified culvert
guesses, zero flooded area, no breach. Findings go to `BATCH_FINDINGS.md` in the folder, the
Terminal, and a macOS notification.

**The rules are the result.** `--with-model` also asks the local model (`heim`, through Ollama)
for a short note on each flagged dam. That is off by default: a model answering while the
solver runs takes the cores the run needs. Run it afterwards with `--once --with-model`, and
check what it says against the report.
