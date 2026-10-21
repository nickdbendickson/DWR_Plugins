"""Runner 3.10.54 (run queue 2026-10-21, c4_hecras_v2e_gmsh_r): Ras.exe -c exited without the
run inputs and the engine's own message ('wrote no ... within {:.0f} s') could not format the
None (no) time limit, so a TypeError hid HEC-RAS's real error, which was in the plan HDF's
Compute Messages. No Wine, no HEC-RAS: Popen / wineserver are faked."""
import os
import shutil
import subprocess as sp
import tempfile

from nd_breach_runner.core import hecras_engine as he
from nd_breach_runner.tests.registry import test

C4_MESSAGES = (
    "Plan: 'DWR_2801' (DWR_2801.p01)\r\n"
    "ERROR with Connection(s). The SA 2D Connection at Dam has a weir elevation lower than "
    "the cells they are connected to for 2D Area Downstream. This is not allowed in HEC-RAS.\r\n"
    "   Weir Station 100.00  Weir Elevation 1066.000  Cell 64353  Cell Elevation 1066.607\r\n"
    "Error starting unsteady simulation - Stopping compute process\r\n")


class _Exited:
    """Ras.exe stand-in that has already exited (rc 0) without writing anything."""
    returncode = 0

    def poll(self):
        return 0

    def kill(self):
        pass


def _compile_after_exit(t, timeout=None, write=None):
    real_popen, real_run = sp.Popen, sp.run

    def popen(*a, **k):
        if write:
            write()
        return _Exited()
    try:
        sp.Popen = popen
        sp.run = lambda *a, **k: sp.CompletedProcess(a, 0, b"", b"")
        d = {"prefix": t, "wine": "wine", "ras_exe": os.path.join(t, "Ras.exe")}
        he.ras_compile(t, "M", "01", d, {}, timeout, poll_s=0.0)
    finally:
        sp.Popen, sp.run = real_popen, real_run


@test
def hecras_none_time_limit_formats_as_no_limit():
    assert he.within_text(None) == "with no time limit (no limit)"
    assert he.within_text(900) == "within 900 s"
    assert he._hm(None) == "no limit"
    assert he.limit_text(None) == "none"
    msg = he.compute_timeout_message(None, 10.0, "", 4.0)    # must not raise
    assert "no limit" in msg, msg


@test
def hecras_ras_compile_exit_without_inputs_and_no_limit_is_a_clear_error():
    """c4: timeout None, Ras.exe exits, no run inputs and no plan HDF: a HecRasEngineError
    that says 'no time limit', not a TypeError."""
    t = tempfile.mkdtemp()
    try:
        try:
            _compile_after_exit(t, None)
            assert False, "expected HecRasEngineError"
        except he.HecRasEngineError as e:
            s = str(e)
            assert "no time limit" in s and "Ras.exe exited" in s, s
            assert "no Compute Messages" in s, s
        try:
            _compile_after_exit(t, 600.0)
            assert False, "expected HecRasEngineError"
        except he.HecRasEngineError as e:
            assert "within 600 s" in str(e), e
    finally:
        shutil.rmtree(t, ignore_errors=True)


@test
def hecras_ras_compile_exit_reports_compute_messages_text_file():
    """No h5py needed: the computeMsgs.txt fallback carries HEC-RAS's message into the error."""
    t = tempfile.mkdtemp()
    try:
        def write():
            with open(os.path.join(t, "M.p01.computeMsgs.txt"), "w", newline="") as f:
                f.write(C4_MESSAGES)
        try:
            _compile_after_exit(t, None, write)
            assert False, "expected HecRasEngineError"
        except he.HecRasEngineError as e:
            s = str(e)
            assert "exited without writing the run inputs" in s, s
            assert "SA 2D Connection at Dam has a weir elevation lower than the cells" in s, s
            assert "M.p01.computeMsgs.txt" in s, s
            assert s.index("ERROR with Connection") < s.index("Plan: 'DWR_2801'"), s
    finally:
        shutil.rmtree(t, ignore_errors=True)


@test
def hecras_ras_compile_exit_reports_plan_hdf_compute_messages():
    """The plan HDF (p01.hdf, as Ras.exe leaves it after renaming p01.tmp.hdf): Results/Summary/
    Compute Messages (text) and the Summary 'Solution' attribute. Skipped without h5py."""
    try:
        import h5py
        import numpy as np
    except ImportError:
        print("    (skipped: h5py is not installed)")
        return
    t = tempfile.mkdtemp()
    try:
        def write():
            with h5py.File(os.path.join(t, "M.p01.hdf"), "w") as f:
                g = f.create_group("Results/Summary")
                g.attrs["Solution"] = np.bytes_(b"Unsteady failed to run")
                g.create_dataset("Compute Messages (text)",
                                 data=np.array([C4_MESSAGES.encode()], dtype=object),
                                 dtype=h5py.string_dtype("ascii"))
        write()
        cm = he.compute_messages(t, "M", "01")
        assert cm["source"] == "M.p01.hdf" and cm["solution"] == "Unsteady failed to run", cm
        assert "weir elevation lower than the cells" in cm["text"] and "\r" not in cm["text"], cm
        try:
            _compile_after_exit(t, None)
            assert False, "expected HecRasEngineError"
        except he.HecRasEngineError as e:
            s = str(e)
            assert "'Unsteady failed to run'" in s and "Cell 64353" in s, s
    finally:
        shutil.rmtree(t, ignore_errors=True)


@test
def hecras_compute_messages_excerpt_puts_the_error_first_and_is_bounded():
    x = he.compute_messages_excerpt(C4_MESSAGES)
    assert x.startswith("ERROR with Connection"), x
    assert "(earlier lines:) | Plan:" in x, x
    long = "ERROR " + "x" * 5000
    assert len(he.compute_messages_excerpt(long)) == he.COMPUTE_MESSAGES_MAX
    assert he.compute_messages_excerpt("") == ""
    assert he.compute_messages(tempfile.gettempdir(), "no_such_model_xyz", "01")["text"] == ""
