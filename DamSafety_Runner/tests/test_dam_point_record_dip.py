"""3.73.0 (batch 44, Nick 2026-11-23 "go"): why two dams were placed wrong, and the fixes.

DWR-2659: the batch layer's latitude / longitude (the old 4D location) is 7.2 km from the dam, so
its DWR line was refused and the dam ran at the wrong place. The DWR dams also carry x_coord /
y_coord (ND State Plane, ft), which lie on the dam's line. A line refused from the batch point is
now tried again from that record location; with no usable line a batch point more than NEAR_M
from it is moved there (a warning). A batch point that passes keeps its own choice (DWR-2665).

DWR-2976: the DWR line's midpoint sits on the spillway dip (1227.6 ft read as the top of dam; the
embankment is ~1231.8 ft). The Runner's mid-crest now moves a midpoint that sits in a dip of the
crest to the middle of the line's longest level stretch (damsuite midcrest avoid_dip).
No QGIS, no model runs."""
import math
import os
import shutil
import tempfile
import types

import numpy as np

from nd_breach_runner.tests.registry import test

FT = 0.3048
# DWR-2659 Tewaukon WS-T-7 in the DWR dams: x/y (ND South, ft) and the wrong 4D lat / lon
X_2659, Y_2659 = 2730841.0, 126167.0
LON_2659, LAT_2659 = -97.42144, 46.00633
# PROJ (EPSG:2266 -> 4269) of the x/y: the location the record's remark gives (45.97277,
# -97.50144)
PROJ_2659 = (-97.50127058162217, 45.9727180084237)


def _DL():
    from nd_breach_runner.core import dwr_lines
    return dwr_lines


def _MC():
    from nd_breach_runner.damsuite import midcrest
    return midcrest


def _to_work(pts):
    """lon / lat -> metres round the dam (equirectangular; enough for the guard)."""
    return [((lon - PROJ_2659[0]) * 111320.0 * math.cos(math.radians(PROJ_2659[1])),
             (lat - PROJ_2659[1]) * 110574.0) for lon, lat in pts]


def _ll(x, y):
    """metres round the dam -> lon / lat (the inverse of _to_work)."""
    return (PROJ_2659[0] + x / (111320.0 * math.cos(math.radians(PROJ_2659[1]))),
            PROJ_2659[1] + y / 110574.0)


def _xy_for(DL, target):
    """State Plane ft (South) whose record location lands on `target` (metres, _to_work) - a
    few Newton steps on record_lonlat (the State Plane grid is turned ~2 degrees here)."""
    x, y = X_2659 + target[0] / FT, Y_2659 + target[1] / FT

    def f(x, y):
        return _to_work([DL.record_lonlat(x, y, LON_2659, LAT_2659)])[0]
    for _i in range(4):
        p = f(x, y)
        ex, ey = target[0] - p[0], target[1] - p[1]
        a, b = f(x + 1.0, y), f(x, y + 1.0)
        j = ((a[0] - p[0], b[0] - p[0]), (a[1] - p[1], b[1] - p[1]))
        det = j[0][0] * j[1][1] - j[0][1] * j[1][0]
        x += (ex * j[1][1] - ey * j[0][1]) / det
        y += (ey * j[0][0] - ex * j[1][0]) / det
    return x, y


def _gpkg(path, table, gtype, fields, rows):
    from nd_breach_runner.damsuite import suitegpkg as G
    w = G.GpkgWriter(path)
    lyr = w.layer(table, gtype, fields, 4326)
    for geom, attrs in rows:
        lyr.add(geom, attrs)
    w.close()


