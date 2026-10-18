"""Audit 1.22 #5: every county download box covers that county's outline."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from nd_dam_screening.core import dem_download as dd  # noqa: E402
from nd_dam_screening.core import nd_counties  # noqa: E402


def test_each_county_box_covers_its_outline():
    assert sorted(dd.COUNTIES) == nd_counties.names() and len(dd.COUNTIES) == 53
    short = []
    for name in nd_counties.names():
        w, s, e, n = nd_counties.box([name])
        bw, bs, be, bn = dd.COUNTIES[name]
        if not (bw <= w and bs <= s and be >= e and bn >= n):
            short.append(name)
    assert not short, "{} boxes miss part of their county: {}".format(len(short), ", ".join(short))


if __name__ == "__main__":
    test_each_county_box_covers_its_outline()
    print("ok")
