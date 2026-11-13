# DamSafety Finder

Finds structures that are **not on the lists**: dams (with or without a pool), road fills that
hold water, ponds, dugouts, open water no hydrography layer has, and the road / rail crossings of streams where a culvert or a bridge
must be. Every candidate is checked against the known-structure layers you give and marked
**listed**, **unsure** or **new**.

Screening aid. The LiDAR is not a survey: check every candidate against an aerial image. A "new"
candidate is new to the lists you gave, not necessarily to the Department.

Install: needs DamSafety Core. Processing Toolbox → DamSafety Finder → Find structures.

## The tools

| Tool | Input | Finds |
|---|---|---|
| `ndfind:embankments` Find fills across streams and draws | bare-earth DEM; stream lines (3DHP / NHD flowlines, optional); roads and railroads (optional) | Every fill that blocks a valley a stream line **or a draw traced from the DEM** crosses: **dam** (holds water), **dry embankment** (a dry / detention dam, a levee, an unmapped crossing), **road fill (ponding)** (a road that holds water: the culvert is missing in the DEM, small or blocked), **road fill**, **long fill (road, rail, canal or levee)** (no road / rail in the layers, but the raised fill runs on 600 m or more: a highway, rail grade, canal bank or levee the layers miss); a **dike that holds water beside a channel** (a dam built along a stream, not across it); where each pool gets out (**spillways**); **dug pits** anywhere; **raised ridges** (levees, dikes) on request; a **score** with a model from the score tool |
| `ndfind:ponds` Find ponds and dugouts | water-body polygons (3DHP / NHD waterbodies, NWI, your own), DEM; optionally the class-9 water-surface mosaic (`water_elev.vrt`) to set each pool level from the LiDAR water | **embankment pond**, **dugout** (natural depressions on request) - the pond engine of Screening tool 16 |
| `ndfind:crossings` Find road and rail crossings of streams | roads, railroads, stream lines; the first tool's fills, a DEM (optional) | Every crossing - the places a culvert or a bridge must be - with the fill there and, with a DEM, the drainage area and the round pipe the rational method asks for |
| `ndfind:water` Find open water in aerial imagery | 4-band or colour-infrared imagery: an ND GIS Hub preset (2018 30 cm 4-band; 2017, 2019, 2020 NAIP colour-infrared), your own GeoTIFF / VRT, or any ArcGIS ImageServer address; **or** a classified LiDAR tile / folder (.las/.laz, ASPRS class 9 = water), **or** a prebuilt class-9 water-count raster / VRT (the statewide LiDAR water mosaic); mapped water bodies, buildings / streets (both optional) | Open-water outlines (one year, or the water of most of several years), each marked **mapped** or not (cast shadows and, with a buildings / streets layer, roofs and pavement in town are left out): the pond outlines for the second tool when no layer has them |
| `ndfind:water_mosaic` Build a class-9 water mosaic from LiDAR tiles | a classified LiDAR tile or a folder of them (.las/.laz, ASPRS class 9); the water classes, their EPSG (only if the files carry none) and the cell size | `water_count.vrt` (class-9 points per cell) and `water_elev.vrt` (their mean elevation, the pond water surface), in North Dakota's UTM 14N (EPSG:26914): build once, then give the count VRT to the open-water tool as its **prebuilt class-9 water-count raster** |
| `ndfind:osm` Extract dams, dikes and culverts from OpenStreetMap | an OpenStreetMap file (.osm.pbf, the Geofabrik extract) | `waterway=dam / weir`, `man_made=dyke / embankment`, `tunnel=culvert`, `ford=yes` as points: one more known-structure layer |
| `ndfind:score` Score fills by how much they look like the listed dams | the `embankments` layers of one or more runs | A model learned from the fills already on a dam list (four kinds; reviewers' marks as trusted labels), and every fill scored 0-1 with it (look at the high-scoring new ones first) |
| `ndfind:evidence` Add evidence to fills | an `embankments` layer; imagery years, NWI, quarries, land cover, soils, the LiDAR return-count and class-9 mosaics, a DEM with buildings / roads (all optional) | Water year after year, bare ground, NWI h / x, LiDAR drop-out in the pool, buildings and roads down the flow path, the 12.5 ac-ft permit check |
| `ndfind:review_batch` Pick a review batch | scored fills; cards already reviewed (optional) | A batch for review: random control, top-ranked, near the cut, each card with its chance of being picked; a share flagged for a blind second look |

The first three take **Known structures** (any number of point, line or polygon layers: DWR dams, DWR
dugouts, Unknown Structures, NID dams, NBI bridges, NDDOT bridges / centre-line pipes, USGS stream
crossings, SARP / ABI crossings), a match distance (YOUR value; unsure up to twice it) and an
optional extent. A known point anywhere inside a pond's outline makes that pond listed.

DWR dam points are often at the quarter-quarter section, not on the embankment: on 10 southwest
ND LiDAR tiles the nearest fill to an intact DWR dam was a median 194 m (640 ft) away, 68 % within
300 m. With the DWR lists use about 1000 ft for fills (unsure up to twice it), and look at the
"unsure" ones.

## How a fill is found (`core/embankments.py`)

1. Each stream line is walked downstream over the DEM. A hump is where the ground rises more than
   0.15 m above the lowest ground already passed and falls back below it within `max_len`.
2. **Spill level**: the highest water level at which the ground just above the hump and the toe
   below it are still separate pools. Above it the water gets over or round the fill (a
   spillway, a saddle, the low end). This 2-D test is what drops a bank the line wanders onto:
   above and below join along the real channel at about the toe level.
3. **Height** = spill level - toe (the foot of the downstream face). **Head** = the upstream level
   - toe, less the fall the valley itself makes over that distance. A head of at least
   `min_head`, or a flat pool surface behind the fill (flat ground whose rim is all higher, as a
   hydro-flattened lake is), = the fill holds water.
4. **Storage** and pool area at the spill level (the LiDAR cannot see under water: a pool surface
   counts as ground, so storage is above the water surface only). A pool reaching the analysis
   window is marked `open_pool` (storage is a minimum).
5. A road / rail mask (the centrelines, 6 m either side) tells a road fill from a dam.
   A fill on no road layer whose **raised ground runs on** (`ridge_len_ft`: the cells standing
   more than max(0.5 m, 30 % of the crest height) above a 75 m grey opening of the ground, joined
   to the crest, measured along their long axis within 600 m) for `long_fill_m` (600 m) or more
   is a **long fill**: a farm dam ends on the valley sides. Added in 0.3.0 after a Wells County
   review (2026-11-12): the top-scored "dams" were canal banks and a divided highway missing from
   the road layers; every dam the reviewer confirmed was under 400 m.
6. **Spillway**: where the pool first gets out when the water rises a few centimetres above the
   spill level (the middle of the shortest way out through the cells it floods). On the fill at
   the line = it overtops there or has a notch / pipe there; beside it = an auxiliary spillway, a
   saddle or the end of the fill. Layer `spillways`, and `spillway`, `spill_dist_ft`,
   `spill_width_ft` on each fill.

**Draws with no stream line** (`DRAIN_AC`, 10 acres; 0 = off; `core/terrain.py`). The DEM is read
again at 10 m, the narrow raised features (fills, roads, embankments narrower than 60 m) are opened
away, and flow is routed by a priority flood (each cell drains to the cell it was reached from, so
a pit drains over its rim). Every draw that drains at least `DRAIN_AC` becomes a line and is walked
exactly like a stream line; `line_source` says which kind of line found the fill. The tile pad is
3 km so most of a draw's basin is in the tile.

**Dug pits** (`DUGOUTS`, on; `core/ridges.py`). A pit below the DEM's grey closing by 120 m, at
least 1 m deep, 150 m2 to 2 ha, filling at least 85 % of its smallest rectangle, no more than 8
times as long as wide, with spoil (the upper quarter of the ground within 8 m at least 0.3 m above
the ground 20-40 m out). Layer `dugouts` (depth below the rim / spoil tops, area, sides,
rectangularity, spoil height, volume), checked against the known structures.

**Raised ridges** (`RIDGES`, off; `core/ridges.py`). The DEM minus its grey opening by 60 m (what
stands up and is narrower than that), at least 1 m high, at least 80 m long, 5 times as long as
wide and straight (it fills at least 60 % of its smallest rectangle), off the road /
rail mask widened to an embankment's base and not one of the fills already found. **dike / levee
(holds water)** when a closed depression at least 0.3 m deep lies along it. A review list: farm
pads, drain spoil banks, shelterbelt snow berms and natural ridges pass too (in the Absaraka box
before the straightness and rail-base tests: 43 ridges, many rail beds and meander spurs).

**Score** (`MODEL`; `core/score.py`). A logistic regression of the fills on a dam list against the
other fills (height, crest height, net head, storage, pool area, length, flat pool, on a road, open
pool, spillway beside, length of the raised fill): a ranking of the new fills by how much they look like listed dams, not a
probability. DamSafety Finder ships `models/nd_fill_score_statewide_2026-11.json` (the default),
learned 2026-11-12 on 66 USGS 1 m tiles in 7 ND regions (Red River Valley, drift prairie north and
south, Coteau north, central south, northwest, southwest): 4,470 fills, 615 listed on a dam layer
within 300 ft (fills listed 300-1000 ft away were left out as unclear). Cross-validated AUC 0.82;
each region held out and scored by a model learned on the other six: Red River 0.83, drift north
0.71, drift south 0.83, Coteau north 0.79, central south 0.85, northwest 0.81, southwest 0.81 (the
first, southwest-only model held out the same way: 0.80, 0.69, 0.79, 0.68, 0.83, 0.80, 0.84).
Relearned for 0.3.0 with the raised-fill length on the same tiles (held out: Red River 0.84,
drift north 0.71, drift south 0.83, Coteau north 0.79, central south 0.85, northwest 0.82,
southwest 0.81) plus Wells County, with the 12 dams a reviewer confirmed from the 2018 aerial
counted as dams. Checked on those Wells marks before they were added: the long-fill kind is what
helps (5 of the 10 top-ranked dam / dry fills are real dams, 9 of the top 20, AUC 0.71, against 4,
5 and 0.64 with long fills left in); the length input itself adds little to the score. Most
not-listed fills are still not dams: review them.
Road fills not listed average 0.02, not-listed dams 0.20; 94 of 3,855 not-listed fills score 0.5
or more, all of them dams. Relearn it for your area with the score tool from runs over areas with many listed dams; the
cross-validated AUC it reports says how well it ranks dams it has not seen.

Fills closer than 60 m are one. The DEM is read in 5 km tiles with a pad, so a county runs in
bounded memory. The elevation unit is what the raster records (its band unit or a vertical CRS;
USGS 3DEP files, `USGS_*`, are metres); a DEM that records none is refused until you choose Metres
or Feet (feet read as metres made every fill 3.28 times too high). The log says which unit was
used and warns when the ground it reads is outside North Dakota's 229-1070 m. The warp reads the
full-resolution cells and keeps the values as stored (`core/warp_opts.py`, as Screening).

**What the output is.** A list to review, not a set of dams. In the review of 2026-11-11 none of
5 "new dam" candidates checked on aerial photos was a dam (farm and road-ditch fills, a rail
curve), and dikes beside a channel (lagoon and pond dikes) are not fills across it: turn on the
raised-ridge search for those. Sort by the score and check each candidate on imagery.

**Cancel** stops within the tile and the tool fails with "Cancelled" (the GeoPackage holds only
the part done). Every output GeoPackage has a `run_info` table: the tool, the Finder version,
GDAL, the time and every setting and input.

What it cannot find: a fill the DEM has been hydro-enforced through (a culvert cut through it), a
breached dam (correctly: it no longer blocks the valley), a pond with no polygon (draw it from imagery with the fourth tool).

## How open water is found (`core/water.py`)

NDWI = (green - near-infrared) / (green + near-infrared). A cell is water when NDWI is at least
`threshold` (0.15; YOUR value - check it on a known pond in the image you use) **and** its
near-infrared is at most `nir_frac` (0.25) of the land's median (open water is dark in NIR; a
metal roof, gravel, rail cars and shadows can have a positive NDWI but are not). The mask is
opened and closed by one cell, and a patch must be at least `min_ac` and somewhere at least
`min_width` (10 m) wide, which drops road and rail ditches. Each patch is marked `mapped` when at
least half of it lies on the mapped water-body layer. Give the `water` layer to *Find ponds and
dugouts* to ask the LiDAR whether each pool is held above the ground.

