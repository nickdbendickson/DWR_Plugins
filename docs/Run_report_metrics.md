# Run report metrics (Runner 3.10.27, issue #91)

Reporting only: none of this changes the model, the result rasters or the flood polygons.
Every field is new; the existing fields (`flooded_acres`, `flooded_acres_over_arrival_depth`,
`max_depth_ft`, `max_velocity_fps`, ...) keep their names and values, so old reports and
comparison scripts keep working. Code: `DamSafety_Runner/core/report_metrics.py`.

## Why

On the 2026-10-18 re-baseline the any-depth area was mostly a thin film (DWR-3161: 4,841.7 ac at
any depth, 703.0 ac at 1 ft or more) and the maximum velocity was one shallow cell (19.36 ft/s in
0.15 ft of water, against a P99 of 4.48 ft/s).

## New `run_report.json` `summary` fields

| Field | Meaning |
|---|---|
| `flooded_acres_ge_0_1ft`, `flooded_acres_ge_1ft`, `flooded_acres_ge_2ft` | area (ac) at or above 0.1, 1 and 2 ft |
| `velocity_p95_fps`, `velocity_p99_fps` | 95th / 99th percentile velocity over wet cells |
| `velocity_wet_min_depth_ft` | what "wet" means for the percentiles (0.1 ft) |
| `max_velocity_depth_ft` | depth at the cell of `max_velocity_fps` |
| `max_velocity_xy`, `max_velocity_xy_crs` | centre of that cell, in the CRS of the final grid |
| `velocity_at_structures_max_fps`, `velocity_at_structures_p95_fps` | over flooded structures (depth at or above the arrival depth) |
| `velocity_at_roads_max_fps`, `velocity_at_roads_p95_fps` | over overtopped road samples (every 30 m) |
| `velocity_at_rail_max_fps`, `velocity_at_rail_p95_fps` | over overtopped rail samples |

A receptor field is there only when that kind of receptor is flooded.

### With a water-body mask

| Field | Meaning |
|---|---|
| `water_mask_source` | file name(s) of the mask |
| `water_mask_acres` | area of the mask inside the model grid |
| `water_mask_flooded_acres` | wet area (any depth) under the mask |
| `flooded_acres_masked`, `flooded_acres_over_arrival_depth_masked` | the existing area fields, without the masked cells |
| `flooded_acres_ge_0_1ft_masked`, `_ge_1ft_masked`, `_ge_2ft_masked` | the threshold areas, without the masked cells |
| `velocity_p95_fps_masked`, `velocity_p99_fps_masked` | the percentiles, without the masked cells |
| `max_velocity_fps_masked`, `max_velocity_depth_ft_masked`, `max_velocity_xy_masked` | the max-velocity cell, without the masked cells |

`run_report.json` `water_mask` lists the sources used, any that could not be read (also a
warning), and `Water Body Mask.geojson` (the mask as polygons) in the dam folder.

### Where the mask comes from (first found are all combined, the user's first)

1. `WATER_MASK` (rundam, advanced) or `RunSettings.water_mask`: a vector file
   (`path|layername=x` works; several separated by `;;`) or polygons in the work CRS.
   Without it, the `NDBREACH_WATERBODY_MASK` environment variable.
2. The dam folder's data section `waterbodies` layer (the Data plugin's per-dam step).
3. `hydro/3DHP_waterbodies.gpkg`, another `hydro/3DHP_*waterbod*.gpkg` (the 3DHP download's own names, first by name), or the pre-1.19 `hydro/NHD_waterbodies.gpkg` in the suite pack
   or the suite data folder, when that file exists. `ND_Dam_Suite_Pack/LAYERS.txt` lists it but
   the pack does not hold it yet; adding it is a Data-plugin step. The Runner only reads.

`off` (in the setting or the variable) turns the mask off. Polygons are reprojected onto the
final grid and burnt at cell centres. Cover the dam's own reservoir and any receiving lake below
the dam with the polygons to leave them out of the masked figures.

## Other places

- `run_report.md` / `.html` and the DOCX report: rows for the threshold areas, P95 / P99, the
  max-velocity cell, receptor velocities and the masked vs unmasked figures.
- `lisflood_runs[].stats` (and the ANUGA run stats): `flooded_acres_any`, the three threshold
  areas, `velocity_p95_fps`, `velocity_p99_fps`, `max_velocity_depth_ft`, `max_velocity_xy`.
  The existing per-pass `flooded_acres` (at or above the arrival depth) is unchanged.
- Batch CSV, appended columns: `Flooded_ge_0_1ft_ac`, `Flooded_ge_1ft_ac`, `Flooded_ge_2ft_ac`,
  `Velocity_P95_fps`, `Velocity_P99_fps`, `Max_Velocity_Depth_ft`, `Flooded_Masked_ac`,
  `Flooded_ge_1ft_Masked_ac`.
- Sensitivity and probabilistic tables, appended columns: `flooded_ac_ge_0_1ft`,
  `flooded_ac_ge_1ft`, `flooded_ac_ge_2ft`, `velocity_p95_fps`, `velocity_p99_fps`,
  `max_velocity_depth_ft`, `flooded_ac_masked`, `flooded_ac_ge_1ft_masked`.
- QA depth map (`qa_depth_map.png`): the title's first line is as before; the next lines give
  the area at any depth, at 0.1, 1 and 2 ft, the P99 and max velocity with the depth at the max
  cell, and the masked area. The mask is drawn hatched with an orange outline.
- TIER 2 (HEC-RAS 2D): `workflow.adopt_final_engine` recomputes all of these from the HEC-RAS
  rasters (old values from the screening pass are dropped first and stay under
  `screening_pass_lisflood`).
