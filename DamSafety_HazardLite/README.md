# DamSafety Hazard Lite 0.1.0

A small QGIS plugin: breach depth and velocity rasters in, hmax / vmax at every 911-addressed
structure, overtopped road and inundated railroad out, plus the ND hazard class from
ND Dam Safety Standards REG_05.2026 Section 5.2. One Processing tool, no custom window.

**Size:** 4 Python files, about 690 lines (`core.py` 520, `plugin.py` 100, `tm11.py` 72), against
about 80,600 lines in DamSafety_Hazard. Only GDAL / OGR and numpy, so `core.run()` also runs outside QGIS.

## Inputs (all from the ND Dam Suite offline pack by default)

| Input | Pack layer | How it is used |
|---|---|---|
| Max depth, max velocity | breach run rasters | ft and ft/s (tick the box for m and m/s) |
| 911 address points | `NDGISHUB_address_points` | each point is a habitable site; `LOT` points are skipped |
| FEMA occupancy | `USA_Structures.PRIM_OCC` / `OCC_CLS` | a point on a FEMA "Agriculture" outline is dropped as an out-building (5.2.1.1), unless the 911 point or occupancy marks a dwelling. On by default; a switch keeps them |
| Structure outlines | `USA_Structures` (FEMA) | the outline holding the point, else the nearest within 30 ft; every cell the outline touches is sampled; no outline = 20 ft circle, flagged |
| Roads | `ND_roads_NG911` | sampled every half cell; each wet run (>= 0.1 ft) is one spot at its deepest point |
| Traffic | `NDDOT_traffic_counts` | the nearest station that sits on the same named road, within 1 mile |
| Railroads | `NARN_rail_lines` | abandoned / removed / trail lines skipped |
| Trains/day | `FRA_grade_crossings` | highest day + night + switching count of the same railroad within 3 miles |

## Rules (REG_05.2026 Section 5.2, as the ND Dam Hazard plugin codes them)

- **High**: a habitable structure in the high danger zone of Fig. 5.1 (houses) or Fig. 5.2
  (mobile homes, from FEMA "Manufactured Home" or 911 "MOBILE HOME") [5.2.1.1]. The judgment zone counts as
  High by default (5.3 borderline rule; can be switched off). An overtopped Interstate or road with AADT > 2,000
  [5.2.1.2]. An inundated passenger line, or a freight line with 10 or more trains/day [5.2.1.3].
- **Medium**: a habitable structure inundated to any degree (>= 0.1 ft) [5.2.2]; an overtopped US or State
  highway, or a road with AADT 500 to 2,000 [5.2.1.2]; any other inundated railroad [5.2.1.3].
- **Low**: none of these.

A structure's zone comes from its worst cell (each cell's own depth and velocity on the chart). The table
reports hmax and vmax over the outline, and the depth and velocity of the governing cell.
The chart table is a verbatim copy of `damsuite/tm11_curves.py` (signed off by Nicholas Bendickson, 2026-10-12).

## Outputs (output folder)

- `hazard_summary.md`: the class, the reasons with section numbers, the items to verify, and tables.
- `hazard_lite.gpkg`: `structures` (outlines), `road_spots`, `rail_spots` (points at the deepest spot), loaded into the project.
- `structures.csv`, `roads.csv`, `railroads.csv`.

## Left out on purpose (compared with DamSafety_Hazard)

NSI and the habitability rules, lives in jeopardy (TM 11 Table 1), the sole-access road network,
lifeline facilities, bridges and culverts, dams in series, future development, EAP products, map book,
PDF report, the spreadsheet plots, downloads, history, review tabs. The summary lists the 5.2 / 5.3 items
it does not screen so the engineer reviews them.

## Absaraka Dam check (run of 2026-10-19, 20 m cells)

Ran in 3.5 s in QGIS 3.34.4 through `qgis_process`.

| | Hazard Lite | DamSafety / ND Hazard 2.32.0 |
|---|---|---|
| ND class | High | High |
| Judgment-zone structures | 10 | 5 |
| Inundated habitable structures | 165 | 104 |
| High roads (AADT > 2,000) | 5: Governors Dr, Langer Ave S | 5: the same |
| Medium roads | 16 | 16 |
| High rail stretches (BNSF KO) | 11 | 12 |

Lite finds more structures because every 911 point counts (no NSI rules) and because a 20 m cell
that only touches an outline is sampled. Example: 101 3rd Ave N reads 3.44 ft here and 2.27 ft in the full plugin.
FEMA "Agriculture" out-buildings are excluded by default (5.2.1.1); on Absaraka the flood reaches a town, so none were dropped.

## Window

Plugins menu > DamSafety Hazard Lite > DamSafety Hazard Lite… opens a one-window form built from the
shared `shared/ui` widgets (info banner, stage pills, CheckPanel, bold Classify beside Check inputs,
▸ Log fold, ⚙ gear), so it matches the other DamSafety windows (docs/gui-grammar.md, checked by
tools/gui_lint.py). The Processing tool stays, so the same run also works headless.

## Install

Copy the `DamSafety_HazardLite` folder into the QGIS plugins folder (or Plugins > Install from ZIP), enable it,
then Processing Toolbox > DamSafety Hazard Lite > Classify dam hazard. The pack folder defaults to the suite's
`ndsuite/pack_path` setting.
