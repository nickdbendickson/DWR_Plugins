"""Embankments across streams found in a LiDAR DEM (Finder 0.1).

Pure numpy / scipy, no QGIS or GDAL, so it is unit-tested outside QGIS. Metres, cubic metres,
an equal-area grid (EPSG:5070 in the plugins). The same file is in both Finder plugins.

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
    long fill (road, rail, canal or levee)
                           no road / rail in the layers, but the raised fill runs on for
                           LONG_FILL_M or more (a highway, rail grade, canal bank or levee the
                           layers miss; a farm dam is a short fill ending on the valley sides)

  6. LENGTH OF THE RAISED FILL (ridge_len): the ground minus its grey opening (a running
     min-then-max over ridge_width_m) is how far each cell stands above its surroundings; the
     cells standing more than max(0.5 m, 30 % of the crest height) above them that touch the crest
     make the fill, and its length is their extent along its long axis (within ridge_half_m of
     the crest). In Wells County (2026-11-12, 88 fills marked from the aerial) every dam a
     reviewer confirmed was under 400 m; 41 of the 76 rejected fills were 600 m or more.

  Finder 0.5 adds evidence fields (they describe a fill; none of them drops one, except that one
  pool is held by one fill - item 7):
  7. THE POOL'S OUTLET. A dam is the fill at its pool's way out. outlet_dist = how far the spill
     point (step 5) is from the raised fill (step 6); within outlet_m the fill holds the pool at
     its outlet (overtopping or an auxiliary spillway at its end), further away the pool gets out
     somewhere else and this hump is not what holds it. Several humps whose pools get out at the
     same spill point (spoil heaps round one dug pit, a road crossing a pond) are one pool: only
     the fill nearest that outlet is kept (one_per_pool). Contributing area at the crest (from
     the caller's flow grid) and its ratio to storage: a "dam" with almost no drainage is a pit,
     a hilltop or a ridge nose.
  8. ABOVE ORIGINAL GROUND (the NRCS 378 test: an embankment pond holds 3 ft or more of water
     against its fill above the original ground on the centerline; a dug pond holds none).
     Original ground under the fill on the stream line is put back by a straight line from where
     the pool's water line meets the ground upstream on the line to the toe (or, when the pool
     runs off the line, from the toe up the valley's own fall below it). ag_head = spill - that
     ground. below_frac = the share of the pool's volume below that original ground, carried up
     the valley at its own fall from the crest: ~1 for a pit dug into level ground, ~0 for a pool
     in a valley behind a fill. Neither reads the pool surface. This is reasoning from the definitions, not a published method.
  9. SHAPE. Of the pool at the spill level: rectangularity (area / smallest rectangle), elongation
     (long / short side), the median slope of its inner wall (dug pits ~1.5:1 = 0.67), the count
     of raised spoil mounds within spoil_m of it that are not the fill. Of the fill on the stream
     line: the flat top width (within 0.15 m of the crest) and the side-slope sum (upstream +
     downstream face, horizontal per vertical; NRCS fills are 5:1 or flatter).
  10. TERRAIN round the crest (terrain_m): relief (95th - 5th percentile ground) and median slope,
     for terrain-aware scoring.

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

from . import ridges as rd

FT = 0.3048
AF_M3 = 1233.4818375
AC_M2 = 4046.8564224

K_DAM = "dam"
K_DRY = "dry embankment"
K_ROAD_POND = "road fill (ponding)"
K_ROAD = "road fill"
K_LONG = "long fill (road, rail, canal or levee)"
KINDS = (K_DAM, K_DRY, K_ROAD_POND, K_ROAD, K_LONG)


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
    long_fill_m: float = 600.0          # a raised fill this long is a road / rail / canal / levee
    ridge_width_m: float = 75.0         # wider than a fill: the opening that takes it away
    ridge_half_m: float = 600.0         # half-width of the window the fill length is measured in
    outlet_m: float = 45.0              # spill point this close to the fill = it holds the outlet
    one_per_pool: bool = True           # keep only the fill nearest each pool's outlet
    spoil_m: float = 40.0               # spoil mounds looked for this far from the pool
    terrain_m: float = 600.0            # half-width of the window for relief / slope


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
    ridge_len_m: float = 0.0            # length of the raised fill along its own axis (step 6)
    # Finder 0.5 evidence (steps 7-10); NaN = not measured
    outlet_dist_m: float = float("nan")  # spill point to the raised fill (step 7)
    spill_on_fill: bool = False         # outlet_dist <= outlet_m
    same_pool: int = 0                  # other humps on this pool that were dropped
    drain_m2: float = float("nan")      # contributing area at the crest
    pool_xy: tuple = None               # centre of the pool at the spill level
    orig_m: float = float("nan")        # original ground under the fill on the line (step 8)
    ag_head_m: float = float("nan")     # spill - original ground
    below_frac: float = float("nan")    # share of the pool volume below the ring's ground
    rect: float = float("nan")          # pool rectangularity (step 9)
    elong: float = float("nan")         # pool long / short side
    wall_slope: float = float("nan")    # median inner-wall slope of the pool (rise / run)
    mounds: int = 0                     # spoil mounds beside the pool, not the fill
    top_w_m: float = float("nan")       # flat top of the fill on the line
    side_sum: float = float("nan")      # upstream + downstream face, H per V
    relief_m: float = float("nan")      # terrain round the crest (step 10)
    slope_pct: float = float("nan")
    fill_road: bool = False             # the raised fill touches a road / rail (road-seeded)


def _need_scipy():
    if ndi is None:
        raise RuntimeError("scipy is required for the embankment finder")


def line_cells(xy, gt, shape, with_dist=False):
    """Grid cells (row, col) along a polyline, in order, each once (consecutive duplicates
    dropped), sampled every half cell. `gt` is a GDAL geotransform with no rotation. with_dist:
    also the distance along the line (m) of each cell - the middle of the stretch of line inside
    it. The samples are one cell apart across the grid and up to 1.41 cells apart on the skew, so
    a profile's index times the cell size is not metres: use these distances."""
    cell = abs(gt[1])
    out, spans = [], []
    last = None
    acc = 0.0
    pts = [(float(x), float(y)) for x, y in xy]
    for (x0, y0), (x1, y1) in zip(pts[:-1], pts[1:]):
        seg = math.hypot(x1 - x0, y1 - y0)
        n = max(1, int(math.ceil(seg / (cell * 0.5))))
        for k in range(n + 1):
            t = k / n
            x, y = x0 + t * (x1 - x0), y0 + t * (y1 - y0)
            c = int(math.floor((x - gt[0]) / gt[1]))
            r = int(math.floor((y - gt[3]) / gt[5]))
            if not (0 <= r < shape[0] and 0 <= c < shape[1]):
                continue
            if (r, c) != last:
                out.append((r, c))
                spans.append([acc + t * seg, acc + t * seg])
                last = (r, c)
            else:
                spans[-1][1] = acc + t * seg
        acc += seg
    if with_dist:
        return out, [0.5 * (a + b) for a, b in spans]
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


