# Run the Finder over the whole state (on the Mac)

This runs the DamSafety Finder tool *Fills across streams and draws* over all of North Dakota
from the 1 m terrain on the SSD, in 10 km tiles, and puts every fill it finds into one
GeoPackage with a ranked list of the dam candidates that are on no list, and a short list of
the top 2,000 to open first.

Two files do it: `run_finder_statewide.sh` (what you run) and `run_finder_statewide.py` (what it
starts). Keep them together in `tools/mac`, next to `run_nickreview.sh`.

**It only reads** the terrain on the SSD, the ND data pack and the installed Finder plugin. It
writes into one folder, `~/ND_Finder_Statewide`, and nowhere else. It downloads nothing.

## Before you start

* The suite is installed (`install_dwr_suite_mac.sh`) and **DamSafety Finder is 0.7.0**: update
  the plugin first, then start. (0.6.0 still runs, but 0.7.0 is the one to use: its ids stay
  the same from run to run, and it sets the false dams at the edge of the terrain aside
  itself; see "The ids" and `at_data_edge` below.) The first lines the script prints say which
  Finder it found. Do the whole state with **one** Finder version: tiles made by another
  version are kept but are neither run again nor merged (see "If something goes wrong").
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
The time is an estimate from the cloud test box, not from your Mac, and the line says so.
If it says `N whose terrain COULD NOT BE READ`, a terrain file the VRT lists is missing or
the drive is not fully connected: sort that out before the long run.
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

The trial shows that everything is in place; it does **not** settle the time. A tile at the
edge of the terrain has little terrain in it and is many times quicker than a full one (on the
test box: 8 to 35 seconds against 2.5 to 4 minutes), and the first tiles of a run, in the
corner of the terrain, can be such tiles. So `--dry-run` keeps the test box's figure until
**three tiles full of terrain** have finished on the Mac, and it says which figure it used and
how many full tiles it has so far. Every estimate counts a tile by how much terrain it holds.
`--list` shows each tile's share of terrain.

## 3. The whole state

```bash
bash run_finder_statewide.sh
```

* It runs 4 tiles at a time. Every time a tile finishes it prints a line like
  `tiles 212/1947 done, 4 running, 0 failed - 48,120 fills so far - 5 h 02 min elapsed, about 1 d 9 h left (to Sun 07:40)`.
* **How long:** not timed on your Mac. On the cloud test box a tile full of terrain took
  4.5 minutes of processor time, which makes the state about **130 processor-hours: about a day
  and a half with 4 at a time** if a Mac core is as fast as a test-box core. At first the time
  left is marked `a first guess, not yet this computer's own figure`. Once a few full tiles are
  done that note goes, and the time left is worked out from your Mac's own run: go by that.
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
  it at the end, with the reason (its own error, or how its process ended); the same command
  again tries it again. Its log is in `failed/` and is taken away when the tile later finishes,
  so what is in `failed/` is what still fails.

When the last tile is done it merges everything (about 5 minutes for the whole state on the test
box) and prints the summary.

## What you get

All in `~/ND_Finder_Statewide`:

| file | what it is |
|---|---|
| `ND_finder_summary.txt` | the Finder version, counts by status and kind, how many candidates score 0.5 or more, tiles run / skipped / failed, the settings used, checksums of the inputs |
| `ND_finder_candidates_top.csv` | **open this one first.** The top 2,000 candidates by score: rank, score, kind, status, height, storage, `lat`, `lon`, tile, `find_id` and `link`. The link opens Google Maps on the satellite picture at that point (nothing is sent anywhere until you click one). It opens at once in Excel or Numbers. `--top 5000` makes it longer |
| `ND_finder_candidates_ranked.csv` | every candidate: fills that are **not on a list** (new or unsure), of kind **dam** or **dry embankment**, inside North Dakota, not at the edge of the terrain - highest score first, every column. About 300,000 rows for the state |
| `ND_finder_fills.gpkg` | for QGIS. Layer `embankments` = every fill (scored); `candidates` = the ranked list as points; `spillways`; `ridges` (channel-side dikes); `run_info` |
| `tiles/` | one folder per tile (what lets it go on after a stop). Leave it there until you are done |
| `logs/`, `failed/` | a log per run; the log of any tile that failed |

The script adds these columns. The first three explain why a fill is **not** in the
ranked lists:

* `in_state` = `no`: the fill is outside North Dakota (the terrain covers a strip of the
  neighbouring states).
* `at_data_edge` = `yes`: the fill is at the edge of the terrain data. Where the terrain stops
  (the state line, the hole in the terrain over Fort Berthold), the missing ground acts like a
  wall, and a spur beside it can read as a dam that is not there. Finder 0.7.0 tests for this
  itself: a fill within 150 m of missing terrain **whose pool reaches the missing terrain**
  gets the kind `at the edge of the terrain data` and no score. Those are the fills marked
  here. A pond just as close to the edge whose water stays clear of it is read correctly: it
  keeps its kind and its place in the lists. `edge_ft` is the distance from the fill to the
  nearest missing terrain, whether or not the fill is set aside (blank: none within 1,000 m).
  (Finder 0.6.x had no such test. For its tiles the script measures the distance itself, to
  200 m, and marks every fill within 150 m of missing terrain, real ponds among them;
  `--edge-m` changes that distance and does nothing with 0.7.0.)
