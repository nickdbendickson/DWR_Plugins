"""Synthetic dam: an E-W embankment along y = 0 from x = -100 to 100.

Terrain falls away from the dam, with an old channel (lowest ground) at x = +30.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "core"))
import breach_dir as bd  # noqa: E402

line = [(-100.0, 0.0), (0.0, 0.0), (100.0, 0.0)]


def dem_south(x, y):
    """Reservoir north of the dam, ground falls to the south, channel at x=30."""
    z = 100.0 + 0.02 * y
    if y < -10:
        z -= 6.0 * np.exp(-((x - 30.0) / 15.0) ** 2)
    return z


def water_north(x, y):
    return 0.0 if y >= 0 else -y


def check(res, flow, name):
    ok = abs(((res["flow_az"] - flow) + 180) % 360 - 180) < 1.0
    print(f"{name}: flow {res['flow_az']:.1f} (want {flow}), method {res['dir_method']},"
          f" conf {res['dir_conf']}, breach x {res['breach'][0]:.1f}")
    assert ok, name


# 1. water + DEM, reservoir north -> flow south (180), breach at the channel
r = bd.resample(line, 3.0)  # sanity
res = bd.resolve_breach(line, (0.0, 5.0), water_north, dem_south)
check(res, 180.0, "water+dem")
assert res["dir_conf"] == "high"
assert abs(res["breach"][0] - 30.0) < 8.0, res["breach"]

# 2. mirrored: reservoir south -> flow north (0)
res = bd.resolve_breach(
    line, (0.0, 5.0), lambda x, y: water_north(x, -y), lambda x, y: dem_south(x, -y))
check(res, 0.0, "mirrored")

# 3. terrain only
res = bd.resolve_breach(line, (0.0, 5.0), None, dem_south)
check(res, 180.0, "dem only")
assert res["dir_conf"] == "medium"

# 4. waterbody only -> breach falls back to point nearest the dam point
res = bd.resolve_breach(line, (55.0, 5.0), water_north, None)
check(res, 180.0, "water only")
assert abs(res["breach"][0] - 55.0) < 1.0 and res["breach_method"] == "nearest to dam point"

# 5. disagreement is flagged
res = bd.resolve_breach(line, (0.0, 5.0), water_north,
                        lambda x, y: 100.0 - 0.02 * y)   # ground rises to the south
assert res["dir_conf"] == "low", res
print("disagree: flagged low confidence")

# 6. nothing to go on
res = bd.resolve_breach(line, (0.0, 5.0), None, None)
assert not res["ok"]

# 7. slightly bent, non-axis-aligned line (diagonal dam, reservoir to NW)
diag = [(0.0, 0.0), (60.0, 60.0), (120.0, 115.0)]
res = bd.resolve_breach(diag, (60.0, 60.0), lambda x, y: 0.0 if (y - x) > 5 else (x - y),
                        None)
print("diagonal: flow %.1f, axis %.1f" % (res["flow_az"], res["axis_az"]))
assert 120 < res["flow_az"] < 150         # SE-ish, away from the NW reservoir

# 8. fallback with no dam line
assert abs(bd.fallback_direction((0.0, 0.0), (0.0, 50.0)) - 180.0) < 1e-6
assert bd.names_match("Lake Ashtabula Dam", "lake  ashtabula dam.")
assert not bd.names_match("Lake Ashtabula Dam", "Baldhill Dam") and not bd.names_match("", "")
assert not bd.names_match(None, "x")
print("OK")