def face_toe(z, e, toe_k, cell, face_deg, m=3, bench_m=40.0):
    """(index of the toe, still falling at the end of the search) below a hump ending at e: the
    face goes on while the ground falls faster than tan(face_deg) over m samples; where it
    flattens, the walk carries on only if the face steepens again within bench_m (a bench on the
    face, not the valley floor), so the toe is the outlet-channel ground at the foot of the fill,
    not the top of a bench part-way down. The toe is the lowest ground from e to where it stops
    (natural valley slopes are gentler, so it does not run on down the valley). None when there is
    no ground below the hump."""
    end = min(len(z) - 1, e + toe_k)
    step = m * cell * math.tan(math.radians(face_deg))
    bench = max(m + 1, int(round(bench_m / cell)))
    k = e
    cut = False
    while k < end:
        k2 = min(end, k + m)
        if not (np.isfinite(z[k]) and np.isfinite(z[k2])):
            break
        if z[k] - z[k2] >= step * (k2 - k) / m:
            k += 1                                      # still on the steep face
            continue
        # flattened: a bench only if the ground falls as steeply as a face again within bench_m
        # (a slope test - a valley floor of a few per cent falls a lot over bench_m too, but never
        # as steeply as a fill's face)
        again = None
        for j in range(k + 1, min(end, k + bench) - m + 1):
            if np.isfinite(z[j]) and np.isfinite(z[j + m]) and z[j] - z[j + m] >= step:
                again = j
                break
        if again is not None:
            k = again
            continue
        cut = k + bench > end                           # the look-ahead ran into the search end
        break                                           # the valley floor / outlet channel: stop
    seg = z[e:k + 1]
    if not np.isfinite(seg).any():
        return None, False
    return e + int(np.nanargmin(seg)), bool(k >= end or cut)


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
    m = pool_mask(win, u, level)
    if not m.any():
        return 0.0, 0.0, False
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


