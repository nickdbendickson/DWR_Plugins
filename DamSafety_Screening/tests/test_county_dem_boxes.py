"""H12 (audit #5): the county DEM download extents must be derived from the shipped county
outlines (nd_counties.geojson) via nd_counties.box(), not hand-typed.

The old hand-typed COUNTIES table ended INSIDE 48 of the 53 counties (McLean by ~34 mi on the
west), so a county DEM download quietly stopped short and dams in the uncovered strip had no
terrain (step-7 FAIL / flood cut at the DEM edge). Each download box must now fully contain its
county's outline bounding box, and the McLean box must extend past the old hand-typed west edge.

Run: PYTHONPATH=tests/qgis_mock:<parent of the plugin> python3 tests/test_county_dem_boxes.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)

from nd_dam_screening.core import dem_download as dd        # noqa: E402
from nd_dam_screening.core import nd_counties                # noqa: E402

# The old hand-typed McLean box (west, south, east, north). It ended ~0.74 deg (~34 mi) east of
# the real western county line, leaving the whole western strip of the county without terrain.
OLD_HANDTYPED_MCLEAN = (-101.65, 47.25, -100.35, 48.05)


def _area(box):
    w, s, e, n = box
    return (e - w) * (n - s)


def test_every_county_box_contains_its_outline():
    """Each download box fully contains that county's outline bbox (the safety property: every
    point inside the county is inside the DEM clip). Fails before the fix for the 48 undersized
    hand-typed boxes."""
    names = nd_counties.names()
    assert len(names) == 53, names
    bad = []
    for name in names:
        box = dd.COUNTIES.get(name)
        assert box is not None and len(box) == 4, (name, box)
        w, s, e, n = box
        ow, os_, oe, on = nd_counties.box([name], 0.0)       # the raw county outline bbox
        if not (w <= ow and s <= os_ and e >= oe and n >= on):
            bad.append((name, box, (ow, os_, oe, on)))
    assert not bad, "download box does not cover the county outline: {}".format(bad)


def test_mclean_covers_previously_missing_west_strip():
    """The documented H12 case: the derived McLean box reaches well west of the old hand-typed
    west edge, and is no smaller in area than the old box."""
    box = dd.COUNTIES["McLean"]
    w, s, e, n = box
    ow, os_, oe, on = nd_counties.box(["McLean"], 0.0)
    # fully contains the outline
    assert w <= ow and s <= os_ and e >= oe and n >= on, (box, (ow, os_, oe, on))
    # the previously-missing western strip is now inside the download
    assert w < OLD_HANDTYPED_MCLEAN[0], (w, OLD_HANDTYPED_MCLEAN[0])
    # the fix adds coverage: the derived box is at least as large as the old hand-typed one
    assert _area(box) >= _area(OLD_HANDTYPED_MCLEAN), (_area(box), _area(OLD_HANDTYPED_MCLEAN))


def test_state_covers_every_county():
    """The STATE extent contains every county outline (statewide download clips nothing off)."""
    w, s, e, n = dd.STATE
    for name in nd_counties.names():
        ow, os_, oe, on = nd_counties.box([name], 0.0)
        assert w <= ow and s <= os_ and e >= oe and n >= on, (name, dd.STATE, (ow, os_, oe, on))


def test_public_shapes_unchanged():
    """COUNTIES / STATE keep the (west, south, east, north) shape the consumers unpack."""
    assert dd.county_names() == sorted(dd.COUNTIES)
    for name, box in dd.COUNTIES.items():
        assert len(box) == 4, (name, box)
        w, s, e, n = box
        assert w < e and s < n, (name, box)
    assert len(dd.STATE) == 4
    # county_features unpacks each box as (w, s, e, n)
    feats = dd.county_features()
    assert len(feats) == 53 and all(len(ring) == 5 for _name, ring in feats)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("ok", name)
    print("OK ({} county-box tests)".format(n))
