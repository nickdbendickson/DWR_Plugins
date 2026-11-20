"""Contributing area of the pothole cascade (core/cascade.py simulate): a pothole's catchment counts once
it and every pothole below it to the outlet are full, whatever the order of the potholes in the network.
No data needed. 0.6.1 regression: 0.6.0 gave every pothole below k the answer for k, so one pothole that
was not yet full upstream left its whole full chain out of the contributing area."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from core.cascade import simulate


def net(pots, direct=0.0, other=0.0):
    return {"potholes": pots, "basin_m2": sum(p["catchment_m2"] for p in pots) + direct + other,
            "direct_to_outlet_m2": direct, "to_other_exit_m2": other}


ex = np.array([1.0, 1.0, 1.0])
# pothole 1 (huge, never fills) spills to pothole 2 (tiny, full at once), which spills to the outlet
p1 = {"id": 1, "volume_m3": 1e9, "catchment_m2": 1e5, "target": 2}
p2 = {"id": 2, "volume_m3": 1.0, "catchment_m2": 9e5, "target": 0}
for pots in ([p1, p2], [p2, p1]):
    r = simulate(net(pots), ex, 1.0)
    assert np.allclose(r["contrib_m2"], 9e5), (pots[0]["id"], r["contrib_m2"])
    assert np.allclose(r["outlet_excess_in"], [0.9 - 1.0 / 0.0254 / 1e6, 0.9, 0.9], atol=1e-6), r["outlet_excess_in"]

# a chain 3 -> 2 -> 1 -> outlet: 1 and 2 tiny (full), 3 huge (never full); 4 tiny spills to an other exit
pots = [{"id": 3, "volume_m3": 1e9, "catchment_m2": 1e5, "target": 2},
        {"id": 2, "volume_m3": 1.0, "catchment_m2": 2e5, "target": 1},
        {"id": 1, "volume_m3": 1.0, "catchment_m2": 3e5, "target": 0},
        {"id": 4, "volume_m3": 1.0, "catchment_m2": 4e5, "target": -1}]
for order in (pots, pots[::-1], [pots[1], pots[3], pots[0], pots[2]]):
    r = simulate(net(order, direct=5e4), ex, 1.0, other_exit_to_outlet=True)
    assert np.allclose(r["contrib_m2"], 5e4 + 2e5 + 3e5 + 4e5), r["contrib_m2"]
    r = simulate(net(order, direct=5e4), ex, 1.0, other_exit_to_outlet=False)
    assert np.allclose(r["contrib_m2"], 5e4 + 2e5 + 3e5), r["contrib_m2"]

# nothing full yet: only the direct area contributes
r = simulate(net([dict(p1, volume_m3=1e12), dict(p2, volume_m3=1e12)], direct=7e4), ex, 1.0)
assert np.allclose(r["contrib_m2"], 7e4), r["contrib_m2"]
print("OK (cascade contributing area: own answer per pothole, order-free)")