def raised_fill(dem, r, c, cell, crest_height, width_m=75.0, half_m=600.0):
    """(length m, mask, (r0, c0), rel) of the raised fill through cell (r, c): the cells standing
    more than max(0.5 m, 0.3 x crest_height) above the grey opening of the ground (size width_m)
    that connect to the crest, measured along their long axis, within half_m of the crest. `mask`
    is that fill in the window whose top-left cell of `dem` is (r0, c0); `rel` is every cell's
    height above the opening there. Length 0 (and an empty mask) when the crest does not stand out
    (a fill wider than width_m)."""
    half = max(4, int(round(half_m / cell)))
    r0, r1, c0, c1 = _window(dem, r, c, half)
    win = dem[r0:r1, c0:c1]
    ok = np.isfinite(win)
    empty = np.zeros(win.shape, bool)
    if not ok.any():
        return 0.0, empty, (r0, c0), np.zeros(win.shape)
    zz = np.where(ok, win, np.nanmax(win))
    k = max(3, int(round(width_m / cell)) | 1)
    rel = np.where(ok, zz - ndi.grey_opening(zz, size=(k, k)), 0.0)
    m = (rel > max(0.5, 0.3 * crest_height)) & ok
    lab, _n = ndi.label(m)
    rr, cc = r - r0, c - c0
    sub = lab[max(0, rr - 3):rr + 4, max(0, cc - 3):cc + 4]
    ids = sub[sub > 0]
    if not len(ids):
        return 0.0, empty, (r0, c0), rel
    fill = lab == np.bincount(ids).argmax()
    ys, xs = np.nonzero(fill)
    if len(xs) < 3:
        return float(len(xs) * cell), fill, (r0, c0), rel
    pts = np.c_[xs, ys].astype(float) * cell
    pts -= pts.mean(axis=0)
    _w, v = np.linalg.eigh(np.cov(pts.T))
    pr = pts @ v[:, -1]
    return float(pr.max() - pr.min() + cell), fill, (r0, c0), rel


def ridge_length(dem, r, c, cell, crest_height, width_m=75.0, half_m=600.0):
    """Length (m) of the raised fill through cell (r, c) (see raised_fill)."""
    return raised_fill(dem, r, c, cell, crest_height, width_m, half_m)[0]


def pool_mask(win, u, level):
    """The pool of {ground < level} holding cell u of `win` (bool grid; all False when u is not
    under the level)."""
    g = np.where(np.isfinite(win), win, np.inf)
    lab, _n = ndi.label(g < level)
    k = lab[u]
    if k == 0:
        return np.zeros(win.shape, bool)
    return lab == k


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


