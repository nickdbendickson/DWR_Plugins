# Results log

Absaraka = DWR-2801. Unless noted, these runs used the old trusted baseline settings with NDBREACH_DEM_RESAMPLE=average. Source: `/workspace/dwr_test/pr12_15_progress.md` on the shared box.

| Date (UTC) | Dam / route | Code | Peak (cfs) | Flooded area (ac) | Max depth (ft) | Class | Notes |
|---|---|---|---|---|---|---|---|
| baseline | Absaraka DWR-2801 | before #14 | 25,801 | 5,067.0 | 13.06 | High | Trusted baseline. Storage 1,992,690 m³, exponent 1.9. The #22 default-settings run reproduced it exactly (identical raster). |
| baseline | DWR-3161 | before and after #14 | 34,272 | 4,876.5 | 13.16 | High | Unchanged by #14. |
| 10-15 03:3x | Absaraka, scripted (runnerhandoff) | #14 @ 9fbbaa0 | 25,801 (expected, not re-logged) | 6,939.5 | 9.38 | High (expected) | Inflow point moved |
| 10-15 03:3x | Absaraka, wizard (rundam) | #14 @ 9fbbaa0 | — | 6,587.6 (from 8,191.6) | 8.84 | — | Inflow point moved |
| 10-15 | Absaraka + structures (LISFLOOD, bridge notches) | #20 | — | 5,067.8 | — | — | 23 structures read, no ValueError |

## Cause of the #14 change
The inflow point moved about 80 m to a cell 2.1 m lower: the lowest qualifying cell instead of the nearest one. The flooded area went up and the depth went down, because the flood now starts further down the valley.

## Open items / discrepancies
- **The 8,191.6 ac "before" value for the wizard route** isn't in `pr12_15_progress.md`. It came from the hand-off. Confirm which run produced it.
- **The 80 m and 2.1 m figures** aren't in the progress file either. They came from the hand-off, so confirm them against `/workspace/dwr_test/absaraka_inflow`.
- The scripted peak and class after #14 aren't recorded in the progress file. The re-baseline plan expects the peak to stay 25,801 cfs and the class to stay High.
- **The full re-baseline on main (plan step 1-7) hasn't been run yet.** Don't replace the trusted baseline numbers without the user's approval.
