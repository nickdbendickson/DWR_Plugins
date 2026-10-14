"""For end-to-end tool tests on fake rasters: the pre-run gate's DEM-coverage check (needs a real
DEM file) and the LISFLOOD 1-second smoke (needs a real LISFLOOD executable) cannot pass here.
Only those two are switched off here; the rest of the gate still runs."""
import functools
import os


def without_dem_and_smoke_checks(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        from nd_breach_runner.core import solve_gate as sg
        saved_env = os.environ.get("ND_DAM_PRE_RUN_SMOKE")
        saved = sg.raise_on_fail
        os.environ["ND_DAM_PRE_RUN_SMOKE"] = "0"
        # only the DEM-file check is dropped (the fake raster has no file); every other gate
        # check still stops the run
        sg.raise_on_fail = lambda checks: saved([c for c in checks if c.get("title") != "DEM covers the dam"])
        try:
            return fn(*a, **k)
        finally:
            sg.raise_on_fail = saved
            if saved_env is None:
                os.environ.pop("ND_DAM_PRE_RUN_SMOKE", None)
            else:
                os.environ["ND_DAM_PRE_RUN_SMOKE"] = saved_env
    return wrapper
