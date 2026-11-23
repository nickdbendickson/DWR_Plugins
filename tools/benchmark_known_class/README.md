# Known-class benchmark

Dams whose hazard class is stated by the regulator, run through the suite's classifiers. Its
jobs:

1. **Catch under-classification.** A dam the suite puts below its class of record fails loudly,
   every time. There is no allowance list.
2. **Prove a rewrite changed nothing.** Moving classify, screening or the Runner's prep steps
   out of QGIS (the DamIT Desk "later swap") must give the same class *and the same drivers* on
   every benchmark dam, under every preset. Any change, up or down, fails.

Nothing here guesses a class. The class of record comes from an official inventory only.

## The dams (`dams.csv`)

From the **ND DWR dam database** (the regulator; the offline pack's `dwr/DWR_dams.gpkg`, which
matches the mapservice export of 2026-11-13 class for class on every benchmark dam), with the
**National Inventory of Dams** hazard potential beside it as a cross-check (`nid_agrees`).
`expected_nd_class` is DWR's class in REG_05.2026 words (Significant = Medium).

Every benchmark dam is Intact. `build_dam_list.py` holds the selection and the reasons for it:

| tier | High | Medium (Significant) | Low |
|------|------|-------|-----|
| cloud | 23: every reviewed High dam up to 12,000 acre-feet with a height | 19: every reviewed Significant dam up to 12,000 acre-feet, plus Swan Buffalo 12 and 8 | 16: one reviewed Low dam per county from 14 random counties (seed 20261123), plus 3034 and 2865 |
| mac | 6 big reservoirs (Dickinson, Lake Ilo, English Coulee, Renwick, Sweetbriar, Nelson Lake): hours each | | |

DWR and NID disagree on two: Mirror Lake (6122, DWR High, reviewed 2024-07-15; NID Significant)
and Crown Butte (6001, DWR High, reviewed 2023-08-02; NID Significant). DWR's more recent
review is the class of record. Left out: 2659 (its DWR dam line is 7 km from the dam).

## The files

| file | what |
|------|------|
| `dams.csv` | the benchmark dams and their classes of record (`build_dam_list.py` writes it) |
| `fixtures/<dam>.json` | what Screening tool 8 classified for that dam: every wet structure, road, railroad, lifeline and downstream dam with its depth and velocity, the settings, and the class tool 8 gave |
| `expected.json` | the class and drivers of every dam under every preset (the regression baseline) |
| `screening_results.csv` | the end-to-end screening class per dam (`run_screening.py` writes it) |

## The presets

`check_classes.py` classifies every fixture with:

* **DWR / Screening** (`DamSafety_Screening/core/hazard.classify_nd`, byte-identical
  `nd_hazard.py` with the Runner): `nd` (the suite's default: ACER TM-11 charts, judgment zone
  Medium, missing AADT Medium) - the one compared with the class of record - and
  `nd_judgment_zone_high`, `nd_borderline_higher`, `nd_unknown_aadt_low`, `nd_all_wet`,
  `nd_1d_rule`, `cmp_feinberg`.
* **DamIT Catalog** (with `--damit <checkout>`): every preset in `presets.json` (19 today). The
  fixture's receptors are translated to the Catalog's receptor fields as written in
  `damit_receptors()`; the chart zone is the one the ND classifier read from the same
  REG_05.2026 table. No occupant counts are invented, so lives-in-jeopardy methods (M5) report
  a missing input rather than a number.

## Running it

```sh
# the check (no QGIS): reproduce, regression, class of record
python3 tools/benchmark_known_class/check_classes.py
python3 tools/benchmark_known_class/check_classes.py --damit ../damit      # + DamIT presets

# the CI gate: fixtures reproduce and nothing changed (under-classifications still printed)
python3 tools/benchmark_known_class/check_classes.py --regression-only

# end to end: run the dams through Screening and capture fresh fixtures (QGIS, 3DEP, hours)
python3 tools/benchmark_known_class/run_screening.py --pack <ND_Dam_Suite_Pack> --work <folder>
```

