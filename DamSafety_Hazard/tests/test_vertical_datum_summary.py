"""Hazard 2.20.0: the summary's VERTICAL DATUM section, from the Runner's run_report.json
"vertical_datum" (else dam.json's): source datum, NGVD29 -> NAVD88 shift and method
(damsuite/vdatum.py). Tiny JSON fixtures, no rasters.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_vertical_datum_summary.py
"""
import json
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

from DamSafety_Hazard import pipeline as P  # noqa: E402
from DamSafety_Hazard import classification as CL  # noqa: E402
from DamSafety_Hazard.damsuite import vdatum as VD  # noqa: E402

LON, LAT = -97.391489, 46.983031          # DWR-2801


def _rec():
    rec = VD.plan(LON, LAT, "", VD.AUTO, use_pyproj=False)
    VD.convert_ft(1071.15, rec, "top_of_dam_ft")
    return VD.public(rec)


def test_datum_lines(d):
    assert P.datum_lines(d, d) == []
    dam = os.path.join(d, "D1")
    os.makedirs(os.path.join(dam, "_breach_model"))
    with open(os.path.join(dam, "dam.json"), "w") as f:
        json.dump({"dam_id": "D1", "vertical_datum": dict(_rec(), source_datum="NAVD88",
                                                          applied=False, fields={})}, f)
    lines = P.datum_lines(os.path.join(d, "out"), dam)
    assert lines and "NAVD88" in lines[0] and lines[-1] == "(from dam.json)", lines
    with open(os.path.join(dam, "_breach_model", "run_report.json"), "w") as f:
        json.dump({"vertical_datum": _rec()}, f)
    lines = P.datum_lines(os.path.join(d, "out"), dam)
    text = "\n".join(lines)
    assert "NGVD29" in lines[0] and "assumed" in lines[0].lower(), lines
    assert "+1.09" in text and "1072.24" in text and "VERTCON" in text, text
    assert lines[-1] == "(from run_report.json)"
    return lines


def test_summary_section(lines):
    nd = {"cls": "low", "counts": {}, "judgment": [], "reasons": []}
    tm11 = {"cls": "low", "lij": 0.0, "lij_structures_high": 0, "lij_structures_judgment": 0,
            "judgment_included": False, "lij_vehicles_high": 0.0, "lij_vehicles_judgment": 0.0,
            "cls_without_judgment": "low", "notes": []}
    L = CL.summary_lines(nd, tm11, [], [], [], "D1", [], False, extra={"datum": lines})
    text = "\n".join(L)
    assert "VERTICAL DATUM OF THE DAM ELEVATIONS" in text, text
    assert "NGVD29 -> NAVD88)" not in text.split("VERTICAL DATUM", 1)[0]
    heading = next(x for x in L if x.startswith("VERTICAL DATUM"))
    assert heading == "VERTICAL DATUM OF THE DAM ELEVATIONS", heading
    assert "1072.24" in text and "+1.09" in text and "NGVD29" in text, text
    assert "VERTICAL DATUM" not in "\n".join(
        CL.summary_lines(nd, tm11, [], [], [], "D1", [], False, extra={}))



def _local_rec():
    rec = VD.plan(LON, LAT, "", VD.AUTO, use_pyproj=False,
                  values={"top_of_dam_ft": 120.7, "streambed_ft": 100.0})
    rec.update(tie="done", local_offset_ft=1509.79,
               dem_crest_from="mid-crest of the dam line", warnings=[])
    rec["fields"] = {
        "top_of_dam_ft": {"source": 120.7, "navd88": 1630.49, "how": "DEM crest"},
        "streambed_ft": {"source": 100.0, "navd88": 1609.79, "how": "DEM-derived"},
    }
    rec["dam_point"] = {
        "xy": [-407005.516, 2822706.843], "z_ft": 1630.49, "moved_m": 27.77,
        "original_xy": [-406980.0, 2822690.0], "offset_m": 0.4, "moved": True,
        "method": "dam line midpoint by length; highest DEM point +-25 m",
    }
    return VD.public(rec)


def test_local_dem_tie(d):
    """2.20.3: a 0-300 ft record, a REVIEW, a DEM-replaced band value, and a
    mid-crest dam point (including one stored beside the record) reach the summary."""
    dam = os.path.join(d, "D4031")
    os.makedirs(os.path.join(dam, "_breach_model"))
    rec = _local_rec()
    point = rec.pop("dam_point")
    with open(os.path.join(dam, "_breach_model", "run_report.json"), "w") as f:
        json.dump({"vertical_datum": rec, "dam_point": point}, f)
    lines = P.datum_lines(os.path.join(d, "out"), dam)
    text = "\n".join(lines)
    assert "0 - 300 ft rule" in text, text
    assert "1609.79" in text and "DEM-derived" in text, text
    assert "Dam point at mid-crest" in text and "27.77" in text, text
    assert lines[-1] == "(from run_report.json)"
    rev = dict(rec, review=True, review_reason="no DEM crest", local_offset_ft=None, tie="review")
    rev["fields"] = {"streambed_ft": {"source": 100.0, "unconverted": True, "note": "no DEM tie"}}
    rtext = "\n".join(VD.summary_lines(rev))
    assert "REVIEW:" in rtext and "UNCONVERTED" in rtext, rtext
    navd = VD.public(VD.plan(LON, LAT, "NAVD88", VD.AUTO, use_pyproj=False))
    navd["fields"] = {"streambed_ft": {"source": 100.0, "navd88": 1695.0, "dem_derived": True,
                                      "how": "DEM-derived: thalweg at the toe"}}
    ntext = "\n".join(VD.summary_lines(navd))
    assert "0-300 ft, not used" in ntext and "1695.00" in ntext, ntext
    nd = {"cls": "low", "counts": {}, "judgment": [], "reasons": []}
    tm11 = {"cls": "low", "lij": 0.0, "lij_structures_high": 0, "lij_structures_judgment": 0,
            "judgment_included": False, "lij_vehicles_high": 0.0, "lij_vehicles_judgment": 0.0,
            "cls_without_judgment": "low", "notes": []}
    L = CL.summary_lines(nd, tm11, [], [], [], "D4031", [], False, extra={"datum": lines})
    heading = next(x for x in L if x.startswith("VERTICAL DATUM"))
    assert heading == "VERTICAL DATUM OF THE DAM ELEVATIONS", heading
    assert "Dam point at mid-crest" in "\n".join(L)


def main():
    d = tempfile.mkdtemp(prefix="hz_vdatum_")
    try:
        lines = test_datum_lines(d)
        test_summary_section(lines)
        test_local_dem_tie(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("ok test_vertical_datum_summary")


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)
