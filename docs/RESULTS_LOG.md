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

- **Class High** (`Hazard_Classification_Summary.csv`): the BNSF KO rail stretches 2.5 km below the dam are wet in all three models (ANUGA 1.7-3.6 ft, re-baseline 0.5-4.3 ft, HEC-RAS 1.4-4.9 ft), plus 11 overtopped roads with AADT > 2,000 in Casselton.
- **Max depth 11.26 ft is a one-cell DEM trench in Casselton**, 16 km downstream. Near the dam, the max is **9.8 ft**. The same-run LISFLOOD has 11.36 ft at the same spot.
- **Use ≥0.1 ft for reporting.** The "any depth" figure counts a 1 mm film: 3,244 ac of it is under 0.1 ft.
- **Edge and time:** not edge-capped (0.42 ft at the mesh edge). It is **time-capped at 48 h**: 102 ac of the ≥1 ft area arrives after 40 h, and the front is still moving east.
- **Mass balance:** 1,612.7 ac-ft in, 1,606.6 ac-ft in the domain at 48 h; the 0.38% difference left through the open boundary.

**Agreement: the headline is F 0.70 at ≥1 ft against the in-run LISFLOOD** (same 20x20 domain, 48 h, DEM and inflow; mean depth bias -0.003 ft).
- The **0.63** (LISFLOOD re-baseline) and **0.45** (HEC-RAS TIER 2) figures are artifacts of the comparison. Those rasters cover only 10 x 9 mi and sit half a cell (20 m) off ANUGA's grid, so 21% of ANUGA's ≥1 ft area could never match. Inside their grid, F is 0.73 and 0.60 (0.84 and 0.83 at ≥0.1 ft). Don't quote 0.63 or 0.45 as model disagreement.

**Flags:**
- **East pond, unconfirmed.** 107 ac at ≥1 ft, 3.2 ft max, around EPSG:5070 (-84,739, 2,656,831), 23.7 km ESE of the dam. It's breach water held behind the BNSF KO grade (crest 910.4-915.3 ft), where no rail culvert is modelled and the triangles are coarse (median 4,323 m²). 1 ft first arrives after 35 h. The same-run LISFLOOD is dry there.
- **Casselton consequences are shallow and late.** 162 habitable structures and 11 roads with AADT > 2,000, but the median structure depth is 0.23 ft (50 structures at ≥1 ft) and the water arrives after about 18-28 h. Same-run LISFLOOD corroborates the extent (446 ac ≥0.1 ft there).

**Refined rerun in progress:** `Absaraka_compare/anuga_gmsh_refined_72h_20261019/`, started 10-19 05:18Z by Grok Bot. Changes: 72 h, triangles ≤500 m² along the Casselton-to-pond corridor and the BNSF grade, 10 BNSF KO rail openings (culvert sizes assumed except the Maple River bridge), an 8 km wider east box with a 2,000 m buffer, and ANUGA elevation from the LiDAR at 10 m. A sensitivity run with the KO-06 and KO-07 culverts closed (`..._ko0607closed`) is prepared, to run afterwards. The review checklist is `qa_review_anuga_20261019/REFINED_RUN_CHECKLIST.md`. Until it's reviewed, report the 48 h run against the in-run LISFLOOD (F 0.70).

## Open items
- **Refined ANUGA rerun (Absaraka) running** (see above). Review it with `REFINED_RUN_CHECKLIST.md`, then the KO-06/07 closed variant.
- **DWR-3161 is on hold:** flagged Low (#93), with breach runs refused. It waits for DWR's answers to the record questions in #93. Its 10-18 TIER 0 numbers aren't used.
- **Pending review, not merged:** #92 (run-report metrics and the optional water-body mask, Runner 3.10.22, issue #91) and #96 (HEC-RAS timeouts on the wall clock, Runner 3.10.24). See CHANGELOG.md.
- HEC-RAS vs ANUGA/TIER 0 area gap: recompute HEC-RAS depth on the 40 m DEM, compare at matching thresholds and sim times, and check Manning's n.
- Re-baseline numbers aren't yet adopted as the trusted baseline. That needs the user's approval. Note that they were run with the non-default culvert burn on.
