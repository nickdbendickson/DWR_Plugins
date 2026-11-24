"""Boann's sub-grid local-inertial solver: the Metal / CPU kernel and the engine around it.

kernels_subli.metal (built as C++ via cpu_kernels_subli.cpp) is the float32 mirror of the float64
reference subli_ref.py, whose own tests (test_subli.py) pin the scheme. Here:

  - the kernel's buffer order and Params match their Python descriptions;
  - the CPU build reproduces the reference step for step on a fixed dt, with and without the
    paper's upwind flux weighting, both friction forms, 1-D and 2-D friction, both inflow orders,
    a point source and open edges - on a rough valley with varying roughness whose ground sits
    1,400 m up (the float32 trap);
  - on flat cells (r = 1) the kernel is the SHIPPED plain ACC kernel;
  - a lake at rest over sub-grid terrain stays at rest in float32, and a closed basin keeps its
    volume at the full CFL step;
  - the engine: an r = 1 deck gives the plain ACC engine's flood, the wet window does not change
    the answer, a deck whose size is not a multiple of r still drains off its open edges, and the
    downscaled rasters are consistent;
  - what it is for: a 5 m meandering valley run on 20 m cells gives the 5 m run's maximum depths
    to a few centimetres, where plain 20 m cells are 0.2 m off - and so is this solver with the
    upwind weighting on, which is why the weighting is off by default;
  - on a Mac, the Apple GPU (Metal) gives what the CPU build gives.

Run standalone:  PYTHONPATH=. python3 DamSafety_Runner/tests/test_subli_kernel.py
"""
import ctypes
import os
import shutil
import sys
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test, Skip
from nd_breach_runner.core.metalflood import subgrid as sg
from nd_breach_runner.core.metalflood import subli_ref as sl
from nd_breach_runner.core.metalflood import backends as be
from nd_breach_runner.core.metalflood import subli_backend as lb
from nd_breach_runner.core.metalflood import engine as mfe
from nd_breach_runner.core.metalflood import engine_subli as le
from nd_breach_runner.core.metalflood.cases import read_asc, valley_deck
from nd_breach_runner.tests.test_subli import valley, _dam_break_start, _acc_kernel, _flat_case

NODATA = -9999.0
S_T, S_HMAX, S_VOUT, S_CLIP = 0, 2, 5, 6


def _need_cpu_build():
    if be.prebuilt_available(lb.SUBLI_SPEC):
        return
    if os.name == "nt":
        raise Skip("the engine's CPU build is not made on Windows (a release ships it prebuilt)")
    if not be.find_compiler():
        raise Skip("no C++ compiler for the engine's CPU build")


class _Kernel:
    """The kernel on one backend, set up from a subgrid.Tables with the engine's own packing."""

    def __init__(self, back, t, V0, free_mask=0, src_cell=None, Q=0.0, dt0=1.0, weighting="upwind",
                 fric2d=True, lf_order=True, friction="compound", ok=None, alpha=0.7, t_end=1.0e9):
        self.back, self.t = back, t
        ny, nx, r = t.ny, t.nx, t.r
        self.shape = (ny, nx)
        ok = np.ones((ny, nx), bool) if ok is None else ok
        n_src = 0 if src_cell is None else 1
        a = self.a = back.allocate(lb.subli_buffer_layout(ny, nx, n_src, 1, 1, r))
        self.z0 = le.pack_tables(a, t, ok)
        le.set_volume(a, t, ok, V0)
        a["scl"][:] = 1.0
        a["tmax"][:] = NODATA
        a["arr"][:] = NODATA
        a["ser_off"][:] = np.array([0, 1], np.uint32)
        a["ser_t"][:] = 0.0
        a["ser_q"][:] = Q
        if n_src:
            a["s_cell"][:] = src_cell[0] * nx + src_cell[1]
            a["s_series"][:] = 0
        p = self.p = lb.SubLIParams()
        p.nx, p.ny, p.r0, p.c0, p.wr, p.wc = nx, ny, 0, 0, ny, nx
        p.free_mask, p.n_src, p.n_series = free_mask, n_src, 1
        p.fric2d, p.clip_end, p.inflow_lf_order = int(fric2d), 0, int(lf_order)
        p.r, p.m = r, r * r
        p.weighting, p.friction = lb.WEIGHTING[weighting], lb.FRICTION[friction]
        p.dx, p.dt0, p.alpha, p.g = t.dx, dt0, alpha, sg.G
        p.dry, p.maxhflow, p.arr_depth = 1e-3, 10.0, 0.05
        p.t0, p.t_end, p.wsub = 0.0, t_end, t.wsub

    def run(self, nsteps):
        self.back.run_batch(self.p, nsteps)
        return self

    def grid(self, name):
        return self.a[name].reshape(self.shape).astype(np.float64)

    def eta(self):
        return self.t.zbed + self.grid("d")

    def time(self):
        return float(self.a["st"].view(np.float32)[S_T])

    def close(self):
        self.back.close()


