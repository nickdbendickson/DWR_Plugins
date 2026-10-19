"""Default ANUGA mesher, culvert ends (Runner 3.10.30 or later): octagons that overlap or
nearly touch are merged into one refined region. From the 2026-10-19 mesh benchmark:
Absaraka's 36 m culverts put two r = 25 m octagons across each other and ANUGA's mesher
made 92 triangles under 5 m2 (the smallest 0.15 m2), cutting the time step about 6x.
Mesh-only: ANUGA is not needed (the regions are checked geometrically)."""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_breach_runner.core import anuga_model as AM  # noqa: E402
from nd_breach_runner.tests.registry import test  # noqa: E402


def _segments_cross(p, q):
    """Do two closed polygons' edges properly cross?"""
    for a, b in zip(p, np.roll(p, -1, axis=0)):
        for c, d in zip(q, np.roll(q, -1, axis=0)):
            def orient(u, v, w):
                return np.sign((v[0] - u[0]) * (w[1] - u[1]) - (v[1] - u[1]) * (w[0] - u[0]))
            if (orient(a, b, c) * orient(a, b, d) < 0) and (orient(c, d, a) * orient(c, d, b) < 0):
                return True
    return False


def _culvert(length, width=3.658, y=0.0):
    return {"end_points": [[1000.0, y], [1000.0 + length, y]], "width": width}


@test
def culvert_octagons_on_a_short_culvert_become_one_region_without_crossing_edges():
    st = AM.AnugaSettings(sys.executable)
    old = [AM.octagon(e, 25.0) for e in _culvert(36.0)["end_points"]]
    assert _segments_cross(old[0], old[1])            # what made Absaraka's 0.15 m2 slivers
    regs = AM.culvert_regions([_culvert(36.0)], st)
    assert len(regs) == 1, regs
    poly = np.asarray(regs[0]["poly"])
    assert regs[0]["area"] == 25.0
    from nd_breach_runner.core import conditioning as cond
    assert cond.points_in_polygon(np.array([1000.0, 1036.0]), np.array([0.0, 0.0]), poly).all()
    # two twin culverts 30 m apart: all four ends in one region, nothing crosses
    regs = AM.culvert_regions([_culvert(36.0), _culvert(36.0, y=30.0)], st)
    assert len(regs) == 1
    # and nothing a hull leaves behind crosses another region
    regs = AM.culvert_regions([_culvert(36.0), _culvert(60.0, y=400.0)], st)
    polys = [np.asarray(r["poly"]) for r in regs]
    assert all(not _segments_cross(a, b) for i, a in enumerate(polys) for b in polys[i + 1:])


@test
def culvert_octagons_apart_are_exactly_as_before():
    st = AM.AnugaSettings(sys.executable)
    c = [_culvert(80.0), _culvert(80.0, y=500.0)]
    old = []
    for cc in c:                                      # the code before 3.10.30
        r = max(3.0 * cc["width"], 25.0)
        for e in cc["end_points"]:
            old.append({"poly": AM.octagon(e, r).tolist(), "area": min(st.fine_area_m2, 25.0)})
    assert AM.culvert_regions(c, st) == old


@test
def culvert_regions_never_cross_for_random_culvert_layouts():
    st = AM.AnugaSettings(sys.executable)
    rng = np.random.default_rng(7)
    for _ in range(20):
        cul = []
        for i in range(6):
            x, y = rng.uniform(300, 2700, 2)
            L, a, w = rng.uniform(8, 90), rng.uniform(0, np.pi), rng.uniform(0.6, 4.0)
            cul.append({"end_points": [[x, y], [x + L * np.cos(a), y + L * np.sin(a)]], "width": w})
        polys = [np.asarray(r["poly"]) for r in AM.culvert_regions(cul, st)]
        for i, p in enumerate(polys):
            for q in polys[i + 1:]:
                assert not _segments_cross(p, q)
                assert not AM.convex_polygons_near(p, q, 0.0)
