#!/usr/bin/env python3
"""run_finder_statewide.py 1.2 - the DamSafety Finder's "fills across streams and draws" over all of
North Dakota, from the statewide 1 m terrain VRT, tile by tile, resumable.

    bash run_finder_statewide.sh --dry-run        # what would run, how long, how much disk; writes nothing
    bash run_finder_statewide.sh --limit 4        # a trial: the first 4 tiles, then the merge
    bash run_finder_statewide.sh                  # the whole state (run it again to go on after a stop)
    bash run_finder_statewide.sh --merge-only     # merge what is finished so far (the terrain drive is not needed)

It reads the terrain VRT and the ND data pack and writes ONLY into --out (default ~/ND_Finder_Statewide).
It never writes, moves or deletes anything in the suite data folder, the pack, the terrain folder or
the plugins folder, and it downloads nothing. It needs only what the suite's QGIS Python has
(GDAL / OGR, numpy, scipy) and the DamSafety Finder plugin (0.6.0 or later).

HOW THE STATE IS TILED, AND WHY (from what finder.run_embankments does)

  * run_embankments(bbox=...) works in 5 km squares. Finder 0.7.0 takes the squares of a fixed
    lattice in EPSG:5070 (corners at whole multiples of 5 km; finder.lattice_squares) that its box
    overlaps, every one whole; up to 0.6.x it cut the box into squares starting at the box's
    south-west corner. For each square it reads the DEM over the square plus a 3 km pad on every
    side (the pad is 3 km whenever the DEM draws are on, drainage_ac > 0; 0.7.0 grows that window
    outward to whole multiples of 30 m, so by under 30 m), finds the fills, and keeps a fill only
    when its crest point lies in the square itself (west and south edges in, east and north edges
    out). Then it applies two rules to everything it kept: one fill per pool (fills whose pools
    get out at the same spill point) and no two fills within 60 m of each other; 0.7.0 then
    leaves out what lies outside the box.
  * So this script cuts the state into squares of --tile-km (10 km by default, always a multiple of
    5 km) on a fixed lattice in EPSG:5070 (the engine's working CRS): tile corners are whole
    multiples of the tile size. Tiles touch and do not overlap. A 10 km tile is then exactly four
    of the engine's 5 km squares, with either Finder: the box of a tile starts and ends on the
    lattice, so 0.7.0 takes those four squares and no other, and 0.6.x, starting at the tile's
    corner, lands on the same four. Every square is the same square, with the same DEM window,
    that ONE run_embankments call over the whole state would use, so a fill is found the same way
    whichever tile holds it, and because a crest point lies in exactly one square, no tile drops
    a fill and no two tiles keep the same one. The 3 km of terrain a tile needs beyond its edge
    comes from the VRT, not from a neighbour's run (the terrain window of a tile is the tile +
    3 km + 600 m, which covers the 30 m). A tile the State outline only clips is run whole, as
    any other: the engine knows no outline, and its fills beyond it get in_state = no.
  * What a single statewide call does after its squares - the two rules above, across squares - is
    done here in the merge, across tile seams, by calling the engine's own one_per_pool and dedupe
    on the fills that lie close to a fill of another tile (inside a tile the engine has done it
    between its four squares). Two cases can come out otherwise than in one statewide call;
    neither was met in the test ground (no fill at all was removed across a seam there):
      - the rules read the values the tiles wrote, which are rounded to 0.1 ft, so an exact tie
        can fall the other way (the fill is the same embankment);
      - a chain of three fills, each within 60 m of the next (or on one pool), that crosses a
        seam: every tile has already applied the rules to its own fills, and the merge applies
        them again to what the tiles kept. A fill a tile dropped in favour of its neighbour does
        not come back when that neighbour is then dropped in favour of a fill across the seam;
        one statewide call, looking at all three at once, could have kept it. So at such a chain
        the merge can keep one fill fewer. It never keeps two fills within 60 m of each other.
  * Stream lines. Finder 0.7.0 reads, for a box, every line that reaches the window of a square
    the box touches; a tile's extract (below) holds all of those, so a tile reads the lines one
    statewide call would. Up to 0.6.x the engine read only the lines that meet the box itself,
    and one difference from a single call was left: a line that does not enter the tile has no
    crest point in it, so the fills are not affected; only the side layer of channel-side dikes
    ('ridges') could miss a dike within 200 m of a tile edge whose only nearby channel is a
    mapped stream outside the tile that the DEM draws do not also trace.
  * To keep every tile small and quick, the tile's process reads its streams, roads, rails and
    known structures from small extracts cut from the pack into --out (features that reach within
    4 km of the tile, in the pack's own order, with their ids), and the terrain through a window VRT
    of the big VRT (the tile plus 3.6 km). Both hold exactly what the engine would have read.

WHICH FINDER (0.6.0 or later; the script looks at what the Finder it finds offers)

  * find_id: up to 0.6.x the crest point of a fill - and the id, a code made from it - moved by a
    few metres with the corner of the run's box, so the ids of two runs over different boxes did
    not match. With 0.7.0 the squares and the DEM cells lie on a fixed lattice, and a fill has
    the same id in every run of that Finder version over the same terrain and settings, whatever
    the box. An id does not carry over from one Finder version to another: marks made on a run
    of another version are matched to the fills of a new run by location (lat / lon), not by
    id. The summary says which Finder version the run used and which of the two holds.
  * One merged result holds the fills of ONE Finder version: the version is part of the settings
    a tile is known by (done.json), tiles made by another version are kept but neither run again
    nor merged (--redo runs them again), and the merge refuses a mix.
  * The edge of the terrain data: 0.7.0 handles it itself and this script follows it; for 0.6.x
    the script has its own, cruder test. See the next part.

THINGS DONE HERE BECAUSE A FINDER RUN ON ONE BOX CANNOT

  * Fills outside North Dakota (the terrain covers a rim of the neighbouring states): every fill
    gets in_state = yes / no, and tiles wholly outside the state are not run. The outline is, in
    this order: --state-outline FILE (any polygon file), the pack's county boundaries
    (boundaries/ND_counties.gpkg) when the pack has them, else a simple built-in outline (see
    BUILTIN_SOUTH and BUILTIN_EAST below). "--state-outline none" switches the test off.
  * Fills at the edge of the terrain data. Where the terrain stops, the missing cells act as a
    wall and a spur beside them reads as a dam. at_data_edge = yes marks such a fill; it stays
    in the raw layer and is in none of the ranked lists.
      - Finder 0.7.0 decides it itself (core/embankments.py step 13): a fill within 150 m of
        missing terrain WHOSE POOL REACHES the missing terrain gets the kind "at the edge of the
        terrain data" and no score. Distance alone is not the test: a pond that near the edge
        whose water stays clear of it is read correctly and keeps its kind (the engine's
        builder tried distance alone first; on an Iowa tile it set aside 6 real dams of 10
        fills). Here at_data_edge is yes exactly for the fills of that kind, such a fill is
        given no score in the merge either, and edge_ft is the Finder's own figure (crest to
        the nearest missing terrain, looked for to 1,000 m on the run's 3 m cells, holes under
        100 m2 not counted; blank = none that near). --edge-m is not used.
      - Finder 0.6.x has no such test, so for its tiles this script measures edge_ft itself
        (to the nearest missing terrain within 200 m, read from the DEM at full resolution;
        holes smaller than 100 m2 are ignored) and at_data_edge = yes when that is within
        --edge-m (150 m): distance alone, so it also sets aside real ponds beside the edge.
  * Ground where a fill is not a dam whatever its shape (of 90 pond-line candidates marked by
    hand on 2026-11-13, 8 were coal-mine spoil and 13 lay in one tile of the Sheyenne sand hills,
    none of those a dam): the script holds no map of such ground. The hook for one is
    --exclude-polygons FILE (any polygon file): a fill inside a polygon keeps its row in the raw
    layer with excluded = yes and is left out of the ranked lists. Without the option every fill
    has excluded = no. (For the sand hills see also the next part.)

CROWDED GROUND: A NOTE, NOT A TEST (script 1.2)

  * Of 90 pond-line candidates marked by hand on 2026-11-13, the 13 in one tile of the Sheyenne
    sand hills (USGS tile x63y516, zone 14) were dune hollows, not dams. What set that ground
    apart was not any one fill but how many there were: many small fills on pond lines close
    together. So every fill gets
      near_n    how many OTHER fills within 1,000 m (crest point to crest point) are on a pond
                line (line_source "DEM pool outlet") and of kind dam or dry embankment;
      near_km2  that count per km2 of the part of the 1 km circle the run searched (cut to one
                decimal, not rounded: 7.96 is written 7.9, so a figure of 8.0 is 8 or more);
      crowded   yes when the fill is itself a pond-line dam or dry embankment and near_km2 is
                --crowd-km2 (8) or more; else no. --crowd-km2 0 switches the note off (the two
                numbers are still written). 8 per km2 is 26 or more others in a whole circle.
    It is a note for the person who looks at the list. It changes no score, leaves no row out
    and does not change the order of the ranked lists. The summary counts the crowded candidates.
  * Why here and not in the Finder: the count needs a fill's neighbours, and a Finder run on one
    box does not know what lies outside its box - inside the engine the number would depend on
    the box. Here every finished tile is merged before the lists are written.
  * The ground the run searched is the squares of the finished tiles, cut to the State outline
    (the one in_state uses), and the fills counted are those on that ground. So a fill beside a
    tile that is not finished yet, or at the State line, is not read as standing in thin ground
    because half its circle was never searched: the count is divided by the searched part
    only. That part is worked out exactly (the circle as a 64-sided ring of the same area, cut
    with the tiles and the outline); where it is under a quarter of the circle the density is
    worked out over a quarter, so that one or two neighbours in a sliver of ground cannot read as
    a crowd. What is NOT known here is where a finished tile inside the outline has no terrain
    (the rim of the hole over Fort Berthold): there the searched part is counted too big and
    the density reads low, so a note can be missed there, never added.
  * The fields are made in the merge from what the tiles hold: a tile finished by script 1.0 or
    1.1 needs no new run, --merge-only is enough, and the numbers of a partial result change as
    more tiles finish (more of each circle has then been searched).
  * Where it was seen to work, and where not (Finder 0.6.0 runs of 66 ND test tiles and 15 Iowa
    tiles, each tile fed through crowd_notes as a run of its own, 2026-11-14): at 8 per km2 it
    marks 10 of those 13 sand-hill cards and none of the 24 cards marked as real dams (the most
    crowded of them has 6.6 per km2); none of the fills at 523 listed Intact dams (2 of them
    have the density, on drainage lines, so they are not marked) and 7 of the fills at 929 Iowa
    census dams (30 have the density). But of all the fills it marks in the 66 ND tiles, 693 of
    929 are in that ONE sand-hill tile: outside it the note is unproven (it marks 7 of the other 52 false
    cards - pothole ground, old river bends). It does not catch mine spoil (1 of 8 cards): for
    mined ground use --exclude-polygons with an outline of the mine.

WHAT IT WRITES (all in --out)

  tiles/<tile>/                    one folder per finished tile: the engine's GeoPackage and CSVs,
                                   the tile's log, and done.json (the marker; a tile without it is
                                   not finished and runs again)
  ND_finder_fills.gpkg             every fill ('embankments', scored), the ranked candidates
                                   ('candidates'), the spill points, the channel-side dikes, run_info
  ND_finder_candidates_ranked.csv  the unlisted dam / dry-embankment candidates, highest score first
                                   (every one: about 300,000 rows for the state)
  ND_finder_candidates_top.csv     the short list a person can open: the top 2,000 of them (--top N)
                                   with lat, lon, score, kind, height, storage, tile, a link to
                                   the satellite picture, and the crowded-ground note
  ND_finder_summary.txt            counts (also: how many candidates score 0.5 or more), the Finder
                                   version, tiles run / skipped / failed, settings, checksums
  ND_finder_review_batch.gpkg/.csv only with --review-batch N: a review batch drawn from the candidates
  logs/run_<time>.log              one log per run; failed/<tile>.log for a tile that failed (it goes
                                   when the tile finishes in a later try, so what is there still fails)

Python 3.9 or later. No QGIS imports.
"""
import argparse
import array
import collections
import csv
import datetime
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
import types
import warnings
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True          # never leave .pyc files in the plugins folder
warnings.filterwarnings("ignore", category=FutureWarning)   # GDAL's note about its exception mode (not switched)

SCRIPT_VERSION = "1.2"
DEFAULT_DEM = "/Volumes/Extreme SSD/ND_DEM_1m/ND_1m_26914.vrt"
DEFAULT_OUT = os.path.join("~", "ND_Finder_Statewide")
ENGINE = "DamSafety_Finder"
MODEL_NAME = "nd_fill_score_trees_2026-11.json"
WORK_EPSG = 5070
ENGINE_TILE_M = 5000.0                  # run_embankments tile_m (its default; not changed here)
ENGINE_PAD_M = 3000.0                   # the pad run_embankments reads round each 5 km square
WINDOW_MARGIN_M = 600.0                 # the terrain window is the tile + pad + this
EXTRACT_M = 4000.0                      # the vector extracts reach this far beyond the tile
# The call the cloud test runs used (train6/run.py): Finder 0.6.0 defaults except these.
CALL = {"cell": 3.0, "z_feet": False, "match_ft": 1000.0, "drainage_ac": 10.0, "hillshade": False}
STREAMS = "hydro/3DHP_flowlines.gpkg"
ROADS = "roads/ND_roads_NG911.gpkg"
RAILS = "railroads/NARN_rail_lines.gpkg"
KNOWN = (("dwr/DWR_dams.gpkg", "DWR dams"), ("dwr/DWR_dam_lines.gpkg", "DWR dam lines"),
         ("dwr/DWR_dugouts.gpkg", "DWR dugouts"), ("dwr/DWR_unknown_structures.gpkg", "DWR unknown"),
         ("hydro/NID_dams.gpkg", "NID dams"), ("barriers/SARP_dams.gpkg", "SARP dams"))
COUNTIES = "boundaries/ND_counties.gpkg"
PACK_NAMES = ("ND_Dam_Suite_Pack",)
# The script's own edge test, for the tiles of a Finder that has none (0.6.x). Finder 0.7.0 decides
# it itself (the kind em.K_EDGE) and writes its own edge_ft; these two are then not used.
EDGE_SEARCH_M = 200.0                   # missing terrain is looked for this far from a fill
EDGE_SPECK_M2 = 100.0                   # a hole in the terrain smaller than this is not an edge
EDGE_M_DEFAULT = 150.0                  # --edge-m
# What the Finder writes as score_note for a fill of its edge kind, which it does not score
# (finder.run_embankments and finder.run_score, 0.7.0); the merge writes the same words.
EDGE_NOT_SCORED = "not scored: at the edge of the terrain data"
FT = 0.3048
OUT_GPKG = "ND_finder_fills.gpkg"
OUT_CSV = "ND_finder_candidates_ranked.csv"
OUT_TOP = "ND_finder_candidates_top.csv"
OUT_SUMMARY = "ND_finder_summary.txt"
# The fields this script adds to the engine's. One the engine already writes is not added twice
# (edge_ft: Finder 0.7.0 has it among its own fields, in feet, and its place there is kept).
# The three of the crowded-ground note (script 1.2) come last: nothing before them moved.
CROWD_FIELDS = [("near_n", "int"), ("near_km2", "float"), ("crowded", "str")]
EXTRA_FIELDS = [("tile", "str"), ("edge_ft", "float"), ("at_data_edge", "str"), ("in_state", "str"),
                ("excluded", "str"), ("id_clash", "str")] + CROWD_FIELDS
TOP_DEFAULT = 2000                      # rows of the short list (--top)
TOP_COLUMNS = ["rank", "score", "kind", "status", "height_ft", "storage_af", "lat", "lon", "tile", "find_id",
               "link"] + [name for name, _t in CROWD_FIELDS]
CROWD_RADIUS_M = 1000.0                 # crowded ground: the other pond fills are counted this far from a fill
CROWD_KM2_DEFAULT = 8.0                 # --crowd-km2: so many of them per km2, or more, is "crowded"
CROWD_MIN_SHARE = 0.25                  # the density is never worked out over less of the circle than this
CROWD_BLOCK = 10                        # the outline is cut to blocks of 10 x 10 tiles, then to the tiles
SCORE_CUT = 0.5                         # the summary counts the candidates at or above this score
# Measured 2026-11-13 on the 4-core cloud test box (see RUN-FINDER-STATEWIDE.md): processor time
# of a 10 km tile full of terrain (9 tiles over 300 km2 of terrain took 780 CPU-seconds), the peak
# memory of one tile process, and the size of the results. The Mac has not been timed; a run times
# its own tiles, and the dry run uses those once EST_MIN_FULL tiles FULL of terrain are finished in
# --out. A tile at the edge of the terrain is much quicker (measured: 8 to 35 s for a tile with
# under 5 % terrain, 155 to 250 s for a full one), so every estimate counts a tile by its share
# of terrain (tile_cost) and never takes the plain mean of the finished tiles.
EST_SEC_PER_TILE = 270.0                # per 100 km2 of terrain, one core
EST_TILE_START_SEC = 20.0               # what a tile costs before any terrain: the process, the extracts
FULL_SHARE = 0.85                       # a tile is "full of terrain" from this share of its square up
EST_MIN_FULL = 3                        # so many finished full tiles make this computer's own figure
EST_PEAK_GB = 1.3                       # one tile process (measured 1.2 GB)
EST_TILE_MB = 0.105                     # a tile folder with no fills
EST_TILE_KB_PER_FILL = 1.8              # ... plus this per fill
EST_MERGED_KB_PER_FILL = 2.0            # the merged GeoPackage and the ranked CSV
EST_FILLS_PER_KM2 = 2.5                 # 14,400 fills on 66 test tiles of 88 km2
GDAL_CACHE_MB = "512"                   # per tile process (GDAL's own default is 5 % of the RAM)

