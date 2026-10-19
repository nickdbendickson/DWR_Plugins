"""Audit 1.22 #5: every county download box covers that county's outline plus a 5 mi box
around any point in it."""
import math
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
        dy = 5 * 1609.344 / 111195.0
        dx = dy / math.cos(math.radians(max(abs(s), abs(n))))
        if not (bw <= w - dx and bs <= s - dy and be >= e + dx and bn >= n + dy):
            short.append(name)
    assert not short, "{} boxes miss part of their county + 5 mi: {}".format(len(short), ", ".join(short))


if __name__ == "__main__":
    test_each_county_box_covers_its_outline()
    print("ok")
