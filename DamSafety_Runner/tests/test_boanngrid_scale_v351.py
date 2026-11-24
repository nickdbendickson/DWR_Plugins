"""Runner 3.51.0: three bugs found running Absaraka (DWR-2801) at 30 m with Boann ACC on the Linux
CPU build (DAMSAFETY_MF_BACKEND=cpu), Grok Bot for Nick, 2026-11-21.

1. The pre-run LISFLOOD 1-second check started Boann's core/metalflood/engine.py as a program
   (PermissionError, the run stopped in 3 s). In-process engines are now skipped like the built-in.
2. BoannGrid's depression census and crossing reconnection built a `labels == id` mask per
   depression: O(n_depressions x cells), 7+ minutes on a 2001 x 2001 prairie-pothole grid.
3. BoannGrid's flow accumulation iterated the kernels' gather until nothing changed: about one
   sweep per cell of the longest flow path, 12+ minutes on the same grid. It is now summed in Kahn
   topological order.

Each fix keeps the numbers identical; the old implementations are copied here verbatim and the new
code is checked against them on small grids (no flood engine, no LISFLOOD-FP)."""
import os
import subprocess
from collections import deque

import numpy as np

from nd_breach_runner.core import lisflood as lf
from nd_breach_runner.core import solve_gate as sg
from nd_breach_runner.core.metalterrain import audit as A
from nd_breach_runner.core.metalterrain import burn as B
from nd_breach_runner.core.metalterrain import cases
from nd_breach_runner.core.metalterrain import fill as F
from nd_breach_runner.core.metalterrain import flow as FL
from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.tests.registry import Skip, test


def _need_cpu_build():
    if os.name == "nt":
        raise Skip("the BoannGrid CPU build is not made on Windows")
    if not mfb.find_compiler():
        raise Skip("no C++ compiler for the BoannGrid CPU build")