# The built-in fallback outline of North Dakota (lon, lat; NAD83 / WGS84). West and north are
# straight lines just outside the surveyed borders (104.0505 W, 49.0006 N). The south border is not
# one parallel: as surveyed it runs from about 45.9452 N at the Montana corner down to about
# 45.9351 N east of 98 W (the southernmost roads of the pack's ND_roads_NG911, by quarter degree
# of longitude), so the south side is the line BUILTIN_SOUTH, about 50 m south of the southernmost
# North Dakota road of each quarter degree (one straight line at 45.9350 N kept a strip of South
# Dakota 1.1 km deep in the south-west). The east side is the Bois de Sioux River and the Red
# River of the North, taken from the pack's own 3DHP flowlines (USGS, public domain) and
# simplified to about 400 m. The outline is used with a 600 m allowance (--state-buffer-m), so it
# never drops a fill that is in North Dakota; within about 1 km of the border it can keep one that
# is not. Give --state-outline for an exact test.
BUILTIN_W, BUILTIN_N = -104.0505, 49.0006
BUILTIN_SOUTH = (
    (-104.0505, 45.9447), (-103.75, 45.9447), (-103.50, 45.9445), (-103.25, 45.9443), (-103.00, 45.9443),
    (-102.75, 45.9445), (-102.50, 45.9443), (-102.25, 45.9439), (-102.00, 45.9435), (-101.75, 45.9433),
    (-101.50, 45.9433), (-101.25, 45.9433), (-101.00, 45.9431), (-100.75, 45.9413), (-100.50, 45.9413),
    (-100.25, 45.9413), (-100.00, 45.9403), (-99.75, 45.9399), (-99.50, 45.9392), (-99.25, 45.9377),
    (-99.00, 45.9377), (-98.75, 45.9365), (-98.50, 45.9356), (-98.25, 45.9353), (-98.00, 45.9350),
    (-97.75, 45.9346), (-97.50, 45.9345), (-97.25, 45.9345), (-97.00, 45.9347), (-96.75, 45.9347),
    (-96.5612, 45.9347),
)
BUILTIN_BUFFER_M = 600.0
BUILTIN_EAST = (
    (-96.5612, 45.9415), (-96.5708, 45.9641), (-96.5779, 46.0273), (-96.5591, 46.0585), (-96.5544, 46.0841),
    (-96.5632, 46.1087), (-96.5602, 46.1164), (-96.5671, 46.1200), (-96.5803, 46.1511), (-96.5767, 46.1744),
    (-96.5892, 46.1814), (-96.5905, 46.1905), (-96.5851, 46.1945), (-96.5912, 46.1957), (-96.5846, 46.1975),
    (-96.5890, 46.2049), (-96.5838, 46.2082), (-96.5911, 46.2087), (-96.5987, 46.2423), (-96.5837, 46.2498),
    (-96.5923, 46.2503), (-96.6004, 46.2618), (-96.5950, 46.2743), (-96.6003, 46.2811), (-96.5948, 46.2860),
    (-96.6018, 46.2860), (-96.5964, 46.2912), (-96.6057, 46.2937), (-96.5973, 46.2976), (-96.6036, 46.3025),
    (-96.5968, 46.3171), (-96.6000, 46.3268), (-96.6061, 46.3266), (-96.5994, 46.3300), (-96.6202, 46.3402),
    (-96.6167, 46.3474), (-96.6294, 46.3543), (-96.6452, 46.3507), (-96.6484, 46.3581), (-96.6432, 46.3619),
    (-96.6671, 46.3756), (-96.6677, 46.3912), (-96.6751, 46.3910), (-96.6805, 46.4071), (-96.6891, 46.4068),
    (-96.6854, 46.4142), (-96.7015, 46.4151), (-96.6922, 46.4161), (-96.6933, 46.4221), (-96.7046, 46.4180),
    (-96.6989, 46.4264), (-96.7077, 46.4298), (-96.7032, 46.4373), (-96.7154, 46.4340), (-96.7219, 46.4398),
    (-96.7123, 46.4413), (-96.7218, 46.4488), (-96.7131, 46.4502), (-96.7176, 46.4544), (-96.7116, 46.4598),
    (-96.7187, 46.4619), (-96.7132, 46.4665), (-96.7231, 46.4667), (-96.7146, 46.4723), (-96.7389, 46.4787),
    (-96.7325, 46.4829), (-96.7397, 46.4867), (-96.7334, 46.4970), (-96.7424, 46.4999), (-96.7335, 46.5035),
    (-96.7404, 46.5111), (-96.7342, 46.5121), (-96.7452, 46.5202), (-96.7367, 46.5241), (-96.7453, 46.5250),
    (-96.7462, 46.5349), (-96.7380, 46.5346), (-96.7427, 46.5381), (-96.7383, 46.5393), (-96.7501, 46.5408),
    (-96.7387, 46.5432), (-96.7505, 46.5441), (-96.7406, 46.5468), (-96.7492, 46.5496), (-96.7460, 46.5561),
    (-96.7544, 46.5568), (-96.7417, 46.5640), (-96.7528, 46.5714), (-96.7466, 46.5755), (-96.7575, 46.5778),
    (-96.7473, 46.5820), (-96.7609, 46.5818), (-96.7508, 46.5852), (-96.7626, 46.5893), (-96.7560, 46.5954),
    (-96.7716, 46.5962), (-96.7682, 46.6063), (-96.7788, 46.6075), (-96.7708, 46.6153), (-96.7812, 46.6154),
    (-96.7755, 46.6212), (-96.7873, 46.6194), (-96.7783, 46.6254), (-96.7913, 46.6250), (-96.7822, 46.6314),
    (-96.7982, 46.6296), (-96.7857, 46.6406), (-96.7992, 46.6543), (-96.7955, 46.6664), (-96.8021, 46.6678),
    (-96.7914, 46.6724), (-96.7989, 46.6785), (-96.7874, 46.6771), (-96.7947, 46.6833), (-96.7839, 46.6857),
    (-96.7906, 46.6894), (-96.7841, 46.7014), (-96.7912, 46.7035), (-96.7839, 46.7059), (-96.7894, 46.7095),
    (-96.7821, 46.7170), (-96.7912, 46.7185), (-96.7791, 46.7268), (-96.7845, 46.7319), (-96.7804, 46.7418),
    (-96.7874, 46.7432), (-96.7804, 46.7456), (-96.7893, 46.7520), (-96.7827, 46.7541), (-96.7897, 46.7567),
    (-96.7778, 46.7617), (-96.7892, 46.7656), (-96.7754, 46.7666), (-96.7889, 46.7708), (-96.7839, 46.7782),
    (-96.7971, 46.7765), (-96.7871, 46.7855), (-96.8018, 46.7967), (-96.7956, 46.8014), (-96.8031, 46.8051),
    (-96.7957, 46.8074), (-96.8026, 46.8115), (-96.7922, 46.8274), (-96.7803, 46.8252), (-96.7940, 46.8333),
    (-96.7844, 46.8335), (-96.7869, 46.8411), (-96.7748, 46.8393), (-96.7817, 46.8460), (-96.7734, 46.8515),
    (-96.7855, 46.8543), (-96.7760, 46.8545), (-96.7840, 46.8620), (-96.7759, 46.8743), (-96.7812, 46.8796),
    (-96.7689, 46.8785), (-96.7674, 46.8843), (-96.7745, 46.8887), (-96.7694, 46.8929), (-96.7763, 46.8969),
    (-96.7597, 46.9166), (-96.7545, 46.9145), (-96.7629, 46.9234), (-96.7528, 46.9240), (-96.7651, 46.9269),
    (-96.7563, 46.9287), (-96.7625, 46.9348), (-96.7769, 46.9257), (-96.7912, 46.9276), (-96.7915, 46.9455),
    (-96.8040, 46.9469), (-96.7988, 46.9515), (-96.8040, 46.9576), (-96.7952, 46.9659), (-96.8103, 46.9639),
    (-96.8228, 46.9703), (-96.8201, 46.9798), (-96.8263, 46.9881), (-96.8210, 47.0033), (-96.8393, 47.0070),
    (-96.8288, 47.0220), (-96.8149, 47.0240), (-96.8272, 47.0317), (-96.8124, 47.0387), (-96.8226, 47.0369),
    (-96.8176, 47.0471), (-96.8275, 47.0464), (-96.8176, 47.0522), (-96.8303, 47.0606), (-96.8197, 47.0617),
    (-96.8244, 47.0717), (-96.8196, 47.0753), (-96.8293, 47.0743), (-96.8159, 47.0791), (-96.8206, 47.0834),
    (-96.8155, 47.0880), (-96.8271, 47.0890), (-96.8172, 47.0924), (-96.8210, 47.1005), (-96.8162, 47.1085),
    (-96.8375, 47.1224), (-96.8258, 47.1232), (-96.8205, 47.1304), (-96.8305, 47.1293), (-96.8264, 47.1349),
    (-96.8346, 47.1439), (-96.8286, 47.1464), (-96.8412, 47.1519), (-96.8224, 47.1563), (-96.8320, 47.1594),
    (-96.8193, 47.1638), (-96.8251, 47.1673), (-96.8225, 47.1731), (-96.8335, 47.1766), (-96.8216, 47.1843),
    (-96.8346, 47.1837), (-96.8264, 47.1915), (-96.8448, 47.1936), (-96.8338, 47.2036), (-96.8272, 47.2012),
    (-96.8371, 47.2072), (-96.8303, 47.2103), (-96.8372, 47.2159), (-96.8340, 47.2219), (-96.8456, 47.2193),
    (-96.8315, 47.2267), (-96.8415, 47.2332), (-96.8287, 47.2333), (-96.8365, 47.2424), (-96.8437, 47.2396),
    (-96.8322, 47.2474), (-96.8444, 47.2511), (-96.8385, 47.2563), (-96.8453, 47.2585), (-96.8360, 47.2625),
    (-96.8474, 47.2626), (-96.8346, 47.2672), (-96.8477, 47.2711), (-96.8401, 47.2756), (-96.8438, 47.2931),
    (-96.8306, 47.3057), (-96.8365, 47.3115), (-96.8430, 47.3088), (-96.8469, 47.3163), (-96.8288, 47.3280),
    (-96.8458, 47.3280), (-96.8306, 47.3388), (-96.8471, 47.3477), (-96.8374, 47.3555), (-96.8505, 47.3571),
    (-96.8484, 47.3646), (-96.8582, 47.3675), (-96.8371, 47.3713), (-96.8546, 47.3738), (-96.8437, 47.3758),
    (-96.8500, 47.3816), (-96.8392, 47.3812), (-96.8375, 47.3895), (-96.8489, 47.3936), (-96.8391, 47.3994),
    (-96.8527, 47.4061), (-96.8421, 47.4117), (-96.8568, 47.4097), (-96.8610, 47.4177), (-96.8718, 47.4190),
    (-96.8553, 47.4243), (-96.8647, 47.4281), (-96.8528, 47.4320), (-96.8629, 47.4364), (-96.8559, 47.4392),
    (-96.8593, 47.4517), (-96.8543, 47.4545), (-96.8636, 47.4570), (-96.8539, 47.4604), (-96.8610, 47.4726),
    (-96.8519, 47.4791), (-96.8685, 47.4785), (-96.8522, 47.4864), (-96.8650, 47.4917), (-96.8488, 47.4981),
    (-96.8525, 47.5075), (-96.8423, 47.5086), (-96.8536, 47.5115), (-96.8424, 47.5161), (-96.8609, 47.5155),
    (-96.8712, 47.5272), (-96.8535, 47.5362), (-96.8623, 47.5412), (-96.8457, 47.5384), (-96.8604, 47.5459),
    (-96.8498, 47.5532), (-96.8617, 47.5593), (-96.8475, 47.5662), (-96.8631, 47.5625), (-96.8689, 47.5670),
    (-96.8516, 47.5687), (-96.8605, 47.5750), (-96.8499, 47.5908), (-96.8589, 47.5957), (-96.8510, 47.5985),
    (-96.8570, 47.6107), (-96.8797, 47.6193), (-96.8759, 47.6234), (-96.8805, 47.6260), (-96.8744, 47.6278),
    (-96.8882, 47.6389), (-96.8824, 47.6427), (-96.8899, 47.6697), (-96.8852, 47.6739), (-96.8962, 47.6742),
    (-96.8993, 47.6896), (-96.9095, 47.6892), (-96.9120, 47.7015), (-96.9209, 47.7000), (-96.9119, 47.7068),
    (-96.9199, 47.7104), (-96.9198, 47.7169), (-96.9310, 47.7155), (-96.9184, 47.7240), (-96.9328, 47.7375),
    (-96.9292, 47.7504), (-96.9397, 47.7548), (-96.9289, 47.7553), (-96.9383, 47.7608), (-96.9349, 47.7672),
    (-96.9563, 47.7751), (-96.9524, 47.7794), (-96.9591, 47.7806), (-96.9580, 47.7857), (-96.9651, 47.7835),
    (-96.9570, 47.7910), (-96.9665, 47.7996), (-96.9763, 47.7978), (-96.9723, 47.8028), (-96.9916, 47.8082),
    (-96.9787, 47.8099), (-96.9805, 47.8219), (-96.9741, 47.8235), (-96.9871, 47.8256), (-96.9816, 47.8296),
    (-96.9863, 47.8371), (-96.9983, 47.8419), (-96.9940, 47.8460), (-97.0059, 47.8644), (-97.0018, 47.8681),
    (-97.0231, 47.8741), (-97.0180, 47.8787), (-97.0254, 47.8848), (-97.0188, 47.8904), (-97.0250, 47.8952),
    (-97.0198, 47.9013), (-97.0252, 47.9077), (-97.0153, 47.9067), (-97.0238, 47.9156), (-97.0179, 47.9199),
    (-97.0364, 47.9308), (-97.0363, 47.9395), (-97.0553, 47.9475), (-97.0525, 47.9574), (-97.0618, 47.9647),
    (-97.0531, 47.9906), (-97.0643, 47.9985), (-97.0630, 48.0148), (-97.0724, 48.0193), (-97.0724, 48.0478),
    (-97.1046, 48.0726), (-97.0998, 48.0851), (-97.1099, 48.0921), (-97.1025, 48.0970), (-97.1113, 48.1059),
    (-97.1237, 48.1071), (-97.1210, 48.1154), (-97.1336, 48.1369), (-97.1229, 48.1398), (-97.1467, 48.1425),
    (-97.1211, 48.1595), (-97.1421, 48.1601), (-97.1476, 48.1706), (-97.1360, 48.1772), (-97.1500, 48.1838),
    (-97.1357, 48.1970), (-97.1410, 48.2040), (-97.1290, 48.2086), (-97.1394, 48.2161), (-97.1178, 48.2096),
    (-97.1378, 48.2216), (-97.1521, 48.2193), (-97.1374, 48.2269), (-97.1207, 48.2247), (-97.1442, 48.2326),
    (-97.1336, 48.2389), (-97.1467, 48.2442), (-97.1256, 48.2498), (-97.1307, 48.2587), (-97.1231, 48.2618),
    (-97.1451, 48.2683), (-97.1256, 48.2700), (-97.1253, 48.2764), (-97.1167, 48.2790), (-97.1237, 48.2910),
    (-97.1356, 48.2869), (-97.1373, 48.2914), (-97.1277, 48.2987), (-97.1124, 48.2961), (-97.1264, 48.3034),
    (-97.1241, 48.3110), (-97.1379, 48.3098), (-97.1329, 48.3190), (-97.1159, 48.3238), (-97.1419, 48.3260),
    (-97.1259, 48.3405), (-97.1402, 48.3446), (-97.1362, 48.3521), (-97.1561, 48.3646), (-97.1534, 48.3706),
    (-97.1377, 48.3712), (-97.1412, 48.3785), (-97.1335, 48.3822), (-97.1499, 48.3818), (-97.1633, 48.3925),
    (-97.1311, 48.4068), (-97.1401, 48.4185), (-97.1505, 48.4127), (-97.1541, 48.4178), (-97.1393, 48.4216),
    (-97.1224, 48.4181), (-97.1457, 48.4311), (-97.1266, 48.4315), (-97.1256, 48.4422), (-97.1505, 48.4407),
    (-97.1307, 48.4518), (-97.1286, 48.4596), (-97.1441, 48.4643), (-97.1474, 48.4725), (-97.1280, 48.4741),
    (-97.1626, 48.4781), (-97.1403, 48.4846), (-97.1426, 48.4923), (-97.1316, 48.4888), (-97.1491, 48.5006),
    (-97.1504, 48.5116), (-97.1359, 48.5124), (-97.1269, 48.5199), (-97.1534, 48.5209), (-97.1538, 48.5294),
    (-97.1414, 48.5349), (-97.1660, 48.5489), (-97.1639, 48.5540), (-97.1390, 48.5551), (-97.1754, 48.5620),
    (-97.1492, 48.5700), (-97.1487, 48.5764), (-97.1606, 48.5809), (-97.1439, 48.5821), (-97.1423, 48.5926),
    (-97.1540, 48.5954), (-97.1343, 48.6074), (-97.1506, 48.6117), (-97.1296, 48.6137), (-97.1339, 48.6228),
    (-97.1195, 48.6186), (-97.1311, 48.6315), (-97.1095, 48.6325), (-97.1157, 48.6446), (-97.0987, 48.6610),
    (-97.1092, 48.6642), (-97.1005, 48.6685), (-97.1013, 48.6823), (-97.0903, 48.6844), (-97.1199, 48.7012),
    (-97.1101, 48.7080), (-97.1374, 48.7266), (-97.1283, 48.7321), (-97.1421, 48.7362), (-97.1375, 48.7496),
    (-97.1538, 48.7540), (-97.1454, 48.7648), (-97.1590, 48.7757), (-97.1475, 48.7814), (-97.1589, 48.7833),
    (-97.1582, 48.7912), (-97.1740, 48.7939), (-97.1633, 48.8006), (-97.1816, 48.7985), (-97.1581, 48.8103),
    (-97.1903, 48.8151), (-97.1780, 48.8228), (-97.1842, 48.8339), (-97.1739, 48.8380), (-97.1829, 48.8469),
    (-97.1731, 48.8506), (-97.1792, 48.8628), (-97.1906, 48.8643), (-97.1778, 48.8756), (-97.1994, 48.8811),
    (-97.2016, 48.8919), (-97.1900, 48.8955), (-97.2114, 48.9056), (-97.2108, 48.9160), (-97.2198, 48.9221),
    (-97.2144, 48.9316), (-97.2246, 48.9338), (-97.2266, 48.9458), (-97.2340, 48.9474), (-97.2277, 48.9624),
    (-97.2389, 48.9666), (-97.2382, 48.9830), (-97.2266, 48.9930), (-97.2342, 48.9964), (-97.2286, 49.0004),
)


class Stop(Exception):
    """The run was asked to stop (Ctrl-C, or a terminate signal)."""


class UserError(Exception):
    """Something the user must put right; printed without a traceback."""


# ----------------------------------------------------------------------------------- small --
def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def dur(sec):
    sec = max(0, int(round(sec)))
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return "{} d {} h".format(d, h)
    if h:
        return "{} h {:02d} min".format(h, m)
    if m:
        return "{} min {:02d} s".format(m, s)
    return "{} s".format(s)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def real(path):
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))


def inside(path, folder, samefile=None):
    """True when path is folder or lies in it. Two tests. First the resolved path strings (links
    followed). Then, because a path string does not settle it where two spellings are one folder
    (a Mac's disk does not tell upper case from lower: ~/nd_suite_data IS ~/ND_Suite_Data), every
    folder from path upward is compared with folder by os.path.samefile, which asks the disk. A
    part of path that does not exist yet is skipped: the first folder above it that exists is
    where a write would land. samefile: a stand-in for the tests."""
    p, f = real(path), real(folder)
    if p == f or p.startswith(f.rstrip(os.sep) + os.sep):
        return True
    samefile = samefile or os.path.samefile
    q = p
    while True:
        try:
            if samefile(q, f):
                return True
        except (OSError, ValueError):
            pass                                            # not there (yet): the folder above decides
        up = os.path.dirname(q)
        if up == q:
            return False
        q = up


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def ram_gb():
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1024.0 ** 3
    except (ValueError, OSError, AttributeError):
        return None


