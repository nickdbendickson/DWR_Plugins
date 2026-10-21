# Results log

Absaraka = DWR-2801. Unless noted, these runs used the old trusted baseline settings with NDBREACH_DEM_RESAMPLE=average. Source: `/workspace/dwr_test/pr12_15_progress.md` on the shared box.

| Date (UTC) | Dam / route | Code | Peak (cfs) | Flooded area (ac) | Max depth (ft) | Class | Notes |
|---|---|---|---|---|---|---|---|
| baseline | Absaraka DWR-2801 | before #14 | 25,801 | 5,067.0 | 13.06 | High | Trusted baseline. Storage 1,992,690 m³, exponent 1.9. The #22 default-settings run reproduced it exactly (identical raster). |
| baseline | DWR-3161 | before and after #14 | 34,272 | 4,876.5 | 13.16 | High | Unchanged by #14. |
| 10-15 ~05:30 | Absaraka, scripted (runnerhandoff) | installed main after #14 | 25,801 | 6,939.5 | 9.38 | High | Exponent 1.9. Inflow (-106263.8, 2666571.5). Source: `runs/final_main` (summ_v2.py final_main). |
| 10-14 | Absaraka, wizard (rundam) | before #14 (m11) | — | 8,191.6 | — | — | Source: `runs/m11` (abs_gui) |
| 10-15 03:3x | Absaraka, wizard (rundam) | #14 @ 9fbbaa0 | — | 6,587.6 | 8.84 | — | Source: `runs/r3pr14` (abs_gui) |
| 10-15 | Absaraka + structures (LISFLOOD, bridge notches) | #20 | — | 5,067.8 | — | — | 23 structures read, no ValueError |

## Cause of the #14 change
The inflow point moved from (-106364, 2666612) at 320.46 m to (-106264, 2666572) at 318.33 m. That is about 108 m away and 2.1 m lower: the lowest qualifying cell instead of the nearest one. Source: run_report and manifest in `runs/m11` and `runs/pr14`. The flooded area went up and the depth went down, because the flood now starts further down the valley. The peak, storage exponent and class are unchanged.

## Culvert burn-in (PR #30, CULVERT_BURN_MATCHED), 10-15 ~06:3x

**Run on the PR #30 branch before it merged (merged into main 10:34Z as 6ffd632).** Inventory: NDDOT bridge inventory, NTAD NBI, and NDDOT Center Line Pipes. Match distance 75 m. Source: `runs/r6pr30` and `runs/r7pr30` (run1 = burn off, run2/run3_on = burn on).

| Dam | Burn | Peak (cfs) | Flooded area (ac) | Max depth (ft) | Class (in this run) |
|---|---|---|---|---|---|
| Absaraka DWR-2801 | off | 25,800.5 | 6,939.5 | 9.38 | High |
| Absaraka DWR-2801 | on | 25,800.5 | 6,939.1 | 10.13 (+0.75) | High |
| DWR-3161 | off | 34,271.7 | 4,876.5 | 13.16 | High |
| DWR-3161 | on | 34,271.7 | 4,870.9 | 11.74 (-1.42) | High |

**Cause at DWR-3161:** one matched guess was burned in, `NDDOT CLP 205:49.5229`, 24.8 m from the guess (road 943 crossing channel 1). It's a double 3.048 m (120 in) pipe with LiDAR inverts of 561.03 m upstream and 560.98 m downstream. **The pipe is on route 205, which is ND-23 (confirmed).** In the NDDOT Center Line Pipes record (ROUTE_ID 205, ROUTE_MEASURE 49.5229), `HIGHWAY` = 23, `HWY_DIR` = E, `PIPE_DESC` = DOUBLE and `DIAMETER_E` = 120. HIGHWAY 23 is also one of the overtopped roads in the hazard summary (AADT 8,501).

**Hazard class source:** `outputs/Hazard_Classification_Summary.csv` in each dam folder is **the authoritative source for hazard class**, under the row "ND classification (screening)". For DWR-3161 it reads High in both runs: 3 overtopped roads with AADT > 2,000 [5.2.1.2], namely 39TH ST NW (AADT 10,113) twice and HIGHWAY 23 (AADT 8,501).

