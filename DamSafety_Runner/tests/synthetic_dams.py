"""Synthetic DEMs for the dam point check (core/dam_point_check.py, 3.20.11). Each builder is a
function z(X, Y) (metres): hydro-flattened water (exactly flat), land with a little texture (so
only the water is flat), a 3H:1V earth embankment and a coulee falling away below it.

DamTerrain wraps a builder like tests/synthetic.SyntheticTerrain and opts in to the check: it
reports a native cell (the check skips a DEM whose cell is unknown) and a stamp for the
check's cache."""
import numpy as np

from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.synthetic import SyntheticTerrain

TOP = 500.0                  # top of dam (m)
H = 9.0                      # dam height (m) = 29.5 ft
H_FT = H / 0.3048
POOL = TOP - 2.5             # pond level, 0.28 H below the top (critique 3)
CW = 6.0                     # crest width (m)
FACE = 3.0                   # embankment faces, H:V
WALL = 0.08                  # coulee side slope
UP = 0.012                   # coulee bed upstream of the dam (m/m)
DOWN = 0.003                 # and below it
CREST_HALF = (TOP - (TOP - H)) / WALL       # 112.5 m: where the walls reach the top


def texture(X, Y, amp=0.02):
    """A deterministic few-cm roughness, so dry land is never hydro-flat."""
    return amp * np.sin(0.9 * X + 0.4 * Y) * np.cos(0.7 * Y - 0.3 * X) + \
        0.5 * amp * np.sin(2.3 * X - 1.1 * Y)


def coulee(X, Y, x_dam=0.0, top=TOP, h=H):
    """Natural ground: a coulee running east, its bed at top - h at the dam, a small channel."""
    dx = X - x_dam
    bed = top - h - np.where(dx > 0, DOWN * dx, UP * dx)
    ch = np.where(np.abs(Y) < 6.0, 0.4 * (1 - np.abs(Y) / 6.0), 0.0)
    return bed + WALL * np.abs(Y) - ch + texture(X, Y)


def embankment(X, Y, x_dam=0.0, top=TOP, axis_angle_deg=0.0):
    """The embankment surface (crest CW wide, 3:1 faces) along a line through (x_dam, 0)."""
    a = np.radians(axis_angle_deg)
    # distance from the axis (a line through (x_dam, 0) at angle a from north)
    d = np.abs((X - x_dam) * np.cos(a) - Y * np.sin(a))
    return top - np.maximum(0.0, d - CW / 2.0) / FACE + texture(X, Y)


def enclosed(X, Y):
    """The basic case: a 9 m dam across the coulee at x = 0, its pond to the west at POOL
    (hydro-flattened), dry land everywhere else."""
    g = coulee(X, Y)
    water = (X < 0) & (g < POOL)
    z = np.where(water, POOL, g)
    emb = embankment(X, Y)
    z = np.where(np.abs(Y) <= CREST_HALF + 20, np.maximum(z, emb), z)
    return z


def with_knoll(X, Y, kx=-80.0, ky=190.0, kh=14.0):
    """enclosed() with a 14 m knoll beside the pond (DWR-3034's point)."""
    return enclosed(X, Y) + kh * np.exp(-((X - kx) ** 2 + (Y - ky) ** 2) / (2 * 25.0 ** 2))


def with_notch(X, Y, ny=60.0, width=12.0, depth=1.5):
    """enclosed() with a spillway notch 1.5 m deep in the crest near its north end."""
    z = enclosed(X, Y)
    cut = (np.abs(Y - ny) < width / 2) & (np.abs(X) < CW / 2 + 6.0)
    return np.where(cut, np.minimum(z, TOP - depth + texture(X, Y)), z)


def two_level(X, Y, step=0.08):
    """enclosed() with the pond on two flat levels 8 cm apart (a seam between two LiDAR projects
    in one VRT): x < -200 m reads 8 cm higher."""
    z = enclosed(X, Y)
    water = (X < 0) & (z == POOL)
    return np.where(water & (X < -200), POOL + step, z)


def not_flattened(X, Y):
    """enclosed() whose water is LiDAR returns, not hydro-flattened (+-5 cm noise)."""
    z = enclosed(X, Y)
    water = (X < -12) & (np.abs(z - POOL) < 1e-9)
    return np.where(water, POOL + 0.05 * np.sin(3.1 * X) * np.cos(2.7 * Y), z)


def _polyline_dist(X, Y, pts):
    best = np.full(np.shape(X), np.inf)
    for (x0, y0), (x1, y1) in zip(pts[:-1], pts[1:]):
        sx, sy = x1 - x0, y1 - y0
        L2 = sx * sx + sy * sy
        t = np.clip(((X - x0) * sx + (Y - y0) * sy) / L2, 0.0, 1.0)
        best = np.minimum(best, np.hypot(X - (x0 + t * sx), Y - (y0 + t * sy)))
    return best


CHEVRON = [(0.0, -400.0), (0.0, 0.0), (-0.75 * 400, 0.66 * 400)]


def l_shaped(X, Y, wall=0.03):
    """A bent dam (a chevron, 48 deg): the axis runs north along x = 0 to (0, 0), then turns
    north-west; gentle coulee walls (3%), so the low ground lies below both legs. A straight
    chord between its ends would leave the crest by about 70 m at the bend."""
    bed = TOP - H - np.where(X > 0, DOWN * X, UP * X)
    g = bed + wall * np.abs(Y) + texture(X, Y)
    up = np.where(Y <= 0, X < 0, 0.66 * X + 0.75 * Y < 0)
    water = up & (g < POOL)
    z = np.where(water, POOL, g)
    emb = TOP - np.maximum(0.0, _polyline_dist(X, Y, CHEVRON) - CW / 2.0) / FACE + \
        texture(X, Y)
    return np.maximum(z, emb)


