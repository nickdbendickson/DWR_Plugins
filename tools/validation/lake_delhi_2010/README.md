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

## Results (13 November 2026)

From the validation job (`.github/workflows/validation.yml`): LISFLOOD-FP 8.3 built from the
official source, 30 m cells, 48 hours, inflow factor 1.10. A run takes about 12 minutes on a
GitHub runner. "As observed" is given the surveyed breach (235 ft, 2.17 hours); "blind" lets
the Runner work the breach out from the dam alone, as it would for a dam that has not failed.

**The breach.**

| | Reported | As observed | Blind |
|---|---|---|---|
| Breach width, ft | 235 | 235 (given) | 243 |
| Time to form, hours | 2.17 | 2.17 (given) | 1.56 |
| Pool when the breach starts, ft | 905.55 | 905.69 | 905.69 |
| Peak outflow, cfs | 69,000 (the panel's estimate, not measured) | 140,855 | 169,257 |

**The flood, model minus surveyed, in feet.**

| Mark | Miles below | As observed, n 0.06 | Blind, n 0.06 | As observed, n 0.04 |
|---|---|---|---|---|
| Below the dam (tailwater) | 0.0 | +14.31 | +16.45 | +7.91 |
| Hopkinton, up side | 9.4 | +3.57 | +3.50 | +1.35 |
| Hopkinton, down side | 9.4 | +3.69 | +3.63 | +1.47 |
| Highway 38, up side | 18.6 | +2.10 | +2.06 | +0.85 |
| Highway 38, down side | 18.6 | +1.82 | +1.79 | +0.73 |
| Main Street, up side | 20.1 | +0.66 | +0.61 | -0.99 |
| Main Street, down side | 20.1 | +1.46 | +1.41 | -0.44 |
| U.S. 151, up side | 21.0 | +1.64 | +1.58 | -0.30 |
| U.S. 151, down side | 21.0 | +1.64 | +1.59 | -0.26 |
| **The eight bridge marks: mean, largest** | | **+2.07, +3.69** | **+2.02, +3.63** | **+0.30, +1.47** |

What it says:

- **The blind breach is close to the real one**: 243 ft against 235 ft, formed in 1.6 hours
  against 2.2.
- **With the suite's default roughness (Manning's n 0.06) the flood is about 2 ft high at the
  bridges**, every mark on the high side. That is conservative, and it is not small: the
  largest is 3.7 ft.
- **With n 0.04 the same run is within 1.5 ft at every bridge, mean +0.3 ft.** One flood on one
  river does not set a default, and this is an Iowa river, not a North Dakota one. It does
  say how much of the 2 ft is the roughness: nearly all of it.
- **The peak outflow is about twice the panel's estimate, and the water just below the dam is
  8 to 16 ft too high.** The breach routing is a level pool emptying through a weir with no
  tailwater. The surveyed tailwater was 889 ft, 27 ft above the breach floor, and the lake is
  nine miles long and narrow, so neither assumption fits this dam well. How much each accounts
  for is not separated here. The panel's 69,000 cfs is itself an estimate.
- **Nine miles down, the breach's peak no longer matters.** The two breaches differ by
  28,000 cfs at the dam and by 0.07 ft at the bridges. What sets the level there is the volume
  of the flood and the roughness of the valley.

Three things the runs showed about the Runner, apart from the numbers:

- **LISFLOOD-FP flow sections near the east or south edge of the model read zero** (three of
  the four bridges here). Fixed in Runner 3.20.4, with a test; water levels were not affected.
  With the fix all four read, and the levels above did not move by 0.01 ft:

  | Flow past, as observed | Peak, cfs (n 0.06) | Hours after noon on 23 July | Peak, cfs (n 0.04) |
  |---|---|---|---|
  | The dam (breach and gates) | 140,855 | | 140,855 |
  | Hopkinton, 9.4 mi | 53,989 | 28.3 | 65,806 |
  | Highway 38, 18.6 mi | 41,580 | 32.3 | 50,173 |
  | Main Street, 20.1 mi | 39,366 | 33.8 | 47,442 |
  | U.S. 151, 21.0 mi | 39,182 | 34.4 | 47,158 |

  (The breach was seen to speed up at 12:30 p.m. on 24 July, hour 24.5.)
  The peak falls to under 40 percent of the breach's within nine miles: the valley stores
  it. That is why the breach's peak matters so little at the bridges.
- **The built-in engine (the stand-in for when LISFLOOD-FP is not installed) is not steady on
  this valley.** At 60 m, and at 40 m with flow sections, its flow at the bridges swung between
  zero and over 100,000 cfs and the levels came out up to 48 ft high, with no warning and a
  volume balance that looked fine. Not fixed here. Use LISFLOOD-FP for this case.
- **Not done:** no change to the breach routing (a tailwater limit changes the numbers of
  every dam, which is a decision and not part of a test), no change to the default roughness,
  and NOAA's exact datum shift is not applied (`observed.json` has the place for it).

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