def more_files():
    """Lift this process's limit on open files towards 4096 (macOS starts at 256; a VRT of
    thousands of tiles keeps up to 100 of them open at a time)."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = 4096 if hard == resource.RLIM_INFINITY else min(4096, hard)
        if soft != resource.RLIM_INFINITY and soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except Exception:                                                   # noqa - best effort
        pass


def cpu_seconds():
    try:
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF)
        return round(r.ru_utime + r.ru_stime, 1)
    except Exception:                                                   # noqa - a record only
        return None


def size_text(mb):
    return "{:.1f} GB".format(mb / 1024.0) if mb >= 1024.0 else "{:.0f} MB".format(max(1.0, mb))


def peak_rss_mb():
    try:
        import resource
        v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(v / (1024.0 * 1024.0) if sys.platform == "darwin" else v / 1024.0, 1)
    except Exception:                                                   # noqa - a record only
        return None


class Log:
    """Lines to the screen and, when a path is given, to a log file."""

    def __init__(self, path=None):
        self.fh = open(path, "a", encoding="utf-8") if path else None

    def __call__(self, msg=""):
        print(msg, flush=True)
        if self.fh:
            self.fh.write("{}  {}\n".format(now(), msg))
            self.fh.flush()

    def close(self):
        if self.fh:
            self.fh.close()
            self.fh = None


# ------------------------------------------------------------------------------ where things are --
def profile_plugins():
    prof = os.environ.get("DAMSAFETY_PROFILE") or os.environ.get("DS_QGIS_PROFILE") or "damsafety"
    home = os.path.expanduser("~")
    return [os.path.join(home, "Library", "Application Support", "QGIS", "QGIS3", "profiles", prof,
                         "python", "plugins"),
            os.path.join(home, ".local", "share", "QGIS", "QGIS3", "profiles", prof, "python", "plugins")]


def find_plugins(a):
    """The folder that holds DamSafety_Finder: --plugins, --repo, $DAMSAFETY_PLUGINS, the damsafety
    QGIS profile, then the DWR_Plugins checkout this script sits in (tools/mac)."""
    here = os.path.dirname(os.path.abspath(__file__))
    given = [(a.plugins, "--plugins"), (a.repo, "--repo")]
    for p, what in given:
        if p:
            p = real(p)
            if os.path.basename(p) == ENGINE and os.path.isfile(os.path.join(p, "finder.py")):
                p = os.path.dirname(p)
            if not os.path.isfile(os.path.join(p, ENGINE, "finder.py")):
                raise UserError("{} {} holds no {}/finder.py".format(what, p, ENGINE))
            return p, what
    cands = [(os.environ.get("DAMSAFETY_PLUGINS"), "$DAMSAFETY_PLUGINS")]
    cands += [(p, "the damsafety QGIS profile") for p in profile_plugins()]
    cands.append((os.path.normpath(os.path.join(here, "..", "..")), "the checkout this script is in"))
    for p, what in cands:
        if p and os.path.isfile(os.path.join(p, ENGINE, "finder.py")):
            return real(p), what
    raise UserError("the {} plugin was not found (looked in $DAMSAFETY_PLUGINS, {} and the folder two "
                    "above this script). Install the suite, or give --plugins FOLDER (the folder that "
                    "holds {}) or --repo CHECKOUT.".format(ENGINE, profile_plugins()[0], ENGINE))


def load_engine(plugins):
    if plugins not in sys.path:
        sys.path.insert(0, plugins)
    import importlib
    return importlib.import_module(ENGINE + ".finder")


def find_data(a):
    d = a.data or os.environ.get("NDSUITE_DATA") or os.path.join("~", "ND_Suite_Data")
    return real(d)


def find_pack(data):
    """The folder that holds hydro/3DHP_flowlines.gpkg: $NDSUITE_PACK, <data>/ND_Dam_Suite_Pack,
    or the data folder itself."""
    cands = [os.environ.get("NDSUITE_PACK")] + [os.path.join(data, n) for n in PACK_NAMES] + [data]
    for c in cands:
        if c and os.path.isfile(os.path.join(os.path.expanduser(c), STREAMS)):
            return real(c)
    raise UserError("the ND data pack was not found: no {} in {} or in {}. Give --data FOLDER (the suite "
                    "data folder, or the pack folder itself).".format(
                        STREAMS, data, os.path.join(data, PACK_NAMES[0])))


def pack_inputs(pack):
    """{role: path} of the pack layers the run reads; refuses when one is missing."""
    files = {"streams": STREAMS, "roads": ROADS, "rails": RAILS}
    for rel, label in KNOWN:
        files["known: " + label] = rel
    out, missing = collections.OrderedDict(), []
    for role, rel in files.items():
        p = os.path.join(pack, *rel.split("/"))
        if os.path.isfile(p):
            out[role] = p
        else:
            missing.append(rel)
    if missing:
        raise UserError("the pack {} lacks: {}".format(pack, ", ".join(missing)))
    return out


# ------------------------------------------------------------------------------------ geometry --
def gdal_mods():
    from osgeo import gdal, ogr, osr
    return gdal, ogr, osr


def srs_of(epsg=None, wkt=None):
    _g, _o, osr = gdal_mods()
    s = osr.SpatialReference()
    if epsg is not None:
        s.ImportFromEPSG(int(epsg))
    else:
        s.ImportFromWkt(wkt)
    if hasattr(s, "SetAxisMappingStrategy"):
        s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return s


def transformer(src, dst):
    _g, _o, osr = gdal_mods()
    return osr.CoordinateTransformation(src, dst)


def grow(box, m):
    return (box[0] - m, box[1] - m, box[2] + m, box[3] + m)


def box_ring(box, n=8):
    x0, y0, x1, y1 = box
    pts = []
    for i in range(n):
        t = i / float(n)
        pts.append((x0 + (x1 - x0) * t, y0))
    for i in range(n):
        t = i / float(n)
        pts.append((x1, y0 + (y1 - y0) * t))
    for i in range(n):
        t = i / float(n)
        pts.append((x1 - (x1 - x0) * t, y1))
    for i in range(n):
        t = i / float(n)
        pts.append((x0, y1 - (y1 - y0) * t))
    return pts


def box_to(box, ct, n=8):
    """The box of a box's outline carried through a coordinate transformation."""
    pts = [ct.TransformPoint(x, y)[:2] for x, y in box_ring(box, n)]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


def meets(a, b):
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def box_geom(box):
    _g, ogr, _s = gdal_mods()
    r = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]), (box[0], box[1])):
        r.AddPoint_2D(x, y)
    p = ogr.Geometry(ogr.wkbPolygon)
    p.AddGeometry(r)
    return p


def rings_of(geom):
    """Every ring (numpy x, y arrays) of a polygon / multipolygon: even-odd counting over all of
    them tests a point against the outer rings and the holes at once."""
    import numpy as np
    out = []
    n = geom.GetGeometryCount()
    if geom.GetGeometryName() == "LINEARRING" or (n == 0 and geom.GetPointCount()):
        pts = geom.GetPoints()
        if pts and len(pts) >= 4:
            a = np.asarray(pts, float)
            out.append((a[:, 0].copy(), a[:, 1].copy()))
        return out
    for i in range(n):
        out.extend(rings_of(geom.GetGeometryRef(i)))
    return out


def points_in_rings(xs, ys, rings):
    """Bool array: the points inside the rings (even-odd rule)."""
    import numpy as np
    xs = np.asarray(xs, float)
    ys = np.asarray(ys, float)
    inside_ = np.zeros(xs.shape, bool)
    for rx, ry in rings:
        x1, y1 = rx[:-1], ry[:-1]
        x2, y2 = rx[1:], ry[1:]
        for k in range(0, len(x1), 4096):
            a1, b1, a2, b2 = x1[k:k + 4096], y1[k:k + 4096], x2[k:k + 4096], y2[k:k + 4096]
            cross = (b1[None, :] > ys[:, None]) != (b2[None, :] > ys[:, None])
            with np.errstate(divide="ignore", invalid="ignore"):
                xc = a1[None, :] + (ys[:, None] - b1[None, :]) * (a2 - a1)[None, :] / (b2 - b1)[None, :]
            inside_ ^= (np.count_nonzero(cross & (xs[:, None] < xc), axis=1) % 2).astype(bool)
    return inside_


def polygons_of(path, where=None):
    """The union of the polygons of a vector file (optionally those a filter function keeps), in
    EPSG:5070; None when it has none."""
    _g, ogr, _s = gdal_mods()
    ds = ogr.Open(path, 0)
    if ds is None:
        raise UserError("cannot open {}".format(path))
    lyr = ds.GetLayer(0)
    src = lyr.GetSpatialRef()
    if src is None:
        raise UserError("{} has no coordinate system".format(path))
    src = src.Clone()
    if hasattr(src, "SetAxisMappingStrategy"):
        from osgeo import osr
        src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = transformer(src, srs_of(WORK_EPSG))
    multi = ogr.Geometry(ogr.wkbMultiPolygon)
    for f in lyr:
        if where is not None and not where(f):
            continue
        g = f.GetGeometryRef()
        if g is None:
            continue
        g = g.Clone()
        g.FlattenTo2D()
        if ogr.GT_Flatten(g.GetGeometryType()) not in (ogr.wkbPolygon, ogr.wkbMultiPolygon):
            continue
        g.Segmentize(0.02 if src.IsGeographic() else 2000.0)
        g.Transform(ct)
        if ogr.GT_Flatten(g.GetGeometryType()) == ogr.wkbPolygon:
            multi.AddGeometry(g)
        else:
            for i in range(g.GetGeometryCount()):
                multi.AddGeometry(g.GetGeometryRef(i).Clone())
    ds = None
    if multi.GetGeometryCount() == 0:
        return None
    u = multi.UnionCascaded()
    return u if u is not None else multi.Buffer(0)


def builtin_outline():
    """The built-in outline of North Dakota in EPSG:5070 (see BUILTIN_ND at the top)."""
    _g, ogr, _s = gdal_mods()
    east = list(BUILTIN_EAST)
    south = list(BUILTIN_SOUTH)                             # west to east; it ends under the first east point
    ring = south + east + [(east[-1][0], BUILTIN_N), (BUILTIN_W, BUILTIN_N), south[0]]
    r = ogr.Geometry(ogr.wkbLinearRing)
    for x, y in ring:
        r.AddPoint_2D(x, y)
    p = ogr.Geometry(ogr.wkbPolygon)
    p.AddGeometry(r)
    p.Segmentize(0.02)
    p.Transform(transformer(srs_of(4326), srs_of(WORK_EPSG)))
    return p


def state_outline(a, pack):
    """(polygon in EPSG:5070 with its allowance, or None; what it is) - see the module text."""
    choice = (a.state_outline or "").strip()
    if choice.lower() == "none":
        return None, "none (fills outside North Dakota are kept; in_state is yes for all)"
    counties = os.path.join(pack, *COUNTIES.split("/")) if pack else ""
    if choice:
        if not os.path.exists(os.path.expanduser(choice)):
            raise UserError("--state-outline {}: no such file".format(choice))
        geom, what, buf = polygons_of(real(choice)), "the polygon file {}".format(real(choice)), 0.0
        if geom is None:
            raise UserError("--state-outline {} holds no polygons".format(choice))
    elif counties and os.path.isfile(counties) and polygons_of(counties) is not None:
        geom, what, buf = polygons_of(counties), "the pack's county boundaries ({})".format(COUNTIES), 0.0
    else:
        geom, buf = builtin_outline(), BUILTIN_BUFFER_M
        what = ("the built-in simple outline of North Dakota (no --state-outline given and the pack "
                "has no {})".format(COUNTIES))
    if a.state_buffer_m is not None:
        buf = float(a.state_buffer_m)
    if buf:
        geom = geom.Buffer(buf, 8)
    return geom, "{}, with a {:.0f} m allowance".format(what, buf)


def county_geom(a, pack):
    """The polygon (EPSG:5070) of --county NAME from --county-file or the pack's county layer."""
    _g, ogr, _s = gdal_mods()
    path = a.county_file or os.path.join(pack, *COUNTIES.split("/"))
    if not os.path.isfile(os.path.expanduser(path)):
        raise UserError(
            "--county {}: this pack has no county layer ({} is not there). Give --county-file FILE "
            "(any polygon file with a county name field), or use --bbox=W,S,E,N (lon / lat).".format(
                a.county, COUNTIES))
    path = real(path)
    want = a.county.strip().lower()
    if want.endswith(" county"):
        want = want[:-7].strip()
    names = set()

    def is_it(f):
        hit = False
        for i in range(f.GetFieldCount()):
            d = f.GetFieldDefnRef(i)
            if d.GetType() != ogr.OFTString or f.GetField(i) in (None, ""):
                continue
            v = str(f.GetField(i)).strip()
            low = v.lower()
            if low.endswith(" county"):
                low = low[:-7].strip()
            if "name" in d.GetName().lower() or "county" in d.GetName().lower() or "cnty" in d.GetName().lower():
                names.add(v)
            if low == want:
                hit = True
        return hit

    geom = polygons_of(path, is_it)
    if geom is None:
        some = ", ".join(sorted(names)[:60])
        raise UserError("--county {}: no polygon of that name in {}{}".format(
            a.county, path, " (it has: {})".format(some) if some else ""))
    return geom, path


def parse_bbox(text):
    """(xmin, ymin, xmax, ymax) in EPSG:5070 from 'W,S,E,N' in lon / lat degrees, or, when the
    numbers are too big for degrees, in EPSG:5070 metres."""
    try:
        v = [float(t) for t in str(text).replace(";", ",").split(",")]
    except ValueError:
        v = []
    if len(v) != 4 or v[0] >= v[2] or v[1] >= v[3]:
        raise UserError("--bbox must be W,S,E,N with W < E and S < N (lon / lat degrees, or EPSG:5070 "
                        "metres), e.g. --bbox=-101.0,46.4,-100.5,46.8")
    if all(abs(t) <= 360.0 for t in v):
        g = box_geom(v)
        g.Segmentize(0.02)
        g.Transform(transformer(srs_of(4326), srs_of(WORK_EPSG)))
        e = g.GetEnvelope()
        return (e[0], e[2], e[1], e[3])
    return tuple(v)


# ------------------------------------------------------------------------------------- the DEM --
class Dem:
    """The terrain raster: its grid, where its source files lie, and quick 'is there terrain' reads."""

    def __init__(self, path):
        gdal, _o, _s = gdal_mods()
        import numpy as np
        self.path = path
        if not os.path.exists(path):
            vol = ""
            parts = path.split(os.sep)
            if len(parts) > 2 and parts[1] == "Volumes" and not os.path.isdir(os.sep.join(parts[:3])):
                vol = " The drive {} is not connected.".format(os.sep.join(parts[:3]))
            raise UserError("the terrain file was not found: {}.{} Give --dem FILE.".format(path, vol))
        self.ds = gdal.Open(path)
        if self.ds is None:
            raise UserError("GDAL cannot open the terrain file {}: {}".format(
                path, (gdal.GetLastErrorMsg() or "").strip()))
        self.gt = self.ds.GetGeoTransform()
        if self.gt[2] or self.gt[4] or self.gt[5] >= 0:
            raise UserError("the terrain raster {} is rotated or south-up; a north-up raster is needed".format(path))
        wkt = self.ds.GetProjection()
        if not wkt:
            raise UserError("the terrain raster {} has no coordinate system".format(path))
        self.nx, self.ny = self.ds.RasterXSize, self.ds.RasterYSize
        self.band = self.ds.GetRasterBand(1)
        self.nodata = self.band.GetNoDataValue()
        self.overviews = self.band.GetOverviewCount()
        self.srs = srs_of(wkt=wkt)
        self.epsg = self.srs.GetAuthorityCode(None)
        self.to_dem = transformer(srs_of(WORK_EPSG), self.srs)
        self.from_dem = transformer(self.srs, srs_of(WORK_EPSG))
        x0, y1 = self.gt[0], self.gt[3]
        self.box_dem = (x0, y1 + self.ny * self.gt[5], x0 + self.nx * self.gt[1], y1)
        self.box_5070 = box_to(self.box_dem, self.from_dem, 64)
        self.rects = self._source_rects()
        self.np = np
        self.read_error = ""

    def _source_rects(self):
        """Pixel boxes (x0, y0, x1, y1) of the VRT's source files, read from the VRT text (no
        pixels are read); None when the file is not a plain VRT (then every tile in its box is
        looked at)."""
        import numpy as np
        if not self.path.lower().endswith(".vrt"):
            return None
        try:
            root = ET.parse(self.path).getroot()
        except (ET.ParseError, OSError):
            return None
        band = root.find("VRTRasterBand")
        if band is None:
            return None
        rects = []
        for src in band:
            d = src.find("DstRect")
            if d is None:
                continue
            try:
                x, y = float(d.get("xOff")), float(d.get("yOff"))
                rects.append((x, y, x + float(d.get("xSize")), y + float(d.get("ySize"))))
            except (TypeError, ValueError):
                return None
        return np.asarray(rects, float) if rects else None

    def window(self, box5070, margin=0.0):
        """(xoff, yoff, xsize, ysize) in pixels of a EPSG:5070 box (plus margin), cut to the raster;
        None when it lies outside."""
        b = box_to(grow(box5070, margin), self.to_dem)
        c0 = int(math.floor((b[0] - self.gt[0]) / self.gt[1]))
        c1 = int(math.ceil((b[2] - self.gt[0]) / self.gt[1]))
        r0 = int(math.floor((b[3] - self.gt[3]) / self.gt[5]))
        r1 = int(math.ceil((b[1] - self.gt[3]) / self.gt[5]))
        c0, r0, c1, r1 = max(0, c0), max(0, r0), min(self.nx, c1), min(self.ny, r1)
        if c1 <= c0 or r1 <= r0:
            return None
        return (c0, r0, c1 - c0, r1 - r0)

    def has_source(self, win):
        if self.rects is None:
            return True
        r = self.rects
        return bool(((r[:, 0] < win[0] + win[2]) & (r[:, 2] > win[0])
                     & (r[:, 1] < win[1] + win[3]) & (r[:, 3] > win[1])).any())

    def valid(self, a):
        np = self.np
        a = np.asarray(a, "float64")
        ok = np.isfinite(a) & (a > -9998.0)
        if self.nodata is not None and math.isfinite(self.nodata):
            ok &= a != self.nodata
        return ok

    def _read(self, *args, **kw):
        """band.ReadAsArray, with GDAL's own error lines kept off the screen. None when the read
        fails (a source file of the VRT is missing, the drive is gone); read_error then says why."""
        gdal, _o, _s = gdal_mods()
        gdal.PushErrorHandler("CPLQuietErrorHandler")
        try:
            gdal.ErrorReset()
            try:
                a = self.band.ReadAsArray(*args, **kw)
            except RuntimeError as e:                       # GDAL with its exceptions switched on
                a = None
                self.read_error = str(e).strip()
            if a is not None and gdal.GetLastErrorType() >= gdal.CE_Failure:
                a = None                                    # an answer GDAL itself called an error
            if a is None:
                self.read_error = ((gdal.GetLastErrorMsg() or "").strip() or self.read_error
                                   or "the read failed")
        finally:
            gdal.PopErrorHandler()
        return a

    def terrain(self, win, box5070=None):
        """(has, share) for a pixel window: has = True / False, or None when the terrain there
        cannot be read now; share = the part (0 to 1) that holds terrain. With box5070 (the tile the
        window was cut for) the share is that of the tile's own square - the window is the square's
        box in the raster's CRS cut to the raster, so a tile half off the raster is half empty
        though its window is full. The share is for the time estimate only; has decides whether a
        tile runs.
        With overviews (a VRT of USGS tiles has them) this reads a 64 x 64 sample from an overview.
        Without, 25 single cells are read first and only an all-empty answer is checked with the
        64 x 64 sample (which then reads the window's full-resolution blocks: slow, but only for
        the empty tiles under a source file); the share is then the coarser 'so many of 25'."""
        np = self.np
        xo, yo, xs, ys = win
        if box5070 is None:
            cut = 1.0
        else:
            b = box_to(box5070, self.to_dem)
            cut = min(1.0, xs * ys / max(1.0, (b[2] - b[0]) * (b[3] - b[1]) / abs(self.gt[1] * self.gt[5])))
        if not self.overviews:
            hits = 0
            for i in range(5):
                for j in range(5):
                    a = self._read(xo + min(xs - 1, int((i + 0.5) * xs / 5)),
                                   yo + min(ys - 1, int((j + 0.5) * ys / 5)), 1, 1)
                    if a is None:
                        return None, 0.0
                    hits += bool(self.valid(a).any())
            if hits:
                return True, cut * hits / 25.0
        bw, bh = max(1, min(xs, 64)), max(1, min(ys, 64))
        a = self._read(xo, yo, xs, ys, buf_xsize=bw, buf_ysize=bh)
        if a is None:
            return None, 0.0
        ok = self.valid(a)
        if not ok.any():
            return False, 0.0
        if box5070 is None:
            return True, float(ok.mean())
        n = 24                                              # a grid of points in the tile's own square
        x0, y0, x1, y1 = box5070
        pts = [(x0 + (i + 0.5) * (x1 - x0) / n, y0 + (j + 0.5) * (y1 - y0) / n) for i in range(n) for j in range(n)]
        tp = np.asarray([q[:2] for q in self.to_dem.TransformPoints(pts)], float)
        ci = np.floor(((tp[:, 0] - self.gt[0]) / self.gt[1] - xo) * bw / float(xs)).astype(int)
        ri = np.floor(((tp[:, 1] - self.gt[3]) / self.gt[5] - yo) * bh / float(ys)).astype(int)
        inw = (ci >= 0) & (ci < bw) & (ri >= 0) & (ri < bh)
        share = float(np.count_nonzero(ok[ri[inw], ci[inw]])) / len(pts)
        return True, min(1.0, max(share, 1.0 / len(pts)))

    def has_data(self, win):
        """True when the window holds any terrain (and when it cannot be read now: the tile then
        runs, and its log says what is wrong)."""
        has, _share = self.terrain(win)
        return True if has is None else has

    def edge_m(self, x, y):
        """Metres from the point (EPSG:5070) to the nearest missing terrain (a NoData cell, or the
        end of the raster) within EDGE_SEARCH_M, read at full resolution; holes smaller than
        EDGE_SPECK_M2 that do not reach the search window's rim are ignored. None when there is none."""
        np = self.np
        px, py = self.to_dem.TransformPoint(float(x), float(y))[:2]
        cell = abs(self.gt[1])
        h = int(math.ceil(EDGE_SEARCH_M / cell))
        c = int(math.floor((px - self.gt[0]) / self.gt[1]))
        r = int(math.floor((py - self.gt[3]) / self.gt[5]))
        n = 2 * h + 1
        miss = np.ones((n, n), bool)
        c0, r0, c1, r1 = max(0, c - h), max(0, r - h), min(self.nx, c + h + 1), min(self.ny, r + h + 1)
        if c1 > c0 and r1 > r0:
            a = self.band.ReadAsArray(c0, r0, c1 - c0, r1 - r0)
            if a is None:
                raise RuntimeError("reading the terrain round a fill failed")
            miss[r0 - (r - h):r1 - (r - h), c0 - (c - h):c1 - (c - h)] = ~self.valid(a)
        if not miss.any():
            return None
        from scipy import ndimage as ndi
        lab, k = ndi.label(miss)
        if k:
            size = np.bincount(lab.ravel())
            rim = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
            small = size < max(1.0, EDGE_SPECK_M2 / (cell * cell))
            small[rim] = False
            small[0] = False
            miss &= ~small[lab]
        rr, cc = np.nonzero(miss)
        if not len(rr):
            return None
        d = float(np.hypot((rr - h) * cell, (cc - h) * cell).min())
        return round(d, 1) if d <= EDGE_SEARCH_M else None