@test
def dwr_record_xy_is_read_as_proj_does():
    DL = _DL()
    lon, lat = DL.record_lonlat(X_2659, Y_2659, LON_2659, LAT_2659)
    assert abs(lon - PROJ_2659[0]) < 1e-7 and abs(lat - PROJ_2659[1]) < 1e-7, (lon, lat)
    # no lat / lon: the zone cannot be told (both give a point in North Dakota)
    assert DL.record_lonlat(X_2659, Y_2659) is None
    # North zone (EPSG:2265, PROJ): x 1968503.937 ft (the false easting), y 0 -> -100.5, 47 N
    lon_n, lat_n = DL._lcc_inverse(1968503.937, 0.0, "north")
    assert abs(lon_n + 100.5) < 1e-7 and abs(lat_n - 47.0) < 1e-7, (lon_n, lat_n)
    for bad in ((None, 1.0), ("", ""), (0, 0), (-5.0, 3.0), ("x", 2.0)):
        assert DL.record_lonlat(*bad) is None, bad


@test
def dwr_record_xy_places_a_dam_whose_batch_point_is_wrong():
    DL = _DL()
    root = tempfile.mkdtemp(prefix="ndbr_rec_")
    try:
        # 2659's line through its record location; 2665: two embankments, one by the batch
        # point (kept) and one by the record location; 7777: no line, its record 3 km away
        line_2659 = [_ll(-60.0, 0.0), _ll(70.0, 0.0)]
        near_pt = [_ll(1000.0, 1000.0), _ll(1000.0, 1200.0)]
        near_rec = [_ll(1300.0, 1000.0), _ll(1300.0, 1300.0)]
        lines = os.path.join(root, "lines.gpkg")
        _gpkg(lines, "DWR_dam_lines", "MULTILINESTRING", [("structure_id", "int")],
              [({"type": "MultiLineString", "coordinates": [ln]}, {"structure_id": s})
               for s, ln in ((2659, line_2659), (2665, near_pt), (2665, near_rec))])
        x_of = {sid: _xy_for(DL, m) for sid, m in ((2665, (1300.0, 1150.0)),
                                                   (7777, (3000.0, 0.0)))}
        dams = os.path.join(root, "dams.gpkg")
        _gpkg(dams, "DWR_dams", "POINT",
              [("structure_id", "int"), ("latitude", "real"), ("longitude", "real"),
               ("x_coord", "real"), ("y_coord", "real")],
              [({"type": "Point", "coordinates": (LON_2659, LAT_2659)},
                {"structure_id": 2659, "latitude": LAT_2659, "longitude": LON_2659,
                 "x_coord": X_2659, "y_coord": Y_2659})] +
              [({"type": "Point", "coordinates": (LON_2659, LAT_2659)},
                {"structure_id": s, "latitude": LAT_2659, "longitude": LON_2659,
                 "x_coord": x_of[s][0], "y_coord": x_of[s][1]}) for s in (2665, 7777)])
        idx = DL.line_index(lines)
        rec = DL.record_lookup_for(dams)
        assert rec is not None and rec("2659") is not None and rec("1") is None
        wrong = _to_work([(LON_2659, LAT_2659)])[0]           # 7.2 km off
        assert 6000.0 < math.hypot(*wrong) < 8000.0, wrong
        # before: refused, the point as given
        d0 = types.SimpleNamespace()
        xy, info, warn = DL.resolve(d0, wrong, [("2659", "id")], idx, _to_work)
        assert xy == wrong and not info.get("used") and "more than 500 m" in warn, info
        # now: the record location passes the guard and the line is used
        d1 = types.SimpleNamespace()
        xy, info, warn = DL.resolve(d1, wrong, [("2659", "id")], idx, _to_work, record=rec)
        assert info["used"] and info["reference"] == "record" and warn is None, info
        assert abs(xy[0] - 5.0) < 1.0 and abs(xy[1]) < 1.0, xy          # the line's midpoint
        assert "from the DWR record location" in info["text"], info
        assert "more than 500 m" in info["batch_point_refused"], info
        assert d1._point_original == wrong
        # a batch point that passes keeps its own line (DWR-2665)
        pt = (1000.0, 1100.0)
        d2 = types.SimpleNamespace()
        xy, info, _w = DL.resolve(d2, pt, [("2665", "id")], idx, _to_work, record=rec)
        assert info["used"] and "reference" not in info and abs(xy[0] - 1000.0) < 1.0, \
            (xy, info)
        # no line: a point far from the record location goes there, with a warning
        d3 = types.SimpleNamespace()
        xy, info, warn = DL.resolve(d3, (0.0, 0.0), [("7777", "id")], idx, _to_work,
                                    record=rec)
        assert abs(xy[0] - 3000.0) < 5.0 and abs(xy[1]) < 5.0, xy
        assert "moved there" in warn and d3._record_point["moved_m"] > 2900, (warn, d3.__dict__)
        # ... but not when it is near
        d4 = types.SimpleNamespace()
        xy, info, warn = DL.resolve(d4, (2900.0, 0.0), [("7777", "id")], idx, _to_work,
                                    record=rec)
        assert xy == (2900.0, 0.0) and warn is None and not hasattr(d4, "_record_point")
        # a DWR dams file without x/y: no lookup, as before
        assert DL.record_lookup_for(os.path.join(root, "none.gpkg")) is None
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _spillway_grid(dip_m=1.5, hill=True):
    """1 m grid, x -10..310: a crest along y = 0 at 10 m (falling 0.2 m per m off it), a
    spillway dip of dip_m at x 140-200 (round the line's midpoint x = 150) and, with hill, the
    line running up a hill at x > 220 (as DWR-2976's line)."""
    xs = np.arange(-10, 310) + 0.5
    ys = 40.0 - (np.arange(80) + 0.5)
    X, Y = np.meshgrid(xs, ys)
    crest = np.full_like(X, 10.0)
    crest[(X >= 140) & (X <= 200)] -= dip_m
    if hill:
        crest[X > 220] = 10.0 + 0.05 * (X[X > 220] - 220.0) * 10.0
    z = crest - 0.2 * np.abs(Y)
    return z, (-10.0, 1.0, 0.0, 40.0, 0.0, -1.0)


