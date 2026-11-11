"""Embankments across streams found in a LiDAR DEM (Finder 0.1).

Pure numpy / scipy, no QGIS or GDAL, so it is unit-tested outside QGIS. Metres, cubic metres,
an equal-area grid (EPSG:5070 in the plugins). The same file is used by DamSafety Finder and
DamIT Finder.

WHAT IT LOOKS FOR
  A bare-earth DEM shows every fill that crosses a valley: a dam, a dry (detention) dam, a road or
  rail fill whose culvert the LiDAR cannot see, a farm crossing. Walking down each stream line
  (3DHP / NHD flowlines) the ground rises above the lowest ground already passed and falls away
  again below it. Each such hump is checked against the terrain round it, not only along the line:

  1. SPILL LEVEL. The highest water level L at which the ground just upstream of the hump (U) and
     the toe below it (D) are still in separate pools of {ground < L}. Above L the water would get
     round or over the fill. A bank the line wanders onto is not a barrier: U and D join along the
     real channel at about the toe level, so its spill height is ~0 and it is dropped.
  2. HEIGHT = spill level - toe (the ground the fill stands on: the foot of its downstream face).
     Below `min_height_m` the hump is not reported. CREST HEIGHT = the highest ground of the hump
     on the stream line - toe: the fill's own height. A dam with a spillway at its normal pool has
     a spill level at the pool and a crest height near its listed height (Absaraka, DWR-2801:
     16.4 ft to the spill, 21.3 ft to the crest on the line, 27.5 ft listed from the streambed).
  3. HEAD = upstream level - toe, less the fall the valley itself makes over that distance (the
     slope of the line just above the hump). At least `min_head_m` - or a flat pool surface behind
     the fill (flat ground whose rim is all higher, as a hydro-flattened lake is) - means the fill
     holds water or sediment above the downstream ground.
  4. STORAGE at the spill level: the volume between the ground and L over the pool of U
     (the LiDAR cannot see under water: a pool surface counts as ground). A pool that reaches the
     edge of the analysis window is marked "open" (its storage is a lower bound).

  5. SPILLWAY: where the water first gets out at the spill level - the cells that join the pool
     to the ground below when the water rises a few centimetres above it. On the fill on the
     stream line = it overtops there (or a pipe / notch through the crest); beside it = an
     auxiliary spillway, a saddle or the end of the fill. Its width is the length of that sill.

  A road or rail mask (rasterised road / rail centrelines, given by the caller) tells a road fill
  from an embankment with no road on it. Kinds:
    dam                    holds water (head) and no road / rail on it
    dry embankment         no head and no road / rail: a dry or detention dam, a levee across the
                           valley, an unmapped road or farm crossing
    road fill (ponding)    a road / rail fill with head: the culvert is missing in the DEM, small,
                           or the road is also a dam
    road fill              a road / rail fill with no head (a culvert or bridge is expected)

Every threshold is a FinderParams value the engineer chooses; the defaults are starting values,
not standards. The result is a list of candidates for review against an aerial image, not a
survey and not a determination.
"""
import math
from dataclasses import dataclass, field

import numpy as np

try:  # scipy ships with the QGIS bundles
    from scipy import ndimage as ndi
except Exception:  # pragma: no cover
    ndi = None

FT = 0.3048
AF_M3 = 1233.4818375
AC_M2 = 4046.8564224

K_DAM = "dam"
K_DRY = "dry embankment"
K_ROAD_POND = "road fill (ponding)"
K_ROAD = "road fill"
KINDS = (K_DAM, K_DRY, K_ROAD_POND, K_ROAD)


