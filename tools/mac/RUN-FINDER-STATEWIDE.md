# Run the Finder over the whole state (on the Mac)

This runs the DamSafety Finder tool *Fills across streams and draws* over all of North Dakota
from the 1 m terrain on the SSD, in 10 km tiles, and puts every fill it finds into one
GeoPackage with a ranked list of the dam candidates that are on no list.

Two files do it: `run_finder_statewide.sh` (what you run) and `run_finder_statewide.py` (what it
starts). Keep them together in `tools/mac`, next to `run_nickreview.sh`.

**It only reads** the terrain on the SSD, the ND data pack and the installed Finder plugin. It
writes into one folder, `~/ND_Finder_Statewide`, and nowhere else. It downloads nothing.

## Before you start

* The suite is installed (`install_dwr_suite_mac.sh`) and **DamSafety Finder is 0.6.0 or later**.
* The SSD is plugged in: `/Volumes/Extreme SSD/ND_DEM_1m/ND_1m_26914.vrt`.
* The data pack is in your suite data folder (`~/ND_Suite_Data/ND_Dam_Suite_Pack`).
* The Mac is on power. The script keeps it awake while it runs.
* About 3 GB free on the Mac's own disk.

## 1. Look first (about a minute, writes nothing)

Open Terminal, then:

```bash
cd ~/Downloads/DWR_Mac_Setup          # the folder that holds run_finder_statewide.sh
bash run_finder_statewide.sh --dry-run
```

It prints which Finder, terrain and pack it found, how many tiles there are, how many have
terrain, and its estimate of the time, memory and disk. Expect about **1,950 tiles** of 10 km.
If it says something was not found, it also says which option to give. The two you may need:

```bash
bash run_finder_statewide.sh --dry-run --dem "/Volumes/Extreme SSD/ND_DEM_1m/ND_1m_26914.vrt" --data "$HOME/ND_Suite_Data"
```

(Paths with spaces go in quotes.)

## 2. A trial (about 10 to 20 minutes)

```bash
bash run_finder_statewide.sh --limit 4
```

This runs the first 4 tiles (the south-west corner of the state), then merges them. Open
`~/ND_Finder_Statewide/ND_finder_summary.txt`. If it looks right, go on. The 4 tiles are kept
and are not run again.

After the trial, `--dry-run` uses the Mac's own tile times, so its time estimate is then real.

## 3. The whole state

```bash
bash run_finder_statewide.sh
```

* It runs 4 tiles at a time. Every time a tile finishes it prints a line like
  `tiles 212/1947 done, 4 running, 0 failed - 48,120 fills so far - 5 h 02 min elapsed, about 1 d 9 h left (to Sun 07:40)`.
* **How long:** not timed on your Mac. On the cloud test box a tile full of terrain took
  4.5 minutes of processor time, which makes the state about **130 processor-hours: about a day
  and a half with 4 at a time** if a Mac core is as fast as a test-box core. The time left that
  it prints after the first tiles is your Mac's own figure; go by that.
* **Memory:** each tile used 1.2 GB at its peak on the test box, so 4 at a time is about 5 GB of
  your 32 GB. If you want it faster, `--workers 6` or `--workers 8` is safe for memory; whether
  it is faster depends on how fast the SSD reads.
* **To stop:** press **Ctrl-C** once. It stops the tiles that are running, keeps every finished
  tile, and says so. Closing the Terminal window or a restart of the Mac does the same thing
  less politely; nothing is lost but the tiles that were running.
* **To go on:** run the same command again. It skips the finished tiles and says how many are
  left. You can stop and go on as often as you like.
* Do not unplug the SSD while it runs. If you do, the running tiles fail, and after a few
  failures in a row it stops by itself; plug the drive in and run the same command again.
* A tile that fails is tried once more. If it fails again the run goes on without it and names
  it at the end; the same command again tries it again.

When the last tile is done it merges everything (about 5 minutes for the whole state on the test
box) and prints the summary.

## What you get

All in `~/ND_Finder_Statewide`:

| file | what it is |
|---|---|
| `ND_finder_summary.txt` | counts by status and kind, tiles run / skipped / failed, the versions and settings used, checksums of the inputs |
| `ND_finder_candidates_ranked.csv` | the candidates: fills that are **not on a list** (new or unsure), of kind **dam** or **dry embankment**, inside North Dakota, not at the edge of the terrain - highest score first. Open it in Excel or Numbers; `lat` / `lon` are in it |
| `ND_finder_fills.gpkg` | for QGIS. Layer `embankments` = every fill (scored); `candidates` = the ranked list as points; `spillways`; `ridges` (channel-side dikes); `run_info` |
| `tiles/` | one folder per tile (what lets it go on after a stop). Leave it there until you are done |
| `logs/`, `failed/` | a log per run; the log of any tile that failed |