@test
def midcrest_moves_off_a_spillway_dip():
    MC = _MC()
    line = [(0.0, 0.0), (300.0, 0.0)]                         # midpoint (150, 0): the dip
    z, gt = _spillway_grid()
    zf = MC.bilinear(z, gt)
    plain = MC.midcrest(line, zf)
    assert abs(plain["z_m"] - 8.5) < 0.15 and "dip" not in plain, plain
    rec = MC.midcrest(line, zf, avoid_dip=True)
    assert abs(rec["z_m"] - 10.0) < 0.15, rec
    d = rec["dip"]
    assert d["s0"] <= 2.0 and 136.0 <= d["s1"] <= 140.0, d     # the embankment, 0-138 m
    assert abs(rec["xy"][0] - 0.5 * (d["s0"] + d["s1"])) < 1.5 and abs(rec["xy"][1]) < 0.6
    assert "dip" in rec["dip_note"] and "Dip check" in MC.summary_line(rec), rec
    # a shallow dip (0.3 m, under DIP_M) and a level crest keep the midpoint
    for g in (_spillway_grid(dip_m=0.3), _spillway_grid(dip_m=0.0, hill=False)):
        r = MC.midcrest(line, MC.bilinear(*g), avoid_dip=True)
        assert "dip" not in r and abs(r["xy"][0] - 150.0) < 1e-6, r
    # the Runner turns it on and warns
    from nd_breach_runner.core import workflow
    from nd_breach_runner.core.grid import Grid
    from nd_breach_runner.tests.test_vertical_datum import _dam, _st

    def read(bounds, cell):
        return Grid(z.copy(), gt[0], gt[3], gt[1])
    dam = _dam(top=1071.15)
    dam.crest_line_xy = line
    xy, rec = workflow.place_midcrest(dam, _st(), (150.0, 0.0), read, 1.0)
    assert rec["moved"] and abs(rec["z_m"] - 10.0) < 0.15 and xy[0] < 140.0, (xy, rec)
    assert rec.get("dip_note"), rec
