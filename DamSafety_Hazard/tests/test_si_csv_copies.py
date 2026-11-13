"""Hazard 2.25.1: when the suite shows SI units, a finished run leaves each result table again
with an SI column beside each US one, in an SI_units folder beside the table. The tables
themselves keep US units and their names (the Runner, the review tools and the submittal
package read them). Under US customary units nothing is written. The SI_units folders are
never taken for data sets by the data-folder scan.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_si_csv_copies.py

Needs PyQGIS (the pipeline module imports it).
"""
import csv
import os
import shutil
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], False)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard import datafolder as DF, pipeline as PL  # noqa: E402
from DamSafety_Hazard.damsuite import display_units as DU  # noqa: E402
from DamSafety_Hazard.constants import SUBDIR_CLASSIFICATION, SUBDIR_QA  # noqa: E402

REAL_SYSTEM = DU.system
TMP = tempfile.mkdtemp(prefix="si_copies_")


def _write(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    return path


def _read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def _outputs(name):
    out = os.path.join(TMP, name, "outputs")
    table = _write(os.path.join(out, SUBDIR_CLASSIFICATION, "Habitable_Structures.csv"),
                   [["Struct_ID", "Max_Depth_ft", "Max_Vel_fps", "Notes"],
                    ["1", "2.0", "12.0", "zone rests on velocity"],
                    ["2", "", "", "dry"]])
    _write(os.path.join(out, SUBDIR_QA, "Review_Log.csv"), [["When", "Who"], ["today", "NB"]])
    top = _write(os.path.join(out, "Evacuation_Timing.csv"),
                 [["Road", "Length_mi"], ["56th St", "1.0"]])
    log = []
    return types.SimpleNamespace(out=out, log=log.append, lines=log), table, top


def _units(value):
    """The unit system for the test, without touching the computer's own setting."""
    DU.system = lambda project=None: value


def test_us_writes_nothing():
    _units("us")
    run, table, _top = _outputs("us")
    before = open(table, "rb").read()
    assert PL._si_csv_copies(run) == [] and run.lines == []
    assert not any("SI_units" in d for _b, d, _f in os.walk(run.out))
    assert open(table, "rb").read() == before


def test_si_copies_each_table_that_has_a_unit_column():
    _units("si")
    run, table, top = _outputs("si")
    before = open(table, "rb").read()
    made = PL._si_csv_copies(run)
    a = os.path.join(run.out, "SI_units", "Evacuation_Timing.csv")
    b = os.path.join(run.out, SUBDIR_CLASSIFICATION, "SI_units", "Habitable_Structures.csv")
    assert made == [a, b], made
    assert _read(b) == [["Struct_ID", "Max_Depth_ft", "Max_Depth_m", "Max_Vel_fps",
                         "Max_Vel_mps", "Notes"],
                        ["1", "2.0", "0.6096", "12.0", "3.658", "zone rests on velocity"],
                        ["2", "", "", "", "", "dry"]], _read(b)
    assert _read(a) == [["Road", "Length_mi", "Length_km"], ["56th St", "1.0", "1.609"]]
    # the tables themselves are untouched, a table with no unit column gets no copy
    assert open(table, "rb").read() == before
    assert not os.path.exists(os.path.join(run.out, SUBDIR_QA, "SI_units"))
    assert len(run.lines) == 1 and "2 table(s)" in run.lines[0] and "US" in run.lines[0]
    # a second run refreshes the copies; a table that is gone takes its copy with it
    os.remove(top)
    assert PL._si_csv_copies(run) == [b]
    assert not os.path.exists(os.path.join(run.out, "SI_units"))


def test_the_copy_step_never_stops_a_run():
    _units("si")
    lines = []
    bad = types.SimpleNamespace(out=None, log=lines.append)
    assert PL._si_csv_copies(bad) == []
    assert PL._si_csv_copies(types.SimpleNamespace(out=os.path.join(TMP, "none"),
                                                   log=lines.append)) == []


def test_the_data_folder_scan_skips_the_copies():
    assert "si_units" in DF._SKIP_DIR_NAMES
    entry = types.SimpleNamespace(name="SI_units")
    assert DF._skip_dir(entry, True) and DF._skip_dir(entry, False)


if __name__ == "__main__":
    failed = 0
    try:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                try:
                    fn()
                    print("ok    " + name)
                except Exception:                                # noqa: BLE001
                    import traceback
                    failed += 1
                    print("FAIL  " + name)
                    traceback.print_exc()
    finally:
        DU.system = REAL_SYSTEM
        shutil.rmtree(TMP, ignore_errors=True)
    print("{} failed".format(failed))
    sys.stdout.flush()
    os._exit(1 if failed else 0)