# ------------------------------------------------------------------------------------ the plan --
Tile = collections.namedtuple("Tile", "id ix iy box")


def tile_id(ix, iy, km):
    def part(v, pos, neg):
        return "{}{:04d}".format(pos if v >= 0 else neg, abs(v))
    return "T{:02d}_{}_{}".format(km, part(ix * km, "E", "W"), part(iy * km, "N", "S"))


def lattice(box, tile_m):
    """Every lattice tile that meets the box: corners at whole multiples of tile_m in EPSG:5070."""
    km = int(round(tile_m / 1000.0))
    out = []
    for ix in range(int(math.floor(box[0] / tile_m)), int(math.ceil(box[2] / tile_m))):
        for iy in range(int(math.floor(box[1] / tile_m)), int(math.ceil(box[3] / tile_m))):
            b = (ix * tile_m, iy * tile_m, (ix + 1) * tile_m, (iy + 1) * tile_m)
            if meets(b, box):
                out.append(Tile(tile_id(ix, iy, km), ix, iy, b))
    out.sort(key=lambda t: (t.ix, t.iy))
    return out


def settings_key(engine_version, a, dem):
    """What a finished tile was made with; a tile made with something else is not mixed in. The
    terrain is known by its file name and grid, not its folder (a drive can mount under another
    name)."""
    return {"script": SCRIPT_VERSION.split(".")[0], "finder": engine_version, "tile_km": int(a.tile_km),
            "call": dict(CALL), "engine_tile_m": ENGINE_TILE_M, "edge_search_m": EDGE_SEARCH_M,
            "edge_speck_m2": EDGE_SPECK_M2,
            "dem": None if dem is None else {"name": os.path.basename(dem.path), "nx": dem.nx, "ny": dem.ny,
                                             "grid": [round(v, 6) for v in dem.gt], "epsg": str(dem.epsg)}}


def key_without_terrain(out, engine_version, a):
    """--merge-only with the terrain drive not connected: the settings key of the finished tiles in
    out that were made with THIS Finder version, tile size and call (everything of the key but the
    terrain), so that they can be merged without reading the terrain raster again. They must all
    come from one terrain raster; with two the drive is needed to tell which is the current one."""
    want = settings_key(engine_version, a, None)
    want.pop("dem")
    dems = collections.OrderedDict()
    base = os.path.join(out, "tiles")
    n_other = 0
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        m = read_json(os.path.join(base, name, "done.json"))
        k = (m or {}).get("key")
        if not isinstance(k, dict):
            continue
        rest = dict(k)
        d = rest.pop("dem", None)
        if rest == want and d:
            dems.setdefault(json.dumps(d, sort_keys=True), [d, 0])[1] += 1
        else:
            n_other += 1
    if not dems:
        raise UserError("{} holds no finished tile made with this Finder ({}) and tile size ({} km){}; there "
                        "is nothing --merge-only could merge without the terrain file".format(
                            out, engine_version, a.tile_km,
                            " ({} were made with other settings)".format(n_other) if n_other else ""))
    if len(dems) > 1:
        raise UserError("the finished tiles in {} come from {} different terrain rasters ({}). Connect the "
                        "terrain drive, so that the script can tell which is the current one.".format(
                            out, len(dems), "; ".join("{} tile(s) from {}".format(n_, d_.get("name"))
                                                      for d_, n_ in dems.values())))
    d, n_ = list(dems.values())[0]
    return dict(want, dem=d), n_


def tile_dir(out, tid):
    return os.path.join(out, "tiles", tid)


def marker(out, tid):
    return read_json(os.path.join(tile_dir(out, tid), "done.json"))


def redo_since(a, out, start=False):
    """--redo: the time the redo began (kept in redo.json, so a stopped --redo goes on with the tiles
    it has not yet run again); a tile finished after it is finished. None without --redo, and then a
    redo.json left by a stopped --redo is taken away (start=True: a real run, not a dry run)."""
    path = os.path.join(out, "redo.json")
    if not a.redo:
        if start and os.path.exists(path):
            os.remove(path)
        return None
    old = read_json(path)
    if old and old.get("since"):
        return float(old["since"])
    since = time.time()
    if start:
        write_json(path, {"since": since, "started": now()})
    return since


def tile_cost(share, sec_full):
    """The seconds one tile is expected to take, from its share of terrain (0 to 1; None = not
    known, counted as full) and the seconds of a tile full of terrain: a tile with a sliver of
    terrain still starts a process and cuts its extracts (EST_TILE_START_SEC), the rest grows with
    the terrain."""
    if share is None:
        return float(sec_full)
    base = min(EST_TILE_START_SEC, float(sec_full))
    return base + min(1.0, max(0.0, float(share))) * (float(sec_full) - base)


def full_tile_seconds(done):
    """(seconds of a tile full of terrain, how many finished full tiles that is the mean of) from
    [(seconds, share)] of the finished tiles. Only tiles FULL of terrain count (share >=
    FULL_SHARE): the mean of all finished tiles is pulled far down by the quick edge tiles. With
    fewer than EST_MIN_FULL of them the seconds are EST_SEC_PER_TILE: the cloud test box's figure,
    not this computer's."""
    full = [float(sec) for sec, share in done if sec and share is not None and share >= FULL_SHARE]
    if len(full) >= EST_MIN_FULL:
        return sum(full) / len(full), len(full)
    return EST_SEC_PER_TILE, len(full)


def time_for(shares, sec_full, workers):
    """Seconds to run tiles with these terrain shares, `workers` at a time."""
    costs = [tile_cost(sh, sec_full) for sh in shares]
    return max(sum(costs) / float(max(1, workers)), max(costs)) if costs else 0.0


def make_plan(a, dem, out, key, outline, select, log, cache_ok, since=None, timed=None):
    """([(Tile, state)], {tile id: share of terrain or None}) for the lattice tiles over the terrain
    raster that the selection keeps. state: done / other (finished with other settings) / outside
    (the state outline) / nosource (no source file of the VRT there) / nodata (source files, but no
    terrain) / unreadable (the terrain there cannot be read now; dem.read_error says why) / todo.
    timed: a dict that is given {tile id: (seconds, fills, share of terrain as its done.json has it
    or None)} of every tile in the plan that has a result made with these settings - also one
    --redo is about to run again: what it took last time is this computer's own figure all the
    same."""
    tile_m = a.tile_km * 1000.0
    tiles = lattice(dem.box_5070, tile_m)
    cache_path = os.path.join(out, "plan_cache.json")
    stamp = "{}|{}|{}".format(dem.path, os.path.getsize(dem.path), int(os.path.getmtime(dem.path)))
    cache = read_json(cache_path) or {}
    if cache.get("stamp") != stamp:
        cache = {"stamp": stamp, "share": {}}
    elif "share" not in cache:                              # a cache of script 1.0: yes / no, no shares
        cache = {"stamp": stamp, "share": {k: 0.0 for k, v in (cache.get("terrain") or {}).items() if v is False}}
    changed, plan, shares, looked, t0 = False, [], {}, 0, time.time()
    for t in tiles:
        g = None
        if select is not None:
            g = box_geom(t.box)
            if not select.Intersects(g):
                continue
        m = marker(out, t.id) if os.path.isdir(out) else None
        if m is not None and m.get("key") == key and timed is not None:
            timed[t.id] = (m.get("seconds"), int(m.get("fills") or 0), m.get("terrain_share"))
        if m is not None and (since is None or float(m.get("finished_epoch") or 0) >= since):
            plan.append((t, "done" if m.get("key") == key else "other"))
            shares[t.id] = m["terrain_share"] if m.get("terrain_share") is not None else cache["share"].get(t.id)
            continue
        if outline is not None and not outline.Intersects(g or box_geom(t.box)):
            plan.append((t, "outside"))
            continue
        win = dem.window(t.box)
        if win is None or not dem.has_source(win):
            plan.append((t, "nosource"))
            continue
        got = cache["share"].get(t.id)
        if got is None:
            has, share = dem.terrain(win, t.box)
            looked += 1
            if looked % 200 == 0:
                log("  looked for terrain in {} tiles ({})".format(looked, dur(time.time() - t0)))
            if has is None:                                 # not readable now: not remembered, and said
                plan.append((t, "unreadable"))
                shares[t.id] = None
                continue
            got = round(share, 4) if has else 0.0
            cache["share"][t.id] = got
            changed = True
        shares[t.id] = got
        plan.append((t, "todo" if got > 0 else "nodata"))
    if changed and cache_ok:
        write_json(cache_path, cache)
    return plan, shares


# ---------------------------------------------------------------------------------- one tile --
def extract(finder, src, dst, box5070):
    """A GeoPackage holding the features of src whose box meets box5070 (grown by EXTRACT_M), in
    the source's own order and with its feature ids, fields, layer name and CRS: what the engine
    reads from it for this tile is what it would read from the whole layer."""
    _g, ogr, osr = gdal_mods()
    ds, lyr = finder.open_layer(src)
    s = lyr.GetSpatialRef()
    if s is None:
        raise RuntimeError("layer {} has no CRS".format(src))
    s2 = s.Clone()
    if hasattr(s2, "SetAxisMappingStrategy"):
        s2.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    b = box_to(grow(box5070, EXTRACT_M), transformer(srs_of(WORK_EPSG), s2), 16)
    lyr.SetSpatialFilterRect(b[0], b[1], b[2], b[3])
    feats = []
    f = lyr.GetNextFeature()
    while f is not None:
        feats.append((f.GetFID(), f))
        f = lyr.GetNextFeature()
    feats.sort(key=lambda t: t[0])
    drv = ogr.GetDriverByName("GPKG")
    out = drv.CreateDataSource(dst)
    if out is None:
        raise RuntimeError("cannot create {}".format(dst))
    ol = out.CreateLayer(lyr.GetName(), s, lyr.GetGeomType())
    dfn = lyr.GetLayerDefn()
    for i in range(dfn.GetFieldCount()):
        ol.CreateField(dfn.GetFieldDefn(i))
    odf = ol.GetLayerDefn()
    out.StartTransaction()
    for fid, f in feats:
        g = ogr.Feature(odf)
        g.SetFrom(f)
        g.SetFID(fid)
        if ol.CreateFeature(g) != 0:
            raise RuntimeError("writing the extract {} failed".format(dst))
    out.CommitTransaction()
    out = None
    ds = None
    return len(feats)


def finder_measures_edge(finder):
    """True when this Finder measures the distance to missing terrain itself: edge_ft is one of
    its fields (0.7.0 on). Known from the Finder, not from its version number or its rows."""
    return "edge_ft" in [q[0] for q in (getattr(finder, "EMB_FIELDS", None) or [])]


def edge_distances(rows, dem, finder=None):
    """([[find_id, metres to the nearest missing terrain or None]], where the figures come from).
    A Finder that measures it itself (0.7.0: edge_ft among its fields, so every row carries it,
    whole feet, None when there is no missing terrain within its search of 1,000 m) is taken at
    its word; for an older one (0.6.x: no such field) it is measured here on the DEM, within
    EDGE_SEARCH_M (200 m). Which of the two is decided by the Finder (finder_measures_edge), so
    that a tile with no fill at all is recorded the way its Finder works; without a Finder, by
    the rows. The list goes into the tile's done.json; the merge checks it against the tile's rows
    and, for a tile whose rows carry edge_ft, reads the rows."""
    own = finder_measures_edge(finder) if finder is not None else bool(rows)
    if own and all("edge_ft" in r for r in rows):
        return ([[r["find_id"], None if r["edge_ft"] is None else round(float(r["edge_ft"]) * FT, 1)] for r in rows],
                "edge_ft as the Finder measured it")
    return ([[r["find_id"], dem.edge_m(r["x"], r["y"])] for r in rows],
            "measured by this script on the DEM, within {:.0f} m".format(EDGE_SEARCH_M))


def edge_kind(finder):
    """The kind this Finder gives a fill at the edge of the terrain data (em.K_EDGE, 0.7.0), or
    None for a Finder that has no such kind (0.6.x)."""
    return getattr(getattr(finder, "em", None), "K_EDGE", None)


def edge_rule(finder):
    """(the Finder's edge kind or None, the distance in metres within which it applies, how far in
    metres it looks for missing terrain) - the last two from the Finder's own defaults, which the
    tiles are run with."""
    kind = edge_kind(finder)
    if kind is None:
        return None, None, None
    try:
        p = finder.em.FinderParams()
        return kind, float(p.edge_m_max), float(p.edge_search_m)
    except Exception:                                                   # noqa - words for a log line only
        return kind, None, None


