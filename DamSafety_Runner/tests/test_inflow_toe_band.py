"""v3.12.10 (Nick, DWR-4031): "can we make the inflow point at the dam toe at the lowest
elevation?" The breach inflow is the lowest cell of a band 5-30 m downstream of the dam line,
along the full line, on the fine (1 m) DEM (terrain.toe_band_inflow). find_inflow_point (with
the 150 m cap) is the fallback, with a warning, when there is no dam line, no downstream side
or no valid cell in the band. Synthetic DEMs only."""
import numpy as np

from nd_breach_runner.core import terrain
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test as register  # "test" would be collected by pytest
from nd_breach_runner.tests.synthetic import TOP_M, H_M

LINE = [(0.0, -60.0), (0.0, 60.0)]      # N-S dam line through the dam point; downstream is east
EAST = (1.0, 0.0)
FLOOR = TOP_M - H_M                     # the valley floor at the toe


def _dem(X, Y, toe_low=(15.0, 40.0), far_low=(60.0, 0.0), upstream_low=True):
    """Embankment along x = 0 (crest 6 m wide), a 2H:1V downstream face to the floor at x ~ 18 m,
    a floor that is flat across the band (then falls 2 m past x = 45 m), a 1 m-deep toe
    channel at `toe_low`, a deeper pit farther out at `far_low` and, upstream, a reservoir
    floor lower than all of them."""
    z = np.full_like(X, FLOOR)
    face = (X > 3.0) & (X < 3.0 + 2.0 * H_M / 0.8)
    z = np.where(face, TOP_M - (X - 3.0) / 2.0 * 0.8 * 1.25, z)
    z = np.where(np.abs(X) <= 3.0, TOP_M, z)
    z = np.maximum(z, FLOOR)
    z = np.where(X > 45.0, FLOOR - 2.0, z)
    if toe_low is not None:
        z = np.where(np.hypot(X - toe_low[0], Y - toe_low[1]) < 2.0, FLOOR - 1.0, z)
    if far_low is not None:
        z = np.where(np.hypot(X - far_low[0], Y - far_low[1]) < 5.0, FLOOR - 4.0, z)
    if upstream_low:
        z = np.where(X < -3.0, FLOOR - 3.0, z)          # reservoir floor (dry), upstream
        z = np.where(X < -200.0, TOP_M + 2.0, z)        # ... closed by upland
    z = np.where(np.abs(Y) > 70.0, TOP_M + 2.0, z)      # abutments
    return z


def _grid(half=120.0, cell=1.0, **kw):
    n = int(round(2 * half / cell))
    xs = -half + (np.arange(n) + 0.5) * cell
    ys = half - (np.arange(n) + 0.5) * cell
    X, Y = np.meshgrid(xs, ys)
    return Grid(_dem(X, Y, **kw), -half, half, cell)


@register
def toe_band_picks_the_toe_low_point_over_a_lower_cell_farther_out():
    g = _grid()
    pt, why = terrain.toe_band_inflow(g, LINE, EAST, H_M, TOP_M, dam_xy=(0.0, 0.0))
    assert pt is not None, why
    assert abs(pt[0] - 15.0) <= 2.0 and abs(pt[1] - 40.0) <= 2.0, (pt, why)   # the toe channel
    assert "lowest ground at the downstream toe" in why and "5-30 m" in why, why
    # the old search takes the deeper pit 60 m out (or the drop past 45 m), not the toe
    old, _w = terrain.find_inflow_point(g, (0.0, 0.0), H_M, TOP_M, downstream=EAST)
    assert old is not None and old[0] > 40.0, old


@register
def toe_band_keeps_to_the_downstream_side():
    g = _grid()
    band, dl = terrain.toe_band_mask(g, LINE, EAST)
    X, _Y = np.meshgrid(g.x_centers(), g.y_centers())
    assert band.any() and (X[band] > 0).all(), "the band is on the downstream side only"
    assert dl[band].min() >= 5.0 and dl[band].max() <= 30.0
    # downstream given the other way: the band is the reservoir side and the pick is there
    pt, _why = terrain.toe_band_inflow(g, LINE, (-1.0, 0.0), H_M, TOP_M)
    assert pt is not None and pt[0] < 0, pt
    # the reservoir floor (3 m lower than anything downstream) is never picked going east
    pt, _why = terrain.toe_band_inflow(g, LINE, EAST, H_M, TOP_M)
    assert pt[0] > 0, pt


@register
def toe_band_runs_the_full_crest_length_and_not_round_the_ends():
    # the toe channel near the north end of the line (y = 55), not at the dam point
    g = _grid(toe_low=(20.0, 55.0))
    pt, _why = terrain.toe_band_inflow(g, LINE, EAST, H_M, TOP_M, dam_xy=(0.0, 0.0))
    assert abs(pt[1] - 55.0) <= 2.0, pt
    band, _dl = terrain.toe_band_mask(g, LINE, EAST)
    X, Y = np.meshgrid(g.x_centers(), g.y_centers())
    # off the ends of the line the band only fans out within 60 degrees of downstream
    end = band & (Y > 60.0)
    assert ((X[end]) >= 0.5 * np.hypot(X[end], Y[end] - 60.0) - 1e-6).all()