@dataclass
class FinderParams:
    """Engineer-chosen values ("YOUR value" in the tools). Metres / cubic metres."""
    min_height_m: float = 5.0 * FT      # spill level - downstream toe
    min_head_m: float = 1.0 * FT        # upstream level - toe for "holds water"
    max_len_m: float = 250.0            # longest hump along the stream line (fill + faces)
    toe_search_m: float = 150.0         # how far below the hump to look for the toe
    face_deg: float = 8.0               # ground falling steeper than this below the fill is its face
    tol_m: float = 0.15                 # rise above the lowest ground passed that starts a hump
    window_m: float = 1200.0            # half-width of the window for the spill / storage test
    flat_tol_m: float = 0.10            # ground within this of the upstream level = flat pool
    min_pool_m2: float = 400.0          # flat area behind the fill that counts as a pool
    min_storage_m3: float = 0.0         # smaller storage is not reported (0 = all)
    dedupe_m: float = 60.0              # candidates closer than this are one embankment
    level_tol_m: float = 0.02           # bisection tolerance of the spill level


@dataclass
class Embankment:
    x: float                            # crest point on the stream line (grid CRS)
    y: float
    kind: str
    on_road: bool
    crest_m: float                      # highest ground of the hump on the line
    spill_m: float                      # spill level (the fill's effective top)
    toe_m: float
    up_m: float                         # upstream level (ground or flat pool surface)
    height_m: float                     # spill - toe
    crest_height_m: float               # crest on the line - toe (the fill's own height)
    head_m: float                       # up - toe
    valley_drop_m: float                # the valley's own fall from U to D (upstream slope)
    len_m: float                        # hump length along the line
    storage_m3: float
    pool_area_m2: float
    pool: bool                          # a flat pool surface behind the fill
    open_pool: bool                     # the pool reaches the window edge (storage is a minimum)
    confidence: str
    line: int                           # index of the stream line
    notes: list = field(default_factory=list)
    toe_xy: tuple = None
    up_xy: tuple = None
    spill_xy: tuple = None              # where the pool first gets out (the spillway / saddle)
    spill_width_m: float = 0.0
    spill_dist_m: float = 0.0           # from the crest point on the line
    line_src: str = ""                  # set by the caller (stream line / DEM drainage)


def _need_scipy():
    if ndi is None:
        raise RuntimeError("scipy is required for the embankment finder")


def line_cells(xy, gt, shape):
    """Grid cells (row, col) along a polyline, in order, each once (consecutive duplicates
    dropped), sampled every half cell. `gt` is a GDAL geotransform with no rotation."""
    cell = abs(gt[1])
    out = []
    last = None
    pts = [(float(x), float(y)) for x, y in xy]
    for (x0, y0), (x1, y1) in zip(pts[:-1], pts[1:]):
        n = max(1, int(math.ceil(math.hypot(x1 - x0, y1 - y0) / (cell * 0.5))))
        for k in range(n + 1):
            t = k / n
            x, y = x0 + t * (x1 - x0), y0 + t * (y1 - y0)
            c = int(math.floor((x - gt[0]) / gt[1]))
            r = int(math.floor((y - gt[3]) / gt[5]))
            if 0 <= r < shape[0] and 0 <= c < shape[1] and (r, c) != last:
                out.append((r, c))
                last = (r, c)
    return out


def cell_xy(gt, r, c):
    return gt[0] + (c + 0.5) * gt[1], gt[3] + (r + 0.5) * gt[5]


def humps(z, tol_m, max_len):
    """(start, end) index pairs of humps in a profile z (1-D, downstream order, NaN = no data):
    z rises more than tol_m above the lowest value already passed at `start` and is back at or
    below it at `end` (exclusive). Longer than max_len samples -> dropped (a ridge, a wrong-way
    line). NaN breaks the walk."""
    out = []
    runmin = np.inf
    i, n = 0, len(z)
    while i < n:
        v = z[i]
        if not np.isfinite(v):
            runmin = np.inf
            i += 1
            continue
        if v <= runmin + tol_m:
            runmin = min(runmin, v)
            i += 1
            continue
        j = i
        while j < n and np.isfinite(z[j]) and z[j] > runmin:
            j += 1
        if j < n and np.isfinite(z[j]) and (j - i) <= max_len:
            out.append((i, j))
        runmin = z[j] if j < n and np.isfinite(z[j]) else np.inf
        i = j + 1
    return out