Three columns are new, and they explain why a fill is **not** in the ranked list:

* `in_state` = `no`: the fill is outside North Dakota (the terrain covers a strip of the
  neighbouring states).
* `at_data_edge` = `yes`: the fill is within 150 m of missing terrain. Where the terrain stops,
  the edge acts like a wall and the Finder sees a dam that is not there. `edge_ft` is the
  distance. This also happens round the hole in the terrain over Fort Berthold.
* `id_clash` = `yes`: two different fills got the same `find_id` (expect about 20 pairs in the
  whole state). Tell them apart by `tile` and `lat` / `lon`.

Expect roughly 450,000 fills in all, most of them low-scoring, and about 300,000 rows in the
ranked list (a CSV of about 160 MB: it opens slowly in Excel or Numbers). The ranked list is the
part to look at, from the top; in QGIS the `candidates` layer is the same list as points.

The outline of the state: the pack has no county layer yet, so the script uses a simple
built-in outline (straight west, south and north borders, the Red River and Bois de Sioux on the
east) with a 600 m allowance. It never drops a fill that is in North Dakota; within about 1 km of
the border it can keep one that is not. If you have a state boundary file, add
`--state-outline "/path/to/ND_boundary.shp"`: it needs only `--merge-only` to apply, not a new run.

## Useful variations

```bash
bash run_finder_statewide.sh --list                          # every tile and what it would do; writes nothing
bash run_finder_statewide.sh --bbox=-101.0,46.4,-100.5,46.8  # only the tiles that touch this box (W,S,E,N)
bash run_finder_statewide.sh --merge-only                    # merge what is finished so far (while you wait, or after a stop)
bash run_finder_statewide.sh --merge-only --no-score         # no score; the list is ranked by height
bash run_finder_statewide.sh --merge-only --review-batch 90  # also draw a 90-card review batch from the candidates
bash run_finder_statewide.sh --workers 6                     # 6 tiles at a time
bash run_finder_statewide.sh --redo --bbox=...               # run finished tiles again
bash run_finder_statewide.sh --out "/Volumes/Extreme SSD/ND_Finder_Statewide"   # write somewhere else
```

* `--bbox` and `--limit` runs are kept: a later full run does not run those tiles again.
* `--county NAME` works once the pack has its county layer (`boundaries/ND_counties.gpkg`), or
  with `--county-file` and any polygon file that has the county names. Until then use `--bbox`.
* `--merge-only` can be run while a full run is stopped, never at the same time as one (it
  says so and refuses).
* Do not point `--out` into the data folder, the pack, the terrain folder or the QGIS profile;
  it refuses.

## What to send back

1. `ND_finder_summary.txt`
2. `ND_finder_candidates_ranked.csv`
3. `ND_finder_fills.gpkg` (zip it; expect a few hundred MB to 1 GB)
4. the `logs` folder, and the `failed` folder if there is one
5. two numbers from the summary's line `tile time:` - the time per tile and the highest memory
   of one tile on the Mac. Neither has been measured on a Mac yet.

Not the `tiles` folder.

## If something goes wrong

| it says | do this |
|---|---|
| `the terrain file was not found ... The drive ... is not connected` | plug the SSD in; or give `--dem` |
| `the ND data pack was not found` | give `--data` with your suite data folder |
| `the DamSafety_Finder plugin was not found` | install the suite, or give `--plugins` with the folder that holds `DamSafety_Finder` |
| `DamSafety_Finder ... is version 0.5.0; 0.6.0 or later is needed` | update the plugin |
| `another run (process N ...) is using ...` | one is already running. If you are sure none is, delete `run.lock` in the output folder |
| `N finished tile(s) were made with other settings` | the Finder version or the terrain changed since those tiles ran. They are kept but not merged. `--redo` runs them again; or keep the old plugin until the run is finished |
| `N tile(s) FAILED` | run the same command again. If the same tiles fail again, send `failed/<tile>.log` |
| nothing prints for a long time | with some conda versions the screen shows nothing until the end. The log is always live: `tail -f ~/ND_Finder_Statewide/logs/run_*.log` in a second Terminal window |
