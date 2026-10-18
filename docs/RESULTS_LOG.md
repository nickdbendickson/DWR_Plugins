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

## Open items
- **The full re-baseline on main (plan steps 1-7) hasn't been run yet.** Don't replace the trusted baseline numbers without the user's approval.
