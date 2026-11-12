"""0.4.3 (audit P3-PMP, 2026-11-07): the vectorised storm step must match the original
per-point loop bit for bit, because the port has to match ArcGIS. Pure numpy, no GDAL, no data:
py2round_array against py2round on 10,000 random values including exact .x5 ties, and
storm_step against the 0.4.1 Python loop on random TAF / TRANS / DAD values."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np

from core.pmp import py2round, py2round_array, storm_step, factor_arrays

rng = np.random.default_rng(20261107)

# 1. random values, several magnitudes, both signs, n = 0 / 1 / 2 / 3
vals = np.concatenate([
    rng.uniform(0.0, 60.0, 5000),                      # rainfall depths and products
    rng.uniform(0.0, 3.0, 2000) * rng.uniform(0.5, 1.5, 2000),
    -rng.uniform(0.0, 10.0, 500),
    rng.uniform(0.0, 1e-3, 500),
    rng.uniform(1e3, 1e6, 500),
])
# exact binary ties: k/8 (x.125, x.375, x.625, x.875 are exact doubles), k/4, k/2, k/20 etc.
ties = np.concatenate([
    (rng.integers(0, 800, 800) + 0.5),                 # n = 0 ties
    (rng.integers(0, 800, 800) / 4.0 + 0.125),         # x.125 / x.375 ... exact in binary
    (rng.integers(0, 400, 400) / 8.0),                 # x.125 steps
    np.array([0.25, 0.5, 1.5, 2.5, 0.125, 0.375, 1.0005, 2.675, 1.005, 0.045, 10.5, -0.5, -2.5,
              -0.25, 1e-7, 123456.785, 0.0, 7.0]),
])
allv = np.concatenate([vals, ties])
assert allv.size >= 10000, allv.size
for n in (0, 1, 2, 3):
    want = np.array([py2round(float(v), n) for v in allv])
    got = py2round_array(allv, n)
    bad = np.nonzero(want != got)[0]
    assert bad.size == 0, (n, [(float(allv[i]), want[i], got[i]) for i in bad[:10]])
    # bit-exact, not just close: the float repr too
    assert all(repr(float(a)) == repr(float(b)) for a, b in zip(want[:2000], got[:2000]))
# NaN passes through, None raises as the scalar does, shape and scalar input kept
assert np.isnan(py2round_array(np.array([np.nan, 1.25]), 1)[0])
assert py2round_array(np.array([[1.25, 2.5], [0.05, -0.05]]), 1).shape == (2, 2)
try:
    py2round_array(np.array([1.0, None], dtype=object), 1)
    raise AssertionError("None must raise TypeError")
except TypeError:
    pass
print("OK (py2round_array == py2round on %d values incl. ties)" % allv.size)


# 2. the storm step against the original loop (pmp.py of 0.4.1)
def loop_step(vals, drivers, s, y, fac, ids):
    trans = 0
    for k, pid in enumerate(ids):
        taf, tr = fac[pid]
        if tr == 1:
            trans += 1
            adj = py2round(y * taf, 1)
            if adj > vals[k]:
                vals[k] = adj; drivers[k] = s
    return trans


ids = list(range(1000, 1000 + 1500))
for trial in range(12):
    storms = ["SPAS_%04d" % k for k in range(8)]
    fac = {}
    for s in storms:
        tafs = rng.uniform(0.6, 1.4, len(ids))
        tafs = np.round(tafs, 3)                                  # the GDB holds 3-decimal TAFs
        trans = (rng.uniform(0, 1, len(ids)) < rng.uniform(0.0, 1.0)).astype(int)
        if trial == 3 and s == storms[2]:
            trans[:] = 0                                          # a storm with no zone here
        nulls = rng.uniform(0, 1, len(ids)) < (0.02 if trial % 2 else 0.0)
        fac[s] = {pid: ((None if (nulls[k] and trans[k] == 0) else float(tafs[k])),
                        (None if (nulls[k] and trans[k] == 0) else int(trans[k])))
                  for k, pid in enumerate(ids)}
    ys = {s: py2round(float(rng.uniform(1.0, 40.0)), 3) for s in storms}
    if trial == 5:
        ys = {s: 12.345 for s in storms}                          # many exact ties between storms
    v_loop = [0.0] * len(ids); d_loop = ["STORM"] * len(ids)
    v_vec = np.zeros(len(ids)); d_vec = np.array(["STORM"] * len(ids), dtype=object)
    kept_loop, kept_vec = list(storms), list(storms)
    for s in storms:
        t1 = loop_step(v_loop, d_loop, s, ys[s], fac[s], ids)
        taf, tr = factor_arrays(fac[s], ids)
        t2 = storm_step(v_vec, d_vec, s, ys[s], taf, tr)
        assert t1 == t2, (trial, s, t1, t2)
        if t1 == 0:
            kept_loop.remove(s); kept_vec.remove(s)
    assert [float(v) for v in v_vec] == v_loop, trial
    assert [str(x) for x in d_vec] == d_loop, trial
    assert kept_loop == kept_vec
print("OK (storm_step == the original per-point loop on 12 random trials)")
