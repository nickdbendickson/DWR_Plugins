"""S6a: warn when a channel burn is floored wider than requested at a coarse cell.
Report text only - the burn geometry and every result are unchanged."""
import numpy as np

from nd_breach_runner.tests.registry import test
from nd_breach_runner.core import conditioning as cond
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.core.units import FT


def _channel_edits(width_ft):
    line = np.array([[0.0, 0.0], [300.0, 0.0]])          # a straight E-W channel line
    return cond.Edits(channels=[cond.ChannelBurn(line, width_ft * FT, 1.0, name="channel 1")])


def _flat_grid(cell):
    # flat ground at 100 m; the channel line lies inside this window so the burn cuts
    return Grid(np.full((8, 20), 100.0), -50.0, 80.0, cell)


@test
def channel_burn_warns_when_cell_forces_it_wider():
    ed = _channel_edits(15.0)                            # 15 ft ~ 4.57 m requested bottom width
    w = cond.channel_width_warning(ed, 30.0)             # 30 m cells force it to one cell wide
    assert w, "expected a width warning at a 30 m cell"
    assert "wider than requested" in w, w
    assert "15.0 ft" in w and "30.0 m" in w, w
    # a cell no coarser than the request: no warning (realized width == requested)
    assert cond.channel_width_warning(ed, 2.0) is None
    assert cond.channel_width_warning(_channel_edits(120.0), 30.0) is None
    # no channels / bad cell: no warning, no raise
    assert cond.channel_width_warning(cond.Edits(), 30.0) is None
    assert cond.channel_width_warning(ed, 0.0) is None


@test
def channel_width_warning_is_report_text_only():
    ed = _channel_edits(15.0)
    cell = 30.0
    out1, _i1, c1 = ed.apply(_flat_grid(cell))
    _ = cond.channel_width_warning(ed, cell)             # must not touch any geometry
    out2, _i2, c2 = ed.apply(_flat_grid(cell))
    assert np.array_equal(np.nan_to_num(out1.data), np.nan_to_num(out2.data))
    assert c1 == c2 and c1["channels:channel 1"] > 0
    assert ed.channels[0].width_m == 15.0 * FT           # the request itself is unchanged
    # the realized width the warning names is ChannelBurn's own floor, not a new number
    half = 0.5 * max(ed.channels[0].width_m, cell)
    assert np.isclose(2.0 * half, cell)                  # floored to one cell at this width