def _high_valley(n_var=True, ny_c=6, nx_c=14, r=4, dxf=2.5):
    """test_subli's rough valley with varying roughness, lifted to 1,400 m (float32 keeps 1e-4 m
    there, so a kernel that worked in absolute elevations would show it)."""
    dem, n = valley(ny_c, nx_c, r, dxf, n_var=n_var)
    return sg.build_from_fine(dem + 1350.0, n, dxf * r, r)


def _against_reference(weighting, fric2d=True, lf_order=True, friction="compound",
                       free_mask=sl.FREE_E, nsteps=600, dt0=0.25):
    """(max |eta kernel - eta reference| over the run's wet cells, relative volume difference,
    relative edge-outflow difference, max |speed| difference, the reference's deepest water and
    fastest flow) after nsteps of a fixed dt0 from a dam-break start with a point source."""
    t = _high_valley()
    eta0 = _dam_break_start(t)
    V0 = sg.cell_volume(eta0, t.s, t.asub)
    src_cell, Q = (3, 1), 5.0
    ref = sl.SubLIRef(t, free_mask=free_mask, weighting=weighting, fric2d=fric2d,
                      inflow_lf_order=lf_order, friction=friction)
    ref.set_volume(V0)
    src = np.zeros((t.ny, t.nx))
    src[src_cell] = Q
    out_ref = 0.0
    vmax = np.zeros((t.ny, t.nx))
    for _ in range(nsteps):
        out_ref += ref.step(dt0, src=src)
        vmax = np.maximum(vmax, ref.speed())
    k = _Kernel(be.CpuBackend(lb.SUBLI_SPEC), t, V0, free_mask, src_cell, Q, dt0, weighting,
                fric2d, lf_order, friction)
    try:
        k.run(nsteps)
        assert abs(k.time() - nsteps * dt0) < 1e-3 * nsteps * dt0, \
            "kernel dt not locked: {} vs {}".format(k.time(), nsteps * dt0)
        wet = ref.depth() > 0.01
        deta = float(np.abs(k.eta() - ref.eta)[wet].max())
        dvol = abs(k.grid("V").sum() - ref.V.sum()) / ref.V.sum()
        out_k = float(k.a["st"].view(np.float32)[S_VOUT])
        dout = abs(out_k - out_ref) / max(out_ref, 1e-9)
        dv = float(np.abs(k.grid("vmax") - vmax).max())
        assert ref.clipped == 0.0 and int(k.a["st"][S_CLIP]) == 0
        return deta, dvol, dout, dv, float(ref.depth().max()), float(vmax.max()), out_ref
    finally:
        k.close()


# -- the kernel is the reference -------------------------------------------------------------------

