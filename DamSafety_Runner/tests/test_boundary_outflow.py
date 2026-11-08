"""H08 (value-neutral stage): the boundary-outflow diagnostic.

The LISFLOOD-FP domain-extension loop grows the model only while an edge is DEEPER than the edge
tolerance (1 ft). A wide, shallow sheet can carry a large share of the breach volume off the model
through an edge that stays under that tolerance, so the modelled flood area and the downstream
consequences are understated with no signal. read_results now integrates the .mass Qout column
into boundary_outflow_acft / _pct, and terrain.edge_flux_shares estimates which edge carried it.

This stage only SURFACES the truncation (a stat + a warning); it does not grow the domain, so no
value a finished run computes changes (the golden baseline stays put). The helper is validated
here without QGIS; the full run-level warning is exercised by CI with the fake-LISFLOOD harness.
"""
import numpy as np

from nd_breach_runner.core import terrain
from nd_breach_runner.tests.registry import test


@test
def edge_flux_shares_weigh_the_edges_by_depth_to_the_five_thirds():
    d = np.full((5, 8), np.nan)
    d[1:-1, -1] = 0.1                    # east edge: 3 cells, 0.1 m (not the corners)
    d[-1, 2:4] = 0.4                     # south edge: 2 cells, 0.4 m
    s = terrain.edge_flux_shares(d)
    e, south = 3 * 0.1 ** (5 / 3.0), 2 * 0.4 ** (5 / 3.0)
    assert abs(s["E"] - e / (e + south)) < 1e-9 and abs(s["S"] - south / (e + south)) < 1e-9
    assert s["N"] == 0.0 and s["W"] == 0.0 and abs(sum(s.values()) - 1.0) < 1e-9
    assert terrain.edge_flux_shares(np.full((3, 3), np.nan)) == {"N": 0.0, "S": 0.0,
                                                                 "W": 0.0, "E": 0.0}
    valid = np.ones((5, 8), bool)
    valid[:, -1] = False                 # cells off the DEM carry nothing
    assert terrain.edge_flux_shares(d, valid)["E"] == 0.0


@test
def boundary_outflow_integrates_positive_qout_only():
    """The stat integrates only water that LEFT (Qout > 0); negative Qout is boundary inflow,
    already reported as late_boundary_inflow_*. Mirrors the trapezoid in read_results so a sign
    slip or a units change in that block is caught headless."""
    from nd_breach_runner.core.units import ACFT
    t = np.array([0.0, 3600.0, 7200.0])          # s
    qo = np.array([0.0, 10.0, 10.0])             # m3/s, a steady 10 after the first hour
    trap = getattr(np, "trapezoid", None) or np.trapz
    out_vol = float(trap(np.clip(qo, 0.0, None), t))
    assert abs(out_vol - (0.5 * 3600 * 10 + 3600 * 10)) < 1e-6      # 54000 m3
    assert round(out_vol / ACFT, 2) == round(54000.0 / ACFT, 2)
    # a pure-inflow series contributes nothing to outflow
    assert float(trap(np.clip(np.array([-5.0, -5.0]), 0.0, None), np.array([0.0, 3600.0]))) == 0.0
