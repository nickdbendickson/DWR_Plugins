"""Boann's OpenCL backend (DamSafety_Core/boann/hydro/opencl_backend.py): the same .metal kernel
files, translated to OpenCL C, on a GPU that is not Apple's (Intel Arc, AMD, NVIDIA).

- the translation of every engine's kernel file runs (no OpenCL needed);
- where an OpenCL device is here (CI: PoCL, a CPU OpenCL, .github/workflows/opencl-engine.yml;
  Nick's Arc PC), each solver on OpenCL against the CPU build with the tolerances the Metal GPU is
  held to (test_metalflood.metalflood_gpu_matches_cpu_build and the FV2 / DG2 twins), two OpenCL
  runs bit for bit, and the dry-tile skip on == off bit for bit on OpenCL.

OpenCL is never picked by 'auto'; nothing here changes a run that does not ask for it."""
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import builtin_lisflood as bl
from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.core.metalflood import engine as mfe
from nd_breach_runner.core.metalflood import engine_fv as fve
from nd_breach_runner.core.metalflood import engine_sgc as sgce
from nd_breach_runner.core.metalflood import engine_subfv as sfe
from nd_breach_runner.core.metalflood.cases import read_asc, valley_deck, diagonal_valley_deck
from nd_breach_runner.core.metalflood.cases import write_asc as _write_asc
from nd_breach_runner.tests.registry import Skip, test
from nd_breach_runner.tests.test_boann_rain import _need_cpu_build

from DamSafety_Core.boann.hydro import opencl_backend as ob

# every engine's kernel file translates to OpenCL C (kernels_subfv.metal's one reference out-param
# became a pointer, valid in Metal and OpenCL alike, so sub-grid FV translates now too)
TRANSLATED = ("kernels.metal", "kernels_fv.metal", "kernels_fv2.metal", "kernels_dg2.metal",
              "kernels_sgc.metal", "kernels_subli.metal", "kernels_subfv.metal")
TOL = (("max", 0.01), ("maxVc", 0.1), ("inittm", 0.01))


def _need_opencl_device():
    ok, why = ob.opencl_available()
    if not ok:
        raise Skip("no OpenCL device here ({})".format(why))


def _need_opencl():
    _need_opencl_device()
    _need_cpu_build()


def _res(folder, ext):
    return read_asc(os.path.join(folder, "results", "run." + ext))


def _run(solver, deck, backend):
    if solver == "acc":
        return mfe.run_par(deck, backend=backend)
    if solver == "sgc":
        return sgce.run_par(deck, backend=backend)
    if solver == "subli":
        from DamSafety_Core.boann.hydro import engine_subli
        return engine_subli.run_par(deck, backend=backend)
    return fve.run_par(deck, backend=backend, order={"fv1": 1, "fv2": 2, "dg2": 3}[solver])


def _compare(da, db, sa, sb, what):
    assert abs(sa["steps"] - sb["steps"]) <= max(3, sa["steps"] // 200), (what, sa["steps"],
                                                                         sb["steps"])
    worst = {}
    for ext, tol in TOL:
        x = _res(da, ext)
        y = _res(db, ext)
        m = (x != bl.NODATA) & (y != bl.NODATA)
        d = float(np.abs(x - y)[m].max()) if m.any() else 0.0
        worst[ext] = d
        assert d <= tol, (what, ext, d)
    return worst


@test
def boann_opencl_translates_every_kernel_file():
    """translate() gives OpenCL C for each engine's kernels: no Metal attribute, reference or
    C++ cast left, one thread position per kernel."""
    here = os.path.dirname(os.path.abspath(ob.__file__))
    for name in TRANSLATED:
        with open(os.path.join(here, name), encoding="utf-8") as f:
            src = f.read()
        out = ob.translate(src).split("\n#line 1\n", 1)[1]     # the source, after the shim
        assert "constant Params&" not in out, name
        assert "as_type<" not in out, name
        assert "uint2 gid GID" not in out, name
        assert out.count("get_global_id(0)") == src.count("kernel void "), name
        assert "typedef struct Params" in out, name


@test
def boann_opencl_solvers_match_cpu_build():
    """ACC, SGC, FV1, FV2 and DG2 on the OpenCL device against the CPU build of the same kernels,
    on the test valley: the Metal GPU's tolerances (1 cm depth, 0.1 m/s velocity, 0.01 h arrival,
    steps within 0.5%), the run reports the OpenCL backend, and water is kept."""
    _need_opencl()
    d = tempfile.mkdtemp(prefix="boann_ocl_")
    try:
        for solver in ("acc", "sgc", "fv1", "fv2", "dg2"):
            a = valley_deck(os.path.join(d, solver + "_cpu"))
            b = valley_deck(os.path.join(d, solver + "_ocl"))
            sa = _run(solver, a, "cpu")
            sb = _run(solver, b, "opencl")
            assert sb["backend"] == "opencl", (solver, sb.get("backend"))
            w = _compare(os.path.dirname(a), os.path.dirname(b), sa, sb, solver)
            if "mass_error_m3" in sb and "injected_m3" in sb:
                assert abs(sb["mass_error_m3"]) < 1e-4 * max(sb["injected_m3"], 1.0), (solver, sb)
            print("  {:<4} OpenCL vs CPU: steps {} vs {}, worst {}".format(
                solver, sb["steps"], sa["steps"], w))
    finally:
        shutil.rmtree(d, True)


