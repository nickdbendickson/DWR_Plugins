"""Boann ACC rain on the grid (Runner 3.60.0): LISFLOOD-FP's "rainfall" keyword (a .rain file, one
rate in mm/hr over every valid cell, linear between its times) read by deckio.read_rain and added
by kernels.metal's k_begin / k_cont. Checked on the CPU build of the kernels: the file reader
(units, mm/hr -> m/s, a negative rate refused), a closed flat box fills to exactly rate x time with
a NoData cell left dry, a sloping plane with an open edge balances its water to round-off, the
other Boann engines refuse a deck with rain instead of dropping it, and (with NDBREACH_LISFLOOD,
--models) the same deck through LISFLOOD-FP 8.3 gives the same depths. On a Mac with a Metal GPU
the GPU is checked against the CPU build."""
import os
import shutil
import subprocess
import tempfile

import numpy as np

from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.core.metalflood import deckio
from nd_breach_runner.core.metalflood import engine as mfe
from nd_breach_runner.tests.registry import Skip, model_test, test


def _need_cpu_build():
    if mfb.prebuilt_available():
        return
    if os.name == "nt":
        raise Skip("the engine's CPU build is not made on Windows (a release ships it prebuilt)")
    if not mfb.find_compiler():
        raise Skip("no C++ compiler for the engine's CPU build")


def _write_rain(path, pairs, unit="hours"):
    with open(path, "w") as f:
        f.write("rain for a test\n{} {}\n".format(len(pairs), unit))
        for v, t in pairs:
            f.write("{} {}\n".format(v, t))


def _deck(folder, z, dx=10.0, free="", rain=((36.0, 0.0), (36.0, 1.0), (0.0, 1.01), (0.0, 3.0)),
          sim=3 * 3600, extra=()):
    """A rain-only deck: DEM z (NaN = NoData), the given FREE edges, no point sources."""
    os.makedirs(folder, exist_ok=True)
    ny, nx = z.shape
    with open(os.path.join(folder, "run.dem.asc"), "w") as f:
        f.write("ncols {}\nnrows {}\nxllcorner 0\nyllcorner 0\ncellsize {}\nNODATA_value -9999\n"
                .format(nx, ny, dx))
        np.savetxt(f, np.where(np.isfinite(z), z, -9999.0), fmt="%.4f")
    _write_rain(os.path.join(folder, "run.rain"), rain)
    par = ["DEMfile run.dem.asc", "resroot run", "dirroot results", "sim_time {}".format(sim),
           "initial_tstep 10.0", "massint 60", "saveint {}".format(sim + 10),
           "rainfall run.rain", "acceleration", "fpfric 0.05", "elevoff", "depthoff"]
    if free:
        with open(os.path.join(folder, "run.bci"), "w") as f:
            for e in free:
                f.write("{} -1e9 1e9 FREE\n".format(e))
        par.append("bcifile run.bci")
    with open(os.path.join(folder, "run.par"), "w") as f:
        f.write("\n".join(par + list(extra)) + "\n")
    return os.path.join(folder, "run.par")