def original_ground(z, ks, kc, kd, spill, cell, down_m=150.0, tol_m=0.15, beyond_m=60.0):
    """(original ground at the crest index kc, its rise per metre going upstream, how it was
    found) of a downstream-ordered profile z whose hump starts at ks and has its toe at kd and
    spill level `spill`. At the crest: a straight line from where the pool's water line meets the
    ground upstream on the line (the first sample above the hump at or over the spill level) to the toe
    - the valley floor the fill was built on; when the pool runs off the line upstream, the toe
    plus the valley's own fall below it (the profile over down_m below the toe) over the
    crest-to-toe distance. The rise carried on upstream is the smaller of that line's and the
    valley's own fall below the toe (the pool can end on another fill upstream, which is not
    ground). Never reads the pool surface: both ends are ground the water does not cover.
    The fall below the toe is fitted only up to the next hump (another fill or a road below this
    one is not the valley); a pool end whose ground drops back below the water line within
    beyond_m further up is the face of another fill, not the valley side, and is not used.
    (NaN, 0, "") when neither can be had."""
    toe = float(z[kd])
    fall = None
    n = max(3, int(round(down_m / cell)))
    seg = z[kd:min(len(z), kd + n + 1)]
    run, stop = np.inf, len(seg)
    for i, v in enumerate(seg):
        if not np.isfinite(v):
            continue
        if v > run + tol_m:                             # the next hump starts: stop the fit
            stop = i
            break
        run = min(run, v)
    seg = seg[:stop]
    f = np.isfinite(seg)
    if f.sum() >= 3:
        xs = np.flatnonzero(f) * cell
        fall = max(0.0, -float(np.polyfit(xs, seg[f], 1)[0]))
    kp = None
    for k in range(ks - 1, -1, -1):
        if not np.isfinite(z[k]):
            break
        if z[k] >= spill:
            kp = k
            break
    if kp is not None:
        far = z[max(0, kp - max(2, int(round(beyond_m / cell)))):kp]
        if np.isfinite(far).any() and np.nanmin(far) < spill - tol_m:
            kp = None                                   # another fill's face, not the valley side
    if kp is not None and kd > kp:
        rise = (float(z[kp]) - toe) / ((kd - kp) * cell)
        orig = toe + rise * (kd - kc) * cell
        return orig, rise if fall is None else min(rise, fall), "pool end to toe"
    if fall is not None:
        return toe + fall * (kd - kc) * cell, fall, "valley fall below the toe"
    return float("nan"), 0.0, ""


def below_grade(win, pool, spill, crest_rc, orig, rise, cell):
    """Share of the pool's volume (at the spill level) below the original ground: the line of
    original_ground carried upstream from the crest (orig at the crest, `rise` per metre away from
    it). A pool behind a fill sits on the old valley floor - on that line or above it (~0); a pit
    dug into the ground is below it (~1). NaN without a pool or an original ground."""
    if not pool.any() or not math.isfinite(orig):
        return float("nan")
    pr, pc = np.nonzero(pool)
    zp = win[pr, pc]
    dist = np.hypot(pr - crest_rc[0], pc - crest_rc[1]) * cell
    grade = orig + rise * dist
    depth = spill - zp
    tot = float(np.nansum(depth))
    if tot <= 0:
        return float("nan")
    return float(np.nansum(np.clip(grade - zp, 0.0, depth))) / tot


def pool_shape(win, pool, cell):
    """(rectangularity, elongation, median inner-wall slope) of a pool mask: its area over its
    smallest rectangle's, that rectangle's long over short side, and the median ground slope
    (rise per run) over its edge cells (the pool's own wall). NaNs for fewer than 4 cells."""
    rows, cols = np.nonzero(pool)
    if len(rows) < 4:
        return float("nan"), float("nan"), float("nan")
    area, a, b = rd.min_rect(rows, cols, cell)
    rect = float(len(rows) * cell * cell / area) if area > 0 else float("nan")
    elong = float(a / b) if b > 0 else float("nan")
    r0, r1 = max(0, rows.min() - 1), min(win.shape[0], rows.max() + 2)
    c0, c1 = max(0, cols.min() - 1), min(win.shape[1], cols.max() + 2)
    sub = win[r0:r1, c0:c1]
    pm = pool[r0:r1, c0:c1]
    edge = pm & ~ndi.binary_erosion(pm, border_value=0)
    gz = np.where(np.isfinite(sub), sub, np.nanmedian(sub))
    gy, gx = np.gradient(gz, cell)
    slope = np.hypot(gx, gy)[edge]
    return rect, elong, float(np.median(slope)) if len(slope) else float("nan")


