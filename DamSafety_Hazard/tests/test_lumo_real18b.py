"""Lumo vReal.18b SB.1 / SB.5 / WK.3 / SH.1 (Hazard 2.18.30).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_lumo_real18b.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication([], False)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard import submittal as SB  # noqa: E402
from DamSafety_Hazard import watch as WK  # noqa: E402
from DamSafety_Hazard import history as HIS  # noqa: E402
from DamSafety_Hazard.constants import SUBDIR_QA, SUBDIR_CLASSIFICATION, SUBDIR_EAP  # noqa


def _touch(p, text="x\n"):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as fh:
        fh.write(text)


def _outputs(multi=True):
    out = os.path.join(tempfile.mkdtemp(), "outputs")
    _touch(os.path.join(out, "Hazard_Classification_Summary.txt"),
           "ND HAZARD CLASSIFICATION (screening): HIGH\n")
    _touch(os.path.join(out, "Hazard_Classification_Run_Log.txt"))
    os.makedirs(os.path.join(out, SUBDIR_QA), exist_ok=True)
    with open(os.path.join(out, SUBDIR_QA, HIS.SNAPSHOT_FILE), "w") as fh:
        json.dump({"version": HIS.SNAPSHOT_VERSION, "nd": "High", "tm11": "High"}, fh)
    sfx = ("_Sunny_Day", "_PMF") if multi else ("",)
    for s in sfx:
        for b in ("Habitable_Structures", "Roads_Inundated"):
            _touch(os.path.join(out, SUBDIR_CLASSIFICATION, b + s + ".csv"))
        _touch(os.path.join(out, SUBDIR_EAP, "EAP_Notification_List" + s + ".csv"))
    _touch(os.path.join(out, SUBDIR_QA, "Evacuation_Timing.csv"))
    _touch(os.path.join(out, SUBDIR_QA, "Sensitivity.csv"))
    _touch(os.path.join(out, SUBDIR_QA, "Population_Source_CrossCheck.csv"))
    return out


def _paths(out, **kw):
    return {os.path.basename(src) for _g, src, _d in SB.collect(out, **kw)}


def test_sb1_multi_scenario_suffixed_tables():
    names = _paths(_outputs(True))
    for s in ("_Sunny_Day", "_PMF"):
        assert "Habitable_Structures{}.csv".format(s) in names, names
        assert "Roads_Inundated{}.csv".format(s) in names
        assert "EAP_Notification_List{}.csv".format(s) in names
    for q in ("Evacuation_Timing.csv", "Sensitivity.csv", "Population_Source_CrossCheck.csv"):
        assert q in names


def test_sb1_exact_name_precedence():
    out = _outputs(False)
    _touch(os.path.join(out, SUBDIR_CLASSIFICATION, "Habitable_Structures_old_copy.csv"))
    names = _paths(out)
    assert "Habitable_Structures.csv" in names
    assert "Habitable_Structures_old_copy.csv" not in names


def test_sb1_sb5_build_readme_and_missing():
    out = _outputs(True)
    res = SB.build(out, dam_name="Dam", zip_it=False, dest_root=tempfile.mkdtemp())
    rels = open(os.path.join(res["folder"], SB.MANIFEST_NAME)).read()
    assert "03_Results_tables/Habitable_Structures_PMF.csv" in rels
    readme = open(os.path.join(res["folder"], SB.README_NAME)).read()
    assert "Packaging warnings:" in readme and "NOT ENCLOSED" in readme
    assert not any(m.startswith("results tables") for m in res["missing"])
    # no tables at all -> named as missing + a warning
    out2 = _outputs(True)
    import shutil
    shutil.rmtree(os.path.join(out2, SUBDIR_CLASSIFICATION))
    res2 = SB.build(out2, dam_name="Dam", zip_it=False, dest_root=tempfile.mkdtemp())
    assert any(m.startswith("results tables") for m in res2["missing"])
    assert "no results tables were packaged" in res2["warnings"]
    assert "WARNING: no results tables" in open(os.path.join(res2["folder"],
                                                             SB.README_NAME)).read()


def test_wk3_missing_changes_flagged():
    d = tempfile.mkdtemp()
    os.makedirs(WK.qa_folder(d))
    assert WK.read_changes(d) is None
    before = {"nd": "High", "tm11": "High", "lij": 3.0, "time": "t0"}
    row = WK.summarise("Dam", d, before, dict(before), None)
    assert row["Newly_Flooded"] == "?" and "NOT checked" in row["Message"], row
    assert row["Alert"] == WK.ALERT_REVIEW
    row = WK.summarise("Dam", d, before, dict(before), [])
    assert row["Newly_Flooded"] == 0 and row["Message"] == "no change"
    p = os.path.join(WK.qa_folder(d), "Changes_Since_Last_Run_2.csv")      # renamed copy
    with open(p, "w") as fh:
        fh.write("Change,Item,Before,Now,X,Y\nRoad newly overtopped,Main,,,,\n")
    assert len(WK.read_changes(d)) == 1


def test_sh1_new_keys_no_spurious_diff():
    for k in ("population", "pop_period", "speed_mph", "occupancy", "access_ft"):
        assert k in HIS.SETTING_KEYS
    from DamSafety_Hazard.pipeline import DEFAULTS
    assert all(k in DEFAULTS for k in HIS.SETTING_KEYS)
    old = {"crs": "EPSG:1", "settings": {"one_d": False}, "structures": [], "roads": [],
           "lifelines": [], "rasters": {}, "nd": "High", "tm11": "High", "lij": 1.0}
    new = dict(old, settings={"one_d": False, "speed_mph": 30.0, "population": "night"})
    _l, rows = HIS.diff(old, new, 30.0)
    assert not [r for r in rows if r[0] == "Setting"], rows
    new2 = dict(new, settings={"one_d": False, "speed_mph": 45.0, "population": "night"})
    _l, rows = HIS.diff(new, new2, 30.0)
    assert [r[1] for r in rows if r[0] == "Setting"] == ["speed_mph"], rows


if __name__ == "__main__":
    fails = 0
    for n, f in sorted(globals().items()):
        if n.startswith("test_") and callable(f):
            try:
                f()
                print("PASS", n)
            except Exception as e:
                import traceback
                traceback.print_exc()
                fails += 1
                print("FAIL", n, e)
    sys.exit(1 if fails else 0)