* `excluded` = `yes`: the fill is inside an area you gave with `--exclude-polygons` (below).
  Without that option it is `no` for every fill.
* `id_clash` = `yes`: two different fills got the same `find_id` (expect about 20 pairs in the
  whole state). Tell them apart by `tile` and `lat` / `lon`.

Expect roughly 450,000 fills in all, most of them low-scoring, and about 300,000 rows in the
full ranked list (a CSV of about 160 MB: it opens slowly in Excel or Numbers, which is why the
short list is there). The summary says how many candidates score 0.5 or more. In QGIS the
`candidates` layer is the full ranked list as points; filter it on `rank` for the top ones.

**The ids.** `find_id` is made from the crest point of a fill.

* With **Finder 0.7.0** a fill has the same `find_id` in every run over the same terrain,
  whatever area the run covered, so a mark made on one 0.7.0 run finds its fill in the next
  by id.
* With **Finder 0.6.x** the crest point moves by a few metres with the area a run covers, and
  the id with it.
* An id does not carry over from one Finder version to another. Marks made on a 0.6 run (a
  county run from the toolbox, the cloud test tiles, or a statewide run made with 0.6) are
  matched to a 0.7.0 run **by location** (`lat` / `lon`), not by id; the same holds if a later
  version moves a fill's crest point.

The summary's second and third lines say which Finder the run used and which of the two holds.

**The outline of the state.** The pack has no county layer yet, so the script uses a simple
built-in outline: straight west and north borders, the Red River and Bois de Sioux on the east,
and on the south a line that follows the surveyed border (which is not one parallel: it runs
from 45.9452 N at the Montana corner to 45.9351 N in the east), all with a 600 m allowance. It
never drops a fill that is in North Dakota; within about 1 km of the border it can keep one that
is not. If you have a state boundary file, add `--state-outline "/path/to/ND_boundary.shp"`: it
needs only `--merge-only` to apply, not a new run.

**Ground to leave out.** Some ground makes fills that are not dams whatever their shape. Of 90
candidates marked by hand, 8 were coal-mine spoil and 8 were in the Sheyenne sand hills. The
script has no map of such ground. If you have one (any polygon file: mine permit areas, a
sand-hills outline you draw in QGIS), give it with `--exclude-polygons "/path/to/file.gpkg"`.
A fill inside any polygon stays in the `embankments` layer with `excluded` = `yes` and is left
out of the ranked lists. It too needs only `--merge-only`, not a new run.

## Useful variations

```bash
bash run_finder_statewide.sh --list                          # every tile and what it would do; writes nothing
bash run_finder_statewide.sh --bbox=-101.0,46.4,-100.5,46.8  # only the tiles that touch this box (W,S,E,N)
bash run_finder_statewide.sh --merge-only                    # merge what is finished so far (after a stop; the SSD is not needed)
bash run_finder_statewide.sh --merge-only --top 5000         # a longer short list (0 = none)
bash run_finder_statewide.sh --merge-only --exclude-polygons "/path/to/mine_areas.gpkg"   # leave that ground out of the lists
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
  says so and refuses). It reads the finished tiles, not the terrain, so it also works with the
  SSD unplugged: it then says `terrain NOT READ`, merges the tiles as they are, and leaves the
  count of tiles still to run out of the summary. The Finder must be the version that made the
  tiles.
* Do not point `--out` into the data folder, the pack, the terrain folder or the QGIS profile;
  it refuses.

## What to send back

1. `ND_finder_summary.txt`
2. `ND_finder_candidates_top.csv` and `ND_finder_candidates_ranked.csv`
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
| `N finished tile(s) were made with other settings` | the Finder version or the terrain changed since those tiles ran. They are kept but not merged: one result never mixes Finder versions, and the summary names the versions left out. `--redo` runs them again with the Finder you have now; or keep the old plugin until the run is finished |
| `N whose terrain COULD NOT BE READ` | a terrain file the VRT lists is not where the VRT says (the line under it names the file). Check the SSD; those tiles fail until the file can be read, the others run |
| `N tile(s) FAILED` | run the same command again. If the same tiles fail again, send `failed/<tile>.log` (only tiles that still fail have a log there) |
| `its process was ended from outside by SIGKILL` | the tile itself reported no error: something stopped it - by hand, or macOS when memory ran out. If it happens again, use a smaller `--workers` |
| `could not start the Python of the 'ndqgis' env` | the lines under it are what conda or mamba answered. If the QGIS env has another name, put `DS_QGIS_ENV=thatname` in front of the command |
| nothing prints for a long time | with some conda versions the screen shows nothing until the end. The log is always live: `tail -f ~/ND_Finder_Statewide/logs/run_*.log` in a second Terminal window |