def _potholes(ny=60, nx=80, seed=3):
    """A gently tilted plain with many small closed depressions (prairie potholes) and a few
    road-like ridges across shallow channels, so the census, the reconnection and the flow all see
    hundreds of depressions of every class."""
    rng = np.random.RandomState(seed)
    yy, xx = np.mgrid[0:ny, 0:nx].astype(np.float64)
    z = 300.0 - 0.02 * xx - 0.01 * yy + 0.4 * rng.standard_normal((ny, nx))
    for _ in range(25):
        cy, cx = rng.uniform(0, ny), rng.uniform(0, nx)
        rad, dep = rng.uniform(1.0, 4.0), rng.uniform(0.3, 3.0)
        z -= dep * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2.0 * rad * rad))
    for r in (ny // 3, 2 * ny // 3):
        z[r, :] -= 1.5                       # shallow channels
        c = int(rng.uniform(nx * 0.3, nx * 0.7))
        z[r, c:c + 2] += 2.5                  # a road fill across each
    return z


# ------------------------------------------------------------------- 1: smoke skips Boann
@test
def smoke_skips_every_in_process_engine_without_starting_a_program():
    saved_env = os.environ.pop("ND_DAM_PRE_RUN_SMOKE", None)
    saved_run = subprocess.run

    def _no_run(*a, **k):
        raise AssertionError("lisflood_smoke started a program for an in-process engine: {}".format(a))

    subprocess.run = _no_run
    try:
        engines = [lf.BUILTIN_EXE, lf.GPU_EXE, lf.GPU_FV_EXE, lf.GPU_FV2_EXE, lf.GPU_DG2_EXE,
                   lf.GPU_SGC_EXE, lf.GPU_SUBFV_EXE, lf.GPU_SUBLI_EXE]
        for exe in engines:
            assert lf.is_inprocess(exe), exe
            assert sg.is_inprocess_engine(exe), exe
            hit = sg.lisflood_smoke(exe)
            assert hit["status"] == "ok", (exe, hit)
            assert "inside this process" in hit["detail"], hit
        # what resolve_exe gives for the GPU words reaches the same skip
        assert sg.lisflood_smoke(lf.resolve_exe("gpu"))["status"] == "ok"
    finally:
        subprocess.run = saved_run
        if saved_env is not None:
            os.environ["ND_DAM_PRE_RUN_SMOKE"] = saved_env


@test
def smoke_still_checks_a_real_program_and_a_py_outside_the_plugin():
    import tempfile
    saved_env = os.environ.pop("ND_DAM_PRE_RUN_SMOKE", None)
    d = tempfile.mkdtemp(prefix="ndbr_smk_")
    try:
        assert not sg.is_inprocess_engine("")
        assert not sg.is_inprocess_engine("/usr/bin/lisflood")
        stray = os.path.join(d, "engine.py")            # same name, not this plugin's file
        with open(stray, "w") as fh:
            fh.write("print('x')\n")
        assert not sg.is_inprocess_engine(stray)
        missing = sg.lisflood_smoke(os.path.join(d, "no_such_lisflood"))
        assert missing["status"] == "fail" and "not found" in missing["detail"], missing
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        if saved_env is not None:
            os.environ["ND_DAM_PRE_RUN_SMOKE"] = saved_env


# ------------------------------------------------------- 2: the pre-3.51.0 census / reconnect
def _census_legacy(dem, filled, cell_m=1.0):
    """audit.depression_census before Runner 3.51.0 (one mask per depression), verbatim."""
    a = np.asarray(dem, dtype=np.float64)
    filled = np.asarray(filled, dtype=np.float64)
    delta = A.fill_delta(a, filled)
    mask = np.isfinite(delta) & (delta > A.MIN_DELTA_M)
    labels, n = A._label4(mask)
    cell_area = cell_m * cell_m
    items = []
    for lid in range(1, n + 1):
        m = labels == lid
        rr, cc = np.nonzero(m)
        d = delta[m]
        items.append({
            "id": lid,
            "n_cells": int(m.sum()),
            "area_m2": float(m.sum() * cell_area),
            "volume_m3": float(d.sum() * cell_area),
            "max_depth_m": float(d.max()),
            "floor_elev_m": float(a[m].min()),
            "spill_elev_m": float(filled[m].max()),
            "centroid": (float(rr.mean()), float(cc.mean())),
        })
        h = rr.max() - rr.min() + 1
        w = cc.max() - cc.min() + 1
        aspect = max(h, w) / max(1, min(h, w))
        items[-1]["klass"] = A.classify(items[-1]["n_cells"], items[-1]["max_depth_m"], aspect,
                                        cell_m)
    return labels, items


def _carve_path_legacy(dem, valid, depr, floor):
    ny, nx = dem.shape
    parent = -np.ones((ny, nx), dtype=np.int64)
    q = deque()
    for r, c in np.argwhere(depr):
        parent[r, c] = r * nx + c
        q.append((r, c))
    exit_rc = None
    while q:
        r, c = q.popleft()
        if (not depr[r, c]) and valid[r, c] and dem[r, c] < floor:
            exit_rc = (r, c)
            break
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            rr, cc = r + dr, c + dc
            if 0 <= rr < ny and 0 <= cc < nx and valid[rr, cc] and parent[rr, cc] < 0:
                parent[rr, cc] = r * nx + c
                q.append((rr, cc))
    if exit_rc is None:
        return None
    path = []
    r, c = exit_rc
    while True:
        path.append((r, c))
        p = int(parent[r, c])
        pr, pc = divmod(p, nx)
        if (pr, pc) == (r, c):
            break
        r, c = pr, pc
    path.reverse()
    return path


def _reconnect_legacy(a, labels, items, accum, min_accum_cells, freeze_mask=None):
    """The carve loop of burn.reconnect_crossings before Runner 3.51.0, verbatim."""
    a = np.asarray(a, dtype=np.float64)
    valid = np.isfinite(a) & (a != B.NODATA)
    out = a.copy()
    records = []
    for it in items:
        m = labels == it["id"]
        amax = float(np.nanmax(np.where(m, accum, np.nan)))
        if amax < min_accum_cells:
            continue
        floor = it["floor_elev_m"]
        path = _carve_path_legacy(a, valid, m, floor)
        if path is None or len(path) < 2:
            continue
        exit_rc = path[-1]
        ze = float(a[exit_rc])
        n = len(path)
        if freeze_mask is not None:
            frozen = [(r, c) for (r, c) in path if freeze_mask[r, c]]
            if frozen:
                records.append({"id": it["id"], "pour": path[0], "exit": exit_rc,
                                "barrier_cells": n, "accum_cells": amax, "floor_elev_m": floor,
                                "spill_before_m": it["spill_elev_m"],
                                "refused": "freeze", "frozen_cells": frozen})
                continue
        for k, (r, c) in enumerate(path):
            t = k / (n - 1)
            ramp = floor + t * (ze - floor)
            if ramp < out[r, c]:
                out[r, c] = ramp
        records.append({"id": it["id"], "pour": path[0], "exit": exit_rc,
                        "barrier_cells": n, "accum_cells": amax, "floor_elev_m": floor,
                        "spill_before_m": it["spill_elev_m"]})
    return out, records


def _grids():
    return [("pitted_plane", cases.pitted_plane(ny=50, nx=70), 10.0),
            ("channel_with_barrier", cases.channel_with_barrier(ny=40, nx=90), 10.0),
            ("nodata_ringed_pit", cases.nodata_ringed_pit(n=48), 10.0),
            ("potholes", _potholes(), 30.0)]


@test
def census_is_identical_to_the_per_mask_loop():
    _need_cpu_build()
    seen = 0
    for name, z, cell in _grids():
        filled, _ = F.fill_dem(z, backend="cpu")
        lab_old, it_old = _census_legacy(z, filled, cell_m=cell)
        lab_new, it_new = A.depression_census(z, filled, cell_m=cell)
        assert np.array_equal(lab_old, lab_new), name
        assert it_old == it_new, name            # every field, bit for bit
        seen += len(it_new)
    assert seen > 30, seen                       # the grids really have depressions


@test
def reconnect_is_identical_to_the_per_mask_loop():
    _need_cpu_build()
    carved = 0
    for name, z, cell in _grids():
        filled, _ = F.fill_dem(z, backend="cpu")
        labels, items = A.depression_census(z, filled, cell_m=cell)
        _d, accum, _i = FL.flow_accum(z, filled=filled, backend="cpu")
        for mn in (3, 20):
            for freeze in (None, "row"):
                fm = None
                if freeze:
                    fm = np.zeros(z.shape, dtype=bool)
                    fm[:, z.shape[1] // 2] = True
                old_out, old_rec = _reconnect_legacy(z, labels, items, accum, mn, freeze_mask=fm)
                new_out, new_rec = B.reconnect_crossings(
                    z, min_accum_cells=mn, cell_m=cell, backend="cpu", filled=filled,
                    labels=labels, items=items, accum=accum, freeze_mask=fm,
                    classes=None, max_cut_m=None)      # no limits: the pre-3.57.0 carve
                assert np.array_equal(old_out, new_out, equal_nan=True), (name, mn, freeze)
                assert old_rec == new_rec, (name, mn, freeze)
                carved += len(new_rec)
    assert carved > 0, carved


# ----------------------------------------------------------- 3: topological accumulation
@test
def flow_topo_accum_is_identical_to_the_kernel_iteration():
    _need_cpu_build()
    for name, z, _cell in _grids():
        for conn in (4, 8):
            d_old, a_old, i_old = FL.flow_accum(z, backend="cpu", conn=conn, method="iterate")
            d_new, a_new, i_new = FL.flow_accum(z, backend="cpu", conn=conn, method="topo")
            assert i_old["converged"] and i_new["converged"], (name, conn, i_old, i_new)
            assert i_new["method"] == "topo" and i_old["method"] == "iterate"
            assert np.array_equal(d_old, d_new), (name, conn)
            assert np.array_equal(a_old, a_new, equal_nan=True), (name, conn)


@test
def flow_topo_accum_on_a_raw_unfilled_surface_matches_too():
    """Unfilled ground (interior sinks, flats in float32): the directions still point strictly
    downhill, so the network is acyclic and both methods agree there as well."""
    _need_cpu_build()
    z = _potholes(ny=40, nx=50, seed=11)
    filled = np.asarray(z, dtype=np.float64)       # pass the raw surface as 'filled'
    d_old, a_old, _ = FL.flow_accum(z, backend="cpu", filled=filled, method="iterate")
    d_new, a_new, info = FL.flow_accum(z, backend="cpu", filled=filled)
    assert info["converged"], info
    assert np.array_equal(d_old, d_new)
    assert np.array_equal(a_old, a_new, equal_nan=True)


@test
def flow_method_is_checked():
    try:
        FL.flow_accum(np.zeros((3, 3)), method="nope")
    except ValueError:
        return
    raise AssertionError("an unknown method was accepted")


if __name__ == "__main__":
    import sys
    fails = 0
    for fn in (smoke_skips_every_in_process_engine_without_starting_a_program,
               smoke_still_checks_a_real_program_and_a_py_outside_the_plugin,
               census_is_identical_to_the_per_mask_loop,
               reconnect_is_identical_to_the_per_mask_loop,
               flow_topo_accum_is_identical_to_the_kernel_iteration,
               flow_topo_accum_on_a_raw_unfilled_surface_matches_too,
               flow_method_is_checked):
        try:
            fn()
            print("ok    ", fn.__name__)
        except Skip as e:
            print("skip  ", fn.__name__, "({})".format(e))
        except Exception:
            import traceback
            fails += 1
            print("FAIL  ", fn.__name__)
            traceback.print_exc()
    sys.exit(1 if fails else 0)