@test
def subli_layout_matches_kernel():
    """subli_buffer_layout is in the exact order of the kernel's buffer indices B(0)..B(27), Params
    follows them, and the CPU build's Params struct is SubLIParams byte for byte in size."""
    _need_cpu_build()
    names = [n for n, _, _ in lb.subli_buffer_layout(3, 4, 1, 1, 2, 2)]
    with open(lb.SUBLI_KERNEL_SOURCE, encoding="utf-8") as f:
        src = f.read()
    for k, n in enumerate(names):
        assert "{} B({})".format(n, k) in src, "buffer {} not at B({}) in the kernel".format(n, k)
    assert "constant Params& p B({})".format(len(names)) in src, "Params not after the buffers"
    assert len(names) + 1 <= 31, "over Metal's 31 buffer slots"
    for k in lb.SUBLI_KERNELS:
        assert "kernel void {}(MF_ARGS_SUBLI)".format(k) in src, k
    lib = ctypes.CDLL(be.build_cpu_library(lb.SUBLI_SPEC))
    assert lib.mf_params_size() == ctypes.sizeof(lb.SubLIParams), "Params size mismatch"
    # the struct's members, in order, are SubLIParams' fields
    body = src[src.index("struct Params {"):src.index("};", src.index("struct Params {"))]
    members = []
    for line in body.splitlines()[1:]:
        decl = line.split("//")[0].strip().rstrip(";")
        if decl:
            members += [m.strip() for m in decl.split(None, 1)[1].split(",")]
    assert members == [n for n, _ in lb.SubLIParams._fields_], members


@test
def subli_kernel_matches_reference_with_and_without_weighting():
    """The CPU build of kernels_subli.metal against the float64 reference, step for step over a
    dam break with a point source and an open edge on a rough valley 1,400 m up: with the paper's
    upwind weighting and without, the two agree to float32 round-off (5e-7 m when written)."""
    _need_cpu_build()
    for weighting in ("upwind", "none"):
        deta, dvol, dout, dv, hmax, vmax, out = _against_reference(weighting)
        assert hmax > 0.5 and vmax > 0.5 and out > 10.0, (weighting, hmax, vmax, out)
        assert deta < 1e-4, "{}: kernel eta vs reference {:.2e} m".format(weighting, deta)
        assert dvol < 1e-5, "{}: kernel volume vs reference {:.2e}".format(weighting, dvol)
        assert dout < 1e-3, "{}: kernel edge outflow vs reference {:.2e}".format(weighting, dout)
        assert dv < 1e-3, "{}: kernel peak speed vs reference {:.2e} m/s".format(weighting, dv)


@test
def subli_kernel_matches_reference_for_each_option():
    """The same gate for the switches: the paper's mean-depth friction, 1-D friction, the
    mass-conserving inflow order, and all four edges open."""
    _need_cpu_build()
    for kw in ({"friction": "mean"}, {"fric2d": False}, {"lf_order": False},
               {"free_mask": sl.FREE_N | sl.FREE_S | sl.FREE_E | sl.FREE_W}):
        deta, dvol, dout, dv, hmax, vmax, out = _against_reference("upwind", **kw)
        assert hmax > 0.5 and vmax > 0.5, (kw, hmax, vmax)
        assert deta < 1e-4 and dvol < 1e-5 and dout < 1e-3 and dv < 1e-3, (kw, deta, dvol, dout, dv)


@test
def subli_kernel_on_flat_cells_is_the_shipped_acc_kernel():
    """r = 1: every coarse cell is one flat fine cell, and the sub-grid kernel must give the plain
    ACC kernel's (kernels.metal) depths and speeds at its default theta 1 - with the 1-D friction
    and the mass-conserving inflow order, and with LISFLOOD-FP 8's 2-D friction and inflow order
    (what a Runner deck runs)."""
    _need_cpu_build()
    z, n, h0, dx = _flat_case()
    ny, nx = z.shape
    src_cell, Q, dt0, nsteps = (4, 2), 30.0, 0.5, 500
    t = sg.build_from_fine(z, n, dx, 1)
    for fric2d, lf in ((False, False), (True, True)):
        h_acc, v_acc, _ = _acc_kernel(z, n, h0, dx, 4, src_cell, Q, dt0, nsteps, 1.0, fric2d, lf)
        k = _Kernel(be.CpuBackend(lb.SUBLI_SPEC), t, h0 * dx * dx, sl.FREE_E, src_cell, Q, dt0,
                    "none", fric2d, lf)
        try:
            k.run(nsteps)
            dh = float(np.abs(k.grid("d") - h_acc).max())
            dv = float(np.abs(k.grid("vmax") - v_acc).max())
        finally:
            k.close()
        assert h_acc.max() > 0.3 and v_acc.max() > 0.5
        assert dh < 1e-4 and dv < 1e-3, "2-D friction {}: depth {:.2e} m, speed {:.2e} m/s off " \
            "the ACC kernel".format(fric2d, dh, dv)