The field `hazard_class` in `Hazard Classification (ND 5.2).json` in the dam folder (the Runner's own structures-only check, also printed as "Hazard class: MEDIUM" in the run log) is **not authoritative**. It reads Medium for DWR-3161, and an earlier draft of this log mistakenly used it.

## Re-baseline 2026-10-18 (TIER 0, main 8b0601c, culvert burn on)

**This used a non-default setting.** Main's default is `CULVERT_BURN_MATCHED` = off. The re-baseline turned it on explicitly with:
- `CULVERT_INVENTORY` = nddot_bridge_inventory.gpkg;;ntad_nbi.gpkg;;research/nddot_centerline_pipes.gpkg
- `CULVERT_MATCH_M` 75
- TIER 0 (LISFLOOD-FP), 40 m cells

> **Hazard figures superseded (2026-10-20):** the hazard numbers from this re-baseline came from a Hazard 2.18.34 batch (NaN lengths and areas). See "Hazard re-baseline on 2.18.44 (2026-10-20)" below. The flood figures stand.

Peaks are unchanged (25,800.5 / 34,271.7 cfs). Source: `/home/box/DamSafety/Dams/rebaseline_20261018/` (`progress.md`, `status`, `extra.json`, `compare_*_vs_prev.json`, `<dam>/dams/<dam>/_breach_model/run_report.json`). The class comes from `outputs/Hazard_Classification_Summary.csv`.

| Dam | Culverts burned | Area any / ≥0.1 ft / ≥1 ft (ac) | Max depth (ft) | Max velocity (ft/s) | Arrival min / median / max (h) | Class |
|---|---|---|---|---|---|---|
| Absaraka DWR-2801 | 2 (NDDOT 09-117-18.1, 09-118-21.1) | 6,939.1 / 5,758.5 / 1,802.1 | 10.13 | 12.71 | 0.15 / 2.66 / 28.21 | High |
| DWR-3161 | 1 (NDDOT CLP 205:49.5229, ND-23) | 4,841.7 / 4,493.0 / 703.0 | 11.29 | 19.36 | 0.19 / 1.56 / 21.95 | High (not used: flagged, Low of record, #93) |

**Reference runs in `compare_*_vs_prev.json`:** both are 10-18 TIER 0 runs with **burn off**, so the comparison isolates the burn.
- DWR-2801 against `Absaraka_compare/hecras_tier2_20261018/tier0`: 6,939.5 ac, 9.38 ft, 15.55 ft/s. Fit index 0.998 at ≥0.1 ft and 0.999 at any depth (QA map). Depth bias -0.00 ft.
- DWR-3161 against `.../dwr3161_tier0`: 4,841.7 ac, 11.40 ft, 19.36 ft/s. Fit index 0.999.

**QA notes:**
- **DWR-2801 max depth (10.13 ft) is at the burned culvert NDDOT 09-118-21.1**, at about (-101220, 2662420), about 45 m from the culvert line and 6.5 km downstream. That cell had 5.29 ft with burn off. Burn off, the maximum (9.38 ft) is at the inflow point.
  - The +0.75 ft "rise" is depth over ground that was lowered by the culvert cut. It isn't a deeper flood at the dam.
  - Over the shared area, the depth difference is ~0 ft (bias -0.00, p90 0.003 ft).
- **Max velocities are single shallow cells, not representative.**
  - DWR-2801: both 12.71 ft/s (burn on) and 15.55 ft/s (burn off) are at the same cell, (-103860, 2666020), in 0.34 ft of water. That is right at the burned culvert NDDOT 09-117-18.1 (-103744, 2665971) and 2.5 km from the inflow. The culvert opening reduced the overtopping jet there. The 99th-percentile velocity is 4.68 vs 4.75 ft/s.
  - DWR-3161: 19.36 ft/s is at (-493980, 2796180), in 0.15 ft of water, 5.5 km from the inflow. The 99th percentile is 4.48 ft/s.
  - Report the p99 velocity, or the velocity at receptors, rather than the maximum.
- **DWR-3161 vs the 10-15 burn-on run: no valid comparison (corrected 10-19).**
  - ~~In `runs/r7pr30`, the inflow was at (-490029, 2792468), 18.1 ft below the crest.~~ **Invalid.** The 10-15 code (Runner 3.10.12/3.10.14) moved the crest 397 m onto a hill. That inflow point's ground is 1916.3 ft, 72 ft **above** the 1844.44 ft top of dam, so "18.1 ft below the crest" was measured from the hill (root cause in #84). The 10-15 DWR-3161 figures (4,870.9 ac, 11.74 ft, the -1.42 ft burn effect) aren't a baseline for anything.
  - On 8b0601c, the dam point is taken as the crest. The inflow was at (-489749, 2792668), only 4.0 ft below the top of dam and 40 m **south** of the dam point, on the pool side, while the downstream trace runs north.
  - HEC-RAS TIER 2 rejected this inflow point too (`hecras_tier2_20261018/status` at 05:07Z).
  - Fixed by #84 and superseded by #93; see "DWR-3161: inflow fix (#84) and flagged structure (#93)" below. The 10-18 DWR-3161 row above (High) is kept as a record of the run, but it isn't used.
- **DWR-3161 extent:** 4,139 of 4,842 ac (85%) is under 1 ft deep, as a uniform sheet over a featureless flat NW of the dam. That is probably the Sanish Bay / Lake Sakakawea water surface in the LiDAR (not checked against NHD). (10-19: #93 confirms the lake is on the downstream, north side.) The "any depth" area is likely overstated. Use ≥1 ft (703 ac), or mask the mapped water body.
- **DWR-2801 extent:** about 1,180 ac is wet at under 0.1 ft, with scattered isolated patches in the SE lowland (potholes). The east edge reached 0.73 ft, which is under the edge tolerance (`edges_over_tolerance` = []).

## HEC-RAS TIER 2 vs TIER 0 and ANUGA: Absaraka DWR-2801 (10-18)

Source: `Absaraka_compare/hecras_tier2_20261018/` (`progress.md`, `compare_tier2_8b0601c.json`). TIER 2 ran on 8b0601c (burn off), and `tier2_8b0601c` is identical to `tier2_manual`.

> **Hazard figures superseded (2026-10-20):** the Class column came from Hazard 2.18.12–2.18.34 batch runs. For current hazard numbers, see "Hazard re-baseline on 2.18.44 (2026-10-20)" below.

| Run | Area any / ≥0.1 / ≥1 ft (ac) | Max depth (ft) | Max velocity (ft/s) | Median arrival (h) | Class |
|---|---|---|---|---|---|
| HEC-RAS TIER 2 (8b0601c) | 8,177.0 / 7,594.2 / 3,023.4 | 12.22 | 6.09 | 3.48 | High |
| LISFLOOD TIER 0, burn off (10-18) | 6,939.5 / 5,760.5 / 1,802.9 | 9.38 | 15.55 | 2.66 | High |
| ANUGA, first run (`anuga_structures_20261015`, 10-15 code) | 5,905.2 | 13.28 | 13.38 | — | High |

- HEC-RAS is **+38.5% area vs ANUGA** (fails the area check), +17.8% vs TIER 0 at any depth, and +31.8% at ≥0.1 ft. Against TIER 0: F = 0.714, hit rate 0.966, false-alarm ratio 0.267.
- **The water surface agrees but depth doesn't.** The WSE bias is -0.10 ft (p90 0.53 ft), while the depth bias is +0.61 ft (ratio 1.54). The difference is mostly in the ground surface each model subtracts. HEC-RAS uses the 1 m subgrid terrain, and its results were then mapped onto the 40 m LISFLOOD grid by `manual_results.py`. That counts fine-terrain lows as wet and deepens depths. **Check:** recompute HEC-RAS depth as WSE minus the same 40 m average-resampled DEM before comparing areas.
- **ANUGA (first run) is truncated.** Its pass 2 hit the mesh edge with 8.58 ft of water ("extent limited by LISFLOOD domain"), so 5,905 ac is a lower bound and not a fair reference (the 20x20 gmsh run below replaces it). It also ran on 10-15 code with 7 guessed culverts and the NBI bridges as structures.
- **Other differences to check:**
  - Simulation length: HEC-RAS latest arrival is 39.5 h vs 28.4 h, so a longer run spreads further.
  - Roughness (Manning's n in the .p01/.u01 vs LISFLOOD).
  - Mesh: 65,739 cells, with Min Courant manually set to 0.45.
  - Accuracy: HEC-RAS max cell WSE error is 2.27 ft and the volume error is 0.17%.
  - Both runs used the same inflow point and hydrograph (25,801 cfs).
  - Compare areas at the same depth threshold. ≥1 ft gives 3,023 vs 1,803 ac (+68%), so the gap isn't just shallow fringe.

## DWR-3161: inflow fix (#84) and flagged structure (#93), 10-18 to 10-19

**#84 (merged 10-18 12:11Z, Runner 3.10.18): the inflow point now stays on the downstream side.** Placement only, checked on the real DEM; no model was run.
- The new inflow is (-489849.0, 2792927.7), **242 m north** of the dam point, with ground at **1,839.45 ft**, 5.0 ft below the 1,844.44 ft top of dam. The trace runs north and never comes back within 282 m of the dam.
- The dam point was never wrong. DWR's dam line and the NID point sit on the south shoulder of the ND-23 (39TH ST NW) fill, so the dam and the highway share one embankment.
- The Runner now writes `inflow_point.side_check` into `run_report.json`. A trace that runs back over the dam point, an inflow behind the dam, or an inflow on the upstream side of the crest gives a **"CHECK THE DAM POINT"** warning at the top of the warnings. A road within 30 m of the dam point gives a road-fill warning (`dam_point_checks`) unless the DWR dam line (`DAM_LINES`) was given.
- #76 (merged 10-18 12:09Z) is the HEC-RAS side of the same problem: HEC-RAS won't quietly use or walk an inflow point that is on the wrong side. With #84's point, HEC-RAS's `auto` mode falls back to a breach model, and the 3.10.17 site checks then stop that model at DWR-3161 (4 problems).

**#93 (merged 10-19 05:23Z, main d217ec2; Runner 3.10.23, Hazard 2.18.38, Screening 0.17.26): DWR-3161 is flagged as a road fill over backwater, hazard Low.**
- The record check (`docs/Flagged_structures.md`, `docs/evidence/DWR-3161/`) found that the "dam" is the 1995 ND-23 highway fill with a twin 10 x 9 ft box culvert (NBI 0023048401, 29 m from the dam point). The pool is the creek valley south of the road. Lake Sakakawea (Sanish Bay) is the downstream side, to the north. Emptying the full 2,861 ac-ft into the lake would raise it by about 0.01 ft.
- The hazard of record is **Low (DWR record)**. The High from the 10-18 re-baseline is a model artifact and isn't used. Breach analysis is "not applicable".
- The Runner refuses breach runs of DWR-3161 unless **`ALLOW_FLAGGED_STRUCTURE=1`** (or the "Run a flagged structure anyway" checkbox, or `--allow-flagged-structure` on the CLI). Batch runs skip it with Status `flagged`. See USER_GUIDE.md.
- **DWR-3161 is on hold** until DWR answers the record questions in #93 (datum, road crest, normal pool, max pool, stoplogs, and whether a breach analysis is wanted at all).

## ANUGA gmsh 20x20 mi / 48 h: Absaraka DWR-2801 (10-18 run, QA 10-19)

Source: `Absaraka_compare/anuga_gmsh_20x20_48h_20261018/` (`compare_stats.json`, `dams/DWR-2801/_breach_model/run_report.json`, `outputs/Hazard_Classification_Summary.csv`). QA: `Absaraka_compare/qa_review_anuga_20261019/QA_FINDINGS.md` (read-only review, Docs & QA). Code: Runner 3.10.14 with the gmsh mesher (#71), 40 m DEM, 7 guessed culverts plus the NBI bridges as structures.

| Run | Area any / ≥0.1 / ≥1 ft (ac) | Max depth (ft) | P99 / max velocity (ft/s) | Arrival first / median (h) | Triangles | Class |
|---|---|---|---|---|---|---|
| ANUGA gmsh 20x20 / 48 h | 15,188 / 11,944 / 2,416 | 11.26 | 3.42 / 9.78 | 0.10 / 3.56 | 214,240 | High |

- **Class High** (`Hazard_Classification_Summary.csv`): 22 inundated BNSF KO rail stretches at **22 trains/day** (≥10/day, ND 5.2.1.3; FRA crossings 980805Y, 071108D, 071105H). The stretches 2.5 km below the dam are wet in all three models (ANUGA 1.7-3.6 ft, re-baseline 0.5-4.3 ft, HEC-RAS 1.4-4.9 ft). Also 11 overtopped roads with AADT > 2,000 in Casselton. (`QA_FINDINGS.md` says 15 trains/day; that is wrong, the summary CSV is authoritative.)
  - **Train-count provenance** (FRA Highway-Rail Crossing Inventory, Form 71, checked 2026-10-20; current records: [FRA crossing inventory (current, data.transportation.gov)](https://data.transportation.gov/resource/m2f8-22s6.json?$where=crossingid%3D%27071105H%27)):

    | Crossing | Location | Day / night trains | Count year | Reported | Notes |
    |---|---|---|---|---|---|
    | [071105H](https://safetydata.fra.dot.gov/OfficeofSafety/PublicSite/Crossing/Crossing.aspx?phasetype=C&rpttype=I&txtcrossingnum=071105H) | 6th Ave, MP 27.782 | 11 / 11 = 22 | 2023 | BNSF 12/18/2024 | Current record revised 05/13/2025 by FRA (Quiet Zone Update, counts carried over). Earlier 15/15 (count year 2019, 2019 report); 26/26 (2004-2019). |
    | [071108D](https://safetydata.fra.dot.gov/OfficeofSafety/PublicSite/Crossing/Crossing.aspx?phasetype=C&rpttype=I&txtcrossingnum=071108D) | 3rd Ave, MP 28.003 | 11 / 11 = 22 | 2023 | BNSF 12/18/2024 | Current record 05/13/2025 FRA quiet zone update. Earlier 15/15. |
    | [980805Y](https://safetydata.fra.dot.gov/OfficeofSafety/PublicSite/Crossing/Crossing.aspx?phasetype=C&rpttype=I&txtcrossingnum=980805Y) | 2nd St N, MP 28.257 | 0 / 0 | 2023 | BNSF 01/16/2025 (revised) | Less than one movement/day (1 train/week). |
    | [102944Y](https://safetydata.fra.dot.gov/OfficeofSafety/PublicSite/Crossing/Crossing.aspx?phasetype=C&rpttype=I&txtcrossingnum=102944Y), [102946M](https://safetydata.fra.dot.gov/OfficeofSafety/PublicSite/Crossing/Crossing.aspx?phasetype=C&rpttype=I&txtcrossingnum=102946M), [102947U](https://safetydata.fra.dot.gov/OfficeofSafety/PublicSite/Crossing/Crossing.aspx?phasetype=C&rpttype=I&txtcrossingnum=102947U) | other KO | 8 / 7 = 15 | 2023 | BNSF 01/14/2025 | |
    | [071099G](https://safetydata.fra.dot.gov/OfficeofSafety/PublicSite/Crossing/Crossing.aspx?phasetype=C&rpttype=I&txtcrossingnum=071099G) | other KO | 11 / 11 = 22 | 2023 | BNSF 12/18/2024 | |
    | [103787L](https://safetydata.fra.dot.gov/OfficeofSafety/PublicSite/Crossing/Crossing.aspx?phasetype=C&rpttype=I&txtcrossingnum=103787L) | other KO | 15 / 15 = 30 | 2023 | BNSF 01/14/2025 | |
    | [071100Y](https://safetydata.fra.dot.gov/OfficeofSafety/PublicSite/Crossing/Crossing.aspx?phasetype=C&rpttype=I&txtcrossingnum=071100Y) | other KO | blank (FRA current); 0 / 0 (pack) | | State report 07/27/2022 | Counts blank in FRA's current record. |

    The pack copy (NTAD Railroad Grade Crossings layer, downloaded 2026-10-14) has no revision-date or count-year field. All flooded KO stretches remain ≥ 10 trains/day, so High (ND 5.2.1.3) is unchanged.
- **Max depth 11.26 ft is a one-cell DEM trench in Casselton**, 16 km downstream. Near the dam, the max is **9.8 ft**. The same-run LISFLOOD has 11.36 ft at the same spot.
- **Use ≥0.1 ft for reporting.** The "any depth" figure counts a 1 mm film: 3,244 ac of it is under 0.1 ft.
- **Edge and time:** not edge-capped (0.42 ft at the mesh edge). It is **time-capped at 48 h**: 102 ac of the ≥1 ft area arrives after 40 h, and the front is still moving east.
- **Mass balance:** 1,612.7 ac-ft in, 1,606.6 ac-ft in the domain at 48 h; the 0.38% difference left through the open boundary.

**Agreement: the headline is F 0.70 at ≥1 ft against the in-run LISFLOOD** (same 20x20 domain, 48 h, DEM and inflow; mean depth bias -0.003 ft).
- The **0.63** (LISFLOOD re-baseline) and **0.45** (HEC-RAS TIER 2) figures are artifacts of the comparison. Those rasters cover only 10 x 9 mi and sit half a cell (20 m) off ANUGA's grid, so 21% of ANUGA's ≥1 ft area could never match. Inside their grid, F is 0.73 and 0.60 (0.84 and 0.83 at ≥0.1 ft). Don't quote 0.63 or 0.45 as model disagreement.

**Flags:**
- **East pond, unconfirmed.** 107 ac at ≥1 ft, 3.17 ft max (the connected ≥1 ft component around the pond, `qa2.json`; WSE 912.8-913.2 ft), around EPSG:5070 (-84,739, 2,656,831), 23.7 km ESE of the dam. It's breach water held behind the BNSF KO grade (crest 910.4-915.3 ft), where no rail culvert is modelled and the triangles are coarse (median 4,323 m²). 1 ft first arrives after 35 h. The same-run LISFLOOD is dry there. The refined run's compare script uses a different measure, everything ≥1 ft within 1.5 km of the pond centre (`east_pond_1500m`), which gives **137.7 ac / 3.4 ft** for this 48 h run because it picks up neighbouring ponded patches. Compare like with like.
- **Casselton consequences are shallow and late.** 162 habitable structures inundated (Hazard summary, ND 5.2.2; *superseded: 152 on Hazard 2.18.44, see the 2026-10-20 re-baseline*) and 11 roads with AADT > 2,000, but the median structure depth is 0.23 ft (50 structures at ≥1 ft) and the water arrives after about 18-28 h. Same-run LISFLOOD corroborates the extent (446 ac ≥0.1 ft there). Don't mix the 162 with `run_report.json` `receptors_flooded` (21 structures, 8.2 km of road, 0.1 km of rail): that is the Runner's own receptor count at the arrival depth, from a different structure layer and threshold.

**Refined rerun in progress:** `Absaraka_compare/anuga_gmsh_refined_72h_20261019/`, started 10-19 05:18Z by Grok Bot; 59 % at 10-20 03:24Z (sim t = 153,600 s of 259,200 s).
- Code: main a71e9e7 (Runner 3.10.21) with in-process shims (`run_anuga.py`). It does **not** include #102/#104 (Runner 3.10.30/3.10.31 ANUGA mesh and bed changes); the 10 m ground comes from the run's own `ANUGA_ELEV_CELL_M=10` shim.
- Mesh: gmsh, **621,334 triangles** (48 h run: 214,240), smallest **44.98 m² (~45 m²)**, min angle 30.64°, gate 40 m² (`GMSH_MIN_AREA_TOL=0.40`). Corridor target 230 m² (≤500 m²) along the Casselton-to-pond corridor and the BNSF grade; embankment lines inside the corridor only.
- **ANUGA_ELEV_CELL_M=10:** ANUGA ground from the LiDAR at 10 m, not the 40 m model DEM. LISFLOOD, the output grids and the culvert inverts stay at 40 m.
- **72 h** (was 48 h). Box 8 km wider east (-112,800..-72,600 × 2,648,300..2,680,500), 2,000 m buffer.
- **10 BNSF KO openings: KO-01, KO-02, KO-03, KO-05, KO-06, KO-07, KO-08, KO-09, KO-10, KO-11** (no KO-04: NHD crossing only, no second source). Culverts KO-01/02/05/06/07/10 have assumed sizes (KO-06's existence is unconfirmed; KO-07's location is confirmed by OSM). KO-03/08/09/11 are bridge DEM cuts; KO-08 (Maple River) uses the Cass Co FIS low chord 907.3 ft with no pressure flow. KO-10 and KO-11 are outside the ANUGA seed ring.
- This run changes duration, box, mesh, structures and ground resolution at once, so a change at the pond or Casselton can't be credited to the rail openings alone. The KO-06/07 closed sensitivity (`..._ko0607closed`, prepared, not run) isolates those two culverts.
- Review: `qa_review_anuga_20261019/REFINED_RUN_CHECKLIST.md`; results go into `REFINED_RUN_RESULTS_TEMPLATE.md` in the same folder. Until it's reviewed, report the 48 h run against the in-run LISFLOOD (F 0.70).

## Hazard re-baseline on 2.18.44 (2026-10-20)

**What was run.** Grok Bot ran `ndhazard:batch` (DamSafety Hazard **2.18.44**, DWR_Plugins main **0a8344b**; `DOWNLOADS=true REPORT=false EAP=false WORKERS=1`, no solver) on **copies** of 65 saved results: 23 Absaraka DWR-2801 results (Absaraka_compare and rebaseline_20261018) and the 42 NickReview dams. Result: **65 classified, 0 failed** (`progress.log`, 20:34Z–21:43Z). **DWR-3161 was skipped** everywhere (flagged, #93), as was one HEC-RAS preview source folder that isn't a dam result. The originals weren't modified. No model was rerun: same depth, velocity and arrival rasters.

Sources: `/workspace/hazard_rebaseline_20261020/` (`SUMMARY.md`, `changes.json`, `progress.log`, `jobs.json`) and the explanation note `/workspace/hazard_diff_2801_20261020/NOTE.md` (with `structure_changes.csv`). Full per-result table: [results/hazard_rebaseline_20261020.csv](results/hazard_rebaseline_20261020.csv). The class comes from each copy's `outputs/Hazard_Classification_Summary.txt` / `.csv`.

**Why.** PR #94 (Hazard 2.18.39) changed hazard results, and it exposed a bug in the older numbers:
- **Every saved DamSafety Hazard *batch* result from 2.18.0 to 2.18.34 has NaN outline areas and 10 ft road and rail stretches.** Each stretch's length came out NaN, and `max(10, NaN)` gave the 10 ft sampling floor. So the motorist part of lives in jeopardy (LIJ) was about 0, and no NSI or 911 point could snap onto a building outline (NaN ≥ 400 ft² is False). From 2.18.39, lengths and areas are real. The fix is attributed to H5 (`transformguard`, f862645) by elimination; the NaN couldn't be reproduced outside a batch run.
- **M3:** a building that only NSI calls a home is now *Unverified* (0 persons) when the parcel's 911 address point is on another building. This can lower the class.
- **H4:** `collapse_dwellings` was removed, so buildings that share an address keep their persons. Population at risk and cut-off counts can go up.
- **Old batch numbers (2.18.0–2.18.34, plus the 2.18.37 in-run hazard of the refined 72 h run) are not comparable with 2.18.39 or later.** Compare only like with like.

**DWR-2801 (rebaseline_20261018 copy; same rasters, SHA-256 0ad6429d / 96400315 / 7fe484a9): ND High / TM 11 Significant, unchanged.** These are the same numbers as the 2.18.39 re-run in NOTE.md.

| Metric | 10-18 (2.18.34) | 10-20 (2.18.44) | Cause (NOTE.md) |
|---|---|---|---|
| Lives in jeopardy | 0.0 | 0.6 | Stretch lengths (H5). Before, all 75 overtopped roads and 4 rail stretches were 10 ft; now they have real lengths up to 5,357 ft. Motorists: High 0.56, mostly 148th Ave SE (AADT 174, 5,357 ft): 0.25. Structure part 0 in both. Rescaling the 10-18 motorists by the measured lengths gives exactly 0.562 / 0.016. |
| Habitable structures inundated | 5 (12 persons) | 3 (6 persons) | M3: #258/#262 (15362 33rd St SE) and #585 (14840 30th St SE) are now Unverified. H4: at 14751 30th St SE the persons move from #471 (Unverified) to the 911 building #577. Residential damage $137k → $2.5k. |
| Dry habitable structures cut off | 8 (22 persons) | 11 (30 persons) | H4: #226, #230, #260, #266 and #602 keep their persons (+14). M3 (#720) and H5 areas + M3 (#754): −6. |

- **The downloads made no difference at DWR-2801.** NSI, address points and NG911 roads have the same features (only OBJECTIDs and order differ), and the FEMA outlines are the same reused file. An inventory-only check with the new code gives the same result from the 10-18 and 10-20 downloads. That check was done for DWR-2801 only; the other dams used fresh downloads too (`DOWNLOADS=true`) and weren't checked.
- The class still rests on the BNSF KO rail stretches (≥10 trains/day, ND 5.2.1.3).

**Summary of all 65 results.**
- 59 results had a stored summary to compare with: 44 on Hazard 2.18.0, 3 on 2.18.11, 2 on 2.18.12, 5 on 2.18.34, 1 on 2.18.37 (the refined 72 h in-run hazard) and 4 on nd_hazard 2.30.1.
- The other 6 had no stored summary, so they have new numbers only.
- Of the 59:
  - **8 changed class** (table below).
  - LIJ changed in 42 (up in 34).
  - Habitable structures inundated changed in 20 (down in 18).
  - Dry structures cut off changed in 33 (down in 27).
  - 14 are unchanged on every metric.

| Set | Results | Class changes | Notes |
|---|---|---|---|
| Absaraka_compare (DWR-2801) | 22 (6 new-only) | 2 (TM 11) | ND High in every result that has a stored summary. The 3 `screen/7_simulations` copies (new-only) are ND Low / TM 11 Significant. |
| rebaseline_20261018 (DWR-2801) | 1 | 0 | See the DWR-2801 table above. |
| NickReview | 42 | 6 (2 ND, 5 TM 11; DWR-8278 both) | |

**Class changes (ND or TM 11).** The driver column comes from each copy's `outputs/QA/Changes_Since_Last_Run.csv` and `outputs/Classification/Habitable_Structures.csv`, compared with the original. "Consistent with M3" means the outputs show the pattern M3 produces: an NSI-only home becomes Unverified with 0 persons, while the address's 911 point is on another building. The outputs don't name the rule for each structure, though; only DWR-2801 was traced to the code (NOTE.md).

| Result | ND | TM 11 (LIJ) | Driver shown in the outputs |
|---|---|---|---|
| NickReview DWR-4560 | **High → Low** | Significant → Significant (3.0 → 0.0) | The only counted structure, #258 (8007 157th Ave SW, NSI AGR1 + RES1, Judgment zone, 3 persons), is now Unverified with 0 persons. The address's 911 point is on #211, which is dry. Consistent with M3. Inundated 1 → 0. |
| NickReview DWR-8278 | **High → Low** | **High → Significant** (6.0 → 0.0) | Judgment #191 (9250 56th Ave SW, 911 + NSI) is now an NSI-only Unverified point (#201). High-zone NSI-only #192 (RES1) moved onto outline #68 and is Unverified. Outline snapping needs the real areas (H5), so this is H5 + M3 by pattern. **To review:** the per-structure cause isn't confirmed in code. |
| NickReview DWR-10633 | High | **High → Significant** (6.0 → 3.7) | High-zone #391 and Judgment #383 (13205 12th St SE, NSI-only RES1) are now Unverified with 0 persons. The 911 point is on #430 (Low zone). Consistent with M3. Judgment #361 (3 persons) remains. Motorists 0 → 0.6 (lengths). ND High still rests on BNSF KO rail (16 stretches). |
| NickReview DWR-2635 | High | **Significant → High** (3.1 → 7.3) | Motorists 0.0 → 4.3 (High 0.5, Judgment 3.8). The old run had all 167 road stretches at 10 ft, the new one has real lengths (H5). The structure part is unchanged (Judgment #2154, 3 persons). |
| NickReview DWR-2666 | High | **High → Significant** (6.0 → 3.2) | Judgment #472 (12979 95th St NE, NSI-only) is now Unverified with 0 persons. The 911 point is on #294 (dry). Consistent with M3. Judgment #729 (3 persons) remains. Motorists +0.2. |
| NickReview DWR-2809 | High | **High → Significant** (9.0 → 3.5) | Judgment #731 and #748 (5487 County Road 12B, NSI-only) and #1122 (NSI point) are now Unverified with 0 persons. High-zone #778 (3 persons) remains. Motorists +0.5. Likely M3, but whether a 911 point sits on another building there wasn't checked: **to review**. |
| Absaraka_compare/gui_ds (2801) | High | **Low → Significant** (0.0 → 0.2) | Motorists only: the 12 High / Judgment road stretches had no AADT in the old run (10-13, 2.18.0 GUI). They now get ND_county_roads AADT (10–337) from the 2026-10-14 offline pack. This is a data-input difference, not the length bug (the old run already had real lengths). The structure part is 0 in both. |
| Absaraka_compare/hecras_tier2_20261018/tier2_ccc5d38 (DWR-2801) | High | **Significant → High** (0.0 → 7.5) | Two structures now count in the High zone. #476 (14853 30th St SE) was an outline-only Unverified house in the Judgment zone (old area NaN); it now has the NSI RES2 point snapped on (H5 areas) and is a mobile home in the High zone at 5.9 ft. #733 is an NSI-only RES1 point, Yes, 3 persons, 8.2 ft, with no counterpart in the old inventory. Motorists 1.4. **To review:** #733's appearance, and whether H4 or M3 should have touched it. |

Other notes:
- **The other 4 HEC-RAS tier2 copies keep their class.** `tier2_8b0601c` and `tier2_manual`: High / High, LIJ 9.0 → 7.5. `tier0`: High / Significant, LIJ 0.0 → 0.6, with the same numbers as rebaseline_20261018. `tier2`: new-only.
- **The ANUGA 48 h run now has 152 inundated, not 162** (`anuga_gmsh_20x20_48h_20261018`, was 2.18.12). LIJ 0.0 → 0.6 and cut-off 765 → 695. Still High (22 BNSF KO stretches at 22 trains/day, 11 roads with AADT > 2,000).
- **The refined 72 h run's corrected hazard is High / Significant** (`anuga_gmsh_refined_72h_20261019`, the in-run 2.18.37 was provisional). LIJ 0.0 → 1.2, inundated 40 → 34, cut-off 815 → 800 (persons 2,881 → 3,831). The summary flags it "incomplete: flood cut off (duration cap)". The run itself is reviewed separately with `REFINED_RUN_CHECKLIST.md`.
- **The nd_hazard 2.30.1 results** (`nd`, `gui_nd`, `nd_vrt`, `nd_vrt_contaminated`) were made by the old helper plugin. The comparison is across plugins. The class and LIJ are unchanged; `nd` cut-off went 445 → 448.
- **Three results changed structure counts without any class or LIJ change.** `SUMMARY.md`'s "Changed" column only flags class and LIJ, so it shows them as "-": NickReview DWR-2827 (cut-off 6 → 1), DWR-2899 (inundated 3 → 0, cut-off 2 → 1) and Absaraka `nd` (cut-off 445 → 448).

**Caveat: main has moved on.** Main is now Hazard **2.18.45** (#113, merged 20:45Z, after this batch started at 20:34Z). Its **H1** change goes back to the conservative default: a 911 address point with no building outline nearby and no NSI record counts as habitable again. These 2.18.44 numbers don't include H1, so H1-type points (for example, 911-only points listed as "ND site address with no building outline or NSI structure - unverified") may count again on 2.18.45. Before the numbers are adopted, decide whether to repeat the batch on 2.18.45. *Done: see "Hazard re-baseline on 2.18.45 (2026-10-20/21)" below.*

**Output folders** (copies next to the originals, suffix `_hazard_2.18.44_20261020`):
- `/home/box/DamSafety/Dams/Absaraka_compare/<run>_hazard_2.18.44_20261020/` for anuga_gmsh_refined_72h_20261019, anuga_gmsh_20x20_48h_20261018, anuga_structures_20261015, anuga_structures_20x20_48h_20261015, ds, gui_ds, gui_nd, hecras_tier2_20261018 (tier0, tier2, tier2_8b0601c, tier2_ccc5d38, tier2_manual), nd, nd_vrt, nd_vrt_contaminated, rerun_20261014, rerun_pr11_20261014 and rerun_quiet_20261015_0446
- `/home/box/DamSafety/Dams/rebaseline_20261018_hazard_2.18.44_20261020/`
- `/home/box/DamSafety/Dams/NickReview_hazard_2.18.44_20261020/`

The exact folder for each result is in the CSV (`new_output_folder`).

**QA (Docs & QA, read-only).** I re-parsed all 65 old and new `Hazard_Classification_Summary.txt` files with an independent script, and they match every class, LIJ, inundated and cut-off figure in `SUMMARY.md`. All 65 new summaries are DamSafety Hazard v2.18.44, written between 20:35Z and 21:43Z. I read DWR-2801 (rebaseline_20261018), DWR-4560, DWR-10633 and DWR-2635 in full. The 8 class-change entries of `changes.json` also match the outputs.

## Hazard re-baseline on 2.18.45 (2026-10-20/21)

**What was run.** Grok Bot repeated the same 65-result batch on DamSafety Hazard **2.18.45** (DWR_Plugins main **5b4ed05**, which adds #113), with the same settings as the 2.18.44 batch (`ndhazard:batch`, `DOWNLOADS=true REPORT=false EAP=false WORKERS=1`, no solver). It ran from a scratch QGIS profile (`/workspace/qgis_profiles/hazard_2_18_45`) on fresh **copies** of the original results, 23:31Z Oct 20 to 00:37Z Oct 21. **65 classified, 0 failed; DWR-3161 skipped.** The originals and the `_hazard_2.18.44_20261020` copies weren't modified, and no model was rerun. The rasters are the same.

Sources: `/workspace/hazard_rebaseline_2.18.45_20261020/` (`SUMMARY.md`, `changes.json`, `progress.log`, `jobs.json`). QA: `/workspace/rebaseline_2.18.45_qa_20261020/` (`NOTE.md`, `h1_restored_points.csv`, `Absaraka_anuga_structures_H1_QA_map.png`). Per-result table (2.18.44 vs 2.18.45, with QA notes): [results/hazard_rebaseline_2.18.45_20261020.csv](results/hazard_rebaseline_2.18.45_20261020.csv). Output folders: the `_hazard_2.18.45_20261020` siblings of the 2.18.44 folders listed above. The exact folder for each result is in the CSV (`output_folder_2_18_45`).

**What changed, and why: #113 / H1 only.** H1 makes a 911 address point count as habitable again (3 persons) when it has no building outline within the snap distance and no NSI record. This was the default before 2.18.39; in 2.18.44 such points were *Unverified* (0 persons). Nothing else changed between the two batches.
- **Every one of the 351 extra inundated structures across the 65 results is an H1-restored 911 point.** That is 106 distinct points, and the count matches exactly in every result.
- The 160 extra cut-off structures come from the same points.
- No ND class changed.

| Result | ND | TM 11 (LIJ) 2.18.44 → 2.18.45 | Inundated | QA verdict |
|---|---|---|---|---|
| Absaraka_compare/anuga_structures_20261015 (DWR-2801) | High = | **Significant → High** (0.9 → 6.9) | 2 → 5 | **Not plausible: keep TM 11 Significant (0.9).** #721 (3550 153rd Ave SE, 8.17 ft, High zone) is open cropland on NAIP. #727 (3520 153rd Ave SE, 5.56 ft, Judgment zone, counted under 5.3(f)) is a field edge, and its 911 NOTES say "added April 2024, WBI Energy is building a natural gas transmission line station". #709 (3485 152nd Ave SE, 0.31 ft, Low zone) is a tower pad. All three are 985–1,160 ft from any FEMA outline and more than 1,000 ft from any NSI point. #721 and #727 also sit in water ponded against the closed SE mesh edge: this 10-15 run's pass 2 hit the mesh edge at 8.58 ft (`progress.md`). |
| Absaraka hecras_tier2 tier2_8b0601c / tier2_ccc5d38 / tier2_manual | High = | High = (7.5 → 10.5) | 15 → 18 | **Double count: QA LIJ 7.5.** The +3 is 911 point #761 (3210 150th Ave SE, 7.19 ft, High zone). It sits on the farmstead's large shop building, which has no FEMA outline. The same address is already counted through NSI RES1-1SWB 103945889 (#733, 8.17 ft, High zone), 105 ft away, just beyond the snap distance. FEMA outline #273 also carries "3210 150TH AVENUE SOUTHEAST". This also explains the #733 "to review" note in the 2.18.44 section. |
| NickReview DWR-2645 (Pheasant Lake Dam) | High = | High = (9.1 → 15.1) | 16 → 23 | **Plausible but unverified.** The +6 is 26 E Lake Dr (4.53 ft) and 138 E Lake Dr (3.94 ft), both in the Judgment zone. They are lakeshore cabin and camper lots with no FEMA outline, so seasonal occupancy is possible. Of the other 5 added points, 4 are Low-zone E Lake Dr lots, and "0 83RD AVE SE" (address number 0, in trees by the river) isn't a dwelling. |
| NickReview DWR-4441 | High = | High = (46.4 → 49.4) | 62 → 72 | **The +3 is not a dwelling: QA LIJ 46.4.** 1255 Southport Loop (3.48 ft, Judgment zone) is a parcel-centroid point (`Placement=Parcel`) on the Southport marina docks. |
| Absaraka anuga_gmsh_refined_72h_20261019 | High = | Significant = (1.2) | **34 → 62** | **Upper bound, no class effect.** 26 of the 28 added points are platted lots of a new north-Casselton subdivision (4th Ave N, 5th Ave N, 8th St N) that has no FEMA or NSI records. On NAIP, about 7 have a house built or under construction; the others are vacant lots, which is arguably 5.3(b) imminent development. The other two are 3467 155th Ave SE (911 NOTES: a temporary address for a communications hut) and 502 Martins Ave. All are Low zone at 0.11–1.10 ft. |
| NickReview DWR-2635 | High = | High = (7.3) | 15 → 30 | **Mostly non-residential, no class effect.** The added points are Casselton commercial addresses (Industrial Blvd, Governors Dr suites A/B/C), plus a Dollar General, a Dairy Queen and a "new farm building" (all three named in the 911 NOTES). All are Low zone. |

**Sample check of the H1-restored points.** There are 106 distinct wet points, listed in `h1_restored_points.csv`.
- None is within 100 ft of a FEMA outline, and 70 are 300 ft or more from one.
- 6 have 911 NOTES naming a non-residential use: a store, a restaurant, a communications tower, a communications hut, a gas station and a farm building.
- 1 has address number 0, and 2 are parcel centroids.

14 points were checked on NAIP:
- **Not dwellings (8):** cropland, gas-station site, tower pad, communications hut, Dollar General site, communications tower, marina docks and riverside trees.
- **Plausible but unverified (5):** a house with no FEMA outline on 15th Ave N in Casselton, a new-subdivision lot, two lake lots and the 3210 150th Ave SE farmstead (a duplicate).

**H1 conservatively over-counts.** It picks up a few real dwellings that FEMA and NSI miss (new homes and cabins), but it also counts many non-dwelling 911 points as 3 persons each.

**Adopted baseline: both kept (2.18.44 not superseded).** QA found the H1 output physically implausible where it changes a class (anuga_structures), so both batches are kept and flagged:
- **Classes:** 64 of the 65 results have the same ND and TM 11 class on 2.18.44 and 2.18.45. For anuga_structures_20261015, the QA'd value is **TM 11 Significant** (2.18.44), not the 2.18.45 High.
- **LIJ:** 2.18.44 for the HEC-RAS tier2 variants (7.5) and DWR-4441 (46.4). The 2.18.45 values are double or non-dwelling counts. DWR-2645 is 9.1 verified, 15.1 if the lake lots are occupied.
- **Inundated and cut-off counts:** the 2.18.45 values are an upper bound that includes unverified 911-only points. The 2.18.44 values are a lower bound. Report both until the H1 points are reviewed. The current tool output is 2.18.45, so its classes need this note wherever they're quoted.

**DWR-8278 (Wells Dam): ND Low (screening), TM 11 Significant. Corrected 2026-10-21 after the footprint-offset study.** This replaces the earlier "contingent on 9250 56th Ave SW being unoccupied" wording. Details are in the latest section of the Docs & QA hand check, `/workspace/dwr8278_handcheck_20261020/NOTE.md`, with the map `DWR-8278_footprint_offsets_map.png`.
- **The FEMA outlines here are shifted, and it isn't our reprojection.**
  - The FEMA USA Structures outlines at these farmsteads come from Maxar scene `1030010045BF2F00` (2015-06-26). They sit about **14.7 m SW** of the real buildings on NAIP 2023.
  - The other sources line up within a few metres: Microsoft ML footprints 1.2 m, NSI 1.3 m, 911 points 0.4 m, OSM 5.3 m N.
  - Hazard's EPSG:5070 transform agrees with GDAL to within 0.2 m, and the raw FEMA download and its own LAT/LONG show the same shift. So the offset is in the source data.
- **With the outlines corrected, 9250 56th Ave SW is High.** The 2.18.45 run was redone with the scene's outlines moved back by (+8.8, +11.7) m (`run_2.18.45_femashift/`).
  - The single-wide at 9250 56th Ave SW then lies in the **mobile-home High zone (2.66 ft, 4.25 ft/s, 3 persons)**.
  - That gives **ND High / TM 11 Significant (LIJ 3)**.
  - The 9260 farmhouse stays dry, and the 9260 machine shed stays Unverified (M3).
- **The single-wide is gone, so Low fits current conditions.** On Esri Wayback / NAIP it is:
  - present 2017-09-09 and 2021-06-22;
  - **gone** in NAIP 2023-07-06 and in Vantor 2025-10-13.
- **But the run's "dry" result for 9250 is a geometry artifact.** The 911 point snapped onto a FEMA outline that is shifted into a dry cell. Don't cite it as the reason for Low. The 911 address (last updated 2021-08-23) and NSI 104080075 still list the home.
- **What doesn't change:**
  - 2.18.45 (H1) gives the same as 2.18.44: Low / Significant, LIJ 0.
  - The old High's double-counted 9260 machine shed (NSI RES1) is still correctly dropped.
  - The NID lists Low.
- **Open:** confirm with Grant County 911 or the owner whether the 9250 pad is vacant or will be reoccupied. If it's reoccupied, the dam is ND High.

**Footprint offsets near DWR-8278**, measured against NAIP 2023 roof centroids. Table: [results/dwr8278_footprint_offsets_20261021.csv](results/dwr8278_footprint_offsets_20261021.csv); per-building values are in `offsets/offsets_validated.csv`.

| Source | n | Mean shift | Mean dx / dy (m) | Median distance (m) | RMS after removing the mean (m) |
|---|---|---|---|---|---|
| FEMA USA Structures (scene 1030010045BF2F00) | 11 | **14.7 m SW** | -8.8 / -11.7 | 15.0 | 4.8 |
| OSM buildings | 16 | 5.3 m N | 0.0 / +5.3 | 5.6 | 3.6 |
| Microsoft Global ML footprints | 14 | 1.2 m NE | +0.9 / +0.7 | 1.8 | 2.1 |
| NSI 2022 points | 8 | 1.3 m NE | +1.0 / +0.8 | 1.7 | 1.8 |
| ND 911 address points | 3 | 0.4 m | +0.1 / -0.4 | 1.8 | 1.6 |

FEMA outlines from the other Maxar scenes in this study area are within about 2 m.

**Caveat for all Hazard results: FEMA outline offsets can flip results.** Hazard samples depth at the FEMA outline that a 911 or NSI point snaps onto. If a whole source scene is shifted 10–15 m, a structure can be read from the wrong 40 m cell, wet as dry or dry as wet, and the class can change, as at DWR-8278. Other dams haven't been checked for scene offsets. SWE has been asked for a fix with three parts:
- a per-scene offset check of FEMA outlines against NSI (`IMAGE_NAME` groups);
- Microsoft Global ML footprints in the offline pack as the geometry source, keeping the FEMA attributes;
- a review flag when a snap moves a structure into a cell with a different depth or zone.

**Also noted.**
- The 2.18.45 run fetched OSM lifelines live from Overpass (`OSM_lifelines_20261020_*.geojson`; for example, 1 dry substation in anuga_structures), whereas the 2.18.44 run used the pack. No lifeline was inundated in either run, so no number changes.
- DWR-8278 and DWR-4560 stay ND Low / TM 11 Significant, LIJ 0.

**Verification (Docs & QA).** I re-parsed all 65 old and new results independently, from `Hazard_Classification_Summary.csv` and `Classification/Habitable_Structures.csv` rather than the `.txt` that `summarize.py` reads.
- Every class, LIJ, inundated and cut-off figure matches Grok Bot's `SUMMARY.md`, and so do the 33 changed results.
- All 65 new summaries are v2.18.45.
- The inundated counts also equal the count of `Habitable=Yes` structures at 0.1 ft or more in every result.

## Run queue 2026-10-21 (main caaf850, Runner 3.10.47): QA by Docs & QA
Read-only QA; reports in `/workspace/run_queue_qa_20261021/<job>/REPORT.md` on the shared box (tracker `QA_TRACKER.md`). Queue: `/workspace/run_queue_20261021/STATUS.md`.

**TIER 0 re-baseline on caaf850: unchanged.** `/home/box/DamSafety/Dams/rebaseline_20261021/DWR-2801/`, same settings as 10-18 (culvert burn on, 2 NDDOT culverts). Depth, velocity, arrival and WSE rasters are identical cell for cell to the 10-18 re-baseline, so nothing merged from 8b0601c through #132 changed Absaraka's TIER 0 numbers.

| Dam | Code | Area any / ≥0.1 ft / ≥1 ft (ac) | Max depth (ft) | Max velocity (ft/s) | Arrival max (h) | Edges N/S/E/W (ft) | Class |
|---|---|---|---|---|---|---|---|
| Absaraka DWR-2801 | caaf850 (Runner 3.10.47) | 6,939.1 / 5,758.5 / 1,802.1 | 10.13 | 12.71 | 28.21 | 0.0 / 0.0 / 0.73 / 0.0 | High |

**HEC-RAS 6.6 comparison references (36.25 h, from the plan HDFs).** Not baselines; the 2D area is truncated downstream (about 70% of the volume leaves through the normal-depth line) and HEC-RAS routes the breach through its own storage area, so its peak is 72-74% of the plugin's.

| Package | Engine | Cells | Peak dam flow (cfs) | Area ≥0.1 / ≥1 ft (ac) | Max depth (ft) | Volume error |
|---|---|---|---|---|---|---|
| v2d (100 ft grid) | old (`rr_tmp/pkgpath`) | 64,100 | 14,612 | 7,754 / 2,926 | 14.75 | 0.56% |
| v2d_gmsh | old | 65,432 | 14,622 | 8,193 / 2,837 | 14.69 | 0.56% |
| v2e (dam line on the crest, #103) | 8d5493f (#137) | 64,181 | 17,189 | 7,937 / 3,071 | 16.68 | 0.63% |

v2e's lower breach invert (2.6 ft below v2d's) raises the peak 18% and the ≥1 ft area 5%. v2d vs v2d_gmsh differ by about 3%. Hazard was not scored for these runs.

**Not used:**
- ANUGA with NDDOT culverts on the 10 x 9 mi TIER 1 domain (`Absaraka_compare/anuga_nddot_culverts_20261021`): the 1,000 m widen pass is clipped by the LISFLOOD grid; water ponds 10 ft deep at the SE mesh corner and the domain ends with 4.15 times the injected volume. Needs a rerun on the refined runs' 25 x 20 mi box.
- HEC-RAS v2e_gmsh_r: HEC-RAS refused to start because the dam connection's spillway notch (1,066.0 ft, stations 100-220) is lower than two connected cells (1,066.25 and 1,066.61 ft). The 8d5493f engine then hid the message behind a TypeError (a None time limit in an error string).

## Open items
- **Queue 2026-10-21:** rerun the NDDOT-culvert ANUGA case on the 25 x 20 mi box; fix v2e_gmsh_r's notch cells and the engine's None-timeout message, then rerun it. Refined base rerun on caaf850 and the KO-06/07-closed variant are still running.
- **Refined ANUGA rerun (Absaraka) running** (see above). Review it with `REFINED_RUN_CHECKLIST.md`, then the KO-06/07 closed variant.
- **DWR-3161 is on hold:** flagged Low (#93), with breach runs refused. It waits for DWR's answers to the record questions in #93. Its 10-18 TIER 0 numbers aren't used.
- **Merged 10-19:** #92 (run-report metrics and the opt-in water-body mask, Runner 3.10.27, closes #91) and #96 (HEC-RAS timeouts on the wall clock, Runner 3.10.26). Main is now Runner 3.10.32 (#94, which changes the default top of dam to the 98th percentile within 30 m, M6), so runs on current main are not directly comparable with the runs above. See CHANGELOG.md.
- HEC-RAS vs ANUGA/TIER 0 area gap: recompute HEC-RAS depth on the 40 m DEM, compare at matching thresholds and sim times, and check Manning's n.
- **The Hazard re-baselines on 2.18.44 and 2.18.45 (2026-10-20/21) supersede the batch hazard numbers above.** All batch hazard results before 2.18.39 are superseded. 2.18.44 is *not* superseded by 2.18.45: both are kept, because H1 counts unverified 911-only points (see the 2.18.45 section). Open: review the H1-restored points (`h1_restored_points.csv`), especially Absaraka anuga_structures (QA value TM 11 Significant) and the DWR-2645 lake lots.
- **DWR-8278:** ND Low (screening) / TM 11 Significant reflects current conditions: the single-wide at 9250 56th Ave SW was removed between 2021 and 2023. Note that the as-run "dry" result for 9250 is a FEMA-offset artifact; with corrected outlines the home is in the mobile-home High zone. Open: confirm with Grant County 911 or the owner whether the pad is vacant or will be reoccupied. If it's reoccupied, the dam is ND High.
- **FEMA outline offsets can flip results.** At DWR-8278, one Maxar scene's outlines are 14.7 m SW of the real buildings. Other dams haven't been checked. SWE has been asked for a fix: a per-scene offset check against NSI, Microsoft footprints as the geometry source, and a flag when a snap changes the depth or zone. Until it lands, treat any class that rests on a snapped FEMA outline near a wet/dry edge as needing an imagery check.
- Re-baseline numbers aren't yet adopted as the trusted baseline. That needs the user's approval. Note that they were run with the non-default culvert burn on.