def _slope(ny=40, nx=60, dx=10.0, slope=0.002):
    yy, xx = np.mgrid[0:ny, 0:nx].astype(float)
    z = 100.0 + slope * dx * (nx - 1 - xx) + 0.3 * np.sin(yy / 5.0)
    z[ny // 2 - 3:ny // 2 + 3, 20:24] -= 1.0                     # a pond that fills and spills
    return z


def _res(folder, ext):
    return np.loadtxt(os.path.join(folder, "results", "run." + ext), skiprows=6)


@test
def boann_rain_reads_lisflood_rain_files():
    d = tempfile.mkdtemp()
    try:
        p = os.path.join(d, "a.rain")
        _write_rain(p, [(36.0, 0), (72.0, 30), (0.0, 60)], unit="minutes")
        t, r = deckio.read_rain(p)
        assert np.allclose(t, [0.0, 1800.0, 3600.0])
        assert np.allclose(r, [1e-5, 2e-5, 0.0])                  # mm/hr -> m/s
        _write_rain(p, [(10.0, 0), (10.0, 7200)], unit="seconds")
        t, r = deckio.read_rain(p)
        assert np.allclose(t, [0.0, 7200.0]) and np.allclose(r, 10.0 / 3.6e6)
        _write_rain(p, [(5.0, 0), (-1.0, 1)])
        try:
            deckio.read_rain(p)
            raise AssertionError("a negative rate was accepted")
        except ValueError as e:
            assert "negative" in str(e)
        with open(p, "w") as f:
            f.write("short\n3 hours\n1 0\n")
        try:
            deckio.read_rain(p)
            raise AssertionError("a short file was accepted")
        except ValueError:
            pass
    finally:
        shutil.rmtree(d, True)


@test
def boann_rain_fills_a_closed_box_to_rate_times_time():
    """36 mm/hr for 1 h on a flat closed box: 36 mm on every valid cell, none on a NoData cell,
    and the water balance closes."""
    _need_cpu_build()
    d = tempfile.mkdtemp()
    try:
        z = np.full((12, 15), 50.0)
        z[3, 4] = np.nan
        # the rate is read at each step's start (as LISFLOOD-FP): 360 steps of 10 s at 36 mm/hr,
        # and the ramp to zero ends at 3,600 s, where the 361st step starts
        par = _deck(d, z, rain=((36.0, 0.0), (36.0, 0.9999), (0.0, 1.0), (0.0, 1.5)),
                    sim=int(1.5 * 3600))
        st = mfe.run_par(par, backend="cpu")
        h = _res(d, "max")
        valid = np.isfinite(z)
        want = 0.036
        assert np.allclose(h[valid], want, atol=2e-5), (h[valid].min(), h[valid].max())
        assert h[~valid].max() == -9999.0
        area = valid.sum() * 100.0
        assert abs(st["rain_m3"] - want * area) < 1e-3 * want * area, (st["rain_m3"], want * area)
        assert abs(st["injected_m3"] - st["rain_m3"]) < 1e-9
        assert st["outflow_m3"] == 0.0 and st["clipped_m3"] == 0.0
        assert abs(st["mass_error_m3"]) < 1e-4 * st["rain_m3"], st
        last = open(os.path.join(d, "results", "run.mass")).read().split("\n")[-2].split()
        assert abs(float(last[-1]) - st["rain_m3"]) < 0.01      # cumulative rain, as LISFLOOD-FP
    finally:
        shutil.rmtree(d, True)


@test
def boann_rain_runs_off_a_slope_and_balances():
    _need_cpu_build()
    d = tempfile.mkdtemp()
    try:
        st = mfe.run_par(_deck(d, _slope(), free="E"), backend="cpu")
        # 36 mm/hr for 1 h plus the 36 s ramp, on 40 x 60 cells of 10 m
        rain = (0.036 + 0.5 * 0.01 * 0.036) * 40 * 60 * 100.0
        assert abs(st["rain_m3"] - rain) < 1e-3 * rain, (st["rain_m3"], rain)
        assert st["outflow_m3"] > 0.5 * rain, st                 # most of it has run off
        assert st["final_volume_m3"] > 0.0
        assert abs(st["mass_error_m3"]) < 1e-4 * rain, st
        h = _res(d, "max")
        assert h[17:23, 20:24].min() > 0.6       # the pond filled from runoff, not 36 mm of rain
    finally:
        shutil.rmtree(d, True)


@test
def boann_rain_other_engines_refuse_it():
    """The FV / FV2 / DG2 / SGC / sub-grid FV engines do not add rain yet: they stop with a message
    rather than run a deck without its rain."""
    from nd_breach_runner.core.metalflood import engine_fv, engine_sgc, engine_subfv
    d = tempfile.mkdtemp()
    try:
        par = _deck(d, _slope(12, 16), free="E")
        for run in (engine_fv.run_par, engine_sgc.run_par, engine_subfv.run_par):
            try:
                run(par, backend="cpu")
                raise AssertionError("{} ran a deck with rainfall".format(run.__module__))
            except mfe.EngineError as e:
                assert "rainfall" in str(e) and "ACC" in str(e), str(e)
    finally:
        shutil.rmtree(d, True)


@test
def boann_rain_gpu_matches_cpu_build():
    """Runs only on a Mac with a Metal GPU."""
    ok, why = mfb.metal_available()
    if not ok:
        raise Skip("no Metal GPU here ({})".format(why))
    _need_cpu_build()
    d = tempfile.mkdtemp()
    try:
        sa = mfe.run_par(_deck(os.path.join(d, "a"), _slope(), free="E"), backend="cpu")
        sb = mfe.run_par(_deck(os.path.join(d, "b"), _slope(), free="E"), backend="metal")
        assert sb["backend"] == "metal"
        assert abs(sa["rain_m3"] - sb["rain_m3"]) < 1e-4 * sa["rain_m3"]
        assert np.abs(_res(os.path.join(d, "a"), "max") - _res(os.path.join(d, "b"), "max")).max() < 0.002
    finally:
        shutil.rmtree(d, True)


@model_test
def boann_rain_matches_lisflood_fp():
    """The same rain deck through LISFLOOD-FP (NDBREACH_LISFLOOD) and Boann ACC. On 2026-11-23
    against LISFLOOD-FP 8.3: the same 3,820 steps, rain volume equal to 0.002 m3, the final volume
    2443.30 vs 2443.25 m3, max depths within 1 mm (the .max file's rounding)."""
    exe = os.environ.get("NDBREACH_LISFLOOD")
    if not exe or not os.path.isfile(exe):
        raise Skip("NDBREACH_LISFLOOD is not set")
    _need_cpu_build()
    d = tempfile.mkdtemp()
    try:
        a = _deck(os.path.join(d, "lf"), _slope(), free="E")
        b = _deck(os.path.join(d, "mf"), _slope(), free="E")
        subprocess.run([exe, os.path.basename(a)], cwd=os.path.dirname(a),
                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, check=True,
                       env=dict(os.environ, OMP_NUM_THREADS="1"))
        st = mfe.run_par(b, backend="cpu")
        x, y = _res(os.path.join(d, "lf"), "max"), _res(os.path.join(d, "mf"), "max")
        assert np.abs(x - y).max() < 0.005, np.abs(x - y).max()
        lf_last = open(os.path.join(d, "lf", "results", "run.mass")).read().split("\n")[-2].split()
        lf_vol, lf_rain = float(lf_last[5]), float(lf_last[-1])
        assert abs(st["rain_m3"] - lf_rain) < 1e-3 * lf_rain, (st["rain_m3"], lf_rain)
        assert abs(st["final_volume_m3"] - lf_vol) < 1e-3 * lf_vol, (st["final_volume_m3"], lf_vol)
    finally:
        shutil.rmtree(d, True)


if __name__ == "__main__":
    for fn in (boann_rain_reads_lisflood_rain_files, boann_rain_fills_a_closed_box_to_rate_times_time,
               boann_rain_runs_off_a_slope_and_balances, boann_rain_other_engines_refuse_it,
               boann_rain_gpu_matches_cpu_build, boann_rain_matches_lisflood_fp):
        try:
            fn()
            print("ok  ", fn.__name__)
        except Skip as e:
            print("skip", fn.__name__, e)