**From a classified LiDAR point cloud instead of imagery** (`core/lidar.py`): give a `.las`/`.laz`
tile, or a folder of them, in `LAS` / `LAS_DIR`. A cell is water when the cloud holds at least
`LAS_MIN_PTS` points of a water class (`LAS_CLASSES`, ASPRS class 9 by default) - no NDWI, sun
angle, shadow or imagery-year to guess at, because the classification is already made (USGS 3DEP
delivers classified LAZ). The points are read with laspy when it is installed (LAS and LAZ), else
a small pure-numpy reader here for uncompressed LAS (so the class filter and rasteriser are
unit-tested without laspy or GDAL); the reader launches no outside process. The same open /
min-size / min-width filters and output follow. Pass `LAS_EPSG` only when a tile carries no CRS of
its own.

**From a prebuilt class-9 water-count raster** (`WATER_RASTER`, band `WATER_BAND`): the statewide
LiDAR class-9 mosaic, rasterised once (point count per cell), is read as the water source - a cell
is water where its count is at least `WATER_COUNT_MIN`. This is the scalable path: reading the raw
LAS for a whole state is far too much, so you pre-rasterise it once and the tool reads the raster
(its nodata, and the -999999 LiDAR nodata, read as no water). Imagery, a raw-LAS folder and this
mosaic are three alternative sources for the one tool.