@test
def subli_kernel_lake_at_rest_and_closed_basin():
    """float32: a level lake over rough sub-grid terrain 1,400 m up stays at rest (no speed above
    1 mm/s, no cell's level moves 0.1 mm in 2,000 steps), and a dam break in a closed basin keeps
    its volume at the full CFL step with nothing clipped."""
    _need_cpu_build()
    t = _high_valley(ny_c=6, nx_c=12)
    lake = np.full((t.ny, t.nx), float(t.zbed.max()) + 0.6)
    V0 = sg.cell_volume(lake, t.s, t.asub)
    k = _Kernel(be.CpuBackend(lb.SUBLI_SPEC), t, V0, dt0=0.4)
    try:
        k.run(2000)
        assert float(k.grid("vmax").max()) < 1e-3, k.grid("vmax").max()
        assert float(np.abs(k.eta() - lake).max()) < 1e-4, np.abs(k.eta() - lake).max()
    finally:
        k.close()
    V0 = sg.cell_volume(_dam_break_start(t), t.s, t.asub)
    k = _Kernel(be.CpuBackend(lb.SUBLI_SPEC), t, V0, dt0=1.0e9, t_end=600.0)
    try:
        k.run(100000)
        assert k.time() > 599.0, k.time()
        assert int(k.a["st"][S_CLIP]) == 0
        V = k.grid("V")
        assert V.min() >= 0.0 and abs(V.sum() - V0.sum()) < 2e-6 * V0.sum(), (V.sum(), V0.sum())
        assert float(k.grid("vmax").max()) > 1.0, "the dam break made no flow"
    finally:
        k.close()