@register
def toe_band_flat_ties_go_to_the_line_then_the_dam_point():
    g = _grid(toe_low=None, far_low=None)                 # the floor is flat over the band
    pt, _why = terrain.toe_band_inflow(g, LINE, EAST, H_M, TOP_M, dam_xy=(0.0, 10.0))
    X = pt[0]
    # the first floor cell out from the face, nearest the dam point
    assert X < 3.0 + 2.0 * H_M / 0.8 + 2.0 and abs(pt[1] - 10.0) <= 1.0, pt


@register
def toe_band_widens_when_the_band_is_all_dam_face():
    tall = 4.0 * H_M          # a dam this tall: the 2H:1V face runs past 30 m
    g = _grid(toe_low=None, far_low=None, upstream_low=False)
    z = g.data.copy()
    X, _Y = np.meshgrid(g.x_centers(), g.y_centers())
    face = (X > 3.0) & (X < 3.0 + 2.0 * tall)
    z = np.where(face, TOP_M - (X - 3.0) / 2.0, z)
    z = np.where(X >= 3.0 + 2.0 * tall, TOP_M - tall, z)
    g2 = Grid(z, g.x0, g.y0, g.dx)
    pt, why = terrain.toe_band_inflow(g2, LINE, EAST, tall, TOP_M)
    assert pt is not None and "band widened" in why, why
    assert TOP_M - z[g2.rc(*pt)] >= 0.5 * tall - 1e-6, (pt, why)
    # never past the cap
    pt, why = terrain.toe_band_inflow(g2, LINE, EAST, tall, TOP_M, max_from_dam_m=25.0)
    assert pt is not None and pt[0] <= 25.0 + 1.0 and "less than half" in why, (pt, why)


@register
def toe_band_says_why_it_cannot_be_used():
    g = _grid()
    for args, word in (((None, EAST), "no dam line"), ((LINE, None), "downstream side"),
                       (([(0.0, 0.0)], EAST), "no dam line")):
        pt, why = terrain.toe_band_inflow(g, args[0], args[1], H_M, TOP_M)
        assert pt is None and why.startswith(terrain.NO_TOE) and word in why, why
    high = Grid(np.full(g.data.shape, TOP_M + 1.0), g.x0, g.y0, g.dx)
    pt, why = terrain.toe_band_inflow(high, LINE, EAST, H_M, TOP_M)
    assert pt is None and why.startswith(terrain.NO_TOE), why


class _Src(object):
    """terrain_src: the synthetic DEM at any cell (nearest sample of a 1 m grid)."""
    def __init__(self, **kw):
        self.g = _grid(half=1000.0, cell=1.0, **kw)
        self.cells = []

    def native_cell_m(self):
        return 1.0

    def read(self, bounds, cell):
        self.cells.append(cell)
        n = max(1, int(round((bounds[2] - bounds[0]) / cell)))
        m = max(1, int(round((bounds[3] - bounds[1]) / cell)))
        xs = bounds[0] + (np.arange(n) + 0.5) * cell
        ys = bounds[3] - (np.arange(m) + 0.5) * cell
        X, Y = np.meshgrid(xs, ys)
        return Grid(_dem(X, Y), bounds[0], bounds[3], cell)


def _workflow_pick(line, crest_line=None, band=None):
    from nd_breach_runner.core import workflow

    class Dam(object):
        crest_line_xy = crest_line

    class St(object):
        midcrest_line = line if crest_line is None else None
        inflow_toe_band_m = band
        inflow_toe_cell_m = None
    src = _Src()
    rad = max(90.0, 8.0 * H_M)
    local = src.read((-430.0, -430.0, 430.0, 430.0), 20.0)
    warnings, info = [], {}
    pt, why = workflow._downstream_inflow(src, local, Dam(), (0.0, 0.0), H_M, TOP_M, rad, 20.0,
                                          warnings, lambda *_a: None, out=info,
                                          **workflow._toe_kwargs(Dam(), St()))
    return pt, why, warnings, info, src


@register
def workflow_places_the_inflow_at_the_toe_on_the_fine_dem():
    pt, why, warnings, info, src = _workflow_pick(LINE, crest_line=LINE)
    assert info["direction"][0] > 0.9, info
    assert abs(pt[0] - 15.0) <= 2.0 and abs(pt[1] - 40.0) <= 2.0, (pt, why)
    assert "downstream toe" in why and not warnings, (why, warnings)
    assert 1.0 in src.cells, src.cells                   # the band is read at 1 m
    # a batch's CREST_LINE (st.midcrest_line) is the dam line when DAM_LINES has none
    pt, why, warnings, _info, _src = _workflow_pick(LINE, crest_line=None)
    assert "downstream toe" in why and not warnings, (why, warnings)
    # the band is a setting
    pt, why, _w, _i, _s = _workflow_pick(LINE, crest_line=LINE, band=(5.0, 12.0))
    assert "5-12 m" in why and pt[0] <= 12.5, (pt, why)


@register
def workflow_without_a_dam_line_falls_back_with_a_warning():
    pt, why, warnings, info, _src = _workflow_pick(None, crest_line=None)
    assert pt is not None and "downstream toe" not in why, why
    assert any("not placed at the downstream toe" in w and "no dam line" in w for w in warnings), \
        warnings
    assert info.get("toe", "").startswith(terrain.NO_TOE), info
