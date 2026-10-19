"""M18 regression: the storage-leak guard must extend the stage-storage curve past the spill
level up to the crest for a dam of KNOWN height, with the reservoir area at the spill level.
Ported semantics from nd_dam_screening/core/storage.py (estimate_storage, 0.19.0).

Before the fix estimate_storage used above_pool_curve_guarded's curve as-is, so the curve
truncated at the spill level and understated storage for known-height dams.

Real scipy is broken in this sandbox (its extension modules fail to import), so storage.ndi is
None and the estimator cannot run. We inject a small pure-numpy fake of the two ndimage routines
the estimator uses: `label` is exact (4-connectivity flood fill); `distance_transform_edt` is a
fast two-pass chamfer approximation. Distance precision only shifts the below-pool constant and
the shore slope, neither of which affects the crest-extension assertions here.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))
import storage  # noqa: E402


class _FakeNdi(object):
    @staticmethod
    def label(binary):
        b = np.asarray(binary, bool)
        lab = np.zeros(b.shape, dtype=int)
        h, w = b.shape
        cur = 0
        for i in range(h):
            for j in range(w):
                if b[i, j] and lab[i, j] == 0:
                    cur += 1
                    stack = [(i, j)]
                    lab[i, j] = cur
                    while stack:
                        y, x = stack.pop()
                        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                            ny, nx = y + dy, x + dx
                            if 0 <= ny < h and 0 <= nx < w and b[ny, nx] and lab[ny, nx] == 0:
                                lab[ny, nx] = cur
                                stack.append((ny, nx))
        return lab, cur

    @staticmethod
    def distance_transform_edt(mask):
        m = np.asarray(mask, bool)
        h, w = m.shape
        INF = 1e12
        d = [[0.0 if not m[i, j] else INF for j in range(w)] for i in range(h)]
        s = 1.4142135623730951
        for i in range(h):
            di = d[i]
            dp = d[i - 1] if i > 0 else None
            for j in range(w):
                if di[j] == 0.0:
                    continue
                best = di[j]
                if dp is not None:
                    if dp[j] + 1.0 < best:
                        best = dp[j] + 1.0
                    if j > 0 and dp[j - 1] + s < best:
                        best = dp[j - 1] + s
                    if j < w - 1 and dp[j + 1] + s < best:
                        best = dp[j + 1] + s
                if j > 0 and di[j - 1] + 1.0 < best:
                    best = di[j - 1] + 1.0
                di[j] = best
        for i in range(h - 1, -1, -1):
            di = d[i]
            dn = d[i + 1] if i < h - 1 else None
            for j in range(w - 1, -1, -1):
                best = di[j]
                if dn is not None:
                    if dn[j] + 1.0 < best:
                        best = dn[j] + 1.0
                    if j < w - 1 and dn[j + 1] + s < best:
                        best = dn[j + 1] + s
                    if j > 0 and dn[j - 1] + s < best:
                        best = dn[j - 1] + s
                if j < w - 1 and di[j + 1] + 1.0 < best:
                    best = di[j + 1] + 1.0
                di[j] = best
        return np.array(d)


# storage.ndi is None here (broken scipy); give the estimator a working stand-in.
storage.ndi = _FakeNdi()


def _basin(n=120, cell=2.0, saddle=103.0):
    """A bowl (rim at 105 m) around a pond at 100 m, with a saddle at `saddle` m on the east side
    that opens onto a wide plain at 101 m (faithful to nd_ test_dwr_kinds_019.basin)."""
    yy, xx = np.mgrid[0:n, 0:n].astype(float)
    r = np.hypot(yy - n / 2, xx - n / 3) * cell
    dem = np.where(r < 40, 99.0 + r / 40.0, 105.0)                 # bowl, rim 105
    dem[:, int(n * 0.62):] = 101.0                                  # the plain beyond the rim
    band = (np.abs(yy - n / 2) < 3) & (xx > n / 3 + 18) & (xx < n * 0.62)
    dem[band] = saddle                                             # the saddle in the rim
    water = r < 15
    dem[water] = 100.0
    return dem, water, cell


DAM_RC = (60, 40)                                                  # on the pond centre (n/2, n/3)


def test_guard_reports_a_spill_for_a_leaking_basin():
    dem, water, cell = _basin()
    free = storage.above_pool_curve(dem, water, 100.0, 105.0, cell)
    curve, spill, note = storage.above_pool_curve_guarded(dem, water, 100.0, 105.0, cell)
    assert spill is not None and 101.0 < spill <= 103.5, spill
    assert curve[-1][1] < 0.5 * free[-1][1], (curve[-1][1], free[-1][1])   # the plain is not storage
    assert "spill level" in note


def test_known_height_extends_the_curve_to_the_crest():
    """The M18 fix: with a known height the curve must not stop at the spill level."""
    dem, water, cell = _basin()
    r = storage.estimate_storage(dem, water, cell, DAM_RC, known_height_m=5.0)
    # a dam of known height holds water to its crest, above the spill level
    assert r.crest_elev > 103.0, r.crest_elev                               # crest well above the spill
    assert abs(r.curve[-1][0] - r.crest_elev) < 1e-6, (r.curve[-1][0], r.crest_elev)
    assert any("leak at the spill level" in x and "goes on to the crest" in x for x in r.notes), r.notes
    assert r.confidence == "low"
    assert all(b[1] >= a[1] - 1e-9 for a, b in zip(r.curve, r.curve[1:]))     # still a rising curve
    # the extended top must exceed the storage held only to the spill level (no understatement)
    held_top = storage.above_pool_curve_guarded(dem, water, 100.0, r.crest_elev, cell)[0][-1][1]
    assert r.above_pool_m3 > held_top + 1.0, (r.above_pool_m3, held_top)


def test_no_height_holds_crest_at_the_spill():
    """With no known height there is nothing to extend to: crest is held at the spill level."""
    dem, water, cell = _basin()
    r = storage.estimate_storage(dem, water, cell, DAM_RC)
    assert r.crest_elev <= 103.0 + 1e-6, r.crest_elev
    assert any("held at the spill level" in x for x in r.notes), r.notes
    assert r.confidence == "low"


def test_no_leak_curve_is_unchanged():
    """A rim with no low saddle does not leak: spill is None and the curve is the plain DEM curve."""
    dem, water, cell = _basin(saddle=106.0)
    curve, spill, _n = storage.above_pool_curve_guarded(dem, water, 100.0, 105.0, cell)
    assert spill is None
    free = storage.above_pool_curve(dem, water, 100.0, 105.0, cell)
    assert abs(curve[-1][1] - free[-1][1]) < 1e-6
    r = storage.estimate_storage(dem, water, cell, DAM_RC, known_height_m=5.0)
    assert not any("spill level" in x for x in r.notes), r.notes


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("ok  ", _name)
    print("test_m18_leak_crest: all passed")
