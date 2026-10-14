"""find_inflow_point: the usual radius takes the LOWEST low-enough cell (the note says so); only
the wider 400 m fallback takes the NEAREST. Taking the nearest in the usual radius put the
inflow on the upstream face of the embankment / the floor of a dry reservoir."""
import numpy as np

from nd_breach_runner.core import terrain
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test as register  # "test" would be collected by pytest
from nd_breach_runner.tests.synthetic import TOP_M, H_M


def _grid(fn, half=200.0, cell=10.0):
    n = int(2 * half / cell)
    xs = -half + (np.arange(n) + 0.5) * cell
    ys = half - (np.arange(n) + 0.5) * cell
    X, Y = np.meshgrid(xs, ys)
    return Grid(fn(X, Y), -half, half, cell)


def _dry_dam(X, Y):
    """A dry flood-control dam at x = 0: the empty reservoir floor (west) is half the dam height
    down, right against the upstream toe; the channel below the dam (east) is lower."""
    up = TOP_M - 0.55 * H_M + 0.0 * X
    down = TOP_M - H_M - 0.002 * np.clip(X, 0, None) - np.where(np.abs(Y) < 12, 0.3, 0.0)
    z = np.where(X < 0, up, down)
    emb = TOP_M - np.abs(X) / 40.0 * H_M
    return np.where(np.abs(X) <= 40, np.maximum(z, emb), z)


@register
def inflow_point_of_a_dry_dam_is_below_the_dam_not_on_the_reservoir_floor():
    pt, why = terrain.find_inflow_point(_grid(_dry_dam), (0.0, 0.0), H_M, TOP_M)
    assert pt is not None and pt[0] > 0, (pt, why)
    assert why.startswith("lowest ground within")


@register
def inflow_point_wider_search_takes_the_nearest_low_cell():
    # nothing low enough within the usual 90 m: a wide embankment, the toe 150 m east and a
    # deeper far valley 350 m east; the wider search takes the toe, not the far valley
    def wide(X, Y):
        z = np.full_like(X, TOP_M - 0.2 * H_M)
        z = np.where((X > 140) & (X < 170), TOP_M - 0.6 * H_M, z)
        return np.where(X > 330, TOP_M - 2.0 * H_M, z)
    pt, why = terrain.find_inflow_point(_grid(wide, half=420.0), (0.0, 0.0), H_M, TOP_M)
    assert pt is not None and 140 < pt[0] < 170, (pt, why)
    assert why.startswith("toe is outside the usual")
