"""Reproduces the Linux HEC-RAS 6.6 chain on HEC's Muncie example (plan p04) through
core/hecras_engine.py and compares the max WSE with HEC's own run inputs.

    python -m nd_breach_runner.tools.hecras_wine.muncie_check <Muncie source folder> \
        <reference .p04 results hdf> [work folder inside the Wine prefix's drive_c]

Needs NDBREACH_HECRAS_WINEPREFIX, NDBREACH_HECRAS_LINUX_DIR, an X display for Wine
(NDBREACH_HECRAS_DISPLAY) and, when HEC's TCU dialog shows, the user's
NDBREACH_HECRAS_TCU_HOOK. Small (about 2 minutes); not a dam run."""
import os
import shutil
import sys
import time

import numpy as np

from nd_breach_runner.core import hecras_engine as he


def seeds_from_text(g_text_path, out_csv):
    L = open(g_text_path, encoding="latin-1").read().splitlines()
    i = next(k for k, s in enumerate(L) if s.startswith("Storage Area 2D Points="))
    n = int(L[i].split("=")[1])
    v, k = [], i + 1
    while len(v) < 2 * n:
        s = L[k]
        v += [float(s[j:j + 16]) for j in range(0, len(s.rstrip()), 16)]
        k += 1
    np.savetxt(out_csv, np.array(v).reshape(-1, 2), delimiter=",", fmt="%.6f")
    return n


def main(src, ref_hdf, work=None):
    import h5py
    d = he.require()
    work = work or os.path.join(d["prefix"], "drive_c", "ndb_muncie")
    M = os.path.join(work, "Muncie")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    shutil.copytree(src, M)
    for f in os.listdir(M):
        if f.endswith((".tmp.hdf",)) or f[-4:-2] in (".b", ".x") or (
                ".p0" in f and f.endswith(".hdf")):
            os.remove(os.path.join(M, f))
    t0 = time.time()
    seeds = os.path.join(work, "seeds.csv")
    print("seeds", seeds_from_text(os.path.join(M, "Muncie.g04"), seeds))
    with h5py.File(os.path.join(M, "Muncie.g04.hdf"), "r+") as f:
        a = "Geometry/2D Flow Areas/2D Interior Area"
        if a in f:
            del f[a]
    prj = "NAD 1983 StatePlane Indiana East FIPS 1301 (US Feet).prj"
    os.rename(os.path.join(M, "Terrain"), os.path.join(M, "Terrain_src"))
    os.makedirs(os.path.join(M, "Terrain"))
    for f in os.listdir(os.path.join(M, "Terrain_src")):
        if f.startswith("TerrainWithChannel") or f == prj:
            shutil.copy2(os.path.join(M, "Terrain_src", f), os.path.join(M, "Terrain"))
    times = he.preprocess(M, "Muncie", plan="04", geom="04", d=d,
                          steps=("terrain", "mesh", "assoc", "props", "ras"),
                          terrain_tif=os.path.join(M, "Terrain_src", "Terrain.muncie_clip.tif"),
                          prj=os.path.join(M, "Terrain", prj),
                          mesh=("2D Interior Area", seeds), log=print)
    print("preprocess", times)
    out = he.compute(M, "Muncie", plan="04", d=d, log=print)
    print("results", out, "total {:.0f} s".format(time.time() - t0))
    ts = ("Results/Unsteady/Output/Output Blocks/Base Output/Unsteady Time Series/"
          "2D Flow Areas/2D Interior Area/Water Surface")
    G = "Geometry/2D Flow Areas/2D Interior Area/"
    with h5py.File(ref_hdf, "r") as A, h5py.File(out, "r") as B:
        ca, cb = A[G + "Cells Center Coordinate"][()], B[G + "Cells Center Coordinate"][()]
        idx = {tuple(np.round(c, 1)): i for i, c in enumerate(ca)}
        m = np.array([idx.get(tuple(np.round(c, 1)), -1) for c in cb])
        ok = m >= 0
        a, b = A[ts][()][:, m[ok]].max(0), B[ts][()][:, ok].max(0)
        mn = A[G + "Cells Minimum Elevation"][()][m[ok]]
    wet = (a - mn) > 0.01
    diff = np.abs(a - b)[wet]
    print("cells matched {} of {}; wet ref {} gen {}; max-WSE |diff| ft max {:.5f} mean {:.5f}"
          .format(int(ok.sum()), len(cb), int(wet.sum()), int(((b - mn) > 0.01).sum()),
                  diff.max(), diff.mean()))
    return 0 if diff.max() < 0.05 else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
