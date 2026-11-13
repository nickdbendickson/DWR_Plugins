"""Runner 3.19.2: the nested, cascade and probabilistic reports are written in SI when the suite
shows SI units, as run_report.md already was (Core 1.1.0). Under US customary units the text is
exactly what it was. The data files beside them keep US units in both systems."""
import os
import shutil
import tempfile
import types

from nd_breach_runner.core import nested
from nd_breach_runner.core import workflow
from nd_breach_runner.tests.registry import test

class _Units:
    """The suite's unit system for one test, without touching the computer's own setting."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        try:
            from nd_breach_runner.damsuite import display_units as DU
        except Exception:                                         # noqa - Core older than 1.1.0
            DU = None
        self.du = DU
        if DU is not None:
            self.old = DU.system
            DU.system = lambda project=None: self.value
        return self

    def __exit__(self, *exc):
        if self.du is not None:
            self.du.system = self.old


def _nested_report(folder):
    out = types.SimpleNamespace(
        folder=folder, warnings=["The interface is 0.5 mi from the dam."],
        interface={"reach_mi": 2.0, "path_xy": (1000.0, 2000.0), "direction": (1.0, 0.0),
                   "width_m": 120.0, "peak_cfs": 10000.0, "volume_acft": 100.0},
        summary={"peak_breach_cfs": 12000, "fine_cells": 1000, "coarse_cells": 2000,
                 "flooded_acres": 250, "max_depth_ft": 10.0, "max_velocity_fps": 5.0,
                 "latest_arrival_hr": 3.5})
    dam = types.SimpleNamespace(dam_id="T-1")
    st = types.SimpleNamespace(cell_size_m=10.0)
    path = nested._write_report(out, dam, st, 2.0, folder)
    with open(path, encoding="utf-8") as f:
        return f.read()


@test
def si_reports_nested_report_is_si_under_si_and_unchanged_under_us():
    tmp = tempfile.mkdtemp(prefix="si_rep_")
    try:
        with _Units("us"):
            us = _nested_report(tmp)
        assert "| Peak flow through the interface | 10,000 cfs |" in us, us
        assert "| Maximum depth / velocity | 10.0 ft / 5.0 ft/s |" in us
        assert "Units: SI" not in us and "m³/s" not in us
        with _Units("si"):
            if workflow._display_units_md("1 ft") == "1 ft":
                return                     # a Core older than 1.1.0: nothing is converted
            si = _nested_report(tmp)
        assert "Units: SI" in si, si[:300]
        assert "| Peak flow through the interface | 283 m³/s |" in si, si
        assert "| Volume through the interface | 123,348.2 m³ |" in si, si
        assert "| Maximum depth / velocity | 3.05 m / 1.52 m/s |" in si, si
        assert "| Flooded area (merged) | 101 ha |" in si, si
        assert " cfs" not in si and " ac-ft" not in si and " ft/s" not in si
        # what was already metric is left alone, and so is the time
        assert "| Section the coarse inflow is spread over | 120 m |" in si
        assert "| Latest arrival | 3.5 h after breach initiation |" in si
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@test
def si_reports_cascade_and_probabilistic_go_through_the_same_converter():
    import inspect
    from nd_breach_runner.core import cascade, probabilistic
    for fn in (cascade._report, probabilistic._write_report, nested._write_report):
        assert "workflow._display_units_md(" in inspect.getsource(fn), fn.__module__
