"""Runner 3.94.0: the result grids' text (run.max, .maxVc, .inittm, .maxtm, .mxe, the -NNNN.wd
snapshots) is written by the CPU build's mf_format_rows instead of np.savetxt, about 7x faster,
and the files are the same bytes.

- asc_writer_writes_what_savetxt_writes: float32 and float64 grids at every precision 0 to 6,
  with rounding ties (k / 2^n, exact in binary), the NODATA value, -0.0, tiny and large values,
  byte for byte against np.savetxt; and the fast path really ran.
- asc_writer_hands_what_it_cannot_write_to_savetxt: NaN, infinity, a value too large for the
  integer rounding, a format it does not know, and DAMSAFETY_MF_FAST_ASC=0 all give np.savetxt's
  file, with no file left half written.
- asc_writer_leaves_a_boann_run_unchanged: an ACC and a sub-grid run of the same deck write the
  same result files with the fast writer and with np.savetxt."""
import filecmp
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.core.metalflood import cli
from nd_breach_runner.core.metalflood import deckio
from nd_breach_runner.core.metalflood.cases import valley_deck
from nd_breach_runner.tests.registry import Skip, test


def _need_cpu():
    if not (mfb.prebuilt_available() or mfb.find_compiler()):
        raise Skip("no C++ compiler and no prebuilt CPU build here")


def _reset():
    deckio._FAST_ASC.clear()


def _slow(path, a, header, fmt):
    np.savetxt(path, a, fmt=fmt, header=header, comments="")


def _grids(rng):
    ties = rng.integers(-10 ** 7, 10 ** 7, (37, 53)) / 2.0 ** rng.integers(0, 30, (37, 53))
    edge = np.array([[0.0, -0.0, 0.0005, 0.0015, 0.0025, 1.0005, -0.0005, -0.0004, -9999.0,
                      0.1, 0.2, 0.3, 5e-324, 1e-300, 0.125, 0.0625, 0.03125, -1.5, 2.5, 3.5,
                      999999.9995, 1e12]])
    return [("float32", rng.normal(0, 30, (41, 67)).astype(np.float32)),
            ("float32 ties", ties.astype(np.float32)),
            ("float64 ties", ties),
            ("float64 wide", rng.normal(0, 1e6, (23, 31)) * rng.random((23, 31)) ** 6),
            ("float64 small", rng.normal(0, 1e-3, (23, 31))),
            ("edge values", edge),
            ("one column", rng.normal(0, 5, (17, 1)).astype(np.float32)),
            ("NODATA grid", np.where(rng.random((29, 43)) < 0.5, -9999.0,
                                     rng.random((29, 43)) * 9).astype(np.float32))]


@test
def asc_writer_writes_what_savetxt_writes():
    _need_cpu()
    _reset()
    if deckio._fast_asc_fn() is None:
        raise AssertionError("the CPU build has no mf_format_rows")
    rng = np.random.default_rng(20261125)
    d = tempfile.mkdtemp()
    try:
        for name, a in _grids(rng):
            for prec in range(0, 7):
                fmt = "%.{}f".format(prec)
                fast, slow = os.path.join(d, "fast.asc"), os.path.join(d, "slow.asc")
                hdr = "ncols {}\nnrows {}".format(a.shape[1], a.shape[0])
                big = np.abs(a).max() * 10.0 ** prec >= 2.0 ** 62
                ran = deckio._write_asc_fast(fast, a, hdr, fmt)
                assert ran or big, "{} at {}: the fast writer gave up".format(name, fmt)
                if not ran:
                    continue
                _slow(slow, a, hdr, fmt)
                assert filecmp.cmp(fast, slow, shallow=False), \
                    "{} at {}: the text differs from np.savetxt's".format(name, fmt)
        # _write_asc itself, as the engines call it
        a = rng.random((300, 700)).astype(np.float32) * 20
        a[a < 4] = deckio.NODATA
        for fmt in ("%.3f", "%.4f"):
            fast, slow = os.path.join(d, "f.max"), os.path.join(d, "s.max")
            deckio._write_asc(fast, a, 1234.5, 6789.25, 2.0, fmt=fmt)
            hdr = ("ncols 700\nnrows 300\nxllcorner 1234.500000\nyllcorner 6789.250000\n"
                   "cellsize 2.000000\nNODATA_value -9999")
            _slow(slow, a, hdr, fmt)
            assert filecmp.cmp(fast, slow, shallow=False), fmt
            back = deckio._read_asc(fast)[0]
            assert back.shape == a.shape
    finally:
        shutil.rmtree(d, True)