The mosaic itself is built by the **Build a class-9 water mosaic from LiDAR tiles**
(`ndfind:water_mosaic`) tool: point it at a folder of classified tiles and it writes, per tile, a
water-count GeoTIFF (class-9 points per cell) and a mean-elevation GeoTIFF (the pond water
surface), then a `water_count.vrt` / `water_elev.vrt` over them in the regional metric grid (North
Dakota's UTM 14N here; CONUS Albers in the U.S.-wide DamIT build). Every tile snaps to one global
grid so the VRT has no seams, and the builder reuses `core/lidar` so it launches no outside
process. Build the mosaic once, then feed `water_count.vrt` to the open-water tool above; the
mean-elevation mosaic is the pond starting level for a later *Find ponds* / Runner step.

An image service is downloaded for the extent only, in 2000-pixel GeoTIFF tiles kept beside the
output (`<output>_imagery/`; a rerun reuses them). The ND GIS Hub's 2021-2024 statewide NAIP is
natural colour only (no near-infrared band) and cannot be used. Imagery shows the day it was flown: a dry year misses ponds, a wet year adds flooded
fields. Pick several years: a cell is then water when it is water in at least `PERSIST` (half)
of the images that cover it, so the ponds stay and a field flooded in one wet year goes
(`wet_share`, `images` on each patch).

## Ponds: NWI codes and dugout shape

The ponds tool reads an NWI layer's wetland code (its `ATTRIBUTE` field, when no type field is
given): a code ending in **x** (excavated) is a dug pit for the pond engine, **h** (diked /
impounded) is noted when the LiDAR shows no embankment holding it (`nwi_modifier`). A natural
depression whose outline fills at least `DUG_RECT` (85 %) of its smallest rectangle (and is at
most 2 acres) is called a dugout: potholes are round or ragged, dug pits are rectangles
(`rectangularity`).