def ring(X, Y, r=110.0, h=4.0):
    """A lagoon: a ring dike (radius r, h high) on gently sloping land, water inside."""
    land = TOP - 0.002 * X + texture(X, Y)
    top = TOP + h
    pool = top - 1.5
    d = np.abs(np.hypot(X, Y) - r)
    emb = top - np.maximum(0.0, d - CW / 2) / FACE + texture(X, Y)
    inside = np.hypot(X, Y) < r - 8
    z = np.where(inside, np.maximum(pool, land), land)
    return np.maximum(z, emb)


def saddle(X, Y, sx=-260.0):
    """enclosed() plus a saddle dike on the north rim at x = sx: the rim dips to a pass and a
    side draw falls away north of it; the dike across the pass is about 5 m high above the
    draw (0.55 H), lower than the 9 m main dam."""
    g = coulee(X, Y)
    pass_ = (np.abs(X - sx) < 60) & (Y > 30)
    draw = TOP - 6.0 - 0.02 * np.maximum(0.0, Y - 150) + texture(X, Y)
    g = np.where(pass_, np.minimum(g, np.maximum(draw, TOP - 6.0 - 0.02 * (Y - 150))), g)
    water = (X < 0) & (g < POOL) & (Y < 135)
    z = np.where(water, POOL, g)
    emb = embankment(X, Y)
    z = np.where(np.abs(Y) <= CREST_HALF + 20, np.maximum(z, emb), z)
    dike = TOP - 1.0 - np.maximum(0.0, np.abs(Y - 140) - CW / 2) / FACE + texture(X, Y)
    z = np.where((np.abs(X - sx) < 70), np.maximum(z, dike), z)
    return z


def neighbours(X, Y, xb=350.0):
    """Two dams in one coulee: A at x = 0 with a DRY pond (a closed basin, no water), B at x =
    xb holding water that backs up to A's toe."""
    g = coulee(X, Y)
    bed_b = TOP - H - DOWN * xb
    top_b = bed_b + H
    pool_b = top_b - 2.5
    water_b = (X > 3 + 3 * H) & (X < xb) & (g < pool_b)
    z = np.where(water_b, pool_b, g)
    emb_a = embankment(X, Y)
    z = np.where(np.abs(Y) <= CREST_HALF + 20, np.maximum(z, emb_a), z)
    emb_b = embankment(X, Y, x_dam=xb, top=top_b)
    z = np.where(np.abs(Y) <= CREST_HALF + 20, np.maximum(z, emb_b), z)
    return z


def upstream_neighbour(X, Y, xb=-250.0, up=0.02):
    """Two dams in one coulee, the other way round (review 2026-11-17): A at x = 0 with a DRY
    pond, B at x = xb UPSTREAM of it, higher (the coulee rises 2 %), holding water. A point on
    A (or in A's dry basin) sees B's pond and B's embankment, with A's dry basin as B's low
    side; B's crest is then more than 0.5 H above the top read on A (A2)."""
    dx = X
    bed = TOP - H - np.where(dx > 0, DOWN * dx, up * dx)
    ch = np.where(np.abs(Y) < 6.0, 0.4 * (1 - np.abs(Y) / 6.0), 0.0)
    g = bed + WALL * np.abs(Y) - ch + texture(X, Y)
    top_b = TOP - up * xb
    pool_b = top_b - 2.5
    z = np.where((X < xb) & (g < pool_b), pool_b, g)
    z = np.where(np.abs(Y) <= CREST_HALF + 20, np.maximum(z, embankment(X, Y)), z)
    z = np.where(np.abs(Y) <= CREST_HALF + 20,
                 np.maximum(z, embankment(X, Y, x_dam=xb, top=top_b)), z)
    return z


class DamTerrain(SyntheticTerrain):
    """SyntheticTerrain that opts in to the dam point check (native cell, cache stamp)."""

    def __init__(self, fn=enclosed, native=1.0, stamp=None):
        SyntheticTerrain.__init__(self, fn)
        self.native = native
        self.dpc_stamp = stamp if stamp is not None else ("synthetic", fn.__name__)

    def native_cell_m(self):
        return self.native

    def checks(self):
        """How many reads the check made (its 2 m window)."""
        return sum(1 for b, c in self.reads if abs(c - 2.0) < 1e-9 and b[2] - b[0] > 2000)


def write_geotiff(fn, path, bounds=(-1300.0, -1300.0, 1300.0, 1300.0), cell=1.0,
                  epsg=26914):
    """A GeoTIFF of builder `fn` (cell-centre values) for the GDAL-backed tests."""
    from osgeo import gdal, osr
    xmin, ymin, xmax, ymax = bounds
    nx, ny = int(round((xmax - xmin) / cell)), int(round((ymax - ymin) / cell))
    X, Y = np.meshgrid(xmin + (np.arange(nx) + 0.5) * cell, ymax - (np.arange(ny) + 0.5) * cell)
    z = fn(X, Y).astype(np.float32)
    ds = gdal.GetDriverByName("GTiff").Create(path, nx, ny, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((xmin, cell, 0.0, ymax, 0.0, -cell))
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    ds.SetProjection(s.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.WriteArray(z)
    b.SetNoDataValue(-9999.0)
    ds.FlushCache()
    ds = None
    return path


def grid_of(fn, bounds, cell):
    xmin, ymin, xmax, ymax = bounds
    nx, ny = int(round((xmax - xmin) / cell)), int(round((ymax - ymin) / cell))
    X, Y = np.meshgrid(xmin + (np.arange(nx) + 0.5) * cell, ymax - (np.arange(ny) + 0.5) * cell)
    return Grid(fn(X, Y), xmin, ymax, cell)