def run_tile(job):
    """One tile, in its own process: the extracts, the terrain window, finder.run_embankments with
    the proven call, the distance of every fill to missing terrain (the Finder's own figure from
    0.7.0 on, else measured here), then the tile folder is put in place in one step (a rename).
    Nothing is left half-finished in tiles/."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)            # Ctrl-C is the parent's to handle
    parent = os.getppid()
    gdal, _o, _s = gdal_mods()
    t0 = time.time()
    tid, box, out = job["id"], tuple(job["box"]), job["out"]
    finder = load_engine(job["plugins"])
    work = os.path.join(out, "work", "{}.{}".format(tid, os.getpid()))
    os.makedirs(work)
    try:
        return _run_tile(job, finder, work, parent, t0)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)             # a failed tile leaves only its log
        raise


def _run_tile(job, finder, work, parent, t0):
    gdal, _o, _s = gdal_mods()
    tid, box, out = job["id"], tuple(job["box"]), job["out"]

    def say(*parts):
        print("{}  {}".format(now(), " ".join(str(p) for p in parts)), flush=True)

    def orphaned():
        return os.getppid() != parent

    say("tile", tid, "bbox (EPSG:5070)", box, "Finder", finder.VERSION, "GDAL", gdal.__version__)
    dem = Dem(job["dem"])
    win = dem.window(box, ENGINE_PAD_M + WINDOW_MARGIN_M)
    if win is None:
        raise RuntimeError("the tile lies outside the terrain raster")
    inp = os.path.join(work, "in")
    os.makedirs(inp)
    dem_win = os.path.join(inp, "dem_window.vrt")
    if gdal.Translate(dem_win, dem.ds, format="VRT", srcWin=list(win)) is None:
        raise RuntimeError("cutting the terrain window failed: {}".format(gdal.GetLastErrorMsg()))
    say("terrain window", win, "of", job["dem"])
    paths = {}
    for role, src in job["inputs"].items():
        dst = os.path.join(inp, os.path.basename(src))
        n = extract(finder, src, dst, box)
        paths[role] = dst
        say("extract", role, n, "feature(s)")
    known = [(paths["known: " + label], label, None) for _rel, label in KNOWN]
    gpkg = os.path.join(work, tid + ".gpkg")
    res = finder.run_embankments(dem_win, paths["streams"], gpkg, roads=paths["roads"], rails=paths["rails"],
                                 known=known, bbox=box, log=say, cancelled=orphaned, **CALL)
    if res.get("cancelled") or orphaned():
        shutil.rmtree(work, ignore_errors=True)
        say("stopped: the run that started this tile is gone")
        return 3
    rows = res["rows"]
    edge, edge_from = edge_distances(rows, dem, finder)
    k_edge, _lim, far = edge_rule(finder)
    n_near = sum(1 for _i, d in edge if d is not None)
    if k_edge is not None and finder_measures_edge(finder) and all("edge_ft" in r for r in rows):
        say("{} fill(s); {} of them the Finder calls '{}' (kept, not candidates); {} have missing terrain "
            "within {} ({})".format(len(rows), sum(1 for r in rows if r["kind"] == k_edge), k_edge, n_near,
                                    "its search" if far is None else "{:,.0f} m".format(far), edge_from))
    else:
        say("{} fill(s), {} within {:.0f} m of missing terrain ({})".format(len(rows), n_near, EDGE_SEARCH_M, edge_from))
    shutil.rmtree(inp, ignore_errors=True)                  # the extracts and the window are not results
    done = {"id": tid, "box": list(box), "key": job["key"], "fills": len(rows),
            "terrain_share": job.get("terrain_share"), "edge_from": edge_from,
            "status": res["counts"]["status"], "kinds": res["counts"]["kinds"], "ridges": len(res["ridges"]),
            "edge_m": edge, "seconds": round(time.time() - t0, 1), "cpu_seconds": cpu_seconds(),
            "peak_rss_mb": peak_rss_mb(),
            "finished": now(), "finished_epoch": round(time.time(), 1), "finder": finder.VERSION,
            "gdal": gdal.__version__}
    write_json(os.path.join(work, "done.json"), done)
    final = tile_dir(out, tid)
    old = None
    if os.path.isdir(final):                                # --redo: the old result goes when the new one is in
        old = os.path.join(out, "work", "{}.old.{}".format(tid, os.getpid()))
        os.rename(final, old)
    if orphaned():
        if old:
            os.rename(old, final)
        shutil.rmtree(work, ignore_errors=True)
        return 3
    os.rename(work, final)
    if old:
        shutil.rmtree(old, ignore_errors=True)
    say("finished in", dur(time.time() - t0), "- peak memory", done["peak_rss_mb"], "MB")
    return 0


# ----------------------------------------------------------------------------- running tiles --
def spawn(t, job_base, out, share=None):
    job = dict(job_base, id=t.id, box=list(t.box), terrain_share=share)
    logp = os.path.join(out, "work", t.id + ".log")
    fh = open(logp, "w", encoding="utf-8")
    env = dict(os.environ)
    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
              "NUMEXPR_NUM_THREADS"):
        env.setdefault(k, "1")                              # one core per tile; --workers sets how many tiles
    env.setdefault("GDAL_CACHEMAX", GDAL_CACHE_MB)        # MB; the results do not depend on it
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--worker-job", json.dumps(job)],
                         stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
    return p, fh, logp


def tail(path, n=12):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return [ln.rstrip("\n") for ln in fh.readlines()[-n:]]
    except OSError:
        return []


FAIL_MARK = "TILE FAILED: "                 # the last line of the log of a tile that raised an error


def why_failed(rc, logp, timed_out=""):
    """One line on why a tile process did not finish: its own error line when it raised one, else
    how the process ended (a signal, an exit code) - never just the last line of its log, which
    for a killed process is whatever it happened to be doing."""
    if timed_out:
        return "stopped after {} (--tile-timeout-min)".format(timed_out)
    last = ([ln for ln in tail(logp, 3) if ln.strip()] or [""])[-1].strip()
    if last.startswith(FAIL_MARK):
        return last[len(FAIL_MARK):]
    if rc is not None and rc < 0:
        try:
            name = signal.Signals(-rc).name
        except ValueError:
            name = "signal {}".format(-rc)
        return "its process was ended from outside by {} (signal {}){}; the tile itself reported no error".format(
            name, -rc, " - killed: by hand, or by the system when memory ran out" if -rc == 9 else "")
    if rc == 3:
        return "stopped: the run that started it had gone"
    if rc == 0:
        return "its process ended without leaving a finished tile (no done.json)"
    return "its process ended with exit code {} and no error line{}".format(
        rc, "; the last line of its log: " + last[:200] if last else "")


def clear_failed(out, tid=None):
    """Take away failed/<tile>.log of a tile that has finished since (all of them with tid None),
    and the failed folder when it is empty: what is left there is what still fails."""
    d = os.path.join(out, "failed")
    if not os.path.isdir(d):
        return
    for name in ([tid + ".log"] if tid else sorted(os.listdir(d))):
        if name.endswith(".log") and marker(out, name[:-4]) is not None:
            try:
                os.remove(os.path.join(d, name))
            except OSError:
                pass
    try:
        os.rmdir(d)                                         # only when nothing is left in it
    except OSError:
        pass


def run_tiles(todo, a, out, job_base, counts, log):
    """Run the tiles, a.workers at a time, each in its own process. A tile that fails is tried once
    more after the others; a second failure is logged and the run goes on. Many failures in a row
    (the drive was unplugged) end the run. Returns {'ran': [...], 'failed': {id: why}, 'gave_up':
    bool}; raises Stop (after stopping the tile processes) on Ctrl-C."""
    queue = collections.deque(todo)
    running, tried, failed, ran = {}, set(), {}, []
    total, start = counts["total"], time.time()
    fills = counts["fills"]
    done_before = counts["done"]
    last_line = time.time()
    in_a_row = 0
    gave_up = False
    timeout = a.tile_timeout_min * 60.0

    def stop_running():
        for _tid, (p, fh, _logp, _t, _t0) in running.items():
            try:
                p.terminate()
            except OSError:
                pass
        t_end = time.time() + 15
        for _tid, (p, fh, _logp, _t, _t0) in running.items():
            try:
                p.wait(timeout=max(0.1, t_end - time.time()))
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
            fh.close()

    shares, sec_full = counts.get("shares") or {}, counts["sec_per_tile"]

    def line():
        # time left: the time so far, scaled by what is left against what has run - each tile
        # counted by its share of terrain, because the tiles at the edge of the terrain (the first
        # ones of a run) are many times quicker than a full one
        n_done = done_before + len(ran)
        left_ids = [t_.id for t_ in queue] + list(running)
        left = len(left_ids)
        cost_ran = sum(tile_cost(shares.get(q), sec_full) for q in ran)
        if not left:
            est = "none left"
        elif cost_ran >= 2.0 * sec_full:
            eta = (time.time() - start) * sum(tile_cost(shares.get(q), sec_full) for q in left_ids) / cost_ran
            when = (datetime.datetime.now() + datetime.timedelta(seconds=eta)).strftime("%a %H:%M")
            est = "about {} left (to {})".format(dur(eta), when)
        else:
            est = "about {} left (a first guess{})".format(
                dur(time_for([shares.get(q) for q in left_ids], sec_full, a.workers)),
                "" if counts.get("measured") else ", not yet this computer's own figure")
        log("  tiles {}/{} done, {} running, {} failed - {:,} fills so far - {} elapsed, {}".format(
            n_done, total, min(a.workers, left), len(failed), fills, dur(time.time() - start), est))

    try:
        while queue or running:
            while queue and len(running) < a.workers:
                t = queue.popleft()
                p, fh, logp = spawn(t, job_base, out, shares.get(t.id))
                running[t.id] = (p, fh, logp, t, time.time())
            time.sleep(0.5)
            for tid, (p, fh, logp, t, t0) in list(running.items()):
                rc = p.poll()
                timed_out = ""
                if rc is None:
                    if timeout and time.time() - t0 > timeout:
                        p.kill()
                        p.wait()
                        rc = -9
                        timed_out = dur(time.time() - t0)
                        with open(logp, "a", encoding="utf-8") as lf:
                            lf.write("stopped after {} (--tile-timeout-min)\n".format(timed_out))
                    else:
                        continue
                fh.close()
                del running[tid]
                m = marker(out, tid) if rc == 0 else None
                if m is not None:
                    try:
                        os.replace(logp, os.path.join(tile_dir(out, tid), tid + ".log"))
                    except OSError:
                        pass
                    clear_failed(out, tid)                  # it failed before (this run or an earlier one) and now ran
                    ran.append(tid)
                    fills += int(m.get("fills") or 0)
                    in_a_row = 0
                    log("{}: {} fill(s) in {}".format(tid, m.get("fills"), dur(m.get("seconds") or 0)))
                    line()
                    last_line = time.time()
                    continue
                os.makedirs(os.path.join(out, "failed"), exist_ok=True)
                keep = os.path.join(out, "failed", tid + ".log")
                try:
                    os.replace(logp, keep)
                except OSError:
                    pass
                why = why_failed(rc, keep, timed_out)
                try:
                    with open(keep, "a", encoding="utf-8") as lf:
                        lf.write("{}  why this tile counts as failed: {}\n".format(now(), why))
                except OSError:
                    pass
                in_a_row += 1
                if tid not in tried:
                    tried.add(tid)
                    queue.append(t)
                    log("{}: FAILED ({}); it will be tried once more. Log: {}".format(tid, why, keep))
                else:
                    failed[tid] = why
                    log("{}: FAILED again ({}); left out of this run. Log: {}".format(tid, why, keep))
                if in_a_row >= max(6, 2 * a.workers):
                    log("{} tile runs in a row failed: stopping. Is the terrain drive still connected? "
                        "See the logs in {}".format(in_a_row, os.path.join(out, "failed")))
                    stop_running()
                    for q in list(tried) + list(running):
                        if q not in ran:
                            failed.setdefault(q, "failed or stopped; not tried again in this run")
                    running.clear()
                    queue.clear()
                    gave_up = True
                    break
            if time.time() - last_line > 600:
                line()
                last_line = time.time()
    except (KeyboardInterrupt, Stop) as e:
        stop_running()
        raise Stop(str(e) or "stopped") from None
    return {"ran": ran, "failed": failed, "gave_up": gave_up}


def clean_work(out):
    """Remove what stopped tile processes left in work/ (never anything in tiles/)."""
    w = os.path.join(out, "work")
    if os.path.isdir(w):
        for name in os.listdir(w):
            p = os.path.join(w, name)
            if ".old." in name and os.path.isdir(p):
                tid = name.split(".old.")[0]              # a --redo stopped between its two renames
                if not os.path.isdir(tile_dir(out, tid)):
                    os.rename(p, tile_dir(out, tid))
                    continue
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            else:
                try:
                    os.remove(p)
                except OSError:
                    pass


def take_lock(out):
    path = os.path.join(out, "run.lock")
    old = read_json(path)
    if old and old.get("pid") and old.get("pid") != os.getpid():
        try:
            os.kill(int(old["pid"]), 0)
            alive = True
        except (OSError, ValueError):
            alive = False
        if alive:
            raise UserError("another run (process {}, started {}) is using {}. Wait for it or stop it; if "
                            "there is none, delete {}".format(old["pid"], old.get("started"), out, path))
    write_json(path, {"pid": os.getpid(), "started": now()})
    return path


# -------------------------------------------------------------------------------------- merge --
def finished_tiles(out, key):
    """([(id, marker)] of the tiles finished with these settings, sorted; [(id, marker)] of the tiles
    finished with others). The settings key holds the Finder version, so tiles made by two Finder
    versions are never in the first list together."""
    good, other = [], []
    base = os.path.join(out, "tiles")
    for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        m = read_json(os.path.join(base, name, "done.json"))
        if m is None:
            continue
        (good if m.get("key") == key else other).append((name, m))
    return good, other


def map_link(lat, lon):
    """A Google Maps link that opens the satellite picture centred on the point (the documented
    'Maps URLs' form; nothing is sent anywhere until a person clicks it)."""
    return "https://www.google.com/maps/@?api=1&map_action=map&center={:.6f}%2C{:.6f}&zoom=17&basemap=satellite".format(
        float(lat), float(lon))


def ids_are_stable(finder):
    """True when this Finder gives a fill the same find_id in every run, whatever the box: 0.7.0
    lays the 5 km squares and the cells of the resampled DEM on a fixed lattice (it has
    finder.lattice_squares). Up to 0.6.x both started at the corner of the run's box, so the crest
    point - and the id made from it - moved by a few metres with the box. The test is what the
    Finder has, with its version number as a second way to the same answer: a Finder that has
    the lattice is stable whatever its number says."""
    try:
        ver = tuple(int(t) for t in str(finder.VERSION).split(".")[:2])
    except ValueError:
        ver = (0, 0)
    return ver >= (0, 7) or hasattr(finder, "lattice_squares")


def exclude_areas(a):
    """--exclude-polygons FILE: (the union of its polygons in EPSG:5070 or None, what it is). The
    hook for ground where a fill is not a dam whatever its shape (coal-mine spoil, the sand hills):
    this script holds no such areas itself; a fill inside one keeps its row in 'embankments' with
    excluded = yes and is left out of the ranked lists."""
    choice = (getattr(a, "exclude_polygons", "") or "").strip()
    if not choice:
        return None, "none"
    if not os.path.exists(os.path.expanduser(choice)):
        raise UserError("--exclude-polygons {}: no such file".format(choice))
    geom = polygons_of(real(choice))
    if geom is None:
        raise UserError("--exclude-polygons {} holds no polygons".format(choice))
    return geom, "{} ({:,.0f} km2)".format(real(choice), geom.GetArea() / 1e6)


def other_versions(other):
    """'Finder 0.6.0: 3 tile(s)' for the tiles left out of a merge, by the Finder that made them."""
    c = collections.Counter(str((m.get("key") or {}).get("finder") or m.get("finder") or "?") for _n, m in other)
    return ", ".join("Finder {}: {} tile(s)".format(v, k) for v, k in sorted(c.items()))


def field_names(lyr):
    d = lyr.GetLayerDefn()
    return [d.GetFieldDefn(i).GetName() for i in range(d.GetFieldCount())]


def as_dict(f, names):
    """feature.items(), read by field number (by name the Python bindings search the field list
    for every value, which is most of the time of a merge)."""
    return {n: f.GetField(i) for i, n in enumerate(names)}


def tile_box(tid, m=None):
    """The square (xmin, ymin, xmax, ymax in EPSG:5070) of a finished tile: as its done.json has
    it, else read from its id (T10_W0370_N2620: 10 km, west edge at -370 km, south edge at 2,620 km)."""
    b = (m or {}).get("box")
    if b and len(b) == 4:
        return tuple(float(v) for v in b)
    km_, ew, ns = str(tid).split("_")
    km = int(km_[1:])
    x = (1.0 if ew[0] == "E" else -1.0) * int(ew[1:]) * 1000.0
    y = (1.0 if ns[0] == "N" else -1.0) * int(ns[1:]) * 1000.0
    return (x, y, x + km * 1000.0, y + km * 1000.0)


def polygons_only(g):
    """The polygons of a geometry (what a cut can hand back beside them - a line or a point where
    two outlines only touch - is dropped); None when it has none."""
    _g, ogr, _s = gdal_mods()
    if g is None:
        return None
    kind = ogr.GT_Flatten(g.GetGeometryType())
    if kind in (ogr.wkbPolygon, ogr.wkbMultiPolygon):
        return g if g.GetArea() > 0.0 else None
    multi = ogr.Geometry(ogr.wkbMultiPolygon)
    for i in range(g.GetGeometryCount()):
        h = polygons_only(g.GetGeometryRef(i))
        if h is None:
            continue
        if ogr.GT_Flatten(h.GetGeometryType()) == ogr.wkbPolygon:
            multi.AddGeometry(h)
        else:
            for j in range(h.GetGeometryCount()):
                multi.AddGeometry(h.GetGeometryRef(j))
    return multi if multi.GetGeometryCount() else None


class Ground:
    """The ground a run searched, for the crowded-ground note: the squares of its finished tiles
    (all of one size, on the lattice), cut to the State outline when there is one. share() says
    how much of the circle round a point lies on that ground.

    Every tile is 'whole' (all of it on the ground), 'part' (the outline cuts it; its piece is
    kept as a polygon) or not there. Nearly every fill of a statewide run has its circle in whole
    tiles only and is settled by four look-ups; only a fill within reach of a cut tile or of a
    tile that is not finished needs its circle cut with the pieces."""

    def __init__(self, boxes, outline=None):
        import numpy as np
        self.np = np
        boxes = [tuple(float(v) for v in b) for b in boxes]
        self.size = (boxes[0][2] - boxes[0][0]) if boxes else 1.0
        idx = [(int(round(b[0] / self.size)), int(round(b[1] / self.size))) for b in boxes]
        self.i0 = min([i for i, _j in idx] or [0]) - 1      # one empty tile all round
        self.j0 = min([j for _i, j in idx] or [0]) - 1
        self.state = np.zeros((max([i for i, _j in idx] or [0]) - self.i0 + 2,
                               max([j for _i, j in idx] or [0]) - self.j0 + 2), "i1")   # 0 none, 1 part, 2 whole
        self.piece, self.local, blocks = {}, {}, {}
        side = CROWD_BLOCK * self.size
        if outline is not None and not outline.IsValid():   # an outline that crosses itself cannot be cut
            mend = getattr(outline, "MakeValid", None)      # (GDAL 3.0 on; it keeps what in_state calls inside)
            outline = mend() if mend is not None else outline.Buffer(0)
        for (i, j), b in zip(idx, boxes):
            st, g = 2, None
            if outline is not None:
                bk = (i // CROWD_BLOCK, j // CROWD_BLOCK)
                if bk not in blocks:                        # the outline cut to this block of tiles, once
                    cut = polygons_only(outline.Intersection(box_geom((bk[0] * side, bk[1] * side, (bk[0] + 1) * side,
                                                                       (bk[1] + 1) * side))))
                    blocks[bk] = (cut, cut is not None and cut.GetArea() >= side * side - 1.0)
                cut, whole = blocks[bk]
                if cut is None:
                    st = 0
                elif not whole:
                    g = polygons_only(cut.Intersection(box_geom(b)))
                    st = 0 if g is None else 2 if g.GetArea() >= self.size * self.size - 1.0 else 1
            self.state[i - self.i0, j - self.j0] = st
            if st == 1:
                self.piece[(i - self.i0, j - self.j0)] = g

    def _cells(self, v, first, count):
        np = self.np
        return np.clip(np.floor(np.asarray(v, float) / self.size).astype(np.int64) - first, 0, count - 1)

    def state_at(self, xs, ys):
        """0 / 1 / 2 for the tile each point lies in: not searched, cut by the outline, whole."""
        return self.state[self._cells(xs, self.i0, self.state.shape[0]), self._cells(ys, self.j0, self.state.shape[1])]

    def on(self, xs, ys):
        """Bool per point: it lies on the searched ground. In a whole tile it does; in a tile the
        outline cuts it is tested against that tile's own piece of the outline (the same test
        in_state makes against the whole outline, on the few corners that matter here)."""
        np = self.np
        xs, ys = np.asarray(xs, float), np.asarray(ys, float)
        i = self._cells(xs, self.i0, self.state.shape[0])
        j = self._cells(ys, self.j0, self.state.shape[1])
        st = self.state[i, j]
        out = st == 2
        cutq = np.nonzero(st == 1)[0]
        if len(cutq):
            code = i[cutq] * self.state.shape[1] + j[cutq]
            order = np.argsort(code, kind="stable")
            cutq, code = cutq[order], code[order]
            ends = np.nonzero(np.diff(code))[0] + 1
            for q in np.split(cutq, ends):                  # the fills of one cut tile
                rings = rings_of(self.piece[(int(i[q[0]]), int(j[q[0]]))])
                for lo in range(0, len(q), 2000):
                    out[q[lo:lo + 2000]] = points_in_rings(xs[q[lo:lo + 2000]], ys[q[lo:lo + 2000]], rings)
        return out

    def _local(self, key):
        """The pieces of ground in the (up to four) tiles a circle's box reaches, as a list of
        polygons that do not overlap (one, when they could be joined)."""
        if key not in self.local:
            a, b, c, d = key
            parts = []
            for cell in sorted(set(((a, c), (a, d), (b, c), (b, d)))):
                st = int(self.state[cell])
                if st == 2:
                    x, y = (cell[0] + self.i0) * self.size, (cell[1] + self.j0) * self.size
                    parts.append(box_geom((x, y, x + self.size, y + self.size)))
                elif st == 1:
                    parts.append(self.piece[cell])
            if len(parts) > 1:
                u = parts[0]
                for h in parts[1:]:
                    u = u.Union(h) if u is not None else None
                if u is not None:
                    parts = [u]
            self.local[key] = parts
        return self.local[key]

    def share(self, xs, ys, radius=CROWD_RADIUS_M):
        """For every point the share (0 to 1) of the circle of `radius` round it that lies on the
        ground. The circle is a 64-sided ring of the circle's own area (so a circle wholly on the
        ground is exactly 1); the answer is as exact as that ring is a circle (its outline is
        within 1 m of the circle's for 1,000 m)."""
        np = self.np
        _g, ogr, _s = gdal_mods()
        xs, ys = np.asarray(xs, float), np.asarray(ys, float)
        out = np.zeros(len(xs))
        if not len(xs):
            return out
        if radius > self.size:
            raise ValueError("the radius must not be larger than a tile")
        ni, nj = self.state.shape
        a, b = self._cells(xs - radius, self.i0, ni), self._cells(xs + radius, self.i0, ni)
        c, d = self._cells(ys - radius, self.j0, nj), self._cells(ys + radius, self.j0, nj)
        st = np.stack([self.state[a, c], self.state[a, d], self.state[b, c], self.state[b, d]])
        whole = (st == 2).all(axis=0)
        out[whole] = 1.0
        full = math.pi * radius * radius
        ring = radius * math.sqrt(math.pi / (32.0 * math.sin(math.pi / 32.0)))   # 64 sides, the circle's area
        pt = ogr.Geometry(ogr.wkbPoint)
        pt.AddPoint_2D(0.0, 0.0)
        for q in np.nonzero(~whole & (st != 0).any(axis=0))[0].tolist():
            parts = self._local((int(a[q]), int(b[q]), int(c[q]), int(d[q])))
            pt.SetPoint_2D(0, float(xs[q]), float(ys[q]))
            circle = pt.Buffer(ring, 16)
            area = 0.0
            for g in parts:
                cut = circle.Intersection(g)
                area += cut.GetArea() if cut is not None else 0.0
            v = area / full
            out[q] = 1.0 if v > 1.0 - 1e-9 else max(0.0, v)   # all of it, to the last rounding of the cutting
        return out


def near_counts(xs, ys, member, radius=CROWD_RADIUS_M):
    """For every point: how many OTHER points of the set `member` (a bool per point) lie within
    `radius` of it (at the radius counts as within)."""
    import numpy as np
    from scipy.spatial import cKDTree
    xs, ys, member = np.asarray(xs, float), np.asarray(ys, float), np.asarray(member, bool)
    if not member.any():
        return np.zeros(len(xs), np.int64)
    tree = cKDTree(np.column_stack([xs[member], ys[member]]))
    pts = np.column_stack([xs, ys])
    try:
        n = np.asarray(tree.query_ball_point(pts, radius, return_length=True), np.int64)
    except TypeError:                                       # a scipy older than 1.3: it hands back the lists
        n = np.fromiter((len(q) for q in tree.query_ball_point(pts, radius)), np.int64, len(pts))
    return n - member                                       # a member found itself


def crowd_notes(pts, drop, boxes, outline, cut_km2, log):
    """The crowded-ground note (see the module text) for every fill the tiles hold.
    pts: what cross_tile_rules kept of each fill, in tile order (ti = tile number, x, y, pool =
    on a pond line, dd = dam or dry embankment); drop: the fills removed across seams; boxes: the
    tiles' squares; outline: the State outline in_state is tested against (None: no outline).
    Returns {'start': the first fill of each tile, so that fill j of tile k is start[k] + j,
    'near_n', 'near_km2', 'crowded' (bool), 'share' (of each circle on the searched ground),
    'counted' (the pond-line dam / dry fills on the searched ground), 'part' (the fills on it
    whose circle is not all on it)}; the fills in drop carry nothing of use and are never
    written."""
    import numpy as np
    t0 = time.time()
    ti, x, y = pts["ti"], pts["x"], pts["y"]
    pond = pts["pool"].astype(bool) & pts["dd"].astype(bool)
    start = np.searchsorted(ti, np.arange(len(boxes) + 1))
    alive = np.ones(len(x), bool)
    for k, j in drop:
        alive[start[k] + j] = False
    ground = Ground(boxes, outline)
    on = ground.on(x, y)
    member = alive & on & pond
    near = near_counts(x, y, member, CROWD_RADIUS_M)
    share = ground.share(x, y, CROWD_RADIUS_M)
    dens = near / (math.pi * (CROWD_RADIUS_M / 1000.0) ** 2 * np.maximum(share, CROWD_MIN_SHARE))
    crowded = (pond & (dens >= cut_km2)) if cut_km2 > 0 else np.zeros(len(x), bool)
    # written cut to one decimal, not rounded: 25 others in a whole circle are 7.96 per km2, which
    # is under 8 and is written 7.9 - a figure written 8.0 is always 8 or more
    km2 = np.floor(dens * 10.0 + 1e-9) / 10.0
    part = int(np.count_nonzero(alive & on & (share < 1.0)))
    log("  crowded ground: {:,} of the {:,} fill(s) on the searched ground are pond-line dams / dry embankments "
        "(the ones counted); for {:,} fill(s) on it part of the 1 km circle was not searched (a tile not "
        "finished, beyond the State outline) and is left out of the area ({})".format(
            int(np.count_nonzero(member)), int(np.count_nonzero(alive & on)), part, dur(time.time() - t0)))
    return {"start": start, "near_n": near, "near_km2": km2, "crowded": crowded, "share": share,
            "counted": int(np.count_nonzero(member)), "part": part}


def cross_tile_rules(finder, tiles, out, log, pts=None):
    """The engine's one-fill-per-pool and 60 m rules across tile seams (see the module text).
    Reads only a few numbers per fill. Returns (drop, same_pool, n_total): drop = {(tile index,
    row index)} of the fills another tile's fill replaces, same_pool = {(tile, row): new count}.
    pts: a dict that is given what the crowded-ground note needs of every fill read, in the order
    read (ti, x, y, pool, dd: see crowd_notes)."""
    import numpy as np
    _g, ogr, _s = gdal_mods()
    em = finder.em
    p = em.FinderParams()
    ti, ri = array.array("l"), array.array("l")
    xs, ys, hs = array.array("d"), array.array("d"), array.array("d")
    sx, sy, sm, od, sd = (array.array("d") for _ in range(5))
    pool, same, damdry = array.array("b"), array.array("l"), array.array("b")
    dam_or_dry = (em.K_DAM, em.K_DRY)
    nan = float("nan")

    def num(v):
        return nan if v is None else float(v)

    for k, (tid, _m) in enumerate(tiles):
        ds = ogr.Open(os.path.join(tile_dir(out, tid), tid + ".gpkg"), 0)
        if ds is None:
            raise RuntimeError("cannot open the result of tile {}".format(tid))
        spill = {}
        sl = ds.GetLayerByName("spillways")
        if sl is not None:
            i_id = sl.GetLayerDefn().GetFieldIndex("find_id")
            for f in sl:
                g = f.GetGeometryRef()
                if g is not None:
                    spill[f.GetField(i_id)] = (g.GetX(), g.GetY())
        lyr = ds.GetLayerByName("embankments")
        d = lyr.GetLayerDefn()
        i_id, i_h, i_sp, i_od, i_sd, i_ls, i_same, i_kind = (d.GetFieldIndex(n_) for n_ in (
            "find_id", "height_ft", "spill_ft", "outlet_dist_ft", "spill_dist_ft", "line_source", "same_pool", "kind"))
        j = 0
        for f in lyr:
            g = f.GetGeometryRef()
            s = spill.get(f.GetField(i_id))
            ti.append(k)
            ri.append(j)
            xs.append(g.GetX())
            ys.append(g.GetY())
            hs.append(num(f.GetField(i_h)))
            sx.append(s[0] if s else nan)
            sy.append(s[1] if s else nan)
            sm.append(num(f.GetField(i_sp)) * FT)
            od.append(num(f.GetField(i_od)) * FT)
            sd.append(num(f.GetField(i_sd)) * FT)
            pool.append(1 if f.GetField(i_ls) == finder.LINE_POOL else 0)
            same.append(int(f.GetField(i_same) or 0))
            damdry.append(1 if f.GetField(i_kind) in dam_or_dry else 0)
            j += 1
        ds = None
    n = len(xs)
    if pts is not None:
        pts.update(ti=np.array(ti, np.int64), x=np.array(xs, float), y=np.array(ys, float),
                   pool=np.array(pool, bool), dd=np.array(damdry, bool))
    drop, same_new = set(), {}
    if n < 2:
        return drop, same_new, n
    from scipy.spatial import cKDTree
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    A = {name: np.frombuffer(arr, dtype=dt) for name, arr, dt in (
        ("ti", ti, np.dtype("l")), ("x", xs, "d"), ("y", ys, "d"), ("h", hs, "d"), ("sx", sx, "d"),
        ("sy", sy, "d"), ("sm", sm, "d"), ("od", od, "d"), ("sd", sd, "d"), ("pool", pool, "b"))}
    alive = np.ones(n, bool)

    def groups(px, py, idx, radius):
        """Lists of fill indices linked by pairs closer than radius, for the groups that hold fills
        of more than one tile (a group of one tile's fills is as the engine left it)."""
        if len(idx) < 2:
            return []
        pairs = cKDTree(np.column_stack([px, py])).query_pairs(radius, output_type="ndarray")
        if not len(pairs):
            return []
        m = len(idx)
        g = coo_matrix((np.ones(len(pairs), "i1"), (pairs[:, 0], pairs[:, 1])), shape=(m, m))
        _k, lab = connected_components(g, directed=False)
        in_pair = np.zeros(m, bool)
        in_pair[pairs.ravel()] = True
        by = {}
        for loc in np.nonzero(in_pair)[0]:
            by.setdefault(int(lab[loc]), []).append(int(idx[loc]))
        return [sorted(v) for v in by.values() if len(set(A["ti"][v].tolist())) > 1]

    # 1. one fill per pool: the pools that get out at the same spill point (2 cells, same level)
    idx = np.nonzero(np.isfinite(A["sx"]))[0]
    n_pool = 0
    for grp in groups(A["sx"][idx], A["sy"][idx], idx, 2.0 * CALL["cell"] + 1e-9):
        cands = [types.SimpleNamespace(i=i, spill_xy=(float(A["sx"][i]), float(A["sy"][i])), spill_m=float(A["sm"][i]),
                                       outlet_dist_m=float(A["od"][i]),
                                       spill_dist_m=float(A["sd"][i]) if math.isfinite(A["sd"][i]) else 0.0,
                                       pool_line=bool(A["pool"][i]), height_m=float(A["h"][i]) * FT,
                                       same_pool=int(same[i]), notes=[]) for i in grp]
        kept = em.one_per_pool(cands, CALL["cell"])
        kept_i = set(c.i for c in kept)
        for c in kept:
            if c.same_pool != int(same[c.i]):
                same_new[(int(A["ti"][c.i]), int(ri[c.i]))] = c.same_pool
        for i in grp:
            if i not in kept_i:
                alive[i] = False
                drop.add((int(A["ti"][i]), int(ri[i])))
                n_pool += 1
    # 2. no two fills within dedupe_m (60 m): the higher one stays (a pool-outlet reading goes first)
    idx = np.nonzero(alive)[0]
    n_near = 0
    for grp in groups(A["x"][idx], A["y"][idx], idx, p.dedupe_m):
        cands = [types.SimpleNamespace(i=i, x=float(A["x"][i]), y=float(A["y"][i]), pool_line=bool(A["pool"][i]),
                                       height_m=float(A["h"][i]) * FT) for i in grp]
        kept_i = set(c.i for c in em.dedupe(cands, p.dedupe_m))
        for i in grp:
            if i not in kept_i:
                drop.add((int(A["ti"][i]), int(ri[i])))
                same_new.pop((int(A["ti"][i]), int(ri[i])), None)
                n_near += 1
    log("  across tile seams: {} fill(s) on a pool another tile's fill holds, {} within {:.0f} m of another "
        "tile's fill: removed".format(n_pool, n_near, p.dedupe_m))
    return drop, same_new, n


def merge(a, finder, out, key, dem_path, outline, outline_what, inputs, plan_note, model_path, log, exclude=None):
    """One GeoPackage of every fill of the finished tiles, scored, with the ranked candidates, the
    ranked CSV, the short list (the top a.top rows with a map link) and the summary. Tile by tile:
    the rows of one tile are in memory at a time; the seam rules and the ranking keep a few numbers
    per fill. exclude: (polygon, what it is) from exclude_areas, or None."""
    import numpy as np
    gdal, ogr, _s = gdal_mods()
    t0 = time.time()
    tiles, other = finished_tiles(out, key)
    if not tiles:
        raise UserError("no finished tile in {} to merge{}".format(
            out, " ({} were made with other settings - {} - and are not merged by this Finder, {}: --redo "
                 "runs them again)".format(len(other), other_versions(other), finder.VERSION) if other else ""))
    # One merged result holds the fills of ONE Finder version. The settings key sees to that (it
    # holds the version); this is the same rule said once more against a done.json changed by hand.
    made_by = sorted(set(str(m.get("finder") or (m.get("key") or {}).get("finder")) for _t, m in tiles))
    if made_by != [str(finder.VERSION)]:
        raise UserError("the finished tiles in {} were made by Finder {} and this is Finder {}: tiles of "
                        "different Finder versions are never merged into one result. Run them again with "
                        "--redo, or merge with the Finder that made them.".format(
                            out, " and ".join(made_by), finder.VERSION))
    log("Merging {} finished tile(s) from {} (all made by Finder {})".format(
        len(tiles), os.path.join(out, "tiles"), finder.VERSION))
    if other:
        log("  {} finished tile(s) made with other settings are NOT merged ({})".format(
            len(other), other_versions(other)))
    pts = {}
    drop, same_new, n_all = cross_tile_rules(finder, tiles, out, log, pts)
    model = None
    if not a.no_score:
        from importlib import import_module
        sc = import_module(ENGINE + ".core.score")
        model = sc.load(model_path)
    rings = rings_of(outline) if outline is not None else None
    ex_geom, ex_what = exclude if exclude else (None, "none")
    ex_rings = rings_of(ex_geom) if ex_geom is not None else None
    crowd_cut = max(0.0, float(getattr(a, "crowd_km2", CROWD_KM2_DEFAULT) or 0.0))
    crowd = crowd_notes(pts, drop, [tile_box(tid, m) for tid, m in tiles], outline, crowd_cut, log)
    del pts
    edge_lim = float(a.edge_m)
    k_edge, k_edge_m, k_edge_far = edge_rule(finder)
    edge_tiles = collections.Counter()                      # tiles by who decided at_data_edge
    engine_names = set(n_ for n_, _t in finder.EMB_FIELDS)
    fields = [f for f in finder.EMB_FIELDS] + [f for f in EXTRA_FIELDS if f[0] not in engine_names]
    kinds = {"str": ogr.OFTString, "float": ogr.OFTReal, "int": ogr.OFTInteger}
    conv = {"str": str, "float": float, "int": int}
    final = os.path.join(out, OUT_GPKG)
    part = os.path.join(out, "work", "merge_{}.gpkg".format(os.getpid()))
    os.makedirs(os.path.dirname(part), exist_ok=True)
    for q in (part, part + "-wal", part + "-shm", part + "-journal"):
        if os.path.exists(q):
            os.remove(q)
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(part)
    if ds is None:
        raise RuntimeError("cannot create {}".format(part))
    srs = srs_of(WORK_EPSG)
    lyr = ds.CreateLayer("embankments", srs, ogr.wkbPoint)
    for name, t in fields:
        lyr.CreateField(ogr.FieldDefn(name, kinds[t]))
    spl = ds.CreateLayer("spillways", srs, ogr.wkbPoint)
    for name, t in finder.SPILL_FIELDS + [("tile", "str")]:
        spl.CreateField(ogr.FieldDefn(name, kinds[t]))
    rdl = ds.CreateLayer("ridges", srs, ogr.wkbPoint)
    for name, t in finder.RIDGE_FIELDS + [("tile", "str")]:
        rdl.CreateField(ogr.FieldDefn(name, kinds[t]))
    dfn = lyr.GetLayerDefn()
    fields_i = [(name, t, dfn.GetFieldIndex(name)) for name, t in fields]
    seen, clash = {}, []
    stat = collections.Counter()
    by_status, by_kind, by_kind_status = collections.Counter(), collections.Counter(), collections.Counter()
    secs, cpus, peak = [], [], 0.0
    ds.StartTransaction()
    written = 0
    for k, (tid, m) in enumerate(tiles):
        secs.append(float(m.get("seconds") or 0.0))
        cpus.append(float(m.get("cpu_seconds") or 0.0))
        peak = max(peak, float(m.get("peak_rss_mb") or 0.0))
        src = ogr.Open(os.path.join(tile_dir(out, tid), tid + ".gpkg"), 0)
        rows = []
        sl_ = src.GetLayerByName("embankments")
        names = field_names(sl_)
        for j, f in enumerate(sl_):
            r = as_dict(f, names)
            g = f.GetGeometryRef()
            r["x"], r["y"], r["_j"] = g.GetX(), g.GetY(), j
            rows.append(r)
        edge = m.get("edge_m") or []
        if len(edge) != len(rows) or any(e[0] != r["find_id"] for e, r in zip(edge, rows)):
            raise RuntimeError("tile {}: done.json does not match its GeoPackage; run it again "
                               "(delete {})".format(tid, tile_dir(out, tid)))
        # The edge of the terrain data. A tile whose rows carry the Finder's own edge_ft was made by
        # a Finder that decides it itself (0.7.0): a fill is at the edge when the Finder gave it
        # its edge kind - near missing terrain AND its pool reaches it; the distance alone does not
        # make it one, and --edge-m is not used. Decided tile by tile, by what the tile holds, so
        # that a tile of a Finder without the test (0.6.x: no edge_ft; this script measured the
        # distance, done.json) is still merged its own way: within --edge-m of missing terrain.
        engine_edge = "edge_ft" in names
        by_kind_ = engine_edge and k_edge is not None
        edge_tiles["finder" if by_kind_ else "script"] += 1
        for r, e in zip(rows, edge):
            r["_edge"] = (None if r.get("edge_ft") is None else float(r["edge_ft"]) * FT) if engine_edge else e[1]
        rows = [r for r in rows if (k, r["_j"]) not in drop]
        gone = set()
        if len(rows) != len(edge):
            keep_ids = set(r["find_id"] for r in rows)
            gone = set(e[0] for e in edge) - keep_ids
        if rows:
            ins = points_in_rings([r["x"] for r in rows], [r["y"] for r in rows], rings) if rings is not None \
                else np.ones(len(rows), bool)
            exs = points_in_rings([r["x"] for r in rows], [r["y"] for r in rows], ex_rings) if ex_rings is not None \
                else np.zeros(len(rows), bool)
            det = sc.score_detail(model, rows) if model is not None else None
            for i, r in enumerate(rows):
                r["tile"] = tid
                if not engine_edge:
                    r["edge_ft"] = None if r["_edge"] is None else round(r["_edge"] / FT, 0)
                if by_kind_:
                    r["at_data_edge"] = "yes" if r["kind"] == k_edge else "no"
                else:
                    r["at_data_edge"] = "yes" if r["_edge"] is not None and r["_edge"] <= edge_lim else "no"
                r["in_state"] = "yes" if ins[i] else "no"
                r["excluded"] = "yes" if exs[i] else "no"
                gi = int(crowd["start"][k]) + r["_j"]       # the note is worked out before this pass
                r["near_n"], r["near_km2"] = int(crowd["near_n"][gi]), float(crowd["near_km2"][gi])
                r["crowded"] = "yes" if crowd["crowded"][gi] else "no"
                if det is not None and k_edge is not None and r["kind"] == k_edge:
                    # as the Finder itself does: no score for a fill whose readings lean on a wall
                    r["score"], r["score_note"] = None, EDGE_NOT_SCORED
                elif det is not None:
                    r["score"] = round(float(det[i][0]), 3)
                    r["score_note"] = det[i][1]
                else:
                    r["score"], r["score_note"] = None, None
                new = same_new.get((k, r["_j"]))
                if new is not None:
                    notes = [t for t in str(r.get("notes") or "").split("; ") if t
                             and not t.endswith("(one fill per pool)")]
                    notes.append("{} other hump(s) on this pool dropped (one fill per pool)".format(new))
                    r["same_pool"], r["notes"] = new, "; ".join(notes)
                feat = ogr.Feature(dfn)
                for name, t, fi in fields_i:
                    v = r.get(name)
                    if v is None or (isinstance(v, float) and not math.isfinite(v)):
                        continue                            # as the engine writes them: '' stays ''
                    feat.SetField(fi, conv[t](v))
                g = ogr.Geometry(ogr.wkbPoint)
                g.AddPoint_2D(r["x"], r["y"])
                feat.SetGeometry(g)
                if lyr.CreateFeature(feat) != 0:
                    raise RuntimeError("writing {} failed (is the disk full?)".format(part))
                fid = feat.GetFID()
                if r["find_id"] in seen:
                    clash.append((seen[r["find_id"]], fid))
                else:
                    seen[r["find_id"]] = fid
                written += 1
                use = r["in_state"] == "yes" and r["at_data_edge"] == "no" and r["excluded"] == "no"
                stat["all"] += 1
                stat["out_of_state"] += r["in_state"] == "no"
                stat["at_edge"] += r["at_data_edge"] == "yes"
                stat["excluded"] += r["excluded"] == "yes"
                if use:
                    by_status[r["status"]] += 1
                    by_kind[r["kind"]] += 1
                    by_kind_status[(r["kind"], r["status"])] += 1
        for layer, dst in (("spillways", spl), ("ridges", rdl)):
            sl = src.GetLayerByName(layer)
            if sl is None:
                continue
            ddf = dst.GetLayerDefn()
            i_tile, i_id = ddf.GetFieldIndex("tile"), sl.GetLayerDefn().GetFieldIndex("find_id")
            for f in sl:
                if layer == "spillways" and gone and f.GetField(i_id) in gone:
                    continue
                g = ogr.Feature(ddf)
                g.SetFrom(f)
                g.SetField(i_tile, tid)
                dst.CreateFeature(g)
        src = None
        if (k + 1) % 50 == 0:
            ds.CommitTransaction()
            ds.StartTransaction()
            log("  {}/{} tiles, {:,} fills".format(k + 1, len(tiles), written))
    ds.CommitTransaction()
    # two different fills with one find_id (the id is a 32-bit hash of the point): both are kept
    if clash:
        ds.StartTransaction()
        i_clash = dfn.GetFieldIndex("id_clash")
        for pair in clash:
            for fid in pair:
                f = lyr.GetFeature(fid)
                f.SetField(i_clash, "yes")
                lyr.SetFeature(f)
        ds.CommitTransaction()
    # the ranked candidates: unlisted, dam or dry embankment, in the state, not at the data edge,
    # not in an --exclude-polygons area. (A fill of the Finder's edge kind is out twice over: by
    # its kind and by at_data_edge. A score can be missing only with --no-score, when the order is
    # by height.)
    kd = finder.em
    order = "score DESC, height_ft DESC, find_id, tile" if model is not None else "height_ft DESC, find_id, tile"
    sql = ("SELECT fid FROM embankments WHERE status <> 'listed' AND kind IN ('{}', '{}') AND in_state = 'yes' "
           "AND at_data_edge = 'no' AND excluded = 'no' ORDER BY {}".format(kd.K_DAM, kd.K_DRY, order))
    res = ds.ExecuteSQL(sql)
    if res is None:
        raise RuntimeError("ranking the candidates failed: {}".format(gdal.GetLastErrorMsg()))
    ranked = array.array("q")
    for f in res:
        ranked.append(f.GetField(0) if f.GetFieldCount() else f.GetFID())
    ds.ReleaseResultSet(res)
    cl = ds.CreateLayer("candidates", srs, ogr.wkbPoint)
    cfields = [("rank", "int")] + fields
    for name, t in cfields:
        cl.CreateField(ogr.FieldDefn(name, kinds[t]))
    cdf = cl.GetLayerDefn()
    i_rank, emb_names = cdf.GetFieldIndex("rank"), field_names(lyr)
    first = ["rank", "score", "find_id", "kind", "status", "lat", "lon", "height_ft", "storage_af", "area_ac",
             "drain_ac", "length_ft", "known_id", "known_layer", "known_ft", "score_note", "tile"]
    cols = first + [n for n, _t in cfields if n not in first]
    csv_part = os.path.join(out, "work", "ranked_{}.csv".format(os.getpid()))
    top_part = os.path.join(out, "work", "top_{}.csv".format(os.getpid()))
    n_top = max(0, int(getattr(a, "top", TOP_DEFAULT) or 0))
    top, n_cut, n_short, n_crowd, n_crowd_top = [], 0, 0, 0, 0
    with open(csv_part, "w", newline="", encoding="utf-8") as fh, \
            open(top_part, "w", newline="", encoding="utf-8") as th:
        w = csv.writer(fh)
        w.writerow(cols)
        tw = csv.writer(th)                                # the short list: the same rows, fewer columns, a map link
        tw.writerow(TOP_COLUMNS)
        ds.StartTransaction()
        for rank, fid in enumerate(ranked, 1):
            f = lyr.GetFeature(fid)
            g = ogr.Feature(cdf)
            g.SetFrom(f)
            g.SetField(i_rank, rank)
            cl.CreateFeature(g)
            vals = as_dict(f, emb_names)
            vals["rank"] = rank
            w.writerow(["" if vals.get(c) is None else vals.get(c) for c in cols])
            if vals.get("score") is not None and vals["score"] >= SCORE_CUT:
                n_cut += 1
            if vals.get("crowded") == "yes":
                n_crowd += 1
                n_crowd_top += rank <= n_top
            if rank <= n_top:
                vals["link"] = map_link(vals["lat"], vals["lon"])
                tw.writerow(["" if vals.get(c) is None else vals.get(c) for c in TOP_COLUMNS])
                n_short += 1
            if rank <= 10:
                top.append(vals)
            if rank % 20000 == 0:
                ds.CommitTransaction()
                ds.StartTransaction()
        ds.CommitTransaction()
    # the record
    plan_note = dict(plan_note or {})
    info = collections.OrderedDict()
    info["made"] = now()
    info["script"] = "run_finder_statewide.py {}".format(SCRIPT_VERSION)
    info["finder_version"] = finder.VERSION
    info["find_ids"] = ("stable: this Finder gives a fill the same id in every run over the same terrain and "
                        "settings, whatever the area of the run; ids do not carry over between Finder versions"
                        if ids_are_stable(finder) else
                        "NOT stable between runs with this Finder (stable from Finder 0.7.0 on): match marks made "
                        "on another run to these fills by location, not by id")
    info["gdal"] = gdal.__version__
    info["numpy"] = np.__version__
    import scipy
    info["scipy"] = scipy.__version__
    info["python"] = sys.version.split()[0]
    info["terrain"] = dem_path
    info["tile_km"] = a.tile_km
    info["call"] = "finder.run_embankments(" + ", ".join("{}={}".format(k_, v) for k_, v in sorted(CALL.items())) \
        + ", bbox=<tile>, the six known layers; everything else the Finder's defaults)"
    info["state_outline"] = outline_what
    by_finder = ("the Finder's own rule: at_data_edge = yes for a fill of its kind '{}' ({} missing terrain that "
                 "its pool reaches; edge_ft is the Finder's figure{}); --edge-m is not used".format(
                     k_edge, "near" if k_edge_m is None else "within {:.0f} m of".format(k_edge_m),
                     "" if k_edge_far is None else ", looked for to {:,.0f} m".format(k_edge_far)))
    by_script = ("this script's own test, for a Finder that has none: at_data_edge = yes within --edge-m = {:.0f} m "
                 "of missing terrain (looked for to {:.0f} m, holes under {:.0f} m2 ignored)".format(
                     edge_lim, EDGE_SEARCH_M, EDGE_SPECK_M2))
    if edge_tiles["finder"] and edge_tiles["script"]:
        info["data_edge"] = "{} tile(s): {}; {} tile(s) made without the Finder's edge test: {}".format(
            edge_tiles["finder"], by_finder, edge_tiles["script"], by_script)
    else:
        info["data_edge"] = by_finder if edge_tiles["finder"] else by_script
    if edge_tiles["script"]:                                # as script 1.0 and 1.1 recorded it
        info["edge_m"] = "{:.0f} (at_data_edge = yes within this of missing terrain; searched to {:.0f} m, holes " \
                         "under {:.0f} m2 ignored)".format(edge_lim, EDGE_SEARCH_M, EDGE_SPECK_M2)
    info["score_model"] = "none (--no-score)" if model is None else model_path
    info["tiles_merged"] = len(tiles)
    for k_, v in plan_note.items():
        info[k_] = v
    if other:
        info["tiles_left_out_other_settings"] = "{} ({})".format(len(other), other_versions(other))
    info["fills_read"] = n_all
    info["fills_removed_across_seams"] = len(drop)
    info["fills"] = stat["all"]
    info["fills_outside_state"] = stat["out_of_state"]
    info["fills_at_data_edge"] = stat["at_edge"]
    info["exclude_polygons"] = ex_what
    info["fills_excluded"] = stat["excluded"]
    info["candidates_ranked"] = len(ranked)
    info["candidates_score_{}_or_more".format(SCORE_CUT)] = n_cut if model is not None else "not scored"
    info["short_list_rows"] = n_short
    info["crowd_km2"] = ("{:g} (a note only: crowded = yes for a pond-line dam or dry embankment with this many or "
                         "more OTHER pond-line dam / dry fills per km2 of searched ground within {:,.0f} m; no row is "
                         "left out and the order is not changed)".format(crowd_cut, CROWD_RADIUS_M) if crowd_cut > 0
                         else "0 (off: crowded is no for every fill; near_n and near_km2 are still written)")
    info["candidates_crowded"] = n_crowd
    info["short_list_crowded"] = n_crowd_top
    info["crowd_fills_counted"] = crowd["counted"]
    info["crowd_fills_circle_partly_unsearched"] = crowd["part"]
    info["find_id_clashes"] = len(clash)
    info["tile_seconds_total"] = round(sum(secs))
    info["tile_seconds_mean"] = round(sum(secs) / len(secs), 1)
    info["tile_seconds_max"] = round(max(secs), 1)
    info["tile_cpu_seconds_total"] = round(sum(cpus))
    info["tile_peak_memory_mb_max"] = round(peak)
    sums = collections.OrderedDict()
    if not a.no_checksums:
        files = list(inputs.items()) + ([("score model", model_path)] if model is not None else [])
        if os.path.isfile(dem_path) and os.path.getsize(dem_path) < 512 * 1024 * 1024 \
                and dem_path.lower().endswith(".vrt"):
            files.append(("terrain VRT (the list of tiles, not the tiles)", dem_path))
        for role, p in files:
            sums[role] = "{}  {}".format(sha256(p), p)
    il = ds.CreateLayer("run_info", None, ogr.wkbNone)
    il.CreateField(ogr.FieldDefn("key", ogr.OFTString))
    il.CreateField(ogr.FieldDefn("value", ogr.OFTString))
    for k_, v in list(info.items()) + [("sha256 " + r_, v_) for r_, v_ in sums.items()]:
        f = ogr.Feature(il.GetLayerDefn())
        f.SetField("key", str(k_))
        f.SetField("value", str(v))
        il.CreateFeature(f)
    ds = None
    os.replace(part, final)
    os.replace(csv_part, os.path.join(out, OUT_CSV))
    if n_top:
        os.replace(top_part, os.path.join(out, OUT_TOP))
    else:                                                   # --top 0: no short list, and no stale one
        os.remove(top_part)
        if os.path.exists(os.path.join(out, OUT_TOP)):
            os.remove(os.path.join(out, OUT_TOP))
    # the summary text
    L = []
    L.append("ND Finder statewide run - summary ({})".format(info["made"]))
    L.append("Finder version {} (every merged tile was made by it; one result never mixes Finder versions)".format(
        finder.VERSION))
    L.append("find_id: {}".format(
        "a fill has the same id in every run with this Finder, whatever area the run covers (it works on the "
        "fixed lattice, as every Finder from 0.7.0 on). Marks made with another Finder version are matched by "
        "location (lat / lon), not by id." if ids_are_stable(finder) else
        "with this Finder the id of a fill changes with the box of a run. Match marks made on another run "
        "to these fills by location (lat / lon), not by id. From Finder 0.7.0 on the ids are the same in "
        "every run."))
    L.append("")
    if plan_note.get("tiles_still_to_run") or plan_note.get("tiles_failed_this_run"):
        L.append("PARTIAL RESULT: {} tile(s) still to run, {} failed in this run. Run the same command again."
                 .format(plan_note.get("tiles_still_to_run", 0), plan_note.get("tiles_failed_this_run", 0)))
        L.append("")
    L.append("Fills found (all tiles merged, duplicates across tile seams removed): {:,}".format(stat["all"]))
    L.append("  outside North Dakota (in_state = no):            {:,}".format(stat["out_of_state"]))
    if edge_tiles["finder"] and not edge_tiles["script"]:
        L.append("  at the edge of the terrain data (at_data_edge):  {:,}".format(stat["at_edge"]))
        L.append("    (the Finder's own kind: {} missing terrain that the fill's pool reaches. Kept, not scored,"
                 .format("near" if k_edge_m is None else "within {:.0f} m of".format(k_edge_m)))
        L.append("    not ranked. A fill that near whose pool stays clear of the missing terrain keeps its kind.)")
    elif edge_tiles["finder"]:
        L.append("  at the edge of the terrain data (at_data_edge):  {:,}".format(stat["at_edge"]))
        L.append("    ({} tile(s) by the Finder's own kind, {} tile(s) made without that test by distance alone,"
                 .format(edge_tiles["finder"], edge_tiles["script"]))
        L.append("    within {:.0f} m of missing terrain)".format(edge_lim))
    else:
        L.append("  within {:.0f} m of missing terrain (at_data_edge):   {:,}".format(edge_lim, stat["at_edge"]))
    if ex_geom is not None:
        L.append("  inside an --exclude-polygons area (excluded):    {:,}".format(stat["excluded"]))
        L.append("  in the state, not at the data edge, not excluded: {:,}".format(sum(by_status.values())))
    else:
        L.append("  in the state and not at the data edge:           {:,}".format(sum(by_status.values())))
    L.append("")
    L.append("Of those, by status (against the DWR, NID and SARP lists, 1000 ft) and kind:")
    sts = ["new", "unsure", "listed"]
    L.append("  {:<42}{:>9}{:>9}{:>9}{:>9}".format("kind", *(sts + ["all"])))
    for kind in sorted(by_kind, key=lambda q: -by_kind[q]):
        L.append("  {:<42}{:>9,}{:>9,}{:>9,}{:>9,}".format(kind, *([by_kind_status[(kind, s)] for s in sts]
                                                               + [by_kind[kind]])))
    L.append("  {:<42}{:>9,}{:>9,}{:>9,}{:>9,}".format("all", *([by_status[s] for s in sts] + [sum(by_status.values())])))
    L.append("")
    L.append("Ranked candidates (not listed; dam or dry embankment; in the state; not at the data edge{}): {:,}"
             .format("; not in an excluded area" if ex_geom is not None else "", len(ranked)))
    L.append("  ranked by {}".format("the score of {}".format(os.path.basename(model_path)) if model is not None
                                     else "height (--no-score)"))
    if model is not None:
        L.append("  with a score of {} or more: {:,}".format(SCORE_CUT, n_cut))
    if n_top:
        L.append("  the short list ({}): the top {:,} of them, each with a link to the satellite picture".format(
            OUT_TOP, n_short))
    if crowd_cut > 0:
        L.append("  in crowded ground (crowded = yes): {:,} of them{}".format(
            n_crowd, ", {:,} of the first {:,}".format(n_crowd_top, n_short) if n_top else ""))
        L.append("    (many small pond fills close together: {:g} or more other pond-line fills per km2 within 1 km."
                 .format(crowd_cut))
        L.append("    In the Sheyenne sand hills these were dune hollows, not dams: look at the ground before you")
        L.append("    count them. A note only: no row is left out and the order is as it would be without it.)")
    else:
        L.append("  crowded ground: not marked (--crowd-km2 0)")
    for v in top:
        L.append("  {:>3}  {}  score {}  {:<15} {:<7} {:>6} ft  {:.5f}, {:.5f}".format(
            v["rank"], v["find_id"], "-" if v.get("score") is None else "{:.3f}".format(v["score"]), v["kind"],
            v["status"], v["height_ft"], v["lat"], v["lon"]))
    if clash:
        L.append("")
        L.append("{} pair(s) of different fills share a find_id (the id is a 32-bit hash of the point; marked "
                 "id_clash = yes, both kept).".format(len(clash)))
    L.append("")
    L.append("Tiles")
    L.append("  merged here: {}".format(len(tiles)))
    for k_, label in (("tiles_in_plan", "in the plan (terrain raster and selection)"),
                      ("tiles_ran_this_run", "run in this run"), ("tiles_failed_this_run", "failed in this run"),
                      ("tiles_still_to_run", "still to run"),
                      ("tiles_skipped_no_terrain", "skipped: no terrain"),
                      ("tiles_skipped_outside_state", "skipped: outside the state outline")):
        if k_ in plan_note:
            L.append("  {}: {}".format(label, plan_note[k_]))
    if plan_note.get("failed_ids"):
        L.append("  failed: {}".format(plan_note["failed_ids"]))
    if other:
        L.append("  left out, made with other settings (another Finder version, tile size or terrain file): {} "
                 "({})".format(len(other), other_versions(other)))
    L.append("  tile time: {} in all ({} of processor time), mean {}, longest {}; highest memory of one tile "
             "{:.0f} MB".format(dur(sum(secs)), dur(sum(cpus)), dur(sum(secs) / len(secs)), dur(max(secs)), peak))
    L.append("")
    L.append("Versions and settings")
    for k_ in ("script", "finder_version", "gdal", "numpy", "scipy", "python", "terrain", "tile_km", "call",
               "state_outline", "exclude_polygons", "data_edge", "edge_m", "crowd_km2", "score_model", "selection"):
        if k_ in info:
            L.append("  {}: {}".format(k_, info[k_]))
    if sums:
        L.append("")
        L.append("Input checksums (sha256)")
        for role, v in sums.items():
            L.append("  {}: {}".format(role, v))
    L.append("")
    L.append("Files")
    L.append("  {}  (layers: embankments = every fill; candidates = the ranked list; spillways; ridges; "
             "run_info)".format(final))
    L.append("  {}  (every ranked candidate)".format(os.path.join(out, OUT_CSV)))
    if n_top:
        L.append("  {}  (the short list: open this one first)".format(os.path.join(out, OUT_TOP)))
    L.append("")
    with open(os.path.join(out, OUT_SUMMARY), "w", encoding="utf-8") as fh:
        fh.write("\n".join(L))
    for ln in L:
        log(ln)
    log("merge took {}".format(dur(time.time() - t0)))
    return info


# ---------------------------------------------------------------------------------------- main --
def parser():
    ap = argparse.ArgumentParser(
        prog="run_finder_statewide", formatter_class=argparse.RawDescriptionHelpFormatter,
        description=__doc__.split("HOW THE STATE IS TILED")[0])
    ap.add_argument("--dem", default=os.environ.get("ND_LOCAL_TERRAIN") or DEFAULT_DEM,
                    help="the statewide 1 m terrain VRT, NAVD88 metres [$ND_LOCAL_TERRAIN, else %(default)s]")
    ap.add_argument("--data", default="", help="the suite data folder (it holds ND_Dam_Suite_Pack), or the pack "
                                               "folder itself; read only [$NDSUITE_DATA, else ~/ND_Suite_Data]")
    ap.add_argument("--out", default=DEFAULT_OUT, help="where everything is written [%(default)s]")
    ap.add_argument("--workers", type=int, default=4, help="tiles run at once, one core each [4]")
    ap.add_argument("--tile-km", type=int, default=10, help="tile size, a multiple of 5 km [10]")
    ap.add_argument("--bbox", default="", help="only the tiles that touch W,S,E,N (lon / lat degrees; or EPSG:5070 "
                                               "metres). Write it --bbox=-101.0,46.4,-100.5,46.8")
    ap.add_argument("--county", default="", help="only the tiles that touch this county (needs a county layer: the "
                                                 "pack's {} or --county-file)".format(COUNTIES))
    ap.add_argument("--county-file", default="", help="a polygon file with the county names, for --county")
    ap.add_argument("--limit", type=int, default=0, help="run at most N tiles in this run (a trial)")
    ap.add_argument("--list", action="store_true", help="list the tiles and what each would do; writes nothing")
    ap.add_argument("--dry-run", action="store_true", help="tile counts, time and disk estimates; writes nothing")
    ap.add_argument("--redo", action="store_true", help="run the selected tiles again though they are finished "
                                                        "(each old result is replaced when its new one is in)")
    ap.add_argument("--merge-only", action="store_true", help="run no tiles: merge the finished ones")
    ap.add_argument("--no-score", action="store_true", help="do not score; the candidates are ranked by height")
    ap.add_argument("--review-batch", type=int, default=0, metavar="N",
                    help="after the merge, draw a review batch of N cards from the ranked candidates "
                         "(finder.run_review_batch with its defaults) into ND_finder_review_batch.gpkg / .csv")
    ap.add_argument("--top", type=int, default=TOP_DEFAULT, metavar="N",
                    help="rows of the short list {}: the N highest-ranked candidates with a map link "
                         "[{}; 0 = no short list]".format(OUT_TOP, TOP_DEFAULT))
    ap.add_argument("--exclude-polygons", default="", metavar="FILE",
                    help="a polygon file of ground to leave out of the ranked lists (mine spoil, sand hills ...): "
                         "a fill inside any polygon stays in the embankments layer with excluded = yes [none]")
    ap.add_argument("--crowd-km2", type=float, default=CROWD_KM2_DEFAULT, metavar="N",
                    help="the crowded-ground note: crowded = yes for a pond-line dam or dry embankment with N or "
                         "more other such fills per km2 within 1 km. A note only: nothing is left out or reordered "
                         "[{:g}; 0 = off]".format(CROWD_KM2_DEFAULT))
    ap.add_argument("--model", default="", help="the score model JSON [the plugin's models/{}]".format(MODEL_NAME))
    ap.add_argument("--state-outline", default="", help="a polygon file of North Dakota; 'none' = no test "
                                                        "[the pack's counties, else the built-in simple outline]")
    ap.add_argument("--state-buffer-m", type=float, default=None,
                    help="allowance round the state outline [0 for a file, {:.0f} for the built-in one]".format(
                        BUILTIN_BUFFER_M))
    ap.add_argument("--edge-m", type=float, default=EDGE_M_DEFAULT,
                    help="only for tiles made by Finder 0.6.x: a fill this close to missing terrain is at_data_edge "
                         "[{:.0f}; at most {:.0f}]. Finder 0.7.0 decides it itself (its kind 'at the edge of the "
                         "terrain data') and this option is then not used".format(EDGE_M_DEFAULT, EDGE_SEARCH_M))
    ap.add_argument("--plugins", default="", help="the folder that holds {} [the damsafety QGIS profile]".format(ENGINE))
    ap.add_argument("--repo", default="", help="a DWR_Plugins checkout to take {} from".format(ENGINE))
    ap.add_argument("--tile-timeout-min", type=float, default=240.0,
                    help="stop a tile that runs longer than this and count it as failed [240; 0 = never]")
    ap.add_argument("--no-checksums", action="store_true", help="do not sha256 the pack layers for the summary")
    ap.add_argument("--worker-job", default="", help=argparse.SUPPRESS)
    return ap


def fix_argv(argv):
    """'--bbox -101,46,-100,47' -> '--bbox=-101,46,-100,47' (argparse takes the minus for an option)."""
    out, i = [], 0
    while i < len(argv):
        if argv[i] == "--bbox" and i + 1 < len(argv) and argv[i + 1][:1] == "-" and argv[i + 1][1:2].isdigit():
            out.append("--bbox=" + argv[i + 1])
            i += 2
        else:
            out.append(argv[i])
            i += 1
    return out


def main(argv=None):
    a = parser().parse_args(fix_argv(list(sys.argv[1:] if argv is None else argv)))
    more_files()
    if a.worker_job:
        try:
            return run_tile(json.loads(a.worker_job))
        except Exception:                                               # noqa - the tile's log says why
            traceback.print_exc()
            sys.stdout.flush()
            e = sys.exc_info()[1]
            print("{}{}: {}".format(FAIL_MARK, type(e).__name__, " ".join(str(e).split())), flush=True)
            return 1
    try:
        return run(a)
    except UserError as e:
        print("ERROR: {}".format(e), file=sys.stderr)
        return 2


def run(a):
    if a.workers < 1:
        raise UserError("--workers must be 1 or more")
    if a.tile_km < 5 or a.tile_km % 5:
        raise UserError("--tile-km must be a multiple of 5 (the engine works in 5 km squares; see the top of "
                        "this script)")
    if not 0 < a.edge_m <= EDGE_SEARCH_M:
        raise UserError("--edge-m must be between 0 and {:.0f}".format(EDGE_SEARCH_M))
    if a.bbox and a.county:
        raise UserError("give --bbox or --county, not both")
    if a.redo and a.merge_only:
        raise UserError("give --redo or --merge-only, not both")
    if a.top < 0:
        raise UserError("--top must be 0 (no short list) or more")
    if not a.crowd_km2 >= 0:
        raise UserError("--crowd-km2 must be 0 (no crowded-ground note) or more")
    try:
        gdal, _o, _s = gdal_mods()
        import numpy                                                    # noqa - checked here, used later
        import scipy                                                    # noqa
    except ImportError as e:
        raise UserError("this Python lacks {} - run it with the suite's QGIS Python (run_finder_statewide.sh "
                        "does: mamba run -n ndqgis python ...)".format(e)) from None
    quiet = a.list or a.dry_run
    out = real(a.out)
    data = find_data(a)
    pack = find_pack(data)
    plugins, plugins_from = find_plugins(a)
    dem_path = real(a.dem) if os.path.exists(os.path.expanduser(a.dem)) else os.path.expanduser(a.dem)
    for folder, what in ((data, "the suite data folder"), (pack, "the data pack"), (plugins, "the plugins folder"),
                         (os.path.dirname(dem_path), "the terrain folder")):
        if inside(out, folder):
            raise UserError("--out {} is inside {} ({}): nothing is ever written there. Choose another "
                            "--out.".format(out, what, folder))
    inputs = pack_inputs(pack)
    finder = load_engine(plugins)
    ver = tuple(int(t) for t in finder.VERSION.split(".")[:2])
    if ver < (0, 6):
        raise UserError("{} in {} is version {}; 0.6.0 or later is needed".format(ENGINE, plugins, finder.VERSION))
    model_path = real(a.model) if a.model else os.path.join(plugins, ENGINE, "models", MODEL_NAME)
    if not a.no_score and not os.path.isfile(model_path):
        raise UserError("the score model {} was not found (give --model FILE, or --no-score)".format(model_path))
    exclude = exclude_areas(a)
    dem, dem_why = None, ""
    try:
        dem = Dem(dem_path)
    except UserError as e:
        # --merge-only reads no terrain (the tiles hold all it needs), so it also works with the
        # terrain drive unplugged: the settings are then those of the finished tiles
        if not a.merge_only or quiet:
            raise
        dem_why = str(e)
    if dem is not None:
        key = settings_key(finder.VERSION, a, dem)
    else:
        try:
            key, _n = key_without_terrain(out, finder.VERSION, a)
        except UserError as e:
            raise UserError("{} And: {}".format(dem_why, e)) from None
    log = Log()
    lock = None
    if not quiet:
        os.makedirs(os.path.join(out, "logs"), exist_ok=True)
        lock = take_lock(out)
        log = Log(os.path.join(out, "logs", "run_{}.log".format(datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))))
    try:
        return run_locked(a, log, quiet, out, data, pack, plugins, plugins_from, dem, dem_path, inputs, finder,
                          model_path, key, dem_why, exclude)
    finally:
        log.close()
        if lock and (read_json(lock) or {}).get("pid") == os.getpid():
            os.remove(lock)