## What 0.5 adds to each fill (`core/embankments.py` steps 7-10)

New fields on every fill; they describe it, and only one of them drops a fill (one fill per pool):

* **The pool's outlet**: `outlet_dist_ft` (how far the spill point is from the raised fill; a dam
  holds its pool at the outlet), `spill_on_fill`, `same_pool` (other humps on the same pool that
  were dropped: spoil heaps round one dug pit, a road across a pond), `drain_ac` and `drain_ratio`
  (contributing area, and its ratio to storage: a "dam" with no drainage is a pit, a hilltop or a
  ridge nose).
* **Above original ground** (the NRCS 378 idea: an embankment pond holds 3 ft or more of water
  against its fill above the original ground; a dug pond holds none): `ag_head_ft` = spill level -
  the original ground put back under the fill, and `below_frac` = the share of the pool's volume
  below that ground (about 1 for a pit dug into level ground, about 0 for a pool behind a fill).
  This is reasoning from the definitions, not a published method.
* **Shape**: of the pool, `rect` (area / smallest rectangle), `elong`, `wall_slope` (dug pits
  are about 1.5:1) and `mounds` (spoil heaps round it); of the fill, `top_w_ft` and `side_sum`
  (upstream + downstream face, horizontal per vertical).
* **Terrain** round the crest: `relief_ft` and `slope_pct` (flat prairie, rolling breaks, cut
  badlands), for the terrain-aware score; `pool_lat` / `pool_lon` (the pool's middle, where the
  evidence tool looks for water).

