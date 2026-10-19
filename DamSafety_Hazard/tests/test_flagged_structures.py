"""Hazard 2.18.38: flagged structures (damsuite/structure_flags.py, docs/Flagged_structures.md).

DWR-3161 / ND00597 is the ND-23 road fill over backwater, hazard Low from the DWR record: the
classification stops with that reason instead of classifying breach rasters, unless
allow_flagged_structure / ALLOW_FLAGGED_STRUCTURE=1 (then the log and DATA SOURCES say the hazard
of record stays Low). Real QGIS (offscreen); no rasters needed - the gate comes first.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_flagged_structures.py
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard import pipeline as PL  # noqa: E402
from DamSafety_Hazard.damsuite import structure_flags as SF, dam_record as DR  # noqa: E402
import DamSafety_Hazard.damsuite as DS  # noqa: E402

NOTE = "road fill over backwater; breach analysis not applicable; hazard Low (DWR record)"


class _Run(object):
    def __init__(self):
        self.lines, self.sources = [], []

    def log(self, m):
        self.lines.append(m)


def main():
    os.environ.pop(SF.ENV_ALLOW, None)
    assert DS.selfcheck() == [], DS.selfcheck()
    work = tempfile.mkdtemp(prefix="hz_flag_")
    try:
        dams = os.path.join(work, "Dams")
        folder = os.path.join(dams, "DWR-3161")
        other = os.path.join(dams, "SANISH")
        plain = os.path.join(dams, "DWR-5289")
        for f in (folder, other, plain):
            os.makedirs(f)
        rec = DR.load(other, create=True)
        rec.set_many({"nid_id": "ND00597"}, "ndscreen", "0.17.26")
        rec.save()
        # 1. the lookup: by the dam folder's name, the subject NID, the dam ID
        assert PL.flagged_structure({"dam_folder": folder})["nid_id"] == "ND00597"
        assert PL.flagged_structure({"subject_dam_nid": "ND00597"}) is not None
        assert PL.flagged_structure({"dam_folder": plain}) is None
        assert PL.flagged_structure({}) is None
        # 2. the gate: refused with the reason
        try:
            PL._flagged_structure_gate(_Run(), {"dam_folder": folder})
            raise AssertionError("expected a refusal")
        except RuntimeError as e:
            msg = str(e)
            assert "DWR-3161" in msg and NOTE in msg and "Not classified" in msg, msg
            assert "allow_flagged_structure" in msg and "ALLOW_FLAGGED_STRUCTURE=1" in msg, msg
        assert PL._flagged_structure_gate(_Run(), {"dam_folder": plain}) is None
        # allowed: the run goes on, the log and DATA SOURCES say the class of record stays Low
        r = _Run()
        e = PL._flagged_structure_gate(r, {"dam_folder": folder, "allow_flagged_structure": True})
        assert e and e["hazard"] == "Low"
        assert r.sources and "hazard of record is Low (DWR record)" in r.sources[0], r.sources
        assert r.lines[0].startswith(">>> WARNING: FLAGGED STRUCTURE"), r.lines
        os.environ[SF.ENV_ALLOW] = "1"
        try:
            assert PL._flagged_structure_gate(_Run(), {"dam_folder": folder})
        finally:
            os.environ.pop(SF.ENV_ALLOW, None)
        # 3. the whole run: stops at the gate (before any raster is needed), the run log says why
        out = os.path.join(folder, "outputs")
        cfg = PL.config(dam_folder=other, output_folder=out, scenarios=[])
        try:
            PL.run_classification(cfg)
            raise AssertionError("expected a refusal from run_classification")
        except RuntimeError as e:
            assert "flagged structure" in str(e) and NOTE in str(e), str(e)
        logs = [os.path.join(d, f) for d, _s, fs in os.walk(out) for f in fs
                if f.startswith("Hazard_Classification_Run_Log")]
        assert logs and "flagged structure" in open(logs[0], encoding="utf-8").read(), logs
        print("ALL OK")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
