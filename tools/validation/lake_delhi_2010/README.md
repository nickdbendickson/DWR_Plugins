# Lake Delhi Dam, 24 July 2010: the Runner against a real failure

Lake Delhi Dam (Maquoketa River, Iowa, NID IA01297) failed on 24 July 2010 during a record
flood. The failure is well recorded: the Iowa DNR and an Independent Panel of Engineers
published the dam, the gates, the timeline and the breach, and the USGS surveyed high-water
marks at the bridges below the dam. This folder turns those into a Runner case and compares the
Runner's flood with the surveyed marks.

It is a check of the Runner on a dam it was not built around: a concrete-core embankment
6 times the storage of the sample dam, failing in a flood, with a narrow valley below.

## Run it

```
<QGIS python> tools/validation/lake_delhi_2010/build_case.py
<QGIS python> DamSafety_Runner/scripts/ndbreach_cli.py run ~/DamSafety/Validation/Lake_Delhi_2010/blind.json
<QGIS python> DamSafety_Runner/scripts/ndbreach_cli.py run ~/DamSafety/Validation/Lake_Delhi_2010/as_observed.json
<QGIS python> tools/validation/lake_delhi_2010/score.py ~/DamSafety/Validation/Lake_Delhi_2010
```

`build_case.py` downloads 21 MB of USGS 3DEP terrain (10 m, the dam to Monticello) and writes
the inflow, the gate rating and the two configurations; it prints the three commands that
follow with their full paths. `score.py` writes `Lake_Delhi_2010_Score.md`, `.csv` and `.json`
into the case folder.

The workflow **Validation (real dam failures)** (`.github/workflows/validation.yml`) does the
same with LISFLOOD-FP built from the official source: by hand from the Actions tab, and on a
pull request that changes this folder. It also runs the observed breach once more with Manning's n 0.04. The
scores are in the job's summary and in its `lake-delhi-2010-score` artifact. It is a report,
not a gate.

Without LISFLOOD-FP, add `--lisflood DamSafety_Runner/core/builtin_lisflood.py` to the two
`run` lines and `--cell 40` to `build_case.py`. The built-in engine is the screening stand-in:
at 30 m this valley takes it about 9 hours, at 40 m about 25 minutes a run, and at 60 m its
flow at the bridges swings between nothing and several times the flood (see Results).
`build_case.py --manning-n` sets one Manning's n for the valley (default: the suite's 0.06).

| Run | The breach |
|---|---|
| `blind` | The Runner works it out from the dam's height and storage (Froehlich 2008, overtopping), as it would for a dam that has not failed. |
| `as_observed` | 235 ft wide, formed in 2.17 hours: what was reported. This run tests the flood model alone. |

Both start the breach at 12:30 p.m. on 24 July, when it was seen to speed up.

## What is compared

| | Surveyed or reported | Source |
|---|---|---|
| Breach width | 235 ft at its largest | McDaniel and others |
| Time to form | 12:30 p.m. (speeds up) to 2:40 p.m. (200 ft wide) | McDaniel and others, table 1 |
| Peak outflow | 69,000 cfs, the panel's estimate (not measured) | McDaniel and others |
| Peak pool | 905.55 ft, high-water mark at the dam | USGS OFR 2011-1301, table 8 |
| Tailwater at the dam | 889.17 ft | the same |
| County Road D47, Hopkinton, 9.4 mi below | 846.36 / 846.51 ft (down / up side of the bridge) | the same |
| State Highway 38, Monticello, 18.6 mi | 811.66 / 813.07 ft | the same |
| Main Street, Monticello, 20.1 mi | 807.62 / 808.79 ft | the same |
| U.S. Highway 151, 21.0 mi | 805.97 / 806.46 ft | the same |

All of it is in `observed.json`, with the full citations. The model is read 100 m up and down
the river from each bridge: the median highest water surface of the flooded cells within 60 m.
The run also records the flow past each bridge, so the score shows when the peak passed and
that the run did not end before it.

## What had to be assumed

Each of these is a stated value in `observed.json` or `build_case.py`, not something hidden in
the model.

- **The flow into the lake.** It was not gauged. The Manchester gauge is 11.7 river miles up
  and has 275 of the dam's 347 square miles above it. Flow at the dam is taken as the gauge's
  flow times **1.10**. The plain drainage-area ratio (1.26) puts water over the dam at
  3:20 a.m. with the pool 2.3 ft over the top by breach time; it was seen over the road at
  9:40 a.m. and the surveyed peak pool is 0.75 ft over the top. 1.10 gives 9:24 a.m. and
  0.9 ft. This is the one fitted number, and it is fitted to the lake, by reservoir routing
  alone, not to the marks below the dam. `--inflow-factor` changes it.
- **The gates.** Gates 1 and 2 fully open (the published 32,000 cfs for three open gates at
  the top of dam, a third each, scaled with head to the 1.5), Gate 3 stuck at 4.25 ft (an
  orifice, coefficient 0.70). Nothing is said about Gate 2 in the paper; it is taken as open.
  The gates are taken as worked to hold the normal pool until the flood was more than they
  could pass. The powerhouse (500 cfs) is left out.
- **The storage curve.** The Runner's curve is a power law through the storage at the top of
  dam. The surface area at the top (969 acres) is the one that makes it also pass through the
  published storage at normal pool (3,790 ac-ft at 896 ft).
- **The datum.** The reports are in NGVD 29, the terrain in NAVD 88. The shift here is small
  (within about 0.2 ft) and is taken as 0.0; `observed.json` has the place for NOAA's value.
- **The terrain.** USGS 3DEP 10 m, bare earth. The river bed below the water surface of the
  day the LiDAR was flown is not in it, and bridges are not modelled.

## Tests

`python3 -W error tools/validation/lake_delhi_2010/test_case.py` (17 tests, numpy only, no
internet): the published numbers hang together, the inputs are made from them as written
above, the Runner's own reservoir routing puts water over the dam and the pool at the breach
where they were observed, and the scoring reads a flood the way it says. It runs in CI's fast
checks, so a renamed Runner setting or a changed routing shows there.

## Sources

- Eash, D.A., 2012, Floods of July 23-26, 2010, in the Little Maquoketa River and Maquoketa
  River Basins, northeast Iowa: U.S. Geological Survey Open-File Report 2011-1301.
  https://pubs.usgs.gov/of/2011/1301/
- McDaniel, L., Garton, J., Fiedler, W., King, W., and Schwanz, N., Lake Delhi Dam Breach -
  Two Perspectives. https://damfailures.org/sites/default/files/wp-pdf/McDaniel_LakeDelhi.pdf
- U.S. Geological Survey National Water Information System, streamgage 05416900 Maquoketa
  River at Manchester, IA (`manchester_05416900_iv.csv`).
- Terrain: USGS 3D Elevation Program. Bridge points: U.S. Census Bureau TIGER roads crossing
  the USGS 3D Hydrography Program river line.