def spoil_mounds(rel, fill, pool, cell, near_m=40.0, min_m=0.5, min_cells=3):
    """Raised mounds (rel > min_m, at least min_cells) within near_m of the pool that are not the
    fill (`rel`, `fill`, `pool`: grids of one window): the spoil heaps a dug pit has along its
    sides. Count."""
    if not pool.any():
        return 0
    near = ndi.binary_dilation(pool, iterations=max(1, int(round(near_m / cell))))
    m = (rel > min_m) & near & ~pool & ~ndi.binary_dilation(fill, iterations=1)
    lab, n = ndi.label(m)
    if not n:
        return 0
    sizes = np.bincount(lab.ravel())[1:]
    return int((sizes >= min_cells).sum())


def fill_profile(z, s, kc, kd, crest, up, toe, cell, top_tol=0.15, d=None):
    """(flat top width m, side-slope sum H per V) of the fill on the stream profile: the run of
    samples round the crest within top_tol of it, and the upstream face (hump start to crest,
    over crest - upstream level) plus the downstream face (crest to toe, over crest - toe). A
    line crossing the fill on the skew reads both wider / flatter than square across. d: the
    distance along the line of each sample (line_cells with_dist); without it, index x cell."""
    # positions are interpolated where the profile crosses each level, so the answer does not
    # depend on where the samples happen to fall on the face; each face's slope is read over its
    # middle half (25 % to 75 % of its height), clear of the rounded top and the toe
    def cross(level, step, stop):
        j = kc
        while j != stop:
            a_, b_ = z[j], z[j + step]
            if not (np.isfinite(a_) and np.isfinite(b_)):
                return None
            if a_ >= level >= b_ and a_ != b_:
                return j + step * (a_ - level) / (a_ - b_)
            j += step
        return None

    def run(i, j):                                      # metres between fractional indices
        if d is None:
            return abs(j - i) * cell
        k = np.arange(len(d))
        return abs(float(np.interp(j, k, d)) - float(np.interp(i, k, d)))

    lo, hi = max(s - 1, 0), kd
    ta, tb = cross(crest - top_tol, -1, lo), cross(crest - top_tol, 1, hi)
    top = run(ta, tb) if ta is not None and tb is not None else float("nan")
    hu, hd = crest - up, crest - toe
    side = float("nan")
    if hu > 0.2 and hd > 0.2:
        u75, u25 = cross(up + 0.75 * hu, -1, lo), cross(up + 0.25 * hu, -1, lo)
        d75, d25 = cross(toe + 0.75 * hd, 1, hi), cross(toe + 0.25 * hd, 1, hi)
        if None not in (u75, u25, d75, d25):
            side = run(u25, u75) / (0.5 * hu) + run(d75, d25) / (0.5 * hd)
    return top, side


def terrain_round(dem, r, c, cell, half_m=600.0):
    """(relief m, median slope %) of the ground within half_m of cell (r, c): the 95th minus the
    5th percentile, and the median of the slope - flat prairie, rolling drift, cut breaks /
    badlands."""
    half = max(4, int(round(half_m / cell)))
    r0, r1, c0, c1 = _window(dem, r, c, half)
    sub = dem[r0:r1:2, c0:c1:2]
    f = np.isfinite(sub)
    if f.sum() < 16:
        return float("nan"), float("nan")
    relief = float(np.percentile(sub[f], 95) - np.percentile(sub[f], 5))
    gz = np.where(f, sub, np.nanmedian(sub))
    gy, gx = np.gradient(gz, 2.0 * cell)
    return relief, float(np.median(np.hypot(gx, gy)[f]) * 100.0)


def one_per_pool(cands, cell=3.0, level_tol=0.15):
    """One fill per pool: humps whose pools get out at the same spill point (within 2 cells, at
    the same level) hold one pool - keep the one nearest that outlet (then the higher), count
    the others on it (same_pool)."""
    out, groups = [], {}
    for c in cands:
        if not c.spill_xy:
            out.append(c)
            continue
        key = None
        for k, g in groups.items():
            h = g[0]
            if (math.hypot(c.spill_xy[0] - h.spill_xy[0], c.spill_xy[1] - h.spill_xy[1]) <= 2.0 * cell
                    and abs(c.spill_m - h.spill_m) <= level_tol):
                key = k
                break
        if key is None:
            groups[len(groups)] = [c]
        else:
            groups[key].append(c)

    def near(e):
        d = e.outlet_dist_m if math.isfinite(e.outlet_dist_m) else e.spill_dist_m
        return (d, -e.height_m)

    for g in groups.values():
        g.sort(key=near)
        if len(g) > 1:                                  # a second pass (across tiles) adds up
            g[0].same_pool += sum(1 + e.same_pool for e in g[1:])
            g[0].notes = [t for t in g[0].notes if not t.endswith("(one fill per pool)")]
            g[0].notes.append("{} other hump(s) on this pool dropped (one fill per pool)".format(g[0].same_pool))
        out.append(g[0])
    return out