def review_batch(a, finder, out, log):
    """--review-batch N: the plugin's own review batch, drawn from the ranked candidates."""
    rb = os.path.join(out, "ND_finder_review_batch.gpkg")
    if os.path.exists(rb):
        os.remove(rb)
    try:
        res = finder.run_review_batch(os.path.join(out, OUT_GPKG) + "|layername=candidates", rb,
                                      n=a.review_batch, log=log)
        os.replace(res["csv"], os.path.join(out, "ND_finder_review_batch.csv"))
        log("review batch: {} and ND_finder_review_batch.csv".format(rb))
    except RuntimeError as e:
        log("review batch not made: {}".format(e))


def merge_without_terrain(a, log, out, pack, dem_path, inputs, finder, model_path, key, dem_why, exclude=None):
    """--merge-only while the terrain file cannot be opened (the drive is not connected): the
    finished tiles hold everything a merge needs, so they are merged as they are. No plan is made
    (that reads the terrain), so the summary has no count of the tiles still to run."""
    d = key.get("dem") or {}
    log("  terrain  NOT READ: {}".format(dem_why))
    log("           --merge-only goes on without it and merges the finished tiles made from {} ({:,} x {:,} "
        "cells, EPSG:{}).".format(d.get("name"), int(d.get("nx") or 0), int(d.get("ny") or 0), d.get("epsg")))
    log("  pack     {}  (read only)".format(pack))
    log("  out      {}".format(out))
    outline, outline_what = state_outline(a, pack)
    log("  state    {}".format(outline_what))
    clean_work(out)
    os.makedirs(os.path.join(out, "work"), exist_ok=True)
    note = collections.OrderedDict()
    note["selection"] = ("not planned: the terrain file was not there, so the tiles still to run were not "
                         "counted (--merge-only merges every finished tile)")
    log("")
    if exclude and exclude[0] is not None:
        log("  exclude  {}".format(exclude[1]))
    merge(a, finder, out, key, dem_path, outline, outline_what, inputs, note, model_path, log, exclude)
    clean_work(out)
    if a.review_batch > 0:
        review_batch(a, finder, out, log)
    return 0