def _subfv_channel_deck(folder, r, sim=120.0):
    """A fine DEM of a channel below a flat floodplain with a start pool over the left end; closed
    walls, 'subgrid r' so the sub-grid FV solver runs on the coarsened grid."""
    os.makedirs(folder, exist_ok=True)
    ny, nx = 4 * r, 10 * r
    dxf = 3.0
    z = np.full((ny, nx), 10.0)
    lo = (ny - r) // 2
    z[lo:lo + r, :] = 8.0
    h0 = np.zeros((ny, nx))
    h0[:, :nx // 4] = 4.0
    xll, yll = 500000.0, 5200000.0
    _write_asc(os.path.join(folder, "run.dem.asc"), z, xll, yll, dxf)
    _write_asc(os.path.join(folder, "run.start"), h0, xll, yll, dxf)
    lines = ["DEMfile run.dem.asc", "resroot run", "dirroot results", "startfile run.start",
             "sim_time {:.1f}".format(sim), "initial_tstep 2.0", "massint {:.1f}".format(sim),
             "saveint {:.1f}".format(sim + 10.0), "fpfric 0.03", "subgrid {}".format(r)]
    with open(os.path.join(folder, "run.par"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return os.path.join(folder, "run.par")


@test
def boann_opencl_subfv_matches_cpu_build():
    """Sub-grid FV (its kernels_subfv.metal now translates) on the OpenCL device against the CPU
    build: the Metal GPU's tolerances, the run reports the OpenCL backend, and mass is kept."""
    _need_opencl()
    d = tempfile.mkdtemp(prefix="boann_ocl_subfv_")
    try:
        r = 4
        a = _subfv_channel_deck(os.path.join(d, "cpu"), r)
        b = _subfv_channel_deck(os.path.join(d, "ocl"), r)
        sa = sfe.run_par(a, backend="cpu")
        sb = sfe.run_par(b, backend="opencl")
        assert sb["backend"] == "opencl", sb.get("backend")
        w = _compare(os.path.join(d, "cpu"), os.path.join(d, "ocl"), sa, sb, "subfv")
        assert abs(sb["mass_error_m3"]) < 1e-4 * max(sb["injected_m3"], 1.0), sb
        print("  subfv OpenCL vs CPU: steps {} vs {}, worst {}".format(
            sb["steps"], sa["steps"], w))
    finally:
        shutil.rmtree(d, True)


@test
def boann_opencl_repeats_bit_for_bit():
    """Two OpenCL runs of the same deck give the same grids bit for bit (every sum over threads is
    an integer atomic, so the order the work-items run in cannot show)."""
    _need_opencl()
    d = tempfile.mkdtemp(prefix="boann_ocl_")
    try:
        for solver in ("acc", "fv2"):
            a = valley_deck(os.path.join(d, solver + "1"))
            b = valley_deck(os.path.join(d, solver + "2"))
            sa = _run(solver, a, "opencl")
            sb = _run(solver, b, "opencl")
            assert sa["steps"] == sb["steps"], (solver, sa["steps"], sb["steps"])
            for ext, _tol in TOL:
                assert np.array_equal(_res(os.path.dirname(a), ext),
                                      _res(os.path.dirname(b), ext)), (solver, ext)
    finally:
        shutil.rmtree(d, True)


@test
def boann_opencl_tile_skip_is_inert():
    """The GPU dry-tile skip on OpenCL: on == off bit for bit (DAMSAFETY_MF_TILES), on the diagonal
    valley whose window is mostly dry ground."""
    _need_opencl()
    d = tempfile.mkdtemp(prefix="boann_ocl_")
    old = os.environ.get("DAMSAFETY_MF_TILES")
    try:
        out = {}
        for tag, val in (("on", "1"), ("off", "0")):
            os.environ["DAMSAFETY_MF_TILES"] = val
            deck = diagonal_valley_deck(os.path.join(d, tag), ny=90, nx=210, dx=10.0,
                                        peak_cms=400.0, sim_s=1800.0)
            out[tag] = (os.path.dirname(deck), mfe.run_par(deck, backend="opencl"))
        assert out["on"][1]["steps"] == out["off"][1]["steps"]
        for ext, _tol in TOL:
            assert np.array_equal(_res(out["on"][0], ext), _res(out["off"][0], ext)), ext
    finally:
        if old is None:
            os.environ.pop("DAMSAFETY_MF_TILES", None)
        else:
            os.environ["DAMSAFETY_MF_TILES"] = old
        shutil.rmtree(d, True)


_COPY_MODES = (("zero_copy", {"DAMSAFETY_MF_CL_ZEROCOPY": "1", "DAMSAFETY_MF_CL_SYNC": None}),
               ("on_demand", {"DAMSAFETY_MF_CL_ZEROCOPY": "0", "DAMSAFETY_MF_CL_SYNC": None}),
               ("full_sync", {"DAMSAFETY_MF_CL_ZEROCOPY": "0", "DAMSAFETY_MF_CL_SYNC": "full"}))


def _with_env(kv, fn):
    was = {k: os.environ.get(k) for k in kv}
    try:
        for k, v in kv.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return fn()
    finally:
        for k, v in was.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _result_files(folder):
    res = os.path.join(folder, "results")
    out = {}
    for name in sorted(os.listdir(res)):
        with open(os.path.join(res, name), "rb") as f:
            out[name] = f.read()
    return out


def _modes_agree(make_deck, run, what):
    """Every result file (grids, .mass, gauge, snapshots) of the same deck, byte for byte, in the
    three ways the grids reach the GPU. A zero-copy leg the device cannot do is left out."""
    d = tempfile.mkdtemp(prefix="boann_ocl_modes_")
    try:
        files, used = {}, []
        for tag, env in _COPY_MODES:
            deck = make_deck(os.path.join(d, tag))
            try:
                st = _with_env(env, lambda: run(deck))
            except Exception as e:          # noqa: BLE001 - only the zero-copy leg may decline
                if tag == "zero_copy" and ("zero-copy" in str(e) or "host memory" in str(e)):
                    print("  {}: zero-copy not possible here ({})".format(what, e))
                    continue
                raise
            assert st["backend"] == "opencl", (what, tag, st.get("backend"))
            files[tag] = _result_files(os.path.dirname(deck))
            used.append(tag)
        ref = files["full_sync"]
        for tag in used:
            assert sorted(files[tag]) == sorted(ref), (what, tag, sorted(files[tag]))
            for name in ref:
                assert files[tag][name] == ref[name], (what, tag, name)
        return used
    finally:
        shutil.rmtree(d, True)


@test
def boann_opencl_copy_modes_agree():
    """Zero-copy buffers (a GPU sharing host memory), copies on demand (only the state words read
    back each batch; a grid read back when the engine looks it up) and the full read-back every
    batch give every result file byte for byte: ACC, SGC, FV1, FV2, DG2 and sub-grid local
    inertial on the test valley with a start depth, a gauge and snapshots (the engine reads h,
    the fluxes and the maxima between batches)."""
    _need_opencl_device()
    for solver in ("acc", "sgc", "fv1", "fv2", "dg2", "subli"):
        extra = ("subgrid 4",) if solver == "subli" else ()
        used = _modes_agree(
            lambda f, x=extra: valley_deck(f, saveint=1800.0, sim_s=2 * 3600.0, extra=x),
            lambda deck, s=solver: _run(s, deck, "opencl"), solver)
        print("  {:<5} copy modes identical: {}".format(solver, ", ".join(used)))


@test
def boann_opencl_copy_modes_agree_with_a_breach():
    """The in-terrain breach reads the maxima at the notch and lowers z between batches: the same
    result files, byte for byte, in every way the grids reach the GPU (ACC, FV1)."""
    _need_opencl_device()
    from nd_breach_runner.tests.test_boann_terrain_breach import _ridge_deck, _spec, _write_breach

    def deck(folder):
        _z, cells, pool = _ridge_deck(folder, sim_s=120.0)
        return _write_breach(folder, _spec(cells, formation_s=60.0, update_s=10.0), pool=pool)

    for solver in ("acc", "fv1"):
        used = _modes_agree(deck, lambda par, s=solver: _run(s, par, "opencl"), solver + "+breach")
        print("  {:<4} breach, copy modes identical: {}".format(solver, ", ".join(used)))


if __name__ == "__main__":
    for fn in (boann_opencl_translates_every_kernel_file,
               boann_opencl_solvers_match_cpu_build,
               boann_opencl_subfv_matches_cpu_build,
               boann_opencl_repeats_bit_for_bit,
               boann_opencl_tile_skip_is_inert,
               boann_opencl_copy_modes_agree,
               boann_opencl_copy_modes_agree_with_a_breach):
        try:
            fn()
            print("ok  ", fn.__name__)
        except Skip as e:
            print("skip", fn.__name__, e)