@test
def asc_writer_hands_what_it_cannot_write_to_savetxt():
    _need_cpu()
    _reset()
    d = tempfile.mkdtemp()
    try:
        base = np.arange(24, dtype=np.float64).reshape(4, 6) / 7.0
        cases = []
        for bad in (np.nan, np.inf, -np.inf, 1e19):
            a = base.copy()
            a[2, 3] = bad
            cases.append(("value {}".format(bad), a, "%.3f"))
        cases.append(("format %.3e", base, "%.3e"))
        cases.append(("format %d", base, "%d"))
        cases.append(("precision 7", base, "%.7f"))
        for name, a, fmt in cases:
            p = os.path.join(d, "x.asc")
            assert not deckio._write_asc_fast(p, a, "h", fmt), name
            assert not os.path.exists(p), "{}: a half-written file was left".format(name)
            fast, slow = os.path.join(d, "f.asc"), os.path.join(d, "s.asc")
            deckio._write_asc(fast, a, 0.0, 0.0, 1.0, fmt=fmt)
            hdr = ("ncols 6\nnrows 4\nxllcorner 0.000000\nyllcorner 0.000000\n"
                   "cellsize 1.000000\nNODATA_value -9999")
            _slow(slow, a, hdr, fmt)
            assert filecmp.cmp(fast, slow, shallow=False), name
        was = os.environ.get("DAMSAFETY_MF_FAST_ASC")
        os.environ["DAMSAFETY_MF_FAST_ASC"] = "0"
        try:
            _reset()
            assert deckio._fast_asc_fn() is None
            assert not deckio._write_asc_fast(os.path.join(d, "y.asc"), base, "h", "%.3f")
        finally:
            if was is None:
                os.environ.pop("DAMSAFETY_MF_FAST_ASC", None)
            else:
                os.environ["DAMSAFETY_MF_FAST_ASC"] = was
            _reset()
    finally:
        shutil.rmtree(d, True)


@test
def asc_writer_leaves_a_boann_run_unchanged():
    _need_cpu()
    d = tempfile.mkdtemp(prefix="boann_asc_")
    was = os.environ.get("DAMSAFETY_MF_FAST_ASC")
    try:
        for solver in ("acc", "subli"):
            runs = []
            for fast in ("1", "0"):
                os.environ["DAMSAFETY_MF_FAST_ASC"] = fast
                _reset()
                f = os.path.join(d, solver, fast)
                par = valley_deck(f, sim_s=1800.0, saveint=600.0)
                cli.run(par, solver=solver, backend="cpu", quiet=True)
                runs.append(f)
            res = [os.path.join(f, "results") for f in runs]
            names = sorted(os.listdir(res[1]))
            assert any(n.endswith(".wd") for n in names), names      # the snapshots too
            assert sorted(os.listdir(res[0])) == names
            for n in names:
                assert filecmp.cmp(os.path.join(res[0], n), os.path.join(res[1], n),
                                   shallow=False), "{} {} differs with the fast writer".format(
                                       solver, n)
    finally:
        if was is None:
            os.environ.pop("DAMSAFETY_MF_FAST_ASC", None)
        else:
            os.environ["DAMSAFETY_MF_FAST_ASC"] = was
        _reset()
        shutil.rmtree(d, True)


if __name__ == "__main__":
    for fn in (asc_writer_writes_what_savetxt_writes,
               asc_writer_hands_what_it_cannot_write_to_savetxt,
               asc_writer_leaves_a_boann_run_unchanged):
        try:
            fn()
            print("ok  ", fn.__name__, flush=True)
        except Skip as e:
            print("skip", fn.__name__, e, flush=True)
