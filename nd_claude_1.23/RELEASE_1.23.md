# ND dam suite release 1.23

Built 2026-10-14 from release 1.22, to the eight serious bugs the code audit of 1.22 put at the
top of its list to fix first (../gui-review-1.22/CODE-AUDIT.md). You asked to build the eight.

| Plugin | Version |
|---|---|
| ND Dam Hazard Classification Helper | 2.31.0 |
| ND Dam Breach Runner | 3.21.0 |
| ND Dam Breach Screening | 0.29.0 |
| ND Data Updater | 1.5.0 |
| shared `damsuite` package | 1.17.0 (release numbers only) |

Install all four from `plugin-zips/`. Each is the whole plugin; no data files changed, so the
offline pack you already have is still current.

## No screening or breach number moves

Every one of the eight is a bug that made something fail, refuse, or quietly do the wrong thing.
None of them changes the breach flow, the flood, or the class of a run that already finished. A
dam you have already modelled does not need to be run again; these fixes let runs that were
failing or going astray finish and land in the right place.

## The eight

### Runner

**1. "Run a queue of dams from a list" now works.** It failed on its very first dam, every time,
with a Python error, and left that dam marked "running". The per-dam set-up step was handed the
wrong kind of progress object. It now gets the real one, so the queue runs through the whole
list; if one dam cannot be set up, it is recorded as a failed dam and the queue carries on
instead of stopping.

**2. The breach is placed on the right dam's crest line.** When you give a crest-line layer, the
breach is now put along *this* dam's line — matched by DWR structure number, or the nearest line
within 500 m — instead of half way along the nearest line of *any* dam in the search box, which
on a tight cluster could be a neighbour's dam. "Breach position" is now optional: leave it empty
to breach where the dam point meets the crest line. Before, the dialog defaulted it to 0.5, and
that default is what forced the breach onto the nearest-line.

**3. A cancelled or crashed run is run again, not skipped.** The queue, and the window's grey/
green results buttons, treated a dam as "already run" as soon as `Depth (Max).tif` existed — even
if that raster was left by a run you cancelled or that died part way. "Done" now means a finished
run (the run report says so), so a half-finished dam is picked up again and its results buttons
stay off until it really finishes.

### Screening

**4. "Merge dams and ponds" (step 18) and "Move breach point" no longer fail on a QGIS
GeoPackage.** Both write with plain Python, and a GeoPackage that QGIS has written carries a
spatial index whose internal triggers call a function (`ST_IsEmpty`) that plain Python does not
have. The merge stopped with "no such function: ST_IsEmpty", and the move logged a failure and
left the point where it was. The spatial index on that one layer is now removed before the write
(QGIS rebuilds it when it next needs one); your data is untouched.

**5. "Download the terrain for a county" now covers the whole county.** The county outlines used
to decide the download were hand-typed boxes, and they ended *inside* 48 of the 53 counties — in
McLean by about 34 miles on the west side. A county DEM download quietly stopped at that edge, so
dams in the missing strip came back with no terrain. The extents are now taken from the county
outlines the suite already ships, so every county downloads edge to edge.

### Data Updater

**6. On Windows, checking the data lock can no longer end another QGIS.** When the updater
checked whether a second QGIS was holding the data folder, it used `os.kill(pid, 0)` — which on
Windows does not ask "is it alive", it *terminates* the process. It now asks the Windows kernel
instead, and a crashed run's stale lock is recognised right away rather than holding the folder
for six hours.

**7. One data folder for the whole suite.** The folder the dam plugins use (chosen in the suite
home) is now the one the updater builds into. Before, the updater kept its own separate folder;
if you set the folder in the suite home, the updater's quiet start-up check would build a second
pack in its default location and switch the dam plugins onto that. The quiet check also no longer
starts a build before any data folder has been chosen, so a fresh install does not download
gigabytes unasked — the first build needs a click.

### Shared (Hazard Helper + Data Updater pack builder)

**8. The offline pack builder keeps the layers you put in by hand.** When you rebuild or update
the pack, a layer you replaced with your own file — and the 3DHP flowlines/waterbodies cut from
the National Map file — are now kept, instead of being silently overwritten by a fresh download
on the next build. And a layer this build could not fetch no longer drops a good copy already in
the pack.

## Testing

All four plugins' test suites were run against this release and against 1.22 as a baseline, on
this Linux container. Every fix introduces zero new test failures beyond the ones that are
already environmental here (the container's QGIS/GDAL/SciPy are partly broken, so a set of
GDAL-dependent tests fail identically in 1.22 and 1.23). The behaviour changes in fixes 3, 4 and
5 are covered by new or updated tests:

- Fix 3: the Runner queue/button tests now require a finished-run report, not just a depth raster.
- Fix 4: verified against a hand-built GeoPackage with the standard spatial-index triggers — the
  write fails with "no such function: ST_IsEmpty" before the fix and succeeds after it.
- Fix 5: the county extents are now generated from the shipped outlines (all 53 counties present).

The full source change is in `changes_vs_1.22.diff`.