Distances along the stream line are true metres whatever the line's direction, and the toe walk
stops at the foot of the downstream face on a steep draw (before 0.5.0 a steep draw could carry
the toe on and make a fill 2-3 times too high).

## Scores: four kinds, and reviewers' marks (`ndfind:score`)

`KIND`: **v1** (the 11 measurements of 0.4), **0.5** (every 0.5 measurement, one fill per listed
dam: the fill nearest each listed point is the dam, the rest of its bag is left out), **0.5
terrain** (also hinge terms and flat / rolling / cut copies of every weight) and **0.5 trees**
(bagged PU boosted trees on the 0.5 measurements). `MARKS`: a CSV of `find_id, mark` (yes / no /
unsure) from a review: each mark replaces that fill's own listed / not-listed label, at weight
`marks_weight` (an unsure one as half a dam, half not). Learn marks per region (a prairie model, a
breaks model): marks from one terrain can misrank another. Each fill gets `score` and
`score_note` (outside the training range, or a terrain group with few listed dams to learn from).

## Evidence the DEM cannot give (`ndfind:evidence`, `core/evidence.py`)

For each fill of an `embankments` layer, every source optional:

* **Water year after year** (`IMAGES`, one line per image: `YEAR | file or ImageServer | bands`):
  NDWI in the pool on each image, `img_years`, `wet_years`, `wet_share`, `first_wet`, `wet_list`.
  The default is every ND GIS Hub year with a near-infrared band (NAIP colour-infrared 2009-2020,
  the 2018 4-band 30 cm). **Check a service's band order** in its keyProperties: the GIS Hub
  colour-infrared years are NIR, green, blue (`nir=1 green=2`), not the classic NIR, red, green.
  Two images of one year count as one year, water in either. `bare_share`: bright bare ground
  (pits, quarries, fresh spoil) on a 4-band image.
* **NWI** (`nwi_code`, `nwi_h` impounded, `nwi_x` excavated, `nwi_regime`), a **quarry** layer
  (`in_quarry`, 50 m), the **pasture** share of a land-cover raster (CDL), a **soil** rating.
* **LiDAR drop-out** (`lidar_points`: the per-cell count of all returns that *Build a class-9 water
  mosaic* writes with `POINTS`; `lidar_water`: its class-9 count): water swallows the pulses, so a
  pond too small to be flattened or classed shows as empty cells. `lidar_pool_empty`,
  `lidar_ring_empty`, `lidar_contrast`, `lidar_class9`; blank where no survey covers the pool
  (a gap between tiles, the empty corner of a tile cut by a project edge or a VRT of
  per-project VRTs is not "empty": a cell counts as surveyed inside a tile and within reach of
  returns, so a pond void inside the survey still reads empty).
* **What is below** (with a DEM and buildings / roads): buildings and road crossings down the flow
  path within 1, 3 and 5 miles, and `conseq`, a second sort for the review. `permit_check` = yes
  when the storage is over 12.5 acre-feet (an ND water permit), for every fill with a storage.

## Review batches and honest numbers (`ndfind:review_batch`, `core/review.py`, `core/evaluate.py`)

