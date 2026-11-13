# Batch runs: many dams, headless

Three scripts that run dams through `dam_chain.py` one after another or side by side, and a
watcher that checks each finished dam by rule. They lived in zip files until 2026-11-13; this
folder is now the copy to use, and `tools/mac/use_local_terrain.sh` looks here first.

| File | What it does |
|---|---|
| `run_lisflood_44.py` | The 44 dams in `dams_44.csv` through the full ND chain (Get data, Screening, Runner with LISFLOOD-FP, Hazard classification), profile `nd`. |
| `run_anuga_culverts_44.py` | The same 44 through the DamSafety Runner only, LISFLOOD-FP for the extent and ANUGA for the final results, with culverts cut into the terrain, profile `damsafety`. |
| `run_county.py` | Every DWR dam or dugout in one county, several, or the State, in either of those two modes. |
| `batch_watch.py` | Watches a run folder and writes findings as dams finish. Rules only unless you ask for the model. |
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
| `dams_44.gpkg` | `run_anuga_culverts_44.py` | the batch-44-dams package |
| `nd_dams_dugouts.csv` | `run_county.py` | the county-batch package |
| `nd_dams_dugouts.gpkg` | `run_county.py --mode anuga` | the county-batch package |

The scripts look next to themselves, then in `$BATCH_DATA_DIR`, then in the folders those
packages unpack to under your home, Downloads and Documents. `--dams` and `--data` name a file
directly. When one is missing the script stops and lists where it looked.

## The 44 dams, LISFLOOD-FP

    python3 run_lisflood_44.py --dry-run
    python3 run_lisflood_44.py --watch
    python3 run_lisflood_44.py --parallel 4 --only DWR-2801,DWR-3034

Dam folders go in `~/ND_Dams/DWR-xxxx`. It needs the offline pack in
`~/ND_Suite_Data/ND_Dam_Suite_Pack`.

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
