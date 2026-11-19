"""S7 (Runner 3.22.x): crossings still blocked after the DEM cuts.

A bare-earth road or rail embankment is a wall across the channel until a culvert, bridge or
notch is cut through it. The cut can be too narrow for the model cell (a 1.2 m guessed box in a
30 m cell cuts nothing), or there may be no structure at all, and the wall then survives into
the model DEM. structures.crossing_barriers walks the flow path on the model DEM AFTER the cuts,
takes the running minimum of the bed (conditioning.ChannelBurn's enforced-fall pattern), and
reports where the bed still rises above the lowest ground already reached, within max(1 cell,
60 m) of a road, rail or STRUCTURES line. Report only: it cuts nothing and changes no number."""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_breach_runner.core import structures as S  # noqa: E402
from nd_breach_runner.core.grid import Grid  # noqa: E402
from nd_breach_runner.tests.registry import test  # noqa: E402


def _crossing(resid, cell=10.0, nx=24, ny=12, bed_row=6, emb_col=10, emb_cells=3):
    """A model DEM with a flat channel approach (bed 100 m) meeting a road embankment whose
    crown, after the cut, sits `resid` m above the approach, then drops to 99 m downstream. The
    flow path is the channel-row centreline (upstream to downstream); the road is a line across
    the channel at the embankment. Returns (grid, trace, road_line, crown_x, channel_y)."""
    z = np.full((ny, nx), 108.0)                       # floodplain
    row = np.full(nx, 100.0)                           # flat approach bed
    row[emb_col:emb_col + emb_cells] = 100.0 + resid   # residual embankment crown after the cut
    row[emb_col + emb_cells:] = 99.0                   # downstream, lower
    z[bed_row, :] = row
    g = Grid(z.astype(float), 0.0, ny * cell, cell)    # y0 is the top edge
    yc = float(g.y_centers()[bed_row])
    xc = g.x_centers()
    trace = np.column_stack([xc, np.full(nx, yc)])     # along the channel row, upstream first
    crown_x = float(xc[emb_col + emb_cells // 2])
    road = np.array([[crown_x, yc - 40.0], [crown_x, yc + 40.0]])   # crosses the channel
    return g, trace, road, crown_x, yc


@test
def crossing_barriers_flags_an_embankment_still_walling_the_channel():
    g, trace, road, crown_x, _ = _crossing(resid=1.07)
    cc = S.crossing_barriers(g, trace, [road], cell=10.0)
    assert cc is not None, "expected a crossing check result"
    assert cc["blocked"] == 1, cc
    assert abs(cc["worst_height_m"] - 1.07) < 0.05, cc
    b = cc["barriers"][0]
    assert abs(b["x"] - crown_x) <= 15.0, b
    assert b["height_m"] >= cc["tol_m"], b


@test
def crossing_barriers_passes_a_crossing_the_cut_opened():
    # the cut brought the crown down to the channel bed: no residual wall
    g, trace, road, _, _ = _crossing(resid=0.0)
    cc = S.crossing_barriers(g, trace, [road], cell=10.0)
    assert cc is not None and cc["blocked"] == 0, cc
    assert cc["worst_height_m"] == 0.0, cc


@test
def crossing_barriers_ignores_a_rise_far_from_any_crossing_line():
    # the embankment is there, but the only line is 500 m off the channel, so it is not a
    # road / rail / STRUCTURES crossing and must not be flagged
    g, trace, _, crown_x, yc = _crossing(resid=1.07)
    far = np.array([[crown_x, yc + 500.0], [crown_x + 10.0, yc + 500.0]])
    cc = S.crossing_barriers(g, trace, [far], cell=10.0)
    assert cc is not None and cc["blocked"] == 0, cc


@test
def crossing_barriers_returns_none_without_lines_or_a_trace():
    g, trace, road, _, _ = _crossing(resid=1.07)
    assert S.crossing_barriers(g, trace, [], cell=10.0) is None
    assert S.crossing_barriers(g, trace[:1], [road], cell=10.0) is None