def orient_down(z):
    """True when the profile should be reversed so it runs downstream (its start is lower)."""
    f = np.isfinite(z)
    if f.sum() < 4:
        return False
    v = z[f]
    k = max(len(v) // 10, 1)
    return bool(np.median(v[:k]) < np.median(v[-k:]))


def face_toe(z, e, toe_k, cell, face_deg, m=3):
    """(index of the toe, still falling at the end of the search) below a hump ending at e: the
    face goes on while the ground falls faster than tan(face_deg) over m samples; the toe is the
    lowest ground from e to where it stops (natural valley slopes are gentler). None when there
    is no ground below the hump."""
    end = min(len(z) - 1, e + toe_k)
    step = m * cell * math.tan(math.radians(face_deg))
    k = e
    while k < end:
        k2 = min(end, k + m)
        if not (np.isfinite(z[k]) and np.isfinite(z[k2])) or z[k] - z[k2] < step * (k2 - k) / m:
            break
        k += 1
    seg = z[e:k + 1]
    if not np.isfinite(seg).any():
        return None, False
    return e + int(np.nanargmin(seg)), bool(k >= end)


def _window(dem, r, c, half):
    r0, r1 = max(0, r - half), min(dem.shape[0], r + half + 1)
    c0, c1 = max(0, c - half), min(dem.shape[1], c + half + 1)
    return r0, r1, c0, c1


def spill_level(win, u, d, lo, hi, tol=0.02):
    """Highest level L in [lo, hi] at which cells u and d of `win` (finite = ground, NaN = no data,
    treated as high ground) are in different 4-connected components of {ground < L}. None when
    they are joined already at lo. Connectivity only grows with L, so bisection is exact."""
    _need_scipy()
    g = np.where(np.isfinite(win), win, np.inf)

    def apart(level):
        lab, _n = ndi.label(g < level)
        a, b = lab[u], lab[d]
        return a == 0 or b == 0 or a != b

    if not apart(lo):
        return None
    if apart(hi):
        return hi
    a, b = lo, hi
    while b - a > tol:
        m = 0.5 * (a + b)
        if apart(m):
            a = m
        else:
            b = m
    return a


def pool_at(win, u, level, cell):
    """(storage m3, area m2, touches the window edge) of the pool of {ground < level} holding u."""
    g = np.where(np.isfinite(win), win, np.inf)
    lab, _n = ndi.label(g < level)
    k = lab[u]
    if k == 0:
        return 0.0, 0.0, False
    m = lab == k
    vol = float((level - win[m]).sum() * cell * cell)
    edge = bool(m[0, :].any() or m[-1, :].any() or m[:, 0].any() or m[:, -1].any())
    return vol, float(m.sum() * cell * cell), edge


def _steps(seed, inside, limit):
    """Steps (8-connected, through `inside` only) from the cells of `seed`; -1 = not reached."""
    st = np.ones((3, 3), bool)
    dist = np.full(seed.shape, -1, dtype=np.int32)
    cur = ndi.binary_dilation(seed, structure=st) & inside
    reached = cur.copy()
    k = 1
    while cur.any() and k <= limit:
        dist[cur] = k
        nxt = ndi.binary_dilation(cur, structure=st) & inside & ~reached
        reached |= nxt
        cur = nxt
        k += 1
    return dist


def spill_point(win, u, d, spill, tol=0.02, limit=400):
    """((row, col), sill width in cells) of where the pool of u at the `spill` level gets out to
    the ground of d when the water rises a little (max(3 tol, 5 cm)) above it: the middle of the
    shortest way from the pool to the ground below through the cells that rise floods; None when
    the pool does not join d."""
    g = np.where(np.isfinite(win), win, np.inf)
    lo, _n = ndi.label(g < spill)
    if lo[u] == 0:
        return None
    pool = lo == lo[u]
    hi, _n = ndi.label(g < spill + max(3.0 * tol, 0.05))
    if hi[u] == 0 or hi[u] != hi[d]:
        return None
    below = (lo == lo[d]) if lo[d] else np.zeros(g.shape, bool)
    band = (hi == hi[u]) & ~pool & ~below
    if not below.any():
        below = np.zeros(g.shape, bool)
        below[d] = True
    if not band.any():
        return None
    br, bc = np.nonzero(band)
    r0, r1 = max(0, br.min() - 1), min(g.shape[0], br.max() + 2)
    c0, c1 = max(0, bc.min() - 1), min(g.shape[1], bc.max() + 2)
    band, pool, below = band[r0:r1, c0:c1], pool[r0:r1, c0:c1], below[r0:r1, c0:c1]
    dp = _steps(pool, band, limit)
    db = _steps(below, band, limit)
    ok = (dp > 0) & (db > 0)
    if not ok.any():
        return None
    tot = np.where(ok, dp + db, np.iinfo(np.int32).max)
    best = int(tot.min())
    sill = ok & (tot <= best + 1) & (dp == 1)               # the pool's edge of the way out
    if not sill.any():
        sill = ok & (tot <= best + 1)
    rr, cc = np.nonzero(sill)
    k = int(np.argmin((rr - rr.mean()) ** 2 + (cc - cc.mean()) ** 2))
    return (int(rr[k]) + r0, int(cc[k]) + c0), int(sill.sum())


def flat_pool_area(win, u, up_level, flat_tol, cell):
    """(area m2, share of its rim that is lower) of the ground within flat_tol of up_level
    connected to u. A water surface in a hydro-flattened DEM is flat and every cell round it is
    higher (shore) - share ~0. A flat band of valley floor on a gentle slope has lower ground on its
    downstream side - share ~0.5. NaN never counts as lower."""
    fin = np.isfinite(win)
    g = np.where(fin, win, np.inf)
    flat = fin & (np.abs(np.where(fin, win, -1e9) - up_level) <= flat_tol)
    lab, _n = ndi.label(flat)
    k = lab[u]
    if not k:
        return 0.0, 1.0
    m = lab == k
    ring = ndi.binary_dilation(m) & ~m & fin
    lower = float((g[ring] < up_level - flat_tol).mean()) if ring.any() else 0.0
    return float(m.sum() * cell * cell), lower


def upstream_slope(z, ku, n, cell):
    """Fall per metre of the profile over the n samples above index ku (0 when unknown or
    rising): the drop the valley itself makes, not the fill."""
    a = max(0, ku - n)
    if ku - a < 3 or not (np.isfinite(z[a]) and np.isfinite(z[ku])):
        return 0.0
    return max(0.0, float(z[a] - z[ku]) / ((ku - a) * cell))


def find(dem, gt, lines, road_mask=None, params=None, keep=None):
    """Embankments across the stream `lines` (lists of (x, y) in the grid CRS) of `dem` (2-D, m,
    NaN or <= -9998 = no data) with geotransform `gt` (square cells, north up).
    `road_mask`: bool grid, True on roads / railroads (dilated by the caller).
    `keep(x, y)`: optional filter on the crest point (e.g. the core of a tile).
    Returns a list of Embankment, deduplicated, highest first."""
    _need_scipy()
    p = params or FinderParams()
    dem = np.asarray(dem, float).copy()
    dem[dem <= -9998.0] = np.nan
    cell = abs(gt[1])
    max_len = max(1, int(round(p.max_len_m / cell)))
    toe_k = max(1, int(round(p.toe_search_m / cell)))
    half = max(4, int(round(p.window_m / cell)))
    found = []
    for li, xy in enumerate(lines):
        if len(xy) < 2:
            continue
        cells = line_cells(xy, gt, dem.shape)
        if len(cells) < 5:
            continue
        z = np.array([dem[r, c] for r, c in cells])
        if orient_down(z):
            cells, z = cells[::-1], z[::-1]
        for s, e in humps(z, p.tol_m, max_len):
            k_crest = s + int(np.nanargmax(z[s:e]))
            crest = float(z[k_crest])
            ku = s - 1
            up = float(z[ku])
            kd, face_open = face_toe(z, e, toe_k, cell, p.face_deg)
            if kd is None:
                continue
            toe = float(z[kd])
            if crest - toe < p.min_height_m:
                continue
            rc = cells[k_crest]
            if keep is not None and not keep(*cell_xy(gt, *rc)):
                continue
            r0, r1, c0, c1 = _window(dem, rc[0], rc[1], half)
            win = dem[r0:r1, c0:c1]
            u = (cells[ku][0] - r0, cells[ku][1] - c0)
            d = (cells[kd][0] - r0, cells[kd][1] - c0)
            lo = max(up, toe) + 1e-3
            spill = spill_level(win, u, d, lo, crest, p.level_tol_m)
            if spill is None:
                continue
            height = spill - toe
            if height < p.min_height_m:
                continue
            head = up - toe
            valley = upstream_slope(z, ku, toe_k, cell) * (kd - ku) * cell
            storage, parea, open_pool = pool_at(win, u, spill, cell)
            if storage < p.min_storage_m3:
                continue
            flat, lower = flat_pool_area(win, u, up, p.flat_tol_m, cell)
            pool = flat >= p.min_pool_m2 and lower <= 0.10
            on_road = False
            if road_mask is not None:
                on_road = any(road_mask[r, c] for r, c in cells[s:e])
            # head the fill makes: what the valley's own fall from U to D does not explain
            holds = pool or (head - valley) >= p.min_head_m
            if on_road:
                kind = K_ROAD_POND if holds else K_ROAD
            else:
                kind = K_DAM if holds else K_DRY
            notes, low = [], False
            if open_pool:
                notes.append("the pool behind it reaches the {:.0f} m analysis window: storage is a "
                             "minimum".format(p.window_m))
                low = True
            if crest - spill > 1.0:
                notes.append("the fill is {:.1f} ft higher on the stream line than its spill "
                             "level: water gets round it lower down (a spillway, a saddle or the "
                             "end of the fill)".format((crest - spill) / FT))
            if pool:
                notes.append("flat water surface behind it ({:.2f} ac)".format(flat / AC_M2))
            if not holds and not on_road:
                notes.append("no water held above the downstream ground: a dry / detention dam, a "
                             "levee, or a road / farm crossing not in the road layer")
            if face_open:
                notes.append("the ground still falls at the end of the toe search: the toe may be "
                             "lower (height is a minimum)")
                low = True
            x, y = cell_xy(gt, *rc)
            sp = spill_point(win, u, d, spill, p.level_tol_m)
            spill_xy, sw, sdist = None, 0.0, 0.0
            if sp is not None:
                spill_xy = cell_xy(gt, sp[0][0] + r0, sp[0][1] + c0)
                sw = sp[1] * cell
                sdist = math.hypot(spill_xy[0] - x, spill_xy[1] - y)
                if sdist > 30.0:
                    notes.append("the water gets out {:.0f} ft from the fill on the line (a "
                                 "spillway, a saddle or the end of the fill)".format(sdist / FT))
            found.append(Embankment(
                x=x, y=y, kind=kind, on_road=on_road, crest_m=crest, spill_m=spill, toe_m=toe,
                up_m=up, height_m=height, crest_height_m=crest - toe, head_m=head, valley_drop_m=valley, len_m=(e - s) * cell, storage_m3=storage,
                pool_area_m2=parea, pool=pool, open_pool=open_pool,
                confidence="low" if low else "medium",
                line=li, notes=notes, toe_xy=cell_xy(gt, *cells[kd]), up_xy=cell_xy(gt, *cells[ku]),
                spill_xy=spill_xy, spill_width_m=sw, spill_dist_m=sdist))
    return dedupe(found, p.dedupe_m)


def dedupe(cands, dist_m):
    """Keep the highest of candidates closer than dist_m to each other (one fill crossed by
    several lines, or one line crossing it twice)."""
    out = []
    for c in sorted(cands, key=lambda e: -e.height_m):
        if all(math.hypot(c.x - o.x, c.y - o.y) > dist_m for o in out):
            out.append(c)
    return out


def kind_counts(cands):
    out = {}
    for c in cands:
        out[c.kind] = out.get(c.kind, 0) + 1
    return out
