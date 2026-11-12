"""0.19.4: the Finder plugin's candidate dams are left uncut by the road-crossing cuts.

Tools 6, 7 and 9 cut every short barrier on a 3DHP stream (<= 8 m high, <= 150 m long) unless a
DWR dam line protects it, so an unlisted stock dam on a mapped stream was cut like a road fill.
A PROTECT layer (points, lines or polygons; the Finder's screening_dams points) now protects
those fills too: a point as an 40 m cross, a line as it is, a polygon by its outline.

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_protect_finder_dams.py
"""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
import numpy as np  # noqa: E402
from qgis.core import QgsGeometry, QgsPointXY, make_layer  # noqa: E402
from nd_dam_screening.algorithms import _common as cm  # noqa: E402
from nd_dam_screening.core import gridutil as gu, hydrocond as hc, workflow as wf  # noqa: E402

# ---- a valley draining south with a road fill (row 150) and an unlisted dam (row 60)
cell = 5.0
ny, nx = 300, 200
gt = (0.0, cell, 0.0, ny * cell, 0.0, -cell)
yy, xx = np.mgrid[0:ny, 0:nx]
dem = 100.0 - yy * cell * 0.002 + 0.02 * np.abs((xx - nx / 2) * cell) ** 1.2
col = nx // 2
dem[148:154, :] = np.maximum(dem[148:154, :], dem[151, col] + 4.0)
dem[58:62, :] = np.maximum(dem[58:62, :], dem[60, col] + 5.0)
sx = col * cell + 2.5
stream = [(sx, ny * cell - 2.5), (sx, 2.5)]
dam_xy = (sx, gt[3] - 60.5 * cell)                  # the Finder's point: on the fill, on the stream


def prot_of(arrs):
    return gu.dilate(gu.rasterize_lines(arrs, gt, dem.shape), 2) if arrs else None


# ---- without protection both fills are cut (the bug: the dam went too)
cut, info = hc.breach_barriers(dem, [stream], gt)
assert info["cuts"] == 2, info
assert cut[60, col] < dem[60, col] - 3.0

# ---- a point as a cross: the dam stays, the road is still cut
cross = cm.point_cross(*dam_xy)
assert len(cross) == 2 and all(a.shape == (2, 2) for a in cross)
assert np.isclose(np.ptp(cross[0][:, 0]), 2 * cm.PROTECT_HALF_M)
kept, info = hc.breach_barriers(dem, [stream], gt, protect=prot_of(cross))
assert info["cuts"] == 1 and info["skipped_protected"] == 1, info
assert kept[60, col] == dem[60, col]
assert kept[151, col] < dem[151, col] - 3.0
print("OK point cross protects the fill:", info)

# ---- protect_near reads points, lines and polygons of any layer
pts = make_layer([("find_id", "string")],
                 [(QgsGeometry.fromPointXY(QgsPointXY(*dam_xy)), {"find_id": "FIND-1"}),
                  (QgsGeometry.fromPointXY(QgsPointXY(9e5, 9e5)), {"find_id": "far"})], "point")
got = cm.protect_near(pts, dam_xy, 3000)
assert len(got) == 2, len(got)                      # one cross; the far point is outside the box
_, info = hc.breach_barriers(dem, [stream], gt, protect=prot_of(got))
assert info["skipped_protected"] == 1 and info["cuts"] == 1, info

line = make_layer([("id", "string")], [(QgsGeometry.fromPolylineXY(
    [QgsPointXY(sx - 100, dam_xy[1]), QgsPointXY(sx + 100, dam_xy[1])]), {"id": "a"})], "line")
assert len(cm.protect_near(line, dam_xy, 3000)) == 1
y0, y1 = dam_xy[1] - 15, dam_xy[1] + 15
ring = [QgsPointXY(sx - 60, y0), QgsPointXY(sx + 60, y0), QgsPointXY(sx + 60, y1),
        QgsPointXY(sx - 60, y1), QgsPointXY(sx - 60, y0)]
poly = make_layer([("id", "string")], [(QgsGeometry.fromPolygonXY([ring]), {"id": "p"})], "polygon")
outline = cm.protect_near(poly, dam_xy, 3000)
assert len(outline) == 1 and len(outline[0]) == 5
_, info = hc.breach_barriers(dem, [stream], gt, protect=prot_of(outline))
assert info["skipped_protected"] == 1, info
assert cm.protect_near(None, dam_xy, 3000) == []
# a road fill from the Finder is never protected (Screening cuts it to open the culvert)
kinds = make_layer([("finder_kind", "string")],
                   [(QgsGeometry.fromPointXY(QgsPointXY(*dam_xy)), {"finder_kind": "dam"}),
                    (QgsGeometry.fromPointXY(QgsPointXY(sx, gt[3] - 151 * cell)), {"finder_kind": "road fill (ponding)"}),
                    (QgsGeometry.fromPointXY(QgsPointXY(sx, gt[3] - 151 * cell)), {"finder_kind": "road fill"})], "point")
got = cm.protect_near(kinds, dam_xy, 3000)
assert len(got) == 2, len(got)                      # only the dam's cross
cond, info = hc.breach_barriers(dem, [stream], gt, protect=prot_of(got))
assert info["cuts"] == 1 and info["skipped_protected"] == 1, info
assert cond[151, col] < dem[151, col] - 3.0         # the road is still cut
assert cm.is_road_fill("road fill") and cm.is_road_fill("Rail fill") and not cm.is_road_fill("dry embankment")
print("OK protect_near: points, lines, polygons")

# ---- the window passes the Finder layer to steps 6, 7 and 9, and nowhere else
assert "finder_dams" in [k for k, _l, _p in wf.LAYER_SLOTS]
S = dict(wf.DEFAULT_SETTINGS)
S["finder_dams"] = "/data/finder.gpkg|layername=screening_dams"
work = "/nonexistent_work"
for step in ("6", "7", "9"):
    assert wf.defaults(step, work, S).get("PROTECT") == S["finder_dams"], step
for step in ("3", "5", "8"):
    assert "PROTECT" not in wf.defaults(step, work, S), step
assert "PROTECT" not in wf.defaults("6", work, dict(wf.DEFAULT_SETTINGS))

# ---- the three tools declare the parameter and use it
for name in ("triage.py", "run_breach_sim.py", "sensitivity.py"):
    src = open(os.path.join(PLUGIN, "algorithms", name), encoding="utf-8").read()
    assert '"PROTECT"' in src, name
for name in ("triage.py", "run_breach_sim.py"):
    src = open(os.path.join(PLUGIN, "algorithms", name), encoding="utf-8").read()
    assert "cm.protect_near(keep," in src, name
print("ALL OK")
