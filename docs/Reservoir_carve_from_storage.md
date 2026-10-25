# Reservoir bottom carved from the storage curve (Runner 3.10.20, opt-in)

## Problem

LiDAR does not see below a water surface. Where the pool was full when the DEM was flown, the
DEM reservoir is a flat sheet a few feet below the top of dam, and the terrain holds a small
fraction of the dam's storage. DWR-3161 (Sanish Bay WPA Dam 1, H 28.1 ft, top of dam
1844.44 ft): the ground below the top of dam upstream covers 30.2 ac against 203.7 ac on the
storage curve, and the reservoir floor is never below 1840.4 ft. The 3.10.17 HEC-RAS site
checks stop the breach model on that (`storage_site_check`).

## Option

`RESERVOIR_CARVE` (Runner tool parameter, advanced; settings file key `RESERVOIR_CARVE`; CLI
`model.reservoir_carve`; `RunSettings.reservoir_carve`; environment `NDBREACH_RESERVOIR_CARVE`):

| value  | what happens |
|--------|--------------|
| `off`  | default. Nothing changes; the run and its report are as before. |
| `auto` | carve only when the DEM shows the flat-water problem: a flat water surface in the footprint **and** the DEM's volume or area at the top of dam under half the storage curve's, or the floor less than half the dam height below the top of dam (the site checks' note thresholds `STORAGE_NOTE_*`). |
| `on`   | carve whenever the DEM holds less than the storage curve at the top of dam. |

## Target curve

The plugin's own storage law, the one the breach hydrograph and the HEC-RAS site checks use
(`breach.Reservoir` through `hecras_model.storage_curve`): `V(y) = Vt (y/Ht)^m` above the
bottom `top of dam - H`, `m = SA*Ht/Vt` (1..6; 2 without a surface area), area `dV/dy`.

## Footprint

On a grid read through the run's terrain (work CRS, metres; cell about 2 m, at most ~6 M cells):

1. Upstream of the dam: the side opposite the downstream direction
   (`terrain.downstream_direction`, the same rule #84 uses for the inflow; the run's
   downstream vector when the run already has it), by more than half a cell.
2. Within `R = clamp(3 sqrt(curve area at the top), 600 m, 5 km)` of the dam point.
3. Ground at or below the top of dam; the DWR dam line (when the run has it) is a wall.
4. A lake or waterbody polygon, when one is given, clips it. The run hook does not pass one
   today (the suite pack's `hydro/NHD_waterbodies.gpkg`, restored 2026-10-24, is not read
   here), so the DEM alone sets the footprint.
5. 4-connected to the lowest such cell next to the dam.

## Carve

* Flat water: the footprint cells within 0.5 ft of its low (1st percentile) elevation; when they are at
  least a quarter of the footprint they are the carve region and their median is the water
  surface `z_w`. Without flat water the carve region is the whole footprint.
* Shoreline: the carve region's edge cells against dry ground are left unchanged, so the pool
  edge stays the LiDAR water line. The dam side is not shoreline (deepest water is at the dam).
* Bowl: `z' = min(z, max(floor, face, z - s (z_w - floor) t^p))`, `t` = distance from the shoreline over the
  largest distance (0 on the shoreline). The floor is the storage curve's bottom (or the
  breach bottom when that is higher): never below it. The upstream face of the dam
  (`us_slope_h`, 3H:1V default, from the crest half width) is kept: no cell goes below it.
* `s = 1` and `p` by bisection so the volume at the top of dam is the curve's (within 2 %);
  when even `p = 8` holds too much, `p = 2` and `s` by bisection. When `p = 0.3` cannot hold
  it (the footprint is too small for the storage), that bowl is kept and the shortfall is a warning.
* Only lowered, only inside the carve region; the source DEM is never written.

## Outputs

* `_breach_model/<dam>_dem_carved.tif` (carved DEM over the carve window) and
  `<dam>_carve_depth.tif` (metres lowered, work CRS), `<dam>_carve_depth.png` (preview).
* The engines read the carved terrain: `CarvedTerrain` wraps the run's terrain source, so
  `model_dem()` (LISFLOOD-FP, ANUGA, the near-dam refine) subtract the depth raster area-averaged onto their grid
  (volume kept at any cell size). The HEC-RAS builder (`hecras_model.build_model`) lowers its
  `Terrain/<dam> Terrain.tif` by the same depth raster, so the site checks see the carve.
* `run_report.json` `reservoir_carve`: mode, applied / why, method, footprint and water areas,
  water surface, floor, `p` and `s`, max carve depth, carved volume, target vs DEM vs carved
  volume and area at the top of dam, the water surface and 25/50/75 % of the height, files.
* Report warning: "Reservoir bottom carved from the storage curve; the lidar showed a water
  surface ..." (plus the shortfall when the footprint is too small).

## Not in scope

Downstream flat water (also DWR-3161: the toe ground is the same water sheet) is a separate
problem: nothing downstream is carved. The HEC-RAS breach-bottom-vs-toe and relief checks
still apply after the carve.
