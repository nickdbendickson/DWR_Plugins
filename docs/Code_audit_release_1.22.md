# Code audit, release 1.22

This is the audit half of what you asked for. The simplification half is in `SIMPLIFY.md`.

Every source file of the four plugins was read, 224,000 lines in 28 units, by two reviewers with different
jobs: one looking for wrong results, hidden failures, lost work and crashes, one looking for
duplicated code, dead code, settings defined twice and tools that overlap. Every finding was
then handed to a third reviewer whose job was to disprove it by reading the same code. Findings
that reviewer disproved are not in this document. Everything that survived is in
`FINDINGS-APPENDIX.md`, one line each, so nothing is lost.

I checked the serious ones myself rather than take them on trust. Where I say "checked", I read
the code or ran it: the county boxes against the shipped county outlines, the GeoPackage writes
against a real GeoPackage with GDAL, and the rest by reading the call path end to end.

Counts: **561 findings** across the Runner, the Screening, the Updater and the shared `damsuite`
package: 22 serious, 204 medium, 335 minor. The Hazard Helper's own modules were read in the same
way; that part is noted at the end.

## Fix these first

Eight things. Each one gives a wrong answer, loses work or stops a tool, and each is a small fix.
They are in the order I would do them.

### 1. The dam queue cannot run at all

`nd_breach_runner/algorithms_release320.py:580`

"Run a queue of dams from a list", the headline feature of Runner 3.20, fails on its first dam,
every time, with `AttributeError: '_Feedback' object has no attribute 'pushInfo'`. The queue hands
the per-dam set-up its own small progress helper instead of the QGIS progress object that set-up
expects. The helper has three methods (`algorithms.py:220`) and the set-up calls a fourth
(`algorithms.py:1255`, `1263`, `1266`). The error is not one of the three the queue catches, so the
whole tool stops and the first dam is left marked "running" in `_queue/breach_queue.json`.

Checked by reading `_one`, `make_fb`, `class _Feedback` and `prepare` end to end. The only test of
the queue replaces `_one` with a stub (`tests/test_v3200.py:1107`), which is why this shipped.

Fix: pass the real `feedback` to `alg.prepare(p, context, feedback)`. The set-up runs on the main
thread, so there is no reason to use the adapter there. Give `_Feedback` pass-throughs for
`pushInfo`, `pushWarning` and `reportError` as well, and catch `Exception` around each dam so one
bad dam is recorded as failed instead of ending the queue. Then make the queue test run the real
`_one` at least once.

### 2. The breach can be placed on another dam's crest line

`nd_breach_runner/algorithms.py:1032`