@test
def subli_gpu_matches_cpu():
    """The Apple-GPU (Metal) build against the CPU build of the same kernel source, with and
    without the flux weighting: a dam break with a source and an open edge. Runs only on a Mac with
    a Metal GPU (the macOS CI job, a Mac with the plugin); skipped in the cloud."""
    ok, why = be.metal_available()
    if not ok:
        raise Skip("no Metal GPU here ({})".format(why))
    t = _high_valley()
    V0 = sg.cell_volume(_dam_break_start(t), t.s, t.asub)
    for weighting in ("upwind", "none"):
        res = []
        for back in (be.CpuBackend(lb.SUBLI_SPEC), be.MetalBackend(lb.SUBLI_SPEC)):
            k = _Kernel(back, t, V0, sl.FREE_E, (3, 1), 5.0, 0.25, weighting)
            try:
                k.run(600)
                res.append((k.grid("d"), k.grid("V"), k.grid("vmax"), k.grid("hmax")))
            finally:
                k.close()
        (dc, Vc, vc, hc), (dg, Vg, vg, hg) = res
        assert np.abs(dg - dc).max() < 1e-4, "{}: GPU vs CPU depth {:.2e} m".format(
            weighting, np.abs(dg - dc).max())
        assert np.abs(hg - hc).max() < 1e-4 and np.abs(vg - vc).max() < 1e-3, weighting
        assert abs(Vg.sum() - Vc.sum()) < 1e-5 * Vc.sum(), weighting
    # and a whole deck through the engine (wet window, sources, open edges, fine rasters)
    d = tempfile.mkdtemp(prefix="subli_")
    try:
        sc = le.run_par(_fine_valley(os.path.join(d, "c"), ["subgrid 4"]), backend="cpu")
        sg_ = le.run_par(_fine_valley(os.path.join(d, "g"), ["subgrid 4"]), backend="metal")
        assert sg_["backend"] == "metal"
        assert abs(sc["steps"] - sg_["steps"]) <= max(3, sc["steps"] // 200), (sc, sg_)
        for ext, tol in (("max", 0.01), ("maxVc", 0.1), ("inittm", 0.01)):
            x, y = _res(os.path.join(d, "c"), ext), _res(os.path.join(d, "g"), ext)
            m = (x != NODATA) & (y != NODATA)
            assert np.abs(x - y)[m].max() <= tol, (ext, np.abs(x - y)[m].max())
        assert abs(sg_["mass_error_m3"]) < 1e-5 * sg_["injected_m3"], sg_
    finally:
        shutil.rmtree(d, True)


# -- the engine ------------------------------------------------------------------------------------

def _res(folder, ext):
    return read_asc(os.path.join(folder, "results", "run." + ext))


@test
def subli_engine_at_r1_is_the_plain_acc_engine():
    """A whole deck (meandering valley, roughness file, start pond, no-data hole, breach
    hydrograph, four open edges) run with 'subgrid 1' against the plain ACC engine: the same
    steps, the same maximum depths, arrival and speeds, the same water in and out."""
    _need_cpu_build()
    d = tempfile.mkdtemp(prefix="subli_")
    try:
        sa = mfe.run_par(valley_deck(os.path.join(d, "a")), backend="cpu")
        sb = le.run_par(valley_deck(os.path.join(d, "b"), extra=["subgrid 1"]), backend="cpu")
        assert sb["weighting"] == "none" and sb["subgrid_r"] == 1
        assert abs(sa["steps"] - sb["steps"]) <= 2, (sa["steps"], sb["steps"])
        ha, hb = _res(os.path.join(d, "a"), "max"), _res(os.path.join(d, "b"), "max")
        assert np.array_equal(ha == NODATA, hb == NODATA)
        assert np.abs(ha - hb).max() < 1e-3, np.abs(ha - hb).max()
        real = ha > 0.01
        assert real.sum() > 500
        for ext, tol in (("mxe", 1e-3), ("inittm", 0.01), ("maxVc", 0.01)):
            x, y = _res(os.path.join(d, "a"), ext)[real], _res(os.path.join(d, "b"), ext)[real]
            assert np.percentile(np.abs(x - y), 99) <= tol, (ext, np.abs(x - y).max())
        for key in ("injected_m3", "outflow_m3", "final_volume_m3"):
            assert abs(sa[key] - sb[key]) < 1e-4 * sa["injected_m3"], (key, sa[key], sb[key])
        assert abs(sb["mass_error_m3"]) < 1e-5 * sb["injected_m3"], sb
    finally:
        shutil.rmtree(d, True)


def _fine_valley(folder, extra=(), ny=118, nx=178, sim_s=1.5 * 3600.0, **kw):
    """The meandering valley deck at a 5 m cell. The default size is NOT a whole number of 4-cell
    coarse cells in either direction (the engine pads it)."""
    return valley_deck(folder, ny=ny, nx=nx, dx=5.0, sim_s=sim_s, peak_cms=250.0,
                       extra=list(extra), **kw)


@test
def subli_engine_runs_a_fine_deck_on_coarse_cells():
    """A 5 m deck run on 20 m cells (r = 4): it conserves water, clips nothing, drains off the
    open edges although the deck is not a whole number of coarse cells, and writes fine rasters
    that agree with each other (surface = ground + depth, arrival and time of maximum exactly
    where the fine cell got wet, speed only on wet cells, one level surface per coarse cell). The
    wet window does not change a number."""
    _need_cpu_build()
    d = tempfile.mkdtemp(prefix="subli_")
    try:
        w = os.path.join(d, "w")
        s = le.run_par(_fine_valley(w, ["subgrid 4"]), backend="cpu")
        assert s["weighting"] == "none" and s["subgrid_r"] == 4
        assert s["coarse_cells"] == 30 * 45 and s["fine_cells"] == 118 * 178
        assert s["injected_m3"] > 1e5
        assert abs(s["mass_error_m3"]) < 1e-5 * s["injected_m3"], s
        assert s["clipped_m3"] < 1e-6 * s["injected_m3"], s
        assert s["outflow_m3"] > 0.3 * s["injected_m3"], s
        z = read_asc(os.path.join(w, "run.dem.asc"))
        hmax, mxe, arr, tmax, vel = (_res(w, e) for e in ("max", "mxe", "inittm", "maxtm", "maxVc"))
        assert hmax.shape == z.shape == (118, 178)
        valid = z != NODATA
        assert (~valid).sum() > 0 and np.array_equal(hmax == NODATA, ~valid)
        wet = valid & (hmax > 0)
        assert wet.sum() > 2000 and (valid & ~wet).sum() > 2000
        assert np.abs(mxe[wet] - (z[wet] + hmax[wet])).max() < 2e-3
        for g in (mxe, tmax):
            assert np.array_equal(g != NODATA, wet)
        assert (arr[~wet] == NODATA).all() and (vel[valid & ~wet] == 0).all()
        deep = wet & (hmax > 0.2)
        assert (arr[deep] != NODATA).all() and (arr[deep] <= tmax[deep] + 1e-3).all()
        assert vel[deep].max() > 0.5 and vel[wet].max() < 15.0, vel[wet].max()
        blk = np.where(wet, mxe, np.nan)[:116, :176].reshape(29, 4, 44, 4)
        some = np.isfinite(blk).any(axis=(1, 3))
        hi = np.nanmax(np.where(np.isfinite(blk), blk, -np.inf), axis=(1, 3))
        lo = np.nanmin(np.where(np.isfinite(blk), blk, np.inf), axis=(1, 3))
        assert (hi - lo)[some].max() < 2e-3, (hi - lo)[some].max()
        # the wet window against the whole grid every step
        f = os.path.join(d, "f")
        s2 = le.run_par(_fine_valley(f, ["subgrid 4"]), backend="cpu", full_grid=True)
        assert s2["steps"] == s["steps"]
        for e in ("max", "inittm", "maxVc", "maxtm"):
            assert np.array_equal(_res(w, e), _res(f, e)), e
    finally:
        shutil.rmtree(d, True)


def _vs_fine(h, fine, wet):
    dd = (h - fine)[wet]
    return float(dd.mean()), float(np.median(np.abs(dd)))


@test
def subli_coarse_cells_give_the_fine_run_and_the_weighting_spoils_it():
    """What the solver is for, and why the paper's flux weighting is off by default. The 5 m
    meandering valley, with the 5 m plain ACC run as the answer (maximum depth where it is over
    0.1 m):

      - on 20 m cells (r = 4) this solver is within 5 cm of it (median miss 2.4 cm, mean -1.4 cm
        when written; 0.9 cm median at r = 2), in about a fifth of the steps on a sixteenth of
        the cells;
      - the same run with 'upwind' reads more than 0.15 m deep (+0.23 m when written, at r = 2 and
        r = 4 alike): the weighting blends a face with the face upwind along the grid axis, which
        on a reach that crosses the grid at an angle lies partly outside the channel, and the
        blend takes momentum out of the flow. Plain 20 m cells are as far off (+0.22 m).

    If the second half ever fails because the weighted run got close to the fine run, that is good
    news: look again at the default (engine_subli.DEFAULT_WEIGHTING)."""
    _need_cpu_build()
    d = tempfile.mkdtemp(prefix="subli_")
    try:
        kw = dict(ny=120, nx=180, sim_s=2400.0, free="NSE", hole=False, pond=False, gauge=False)
        sa = mfe.run_par(_fine_valley(os.path.join(d, "fine"), **kw), backend="cpu")
        fine = _res(os.path.join(d, "fine"), "max")
        wet = fine > 0.1
        assert wet.sum() > 3000
        runs = {}
        for name, extra in (("r4", ["subgrid 4"]), ("r2", ["subgrid 2"]),
                            ("r4 upwind", ["subgrid 4", "upwind"])):
            f = os.path.join(d, name.replace(" ", "_"))
            s = le.run_par(_fine_valley(f, extra, **kw), backend="cpu")
            assert abs(s["mass_error_m3"]) < 1e-5 * s["injected_m3"] and s["clipped_m3"] == 0, s
            runs[name] = (s,) + _vs_fine(_res(f, "max"), fine, wet)
        s4, mean4, med4 = runs["r4"]
        s2, mean2, med2 = runs["r2"]
        su, meanu, medu = runs["r4 upwind"]
        assert s4["weighting"] == "none" and su["weighting"] == "upwind"
        assert abs(mean4) < 0.05 and med4 < 0.05, (mean4, med4)
        assert abs(mean2) < 0.03 and med2 < 0.03, (mean2, med2)
        assert s4["steps"] < 0.3 * sa["steps"] and s2["steps"] < 0.6 * sa["steps"], \
            (s4["steps"], s2["steps"], sa["steps"])
        assert meanu > 0.15, "the weighted run is within {:.3f} m of the fine run".format(meanu)
    finally:
        shutil.rmtree(d, True)


@test
def subli_engine_reads_its_options_from_the_deck():
    """'upwind' turns the paper's flux weighting on and 'noupwind' off; without either the default
    (off) applies; 'theta' does not pick a weighting here; 'subgrid' outside 1-8 is refused."""
    d = tempfile.mkdtemp(prefix="subli_")
    try:
        def deck(name, extra):
            return le.Deck(valley_deck(os.path.join(d, name), ny=12, nx=16, extra=extra))
        assert le.DEFAULT_WEIGHTING == "none"
        assert deck("a", []).weighting == "none" and deck("a2", []).subgrid_r == le.DEFAULT_R == 4
        assert deck("b", ["upwind"]).weighting == "upwind"
        assert deck("c", ["noupwind"]).weighting == "none"
        assert deck("e", ["theta 0.7", "cfl 0.5"]).weighting == "none"
        assert deck("f", ["subgrid 8"]).subgrid_r == 8
        for bad in ("0", "9", "x"):
            try:
                deck("bad" + bad, ["subgrid " + bad])
            except le.EngineError:
                continue
            raise AssertionError("subgrid {} was accepted".format(bad))
    finally:
        shutil.rmtree(d, True)


# -- the Runner ------------------------------------------------------------------------------------

@test
def subli_engine_is_a_runner_engine_choice():
    """Runner 3.79.0: 'subli' (or 'gpu-subli' / 'sub-grid local inertial' ...) selects Boann's
    sub-grid local-inertial solver. It runs in-process, is ACC family (not shock-capturing, so a
    rerun for a lost balance uses the ACC Courant number), is labelled as not LISFLOOD-FP
    everywhere including the classification record, is distinct from every other engine and is a
    FLOOD_ENGINE choice appended after the others; 'subgrid r' and 'upwind' reach the .par for it,
    and 'upwind' for no other engine; model.subgrid_upwind is the headless key."""
    from nd_breach_runner.core import lisflood as lf
    from nd_breach_runner.core import workflow
    from nd_breach_runner.damsuite import record
    from nd_breach_runner.core.breach import Hydrograph
    from nd_breach_runner.core.grid import Grid
    from nd_breach_runner.core.metalflood import cli
    for w in ("subli", "SUBLI", " gpu-subli ", "metal-subli", "sub-grid local inertial"):
        assert lf.resolve_exe(w) == lf.GPU_SUBLI_EXE, w
    e = lf.GPU_SUBLI_EXE
    assert os.path.isfile(e)
    assert lf.is_gpu_subli(e) and lf.is_inprocess(e) and lf.is_gpu_any(e)
    assert not lf.is_gpu_shock(e) and lf.stable_cfl(e) == lf.STABLE_CFL
    assert not (lf.is_gpu(e) or lf.is_gpu_fv_any(e) or lf.is_gpu_dg2(e) or lf.is_gpu_sgc(e)
                or lf.is_gpu_subfv(e))
    for other in (lf.GPU_EXE, lf.GPU_FV_EXE, lf.GPU_FV2_EXE, lf.GPU_DG2_EXE, lf.GPU_SGC_EXE,
                  lf.GPU_SUBFV_EXE, lf.BUILTIN_EXE):
        assert not lf.is_gpu_subli(other), other
    for text in (lf.engine_label(e), lf.describe_exe(e)):
        assert "NOT LISFLOOD-FP" in text and "sub-grid local inertial" in text, text
    assert lf.engine_name(e) == "Boann (sub-grid local inertial)"
    info = lf.boann_info(e)
    assert info["solver"] == "subli" and info["engine"] == le.NAME, info
    assert cli.solver_key("gpu-subli") == "subli" and cli.solver_module("subli") is le
    for path in (e, "C:\\x\\core\\metalflood\\engine_subli.py"):
        blk = record._engine_block(None, {"software": {"lisflood_exe": {"path": path}}})
        assert blk["model"].startswith("Boann, sub-grid local inertial"), blk
        assert not blk["built_in"], blk
    for other in (lf.GPU_SUBFV_EXE, lf.GPU_SGC_EXE, lf.GPU_EXE):
        assert "local inertial" not in record._engine_block(
            None, {"software": {"lisflood_exe": {"path": other}}})["model"], other
    try:
        from nd_breach_runner import algorithms as algs
    except ImportError:
        algs = None
    if algs is not None:
        assert algs.FLOOD_ENGINES[8] == "gpu-subli" and algs.FLOOD_ENGINES[7] == "auto"
        assert len(algs.FLOOD_ENGINES_TEXT) == len(algs.FLOOD_ENGINES)
        assert algs.FLOOD_ENGINES_TEXT[8].startswith("Advanced: Boann, sub-grid local inertial")
    d = tempfile.mkdtemp(prefix="sublipar_")
    try:
        dem = Grid(np.full((8, 8), 10.0), 0.0, 80.0, 10.0)
        hy = Hydrograph(np.array([0.0, 60.0]), np.array([1.0, 0.0]), "t")

        def par(name, exe, **kw):
            st = lf.LisfloodSettings(exe, sim_time_s=60.0, **kw)
            with open(lf.write_inputs(os.path.join(d, name), dem, (5.0, 40.0), hy, st),
                      encoding="utf-8") as f:
                return f.read().split("\n")
        lines = par("a", e, subgrid_r=6, subgrid_upwind=True)
        assert "subgrid 6" in lines and "upwind" in lines, lines
        lines = par("b", e)
        assert "upwind" not in lines and not any(x.startswith("subgrid") for x in lines), lines
        for other in (lf.GPU_SUBFV_EXE, lf.GPU_EXE):
            assert "upwind" not in par("c" + os.path.basename(other), other, subgrid_upwind=True)
    finally:
        shutil.rmtree(d, True)
    # headless: model.subgrid_upwind, in the resume key only when it is on
    import importlib
    ncli = importlib.import_module("nd_breach_runner.scripts.ndbreach_cli")
    assert ncli.MODEL_KEYS["subgrid_upwind"] == ("user_subgrid_upwind", ("bool",))
    opts = type("O", (), {"lisflood": sys.executable})()
    st = ncli.build_settings(ncli.Config({"model": {"subgrid_upwind": True}}, ""), opts,
                             need_models=False)
    assert st.user_subgrid_upwind is True
    assert workflow.key_settings(st)["user_subgrid_upwind"] is True
    plain = ncli.build_settings(ncli.Config({"model": {}}, ""), opts, need_models=False)
    assert plain.user_subgrid_upwind is False and workflow.RunSettings().user_subgrid_upwind is False
    assert "user_subgrid_upwind" not in workflow.key_settings(plain)   # earlier checkpoints resume


def _main():
    import traceback
    fns = [subli_layout_matches_kernel, subli_kernel_matches_reference_with_and_without_weighting,
           subli_kernel_matches_reference_for_each_option,
           subli_kernel_on_flat_cells_is_the_shipped_acc_kernel,
           subli_kernel_lake_at_rest_and_closed_basin, subli_gpu_matches_cpu,
           subli_engine_at_r1_is_the_plain_acc_engine,
           subli_engine_runs_a_fine_deck_on_coarse_cells,
           subli_coarse_cells_give_the_fine_run_and_the_weighting_spoils_it,
           subli_engine_reads_its_options_from_the_deck, subli_engine_is_a_runner_engine_choice]
    f = 0
    for fn in fns:
        try:
            fn()
            print("ok   ", fn.__name__)
        except Skip as s:
            print("skip ", fn.__name__, "({})".format(s))
        except Exception:
            f += 1
            print("FAIL ", fn.__name__)
            traceback.print_exc()
    print("\n{} tests, {} failed".format(len(fns), f))
    return 1 if f else 0


if __name__ == "__main__":
    import sys
    sys.exit(_main())