def find(dem, gt, lines, road_mask=None, params=None, keep=None, cancelled=None, drain_area=None):
    """Embankments across the stream `lines` (lists of (x, y) in the grid CRS) of `dem` (2-D, m,
    NaN or <= -9998 = no data) with geotransform `gt` (square cells, north up).
    `road_mask`: bool grid, True on roads / railroads (dilated by the caller).
    `keep(x, y)`: optional filter on the crest point (e.g. the core of a tile).
    `drain_area(x, y)`: optional contributing area (m2) at a point, from the caller's flow grid.
    Returns a list of Embankment, one per pool (params.one_per_pool), deduplicated, highest
    first."""
    _need_scipy()
    p = params or FinderParams()
    dem = np.asarray(dem, float).copy()
    dem[dem <= -9998.0] = np.nan
    cell = abs(gt[1])
    half = max(4, int(round(p.window_m / cell)))
    found = []
    for li, xy in enumerate(lines):
        if cancelled is not None and li % 50 == 0 and cancelled():
            break
        if len(xy) < 2:
            continue
        cells, dist = line_cells(xy, gt, dem.shape, with_dist=True)
        if len(cells) < 5:
            continue
        # metres between profile samples on this line (1 to 1.41 cells, by its angle to the grid)
        cl = (dist[-1] - dist[0]) / (len(cells) - 1)
        cl = cl if cl > 0 else cell
        max_len = max(1, int(round(p.max_len_m / cl)))
        toe_k = max(1, int(round(p.toe_search_m / cl)))
        z = np.array([dem[r, c] for r, c in cells])
        if orient_down(z):
            cells, z, dist = cells[::-1], z[::-1], dist[::-1]
        dist = np.asarray(dist, float)
        for s, e in humps(z, p.tol_m, max_len):
            k_crest = s + int(np.nanargmax(z[s:e]))
            crest = float(z[k_crest])
            ku = s - 1
            up = float(z[ku])
            kd, face_open = face_toe(z, e, toe_k, cl, p.face_deg)
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
            valley = upstream_slope(z, ku, toe_k, cl) * (kd - ku) * cl
            pm = pool_mask(win, u, spill)
            storage = float((spill - win[pm]).sum() * cell * cell)
            parea = float(pm.sum() * cell * cell)
            open_pool = bool(pm[0, :].any() or pm[-1, :].any() or pm[:, 0].any() or pm[:, -1].any())
            if storage < p.min_storage_m3:
                continue
            flat, lower = flat_pool_area(win, u, up, p.flat_tol_m, cell)
            pool = flat >= p.min_pool_m2 and lower <= 0.10
            on_road = False
            if road_mask is not None:
                on_road = any(road_mask[r, c] for r, c in cells[s:e])
            # Holds water = the fill impounds water ABOVE the downstream ground: the upstream level
            # clears the toe by more than the valley's own fall from U to D (head - valley), by at
            # least min_head_m. A flat water surface alone (`pool`) is NOT that - an excavated
            # dugout or a gravel pit has standing water with no fill holding it above the ground
            # below, and used to be labelled a dam on the strength of that surface. The pool is
            # still reported (a note and the pool / pool_area fields), but it no longer, on its own,
            # makes the fill a dam.
            holds = (head - valley) >= p.min_head_m
            ridge, fmask, (fr0, fc0), rel = raised_fill(dem, rc[0], rc[1], cell, crest - toe,
                                                         p.ridge_width_m, p.ridge_half_m)
            if on_road:
                kind = K_ROAD_POND if holds else K_ROAD
            elif ridge >= p.long_fill_m:
                kind = K_LONG
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
            if kind == K_LONG:
                notes.append("the raised fill runs on for {:.0f} ft: a highway, rail grade, canal "
                             "bank or levee the road layers miss, not a farm dam".format(ridge / FT))
            elif not holds and not on_road:
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
            # --- Finder 0.5 evidence (steps 7-10) ---
            fr, fc = np.nonzero(fmask)
            outlet = float("nan")
            if sp is not None:
                if len(fr):
                    outlet = float(np.hypot(fr + fr0 - (sp[0][0] + r0), fc + fc0 - (sp[0][1] + c0)).min() * cell)
                else:
                    outlet = sdist
                if outlet > p.outlet_m:
                    notes.append("the pool gets out {:.0f} ft from this fill: something else holds "
                                 "it".format(outlet / FT))
            fill_road = bool(road_mask is not None and len(fr)
                             and road_mask[fr + fr0, fc + fc0].any())
            pool_xy = None
            if pm.any():
                pr_, pc_ = np.nonzero(pm)
                pool_xy = cell_xy(gt, float(pr_.mean()) + r0, float(pc_.mean()) + c0)
            orig, rise, _how = original_ground(z, s, k_crest, kd, spill, cl, p.toe_search_m)
            ag = spill - orig if math.isfinite(orig) else float("nan")
            below = below_grade(win, pm, spill, (rc[0] - r0, rc[1] - c0), orig, rise, cell)
            if math.isfinite(ag) and ag < 3.0 * FT and holds:
                notes.append("holds {:.1f} ft above the original ground at the fill (under the "
                             "NRCS 378 3 ft line: a dug pond, not an embankment pond)".format(ag / FT))
            if math.isfinite(below) and below >= 0.5:
                notes.append("{:.0f} % of the pool is below the ground round it (dug)".format(100 * below))
            if pm.sum() <= 40000:
                rect, elong, wall = pool_shape(win, pm, cell)
            else:
                rect, elong, wall = float("nan"), float("nan"), float("nan")
            # the pool (analysis window) and the raised fill (its own window) on their overlap
            R0, R1 = max(r0, fr0), min(r0 + pm.shape[0], fr0 + rel.shape[0])
            C0, C1 = max(c0, fc0), min(c0 + pm.shape[1], fc0 + rel.shape[1])
            mounds = 0
            if R1 > R0 and C1 > C0:
                mounds = spoil_mounds(rel[R0 - fr0:R1 - fr0, C0 - fc0:C1 - fc0],
                                      fmask[R0 - fr0:R1 - fr0, C0 - fc0:C1 - fc0],
                                      pm[R0 - r0:R1 - r0, C0 - c0:C1 - c0], cell, p.spoil_m)
            top_w, side = fill_profile(z, s, k_crest, kd, crest, up, toe, cl, d=dist)
            relief, slope = terrain_round(dem, rc[0], rc[1], cell, p.terrain_m)
            drain = float(drain_area(x, y)) if drain_area is not None else float("nan")
            found.append(Embankment(
                x=x, y=y, kind=kind, on_road=on_road, crest_m=crest, spill_m=spill, toe_m=toe,
                up_m=up, height_m=height, crest_height_m=crest - toe, head_m=head, valley_drop_m=valley, len_m=(e - s) * cl, storage_m3=storage,
                pool_area_m2=parea, pool=pool, open_pool=open_pool,
                confidence="low" if low else "medium",
                line=li, notes=notes, toe_xy=cell_xy(gt, *cells[kd]), up_xy=cell_xy(gt, *cells[ku]),
                spill_xy=spill_xy, spill_width_m=sw, spill_dist_m=sdist, ridge_len_m=ridge,
                outlet_dist_m=outlet, spill_on_fill=bool(math.isfinite(outlet) and outlet <= p.outlet_m),
                drain_m2=drain, pool_xy=pool_xy, orig_m=orig, ag_head_m=ag, below_frac=below,
                rect=rect, elong=elong, wall_slope=wall, mounds=mounds, top_w_m=top_w, side_sum=side,
                relief_m=relief, slope_pct=slope, fill_road=fill_road))
    if p.one_per_pool:
        found = one_per_pool(found, cell)
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