When a crest-line layer is given, the tool first picks the line properly: the line whose DWR
structure number matches this dam, else the nearest line within 500 m, else none, and it writes
"Dam crest line not used: the nearest crest line is 8231 m from the dam point" when it refuses.
The next block then ignores that decision. It re-reads every line in the search box, which is
three quarters of the largest domain side (15 miles at the 20 mi default; 45 miles at the audit's 60 mi default), picks the line with the nearest
single vertex, and puts the breach half way along it.

That block runs whenever "Breach position" has a value, and it is declared with 0.5 already filled
in (`algorithms.py:491`), so from the Processing dialog it always runs. The log says only "Breach
starts at 50% along the crest line".

Two consequences. For a dam whose own line is missing from the DWR dam lines layer, the breach
starts on a different dam's crest, possibly miles away, and the whole flood map is for the wrong
place. For a dam that does have its line, the point the engineer put on the crest is silently
replaced by the midpoint of the line.

Checked: `_pick_crest_line` at `algorithms.py:164`, the two blocks at `1013` and `1032`, the
parameter default at `491`, and `_point_along_nearest` at `112` (which measures to the nearest
vertex, not the nearest point on the line, so it can even pick a different line than the first
block did).

Fix: measure the position along the line `_pick_crest_line` already chose, and run that block only
when a line was chosen. Make "Breach position" optional with no default so the "move the dam point
onto the crest line" path can happen again.

### 3. A cancelled dam is skipped as "already run", and its half-finished rasters stay as the result

`nd_breach_runner/core/batchqueue.py:275`, `core/workflow.py:1069`

Since 3.18 every finished model pass writes `Depth (Max).tif`, `Velocity (Max).tif` and
`Arrival Time.tif` into the dam folder under their final names, so a cancelled run does not lose
everything. Nothing on disk says they came from an unfinished run. The queue's "is this dam done"
test asks only whether `Depth (Max).tif` exists, and the window's "can I press the results
buttons" test asks for those same three names (`core/runblock.py:63`).

So: cancel the queue on dam 7 of 40 while it is in its second domain pass, then run the queue
again with the same list, which is what the log tells you to do. Dam 7 is reported "skipped:
already run (the dam folder has the final rasters)" and is never finished. The CSV says every dam
is done. Its folder holds the first pass: a smaller domain, a shorter clock, no WSE raster, no
`run_report.json`. Classification, consequences, the submittal and the hand-off all run on those
rasters with no warning, and the earlier run's classification files have already been deleted
(`STALE_ON_RERUN` is applied at the first pass).

Fix: write the per-pass rasters inside `_breach_model/` and copy them up under the final names
only when the run finishes, or leave a `pass_in_progress.json` while it runs. Make the queue's
test and the window's test require a finished run, which means `dam.json`'s latest "modelled"
status or `run_report.json`, not a file name. A dam that was left "running" should go back in the
queue, never be skipped.

### 4. Two Screening steps cannot write to a GeoPackage that QGIS wrote

`nd_dam_screening/core/merge.py:61`, `core/review.py:215`

Step 18 "Merge dams and ponds" and the Review tab's "Move breach point" both write with plain
Python `sqlite3`. A GeoPackage that QGIS or GDAL wrote carries rtree spatial-index triggers, and
those triggers call `ST_IsEmpty`, a function that only exists inside GDAL's SQLite dialect. Plain
`sqlite3` does not have it, so the INSERT or UPDATE fails.

I reproduced both in this container: writing a QGIS-written GeoPackage with plain `sqlite3` gives
`no such function: ST_IsEmpty`, and the same write through GDAL succeeds. The merge module's own
docstring says "Nothing here needs QGIS or GDAL. Not run in a real QGIS", and the test writes its
GeoPackages with a hand-rolled writer that creates no triggers (`tests/test_merge.py`), which is
exactly why the test passes and the tool does not.

What you see: step 18 stops with "merge failed: no such function: ST_IsEmpty", so the Simple tab's
"Both" chain can never get past the merge and steps 5 to 10 never see dams and ponds together. In
Review, the new coordinates are written to `review_decisions.csv` and a box says the layer could
not be written, but the point does not move, and the next runs of steps 5 to 8 use the old point.

Fix: do both writes through GDAL/OGR (`ogr.Open(path, 1)` and a layer `CreateFeature` /
`SetFeature`), or drop and recreate the rtree triggers around the plain write. Then add a test that
writes a GeoPackage made by GDAL, not by the hand-rolled writer.

### 5. County terrain downloads stop short of 48 of the 53 counties

`nd_dam_screening/core/dem_download.py:54`

The county bounding boxes are hand-typed, above a comment saying they are padded outward so a dam
on the county line is inside. I compared all 53 against the county outlines the suite itself ships
(`core/nd_counties.geojson`, the same file that decides which county a dam is in). Only five boxes
cover their county. The worst are McLean, short 34 miles on the west, Morton 21 miles, Sioux 17,
Logan 13, Cass 12, Grand Forks 12, Dunn 12, Ward 11.

The same table drives the 3DEP county download (`algorithms/download_dem.py:145`), the ND DWR
LiDAR tile pick (`dem_download.py:376`) and the rectangle the DEM tab draws on the map
(`dialog.py:954`), so the picture on screen looks like the county and nothing says anything is
missing. Dams in the missing strip have no terrain: the clip fails with "less than half of the
terrain clip has data", or step 7 reports FAILED, or the flood is cut at the DEM edge. A McLean
County screening quietly leaves out the western third of the county.

Fix: delete the table. `core/nd_counties.py:113` already has `box(counties, pad_deg)`, built from
the outlines. Use it for the county list, the county box and the LiDAR tile pick, and add a test
that each county box contains that county's outline.

### 6. The Updater can kill your other QGIS, and locks the folder for six hours after a crash

`nd_data_updater/core.py:107`

The lock asks whether the process that holds it is still alive with `os.kill(pid, 0)`, under a
comment saying signal 0 asks the kernel without touching the process. That is true on Linux and
macOS. On Windows, Python maps every signal except the two console ones to `TerminateProcess`, so
on the platform you actually run this on, the check ends that process.

What that means: two QGIS windows open, one is 40 minutes into a 90-minute pack build. In the
other window, "Check for data updates now", or the automatic check that fires 20 seconds after
QGIS starts and then hourly, reaches the lock and kills the first QGIS. Unsaved project edits go
with it and the build dies mid-write.

The other half of the same block: on Windows a pid that no longer exists raises `OSError
[WinError 87]`, not `ProcessLookupError`, and the catch-all treats that as "still running". So
after a real crash the folder stays locked with "another update is already running" until the
six-hour stale-lock timeout, which is the very thing the 1.4.0 note says was fixed.

Fix: on `os.name == "nt"` do not call `os.kill` at all. Use `ctypes` `OpenProcess` with
`PROCESS_QUERY_LIMITED_INFORMATION` and `GetExitCodeProcess`: a failure with
`ERROR_INVALID_PARAMETER` means the pid is dead, exit code 259 means alive. Keep `os.kill(pid, 0)`
for Linux and macOS, and run `tests/test_lock.py` on Windows.

### 7. The Updater's data folder is still a second setting, and its quiet first run moves the suite onto it

`nd_data_updater/settings.py:26`

The Updater reads its own setting (`nd_data_updater/data_dir`, default `~/Documents/ND Structure
Data`) and never looks at the suite folder (`ndsuite/data_folder`) the other three plugins read.
The 1.20 sync is one way: the Updater pushes its folder to the suite, the suite never pushes back.
The suite home's own "Suite folder" button writes only the suite key.

So the path the home itself recommends breaks it. Pick the folder on the Start tab, drop the DWR
exports and the DEM in, and the Updater is still on its default. The automatic check is on by
default and fires 20 seconds after QGIS starts, so it downloads tens of gigabytes for 90 minutes
into that other folder, and when it finishes it calls `adopt_folder` on its own folder
(`packsource.py:628`), which rewrites the suite folder for all three plugins. Their `dwr/`, `dem/`
and `nlcd/` are now the empty ones and the next classification runs without the files you put in.
Nothing on screen says the folder changed.

The configured screenshots show exactly this split: the suite folder is the one I adopted, with a
pack and a Dams folder in it, while both Updater windows show `/root/Documents/ND Structure Data`
and "not built yet".

Fix: make it one setting. `Settings.data_dir` returns `suitedata.data_folder()` when the suite has
one and falls back to the old key only for an old profile; the setter goes through
`adopt_data_folder`; `_share` stops calling `adopt_folder` with its own path. And until a folder
has been chosen, the automatic check must not start a run.

### 8. The next Updater run puts back the download over your own file

`nd_data_updater/ndpack/build_nd_pack.py:1697`

At the end of every build, the step that writes the pack folder walks every layer in the working
file, not only the ones this run fetched, and rewrites any folder layer whose date or feature count
differs. The folder's copy differs exactly when it is the better one: a layer you replaced with
your own file (the 1.4.0 feature), the 3DHP flowlines and waterbodies cut from the 12 GB National
Map file, which are written straight to the folder and never into the working file, or a layer
added from a Geofabrik file.

So the run after you drop in your own county roads or dam file silently puts the old download back
and clears the "replaced" flag, so that layer also returns to the 30-day refresh cycle. The log
says nothing. When the 3DHP service was down and the build fell back to the National Map file, the
freshly cut flowlines are replaced by the 30-day-old service copy still in the working file, so the
12 GB download bought nothing.

The same loop has a second failure. When the working file has no usable copy of a layer and this
run's fetch fails, the folder's complete copy is dropped from the manifest and listed under
"left out", with the GeoPackage left orphaned on disk. That is the case when a pack is copied from
another computer with no `_work` beside it: one service down that day, and a layer that was fine
before disappears from the three plugins' view. `packsource`'s own promise is "a layer that cannot
be fetched keeps its old copy".

Fix: pass `_write_folder` the names this run actually fetched and convert only those, plus names
whose GeoPackage the folder lacks. Never replace a folder entry that is marked replaced or whose
date is newer than the working file's, and never drop a usable folder entry because a re-fetch
failed; append the failure note to it instead.

## Also serious

Confirmed by a second reviewer, each a wrong result or lost work, each worth the next release.

- **The 1.22 shallow-basin rule can throw away the measured curve for a full reservoir.**
  `nd_dam_screening/algorithms/estimate_storage.py:217`. The guard I added compares
  `est_crest_m - est_pool_m` with half the dam height, and for water found in the DEM that span is
  the freeboard, not the depth of the basin: the pool elevation is the water surface
  (`core/storage.py:183`), and the note that arms the rule is written for flat water as well as for
  a dry depression (`core/storage.py:369`). A full reservoir sitting a few feet below the crest of
  a 20 to 30 ft dam therefore fails the test, its measured stage-storage curve is discarded, and an
  assumed quadratic curve from the ground at the dam point is used, with `stor_method` "inventory
  curve". It only bites dams with no mapped water body within the search distance, which is the
  dry-dam case the rule was written for plus any reservoir missing from the hydro layer. Fix:
  arm the rule only for the closed-depression branch, or compare `span + max_depth_m` with the dam
  height. The new test only covers the dry case.
- **Nine report columns are never filled.** `nd_dam_screening/algorithms/reports.py:13`. The report
  code reads `hz_basis`, `hz_border`, `hz_gaps`, `kind`, `h_src`, `stor_src`, `stor_notes`,
  `status` and `source`, all of which tool 8 and tool 1 do write, but the record the tool hands it
  is built from a fixed field list that has none of them. So `summary.csv` has empty border and
  data-gap columns, the Hazard portfolio dashboard reads blanks, the "ESTIMATED height / storage"
  review reason never appears, a class from the conservative no-chart method is shown as plain
  "High" with no label, and every dam page says "Structure kind: DWR dam" including LiDAR ponds and
  dugouts.
- **Tool 0 never writes the structure points layer, and the log says it did.**
  `nd_dam_screening/algorithms/data_package.py:177`. Every package built by tool 0 lacks
  `structure_points` while the run log reads `structure_points (523,000)`. The window falls back to
  the polygon structures, so it is not fatal, but the hand-off to the Runner is short a layer.
- **The self-test deletes a folder you chose.** `nd_dam_screening/algorithms/selftest.py:139`.
  "Work folder (empty = a temporary folder)" accepts any folder, and when "Keep" is unticked, which
  is the default, the whole folder is removed with everything in it. `ignore_errors=True`, so
  nothing is said. From the window the folder is always a temporary one; from the Toolbox it is
  whatever you picked. Fix: only delete a folder the tool made itself.
- **Opening a settings file overwrites its run options with the window's.**
  `nd_dam_screening/dialog.py:1512`. The grid's `load()` calls the change handler before it returns,
  and that handler copies the window's current choices into the freshly loaded settings and saves
  the file; only then does the window read the file's "window" rows back, so it reads what it just
  wrote. Tool rows load correctly, so it looks as if the file loaded. Two engineers sharing a
  settings file overwrite each other's run options every time they open the window.
- **The nested run models a different dam than the one-dam tool does.**
  `nd_breach_runner/core/workflow.py:1556`. `preview()` writes its own top-of-dam guess, the highest
  DEM cell within 30 m, into the caller's dam object, and the nested tool then hands that same
  object to the run, which treats the guess as a value you gave: no "top of dam estimated" warning,
  and the crest line raised to a road or a hillside spur. The same dam through "Run breach model for
  one dam" uses the 98th percentile instead and gives a different crest. Fix: `preview()` must work
  on a copy.
- **A second run leaves the first run's baseline rasters in the dam folder.**
  `nd_breach_runner/core/workflow.py:1391`. Only the rasters the new run produces are rewritten.
  Run the PMF flood case and then the sunny-day case into the same folder, the normal order, and
  the sunny-day run keeps the flood run's `Baseline Depth (Max).tif` and
  `Incremental Depth (Max).tif`. "Life safety and damage" then computes the sunny-day incremental
  consequences against the flood run's no-breach case, and the hand-off passes that stale baseline
  to the Hazard Helper, with no warning. Fix: delete every result raster the new run will not write.
- **The reservoir-in-grid engine crashes instead of falling back.**
  `nd_dam_screening/core/runner.py:243`, when the reservoir reaches the largest clip.

## The patterns behind the other 539

The long tail is not 539 separate problems. It is about ten habits, and most of the medium
findings are instances of one of them. Fixing the habit is the simplification work in `SIMPLIFY.md`;
what follows is what the audit adds to it.

**Six window files are copied by hand and nothing checks them.** `damsuite` is guarded by a
`FILES.json` checksum, but `suite_home.py`, `suite_data.py`, `suite_data_tools.py`,
`suite_settings.py`, `suite_settings_tools.py` and `suite_styles.py` are not, and they have already
drifted: the Hazard copy of the data window is 878 lines with the DWR files row, the Runner and
Screening carry an 830-line pair without it. `proposed.py` and `nd_counties.py` are shared the same
way with no master declared. Add all of them to the checksum, and make the release script copy them.

**The same quantity is defined in two plugins with different values.** Manning's n by land cover
(`nd_breach_runner/core/nd_data.py:21` against the Screening's `solver_io.py:22`) disagrees on
cropland, pasture and developed land, so the same dam floods differently depending on which plugin
ran it. "Wet" is defined in several places with different depths, and the dams table's area and the
extent polygon disagree. Two tile-size limits for the same 3DEP request. Two breach-routing
implementations whose equations agree and whose routing does not.

**Two tools for one job, and they have drifted.** Batch and Queue both run many dams through the
same function, and only one of them fills each dam's inputs from its dam folder. Two HEC-RAS plan
readers, one per plugin, with different abilities. Two ND 5.2 rule engines that disagree on roads.
Two menu entries in the Runner that classify the same dam folder. `preview()` is a hand copy of the
run set-up. Each of these is a deletion, not a merge.

**Tool defaults disagree with the window that drives them.** Tool 7 and tool 2 ship with a
different engine and a different cell size than the window passes; tool 9's sensitivity variants
pin terrain to "cached clips only" and ignore the preset, so they run at Quick 50 m whatever you
chose; the batch's preset silently overrides the cell size typed in the same dialog. Anyone running
a tool from the Toolbox gets a different run than the window gives.

**Failures that read as success.** A failed land-cover step leaves no trace in the manifest, so the
end-of-run block says every layer is in the pack. "Stop" promises the finished work is kept and
deletes it for every dataset except the pack. With the pack dataset turned off the pack buttons do
nothing and report that everything is up to date. A pack layer whose file is missing is dropped
without a word while the dialog still counts it. An all-failed step 7 only stops the chain when no
dam was cleared by the quick screen.

**Signed-off tables that are not used, or are lost.** The signed fatality rate table is written
inside the plugin's own folder, so the next plugin update throws away the engineer's signature
(`nd_breach_runner/dialog.py:754`). "Sign off the fatality rate table" writes to a temporary file by
default, so the signed table is never read. A table signed only in "checked by" is recorded as
signed and then not used. And loss of life is printed in bold from an unsigned, from-memory table
in one of the two paths through the Consequences tool.

**The report names the wrong model.** The submittal report and the Verification Record both say
LISFLOOD-FP even when the built-in engine produced the results. Every elevation at or above 1,000 ft
is rounded to the whole foot in the submittal.

**Long work on the window's thread.** Between steps the chain clips the pack or downloads server
data with no progress bar and no Cancel; every window refresh re-reads and SHA-256s whole output
folders, about twenty times in a row while the widgets are being filled; "Copy pack to USB" hashes
10 to 40 GB back off the stick on the GUI thread with no progress and no Cancel; every download is
held whole in memory, two to three copies, and cannot resume.

**Tests that cannot fail.** Tests that cannot run print "ok" in all four suites, so a skip counts
as a pass. The Hazard suite's own test double hides GDAL, so four tests that need it fail instead
of skipping. The built-in engine's "conserves volume" test checks a column the engine always
writes as zero. The queue test replaces the function under test, which is why finding 1 shipped.
The Screening has 52 standalone scripts and no runner and no aggregate exit code. Release numbers
are pinned as literals in the Hazard suite, so the next release breaks tests that pass today.

**Release numbers by hand.** A release needs about ten hand edits in five files plus five test
edits, three copies of `damsuite` and a `FILES.json` rebuild. `damsuite/versions.py` could drive all
of it. Two of the four plugins already stamp stale versions into `dam.json`. The Hazard README still
says 2.30.0.

## What was checked and is sound

Worth saying, because it is most of the suite.

The ND 5.2 classification rules, the ACER TM-11 curve evaluation and the chart sign-off mechanism
read correctly against the regulation text. The breach-parameter equations (Froehlich 1995 and
2008, MacDonald and Langridge-Monopolis, Von Thun and Gillette) match their papers, and the
reservoir drawdown routing conserves volume. The LISFLOOD-FP wrapper, the parameter file writing
and the result reading are sound, including the 1.22 dry-dam exponent change. The structure
inventory build, the NSI 2026 merge, the habitability rules and the address de-duplication hold up.
The GeoPackage pack format, its manifest and the 30-day refresh logic are sound apart from the two
`_write_folder` findings above. The proxy fix from 1.21.1 is applied on every download path I
followed. One security finding, and a mild one: the Runner-against-Screener comparison sheet puts dam names
and the runner's own text into HTML without escaping it (`nd_dam_screening/core/comparison.py`), so
a dam name with a `<` in it breaks the page. Nothing runs a shell, no `shell=True`, and no path is
built from downloaded text.

## The Hazard Helper

The Hazard Helper's own modules, about 69,000 lines in ten units, were read by the same two lenses
and are going through the disprove stage now. Three of its serious ones are already clear
and worth stating here because you will want them in the same release:

- **A scenario with no velocity raster puts every structure in the LOW zone.**
  `ND_Dam_Hazard_Helper/structures.py:2165`. The substitution of zero for a missing velocity is
  guarded by "the velocity raster loaded", so when there is no velocity raster at all the velocities
  stay as not-a-number, every comparison against the chart curves is false, and the zone comes out
  LOW for a house in 20 ft of water. The note "velocity missing on some wet cells (used 0)" is
  written only in the case where the raster did load, so nothing on screen says why. This is
  reachable today: the Runner's near-dam refine writes no velocity raster (the known FV1 bug), so a
  classification of such a run reports no probable loss of life at all. Checked by reading
  `classify_values`, `apply_type` and `structure_outcome`.
- **The suite chain's screening step can never run.** `chain_algorithms.py:284`. `ndhazard:screendam`
  and the SCREEN step of `suitechain` pass dam-folder parameters to `nddam:triage`, which has none.
- **A review checklist with comments but no decisions is silently overwritten.** `reviewpkg.py:726`.

`FINDINGS-APPENDIX.md` in this folder holds all 561 findings for the other three plugins and the
shared package, one line each with the file, the line, what you see and the fix, grouped by plugin
and by severity. The Hazard Helper's own list is added to the same file when its last reviewers
finish, and I will say so here.