def run_locked(a, log, quiet, out, data, pack, plugins, plugins_from, dem, dem_path, inputs, finder, model_path, key,
               dem_why="", exclude=None):
    gdal, _o, _s = gdal_mods()
    log("run_finder_statewide {} - {}".format(SCRIPT_VERSION, now()))
    log("  Finder   {} from {} ({})".format(finder.VERSION, os.path.join(plugins, ENGINE), plugins_from))
    k_edge = edge_kind(finder)
    if k_edge is not None:
        log("  edge     this Finder sets a fill at the edge of the terrain data aside itself (kind '{}'){}".format(
            k_edge, "; --edge-m {:g} is not used".format(a.edge_m) if a.edge_m != EDGE_M_DEFAULT else ""))
    else:
        log("  edge     this Finder has no test for the edge of the terrain data: this script measures the distance "
            "and sets a fill within {:.0f} m of missing terrain aside (--edge-m)".format(a.edge_m))
    if dem is None:
        return merge_without_terrain(a, log, out, pack, dem_path, inputs, finder, model_path, key, dem_why, exclude)
    log("  terrain  {}  ({:,} x {:,} cells of {:g} m, EPSG:{}, {} source file(s) listed, {} overview level(s))".format(
        dem_path, dem.nx, dem.ny, abs(dem.gt[1]), dem.epsg, "?" if dem.rects is None else len(dem.rects),
        dem.overviews))
    log("  pack     {}  (read only)".format(pack))
    log("  out      {}".format(out))
    unit = (dem.band.GetUnitType() or "").strip().lower()
    if unit and ("foot" in unit or "feet" in unit or unit in ("ft", "ftus", "us-ft")):
        raise UserError("the terrain raster is tagged with elevations in '{}'. This script reads the terrain in "
                        "metres (the statewide VRT is NAVD88 metres): it is not the right file.".format(unit))
    if dem.nodata is None:
        log("  WARNING: the terrain raster names no NoData value; gaps cannot be told from ground")
    if abs(dem.gt[1]) > 2.0:
        log("  WARNING: the terrain cells are {:g} m; the Finder was tested on 1 m LiDAR".format(abs(dem.gt[1])))
    outline, outline_what = state_outline(a, pack)
    log("  state    {}".format(outline_what))
    if exclude and exclude[0] is not None:
        log("  exclude  {} - fills inside stay in 'embankments' (excluded = yes), out of the ranked lists".format(
            exclude[1]))
    select, sel_what = None, "the whole terrain raster"
    if a.bbox:
        b = parse_bbox(a.bbox)
        select, sel_what = box_geom(b), "--bbox {} (EPSG:5070: {:.0f}, {:.0f}, {:.0f}, {:.0f})".format(a.bbox, *b)
    elif a.county:
        select, cpath = county_geom(a, pack)
        sel_what = "--county {} (from {})".format(a.county, cpath)
    log("  area     {} - whole {} km tiles".format(sel_what, a.tile_km))
    if not quiet:
        clean_work(out)
        clear_failed(out)
        os.makedirs(os.path.join(out, "work"), exist_ok=True)
        os.makedirs(os.path.join(out, "tiles"), exist_ok=True)
    log("Planning the tiles (looking for terrain) ...")
    since = redo_since(a, out, start=not quiet and not a.merge_only) if os.path.isdir(out) \
        else (time.time() if a.redo else None)
    timed = {}
    plan, shares = make_plan(a, dem, out, key, outline, select, log, cache_ok=not quiet, since=since, timed=timed)
    n = collections.Counter(s for _t, s in plan)
    # a tile whose terrain cannot be read now is still to run: it fails, and its log says why
    todo = [t for t, s in plan if s in ("todo", "unreadable")]
    if a.redo:
        log("  --redo: tiles finished before {} run again (each old result is replaced when its new one is in)".format(
            datetime.datetime.fromtimestamp(since).strftime("%Y-%m-%d %H:%M:%S")))
    with_terrain = n["todo"] + n["done"] + n["other"]
    log("  {} tile(s) of {} km in the selection: {} with terrain ({} finished, {} to run), {}{} without terrain "
        "(skipped), {} outside the state outline (skipped)".format(
            len(plan), a.tile_km, with_terrain, n["done"], n["todo"],
            "{} whose terrain COULD NOT BE READ, ".format(n["unreadable"]) if n["unreadable"] else "",
            n["nodata"] + n["nosource"], n["outside"]))
    if n["unreadable"]:
        log("  WARNING: {} tile(s) lie under a terrain file of the VRT, but the terrain there could not be read "
            "just now: {}".format(n["unreadable"], dem.read_error or "the read failed"))
        log("           Is the drive connected, and are the terrain files where the VRT says? Such a tile is "
            "counted as still to run; it fails until its terrain can be read.")
        if not n["todo"] and not quiet and not a.merge_only:
            raise UserError("the terrain of none of the {} tile(s) still to run can be read ({}). Nothing was "
                            "run{}.".format(n["unreadable"], dem.read_error or "the read failed",
                                            "; --merge-only merges the finished tiles" if n["done"] else ""))
    if n["other"]:
        log("  {} finished tile(s) were made with other settings (another Finder version, tile size or terrain "
            "file): they are left as they are, NOT run and NOT merged - one result never mixes Finder versions. "
            "--redo runs them again with the settings of this run.".format(n["other"]))
    if a.list:
        ll = transformer(srs_of(WORK_EPSG), srs_of(4326))
        words = {"todo": "to run", "done": "finished", "other": "finished with other settings",
                 "nodata": "no terrain", "nosource": "no terrain (no source file)", "outside": "outside the state",
                 "unreadable": "to run - its terrain COULD NOT BE READ just now"}
        log("{:<20}{:>11}{:>10}  {}".format("tile", "lon", "lat", "state"))
        for t, s in plan:
            lon, lat = ll.TransformPoint(0.5 * (t.box[0] + t.box[2]), 0.5 * (t.box[1] + t.box[3]))[:2]
            m = marker(out, t.id) if s == "done" else None
            log("{:<20}{:>11.4f}{:>10.4f}  {}{}{}".format(
                t.id, lon, lat, words[s], " - {} fill(s)".format(m.get("fills")) if m else "",
                " - {:.0f} % terrain".format(100.0 * shares[t.id]) if s == "todo" and shares.get(t.id) is not None
                else ""))
    # what a tile full of terrain costs: this folder's own finished FULL tiles when there are 3,
    # else the cloud-box number; every tile is then counted by its share of terrain
    sec_per, n_full = full_tile_seconds([(sec, shares.get(tid) if own is None else own)
                                         for tid, (sec, _nf, own) in timed.items()])
    measured = n_full >= EST_MIN_FULL
    fills_done = sum(timed[t.id][1] for t, s in plan if s == "done" and t.id in timed)
    if a.limit and a.limit > 0 and not a.merge_only:
        todo_now = todo[:a.limit]
    else:
        todo_now = list(todo)
    if a.dry_run or a.list:
        w = max(1, a.workers)
        log("")
        log("Estimate for the {} tile(s) still to run, {} at a time:".format(len(todo), w))
        todo_shares = [shares.get(t.id) for t in todo]
        worth = sum(tile_cost(sh, sec_per) for sh in todo_shares) / sec_per
        how = ("the mean of the {} finished tiles in this folder that are full of terrain".format(n_full)
               if measured else
               "processor time on the cloud test box, NOT timed on this computer - this computer's own figure "
               "is used once {} tiles full of terrain are finished here ({} so far)".format(EST_MIN_FULL, n_full))
        if todo:
            log("  time    about {} (a tile full of terrain taken as {}: {}; a tile with less terrain is counted "
                "by its share, so the {} tile(s) count as {:.1f} full ones)".format(
                    dur(time_for(todo_shares, sec_per, w)), dur(sec_per), how, len(todo), worth))
        else:
            log("  time    nothing left to run (a tile full of terrain took {}: {})".format(dur(sec_per), how)
                if measured else "  time    nothing left to run")
        if n["unreadable"] and todo:
            log("          (the {} tile(s) whose terrain could not be read are counted as full)".format(
                n["unreadable"]))
        ram = ram_gb()
        log("  memory  about {:.1f} GB per tile at its peak (1.2 GB measured on the test box), so about {:.0f} GB "
            "for {} at a time{}".format(EST_PEAK_GB, math.ceil(EST_PEAK_GB * w), w,
                                        "; this computer has {:.0f} GB".format(ram) if ram else ""))
        with_terrain += n["unreadable"]
        area = with_terrain * a.tile_km ** 2
        nf = area * EST_FILLS_PER_KM2
        mb_tiles = with_terrain * EST_TILE_MB + nf * EST_TILE_KB_PER_FILL / 1024.0
        mb_merged = nf * EST_MERGED_KB_PER_FILL / 1024.0
        log("  disk    about {} in --out for all {} tiles with terrain: {} of tile results and {} for the merged "
            "GeoPackage and CSV (about {:,.0f} fills expected, at {} per km2 as in the test tiles)".format(
                size_text(mb_tiles + mb_merged), with_terrain, size_text(mb_tiles), size_text(mb_merged), nf,
                EST_FILLS_PER_KM2))
        log("Nothing was run and nothing was written ({}).".format("--list" if a.list else "--dry-run"))
        return 0
    ram = ram_gb()
    if ram and todo_now and a.workers * EST_PEAK_GB > 0.8 * ram:
        log("  WARNING: {} tiles at a time need about {:.0f} GB and this computer has {:.0f} GB; use a smaller "
            "--workers".format(a.workers, a.workers * EST_PEAK_GB, ram))
    result = {"ran": [], "failed": {}, "gave_up": False}
    if not a.merge_only and todo_now:
        if a.limit and len(todo_now) < len(todo):
            log("  --limit {}: running {} of the {} tiles still to run".format(a.limit, len(todo_now), len(todo)))
        log("Running {} tile(s), {} at a time. Ctrl-C stops; the same command again goes on.".format(
            len(todo_now), a.workers))
        job_base = {"out": out, "dem": dem_path, "plugins": plugins, "inputs": dict(inputs), "key": key}
        counts = {"total": n["done"] + len(todo_now), "done": n["done"], "fills": fills_done,
                  "sec_per_tile": sec_per, "shares": shares, "measured": measured}
        old_term = signal.getsignal(signal.SIGTERM)

        def on_term(_s, _f):
            raise Stop("terminated")
        signal.signal(signal.SIGTERM, on_term)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, on_term)
        # Ctrl-C also when the run was started in the background (a shell then starts it with
        # SIGINT ignored, and Python would not raise KeyboardInterrupt)
        signal.signal(signal.SIGINT, signal.default_int_handler)
        try:
            result = run_tiles(todo_now, a, out, job_base, counts, log)
        except Stop:
            clean_work(out)
            log("")
            log("STOPPED. The finished tiles are kept. Run the same command again to go on{}; "
                "--merge-only merges what is finished.".format(
                    " (with --redo: it goes on with the tiles not yet run again)" if a.redo else ""))
            return 130
        finally:
            signal.signal(signal.SIGTERM, old_term)
    elif not a.merge_only:
        log("No tile to run: every tile in the selection is finished or has no terrain.")
    left = len(todo) - len(result["ran"])
    if a.redo and not left and os.path.exists(os.path.join(out, "redo.json")):
        os.remove(os.path.join(out, "redo.json"))
    note = collections.OrderedDict()
    note["selection"] = "{}; whole {} km tiles".format(sel_what, a.tile_km)
    note["tiles_in_plan"] = len(plan)
    note["tiles_ran_this_run"] = len(result["ran"])
    note["tiles_failed_this_run"] = len(result["failed"])
    note["tiles_still_to_run"] = left
    note["tiles_skipped_no_terrain"] = n["nodata"] + n["nosource"]
    note["tiles_skipped_outside_state"] = n["outside"]
    if result["failed"]:
        note["failed_ids"] = ", ".join(sorted(result["failed"]))
    log("")
    bad = bool(result["failed"] or result["gave_up"])
    try:
        merge(a, finder, out, key, dem_path, outline, outline_what, inputs, note, model_path, log, exclude)
    except UserError as e:
        if not bad:
            raise
        log("Nothing merged: {}".format(e))
        a.review_batch = 0
    clean_work(out)
    if a.review_batch > 0:
        review_batch(a, finder, out, log)
    if bad:
        log("")
        log("{} tile(s) FAILED{}: {}. Their logs are in {}. Run the same command again to try them again.".format(
            len(result["failed"]), " and the run stopped after failures in a row" if result["gave_up"] else "",
            ", ".join(sorted(result["failed"])[:20]) or "see the log", os.path.join(out, "failed")))
        return 1
    if left:
        log("{} tile(s) are still to run: run the same command again{}.".format(
            left, " without --limit" if a.limit else ""))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        sys.exit(130)