A review batch is the Finder's experiment: the marks are the only ground truth. A batch samples
the cards not yet reviewed (give it the earlier marks file as `REVIEWED`). It holds a **random
control** (about 18 %: the unreviewed cards with the smallest hash of the card id for the `SEED`,
so the control stays put from one model version to the next, and a later round with the same
seed takes the next-smallest hashes), the **top-ranked** unreviewed cards (about 37 %) and cards
**near the cut** (ranks k to 3k, at most `CAP` from one 5 km tile), and flags about 12 % for a
blind second reviewer. Every card carries `incl_prob`, the chance it had of being picked from the
unreviewed pool (`pool_n` cards; the tile cap counted), so `evaluate.ht_share` turns a batch's
marks into the share of real dams among the unreviewed cards with an honest interval (a control
with no dams still gives an upper bound, not 0 +/- 0). For the whole list, add the cards marked in
earlier rounds at `incl_prob` 1: they are a census of what has been looked at.
A review sheet (an aerial and a hillshade chip per card; yes / no / unsure / offset, where offset is
a real dam just off the point) gives the marks the score tool's `MARKS` takes, and
`core/dataset.py` files every reviewed card with its chips (a training set for an image model once
there are about 100 confirmed dams; `stats()` says when).

## Culvert size at a crossing (`core/crossings.py`)

With a DEM the crossings tool reads the drainage area above each crossing (the same priority-flood
routing at 10 m, the crossing snapped to the largest flow within 30 m) and its longest flow path,
then: Kirpich time of concentration, rational-method peak Q = C i A for YOUR runoff coefficient C
(0.35) and rainfall intensity i (2 in/hr: take it from NOAA Atlas 14 at the time of concentration
and your return period), and the round pipe that carries Q flowing full at YOUR velocity (8 ft/s;
`pipe_in`, `pipe_size`, or "box culvert / bridge"). An order of magnitude for screening: the
rational method is meant for areas up to about 200 acres (flagged above that; use a regional
regression, USGS StreamStats), and a basin longer than the DEM read is a minimum. Not a design.

## Outputs

One GeoPackage per tool run (EPSG:5070 points or polygons, lat / lon fields) and a CSV beside it:

* `embankments` / `ponds` / `crossings`: every candidate, new first, with `status`, `known_id`,
  `known_layer`, `known_ft`, heights in feet, storage in acre-feet, areas in acres, `confidence`
  and `notes`.
* `screening_dams` / `screening_ponds`: the not-listed candidates that could release water
  (dams, dry embankments, levees; never a road fill, which Screening cuts to open its culvert), as
  DWR-export dam records (`structure_id` = FIND-… / POND-…, `dam_name`, `max_dam_height`,
  `max_pool_vol`, `max_pool_area`, `status` = Candidate). DamSafety Screening tool 1 reads them
  (set its status filter to Candidate; their IDs become DWR-FIND-… / DWR-POND-…).
* `spillways`, `dugouts`, `ridges`: points, as above; `osm_structures` from the OpenStreetMap
  tool; `scored_fills` and `<output>_score_model.json` from the score tool.
* `water`: open-water polygons (`water_id` WATER-…, `area_ac`, mean `ndwi`, `mapped`,
  `mapped_share`, lat / lon) and `<output>_water.csv`.
* `<output>_hillshade.tif`: a hillshade lit from all round (`gdaldem hillshade -multidirectional`)
  for the review. A fill parallel to a single sun direction does not show; this shows every
  embankment and pond rim.

## Files

`core/embankments.py`, `core/listed.py`, `core/crossings.py`, `core/water.py`, `core/terrain.py`, `core/ridges.py`, `core/score.py`, `core/challenger.py` (boosted trees), `core/evidence.py`, `core/review.py`, `core/evaluate.py` (AUC with DeLong intervals, Wilson, Horvitz-Thompson), `core/dataset.py`, `core/ponds.py`,
`core/storage.py`, `core/warp_opts.py` (byte-identical to Screening's, checked by
`tools/ci_checks.py`), `finder.py` (the runs on files: GDAL / OGR, no QGIS), `plugin.py`
(the Processing tools), `brand.py` (what differs from DamIT Finder in the damit repository).

Tests: `python -m unittest DamSafety_Finder.tests.test_core DamSafety_Finder.tests.test_finder
DamSafety_Finder.tests.test_qgis` (synthetic terrain and imagery, a mocked image service; the QGIS tests skip without QGIS).