## The full baseline is a Mac job

Each dam's screening flood (built-in engine) takes roughly 30 minutes, so the 58 cloud-tier
dams take most of a day. In the cloud, background jobs are stopped after about two hours, so the
cloud produces only a partial baseline (the fastest dams). **The full baseline is generated on a
workstation** — Nick's Mac, where the suite already runs for the batch-44 work.

`run_screening.py` runs one dam per chain (`--batch 1`) so each dam's fixture is written as soon
as that dam finishes, and a run stopped part-way keeps every fixture it already wrote; re-running
skips the dams whose work folder already has `8_hazard.gpkg`. On a Mac, point it at the conda
`qgis_process`:

```sh
QGIS_PROCESS="conda run -n ndqgis qgis_process" \
  python3 tools/benchmark_known_class/run_screening.py \
  --pack ~/ND_Suite_Data/ND_Dam_Suite_Pack --work ~/ND_benchmark --batch 1 --tier cloud
# then the 6 big reservoirs (hours each):
QGIS_PROCESS="conda run -n ndqgis qgis_process" \
  python3 tools/benchmark_known_class/run_screening.py \
  --pack ~/ND_Suite_Data/ND_Dam_Suite_Pack --work ~/ND_benchmark --batch 1 --tier mac
```

That writes the fixtures and `screening_results.csv`. Then `check_classes.py --update` records
`expected.json`, and `check_classes.py` (no flags) reports any dam the suite under-classifies
against its DWR class of record. Commit the new fixtures and `expected.json`.

Exit status: 0 pass, 1 a fixture does not reproduce or a class or driver changed, 2 under-
classification against the class of record, 3 both.

`DamSafety_Screening/tests/test_known_class_benchmark.py` runs the regression gate in CI.

## When a class changes

A changed class or driver is not automatically wrong: a rule fix can move a dam on purpose.
Review it, say why in the pull request, then `check_classes.py --update` (add `--damit` to
refresh the DamIT rows too). An under-classification against the class of record is never
cleared by `--update`: it stays on the report until the suite classifies the dam at or above
its class of record, or the class of record changes.

## Findings so far (2026-11-23, first 9-dam cloud batch)

The first batch (the 9 fastest dams, 3 per class) already surfaced a real screening bug and
confirmed the gate works both ways:

* **The quick screen (tool 6) over-clears two High dams.** Queen City Dam (DWR-2636) and Prairie
  Dam 1 (DWR-10551) are both **High** in the DWR record. In the shipped county-screening default
  (quick screen on), triage reported "nothing inside the envelope" and the suite classed them
  **Low**. Re-run with the quick screen off (a full 2D flood), Queen City is **High** (a road
  with AADT 4,813 overtops 5.1 ft, 12 buildings wet) and Prairie Dam 1 is **Medium** (15
  structures inundated). So triage is clearing dams a real breach flood reaches — a genuine
  under-classification in the county-screening path. (This benchmark only flags it; the fix is a
  separate change to the triage envelope / hit test.)
* Prairie Dam 1 is still **Medium**, not High, even on a full flood: the 15 inundated structures
  are in the low-danger zone (some 0.48 ft under the judgment-zone boundary). Whether the DWR
  High rests on a structure the offline pack's inventory misses, or on the borderline rule, is a
  follow-up.
* **Conservative over-classification** is reported, not failed: Belfield Railroad Dam (DWR-3037,
  record Significant/Medium) screens **High** (7 structures in the high-danger zone).
* The three small Low dams (and 11409) screen **Low** — matching the record.

## Capturing fixtures

Screening tool 8 writes a fixture for every dam it classifies from flood rasters when the
environment variable `DAMSAFETY_RECEPTOR_DUMP` names a folder (`core/receptor_dump.py`); it is
off otherwise. `run_screening.py` sets it. A dam cleared by the quick screen (tool 6) has no
flood and so no fixture; its end-to-end class (Low by triage) is still checked against the
class of record.
