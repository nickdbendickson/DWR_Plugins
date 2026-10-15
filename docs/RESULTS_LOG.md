# Results log

Absaraka = DWR-2801. Unless noted, these runs used the old trusted baseline settings with NDBREACH_DEM_RESAMPLE=average. Source: `/workspace/dwr_test/pr12_15_progress.md` on the shared box.

| Date (UTC) | Dam / route | Code | Peak (cfs) | Flooded area (ac) | Max depth (ft) | Class | Notes |
|---|---|---|---|---|---|---|---|
| baseline | Absaraka DWR-2801 | before #14 | 25,801 | 5,067.0 | 13.06 | High | Trusted baseline. Storage 1,992,690 m³, exponent 1.9. The #22 default-settings run reproduced it exactly (identical raster). |
| baseline | DWR-3161 | before and after #14 | 34,272 | 4,876.5 | 13.16 | High | Unchanged by #14. |
| 10-15 ~05:30 | Absaraka, scripted (runnerhandoff) | installed main after #14 | 25,801 | 6,939.5 | 9.38 | High | Exponent 1.9. Inflow (-106263.8, 2666571.5). `runs/final_main` (summ_v2.py final_main). |
| 10-14 | Absaraka, wizard (rundam) | before #14 (m11, main 41f4a4e) | — | 8,191.6 | — | — | `runs/m11` (abs_gui) |
| 10-15 03:3x | Absaraka, wizard (rundam) | #14 @ 9fbbaa0 | — | 6,587.6 | 8.84 | — | `runs/r3pr14` (abs_gui) |
| 10-15 | Absaraka + structures (LISFLOOD, bridge notches) | #20 | — | 5,067.8 | — | — | 23 structures read, no ValueError |

## Cause of the #14 change
The inflow point moved from (-106364, 2666612) at 320.46 m to (-106264, 2666572) at 318.33 m. That is about 108 m away and 2.1 m lower: the lowest qualifying cell instead of the nearest one. Source: run_report and manifest in `runs/m11` and `runs/pr14`. The flooded area went up and the depth went down, because the flood now starts further down the valley. The peak (25,801 cfs), storage exponent (1.9) and class (High) are unchanged.

Paths are under `/workspace/dwr_test/` on the shared box. The values were spot-checked by text search of those run folders.

## Open items
- **The full re-baseline on main (plan steps 1-7, including the NickReview 44-dam set) hasn't been run yet.** Don't replace the trusted baseline numbers without the user's approval.
