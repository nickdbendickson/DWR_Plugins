"""Hazard 2.21.0: 'Add model results to a dam' (adddam.py, ndhazard:adddamresults and the
window opened from the Classify view). Synthetic data only, in temp folders:

  - the dam folder is found by folder name or dam.json ID / name (case, spaces, ID forms);
    when there is none it is made, with a dam.json
  - nothing is overwritten (a numbered folder), the same source is not copied twice
  - a DSS-WISE Lite zip, RAS Mapper rasters (VRT + tiles, SI converted), a HEC-RAS plan
    (rasimport stubbed) and LISFLOOD-FP .max / .maxVc / .inittm ESRI-ASCII grids
  - autodetect.find_rasters (the batch) takes the results; a no-breach run is the baseline
  - the source is never changed; the read-only pack is refused
  - the window (offscreen) and the Processing algorithm

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_add_dam_results.py
"""
import json
import os
import shutil
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.pop("NDSUITE_SETTINGS", None)

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

import numpy as np  # noqa: E402
from osgeo import gdal, osr  # noqa: E402

from DamSafety_Hazard import adddam as AD  # noqa: E402
from DamSafety_Hazard.autodetect import find_rasters  # noqa: E402

UTM14 = osr.SpatialReference()
UTM14.ImportFromEPSG(26914)


def _tif(path, value, unit=None, n=20):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ds = gdal.GetDriverByName("GTiff").Create(path, n, n, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((600000, 10, 0, 5200000, 0, -10))
    ds.SetProjection(UTM14.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.WriteArray(np.full((n, n), value, "float32"))
    b.SetNoDataValue(-9999)
    if unit:
        b.SetUnitType(unit)
    ds = None
    return path


def _asc(path, value, prj=False):
    """A tiny LISFLOOD-FP-style ESRI-ASCII grid (5 x 4, one NoData cell)."""
    rows = []
    for r in range(4):
        rows.append(" ".join("-9999" if (r, c) == (0, 0) else "{:.3f}".format(value)
                             for c in range(5)))
    with open(path, "w") as fh:
        fh.write("ncols 5\nnrows 4\nxllcorner 600000\nyllcorner 5199960\ncellsize 10\n"
                 "NODATA_value -9999\n" + "\n".join(rows) + "\n")
    if prj:
        with open(os.path.splitext(path)[0] + ".prj", "w") as fh:
            fh.write(UTM14.ExportToWkt())
    return path


def _read(path):
    ds = gdal.Open(path)
    b = ds.GetRasterBand(1)
    return b.ReadAsArray(), b.GetNoDataValue(), b.GetUnitType(), ds.GetProjection()


def _tree(folder):
    """{relpath: (size, mtime_ns, bytes)} - to prove the source is not changed."""
    out = {}
    for dp, _d, fs in os.walk(folder):
        for f in fs:
            p = os.path.join(dp, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, folder)] = (os.path.getsize(p),
                                                   os.stat(p).st_mtime_ns, fh.read())
    return out


def _dams():
    """A Dams folder with 'Absaraka Dam' (dam.json: DWR-2801) and '0199' (no record)."""
    root = os.path.join(tempfile.mkdtemp(prefix="hz_add_"), "Dams")
    a = os.path.join(root, "Absaraka Dam")
    os.makedirs(a)
    with open(os.path.join(a, "dam.json"), "w") as fh:
        json.dump({"format": "nd_dam_suite/dam_record", "format_version": 1,
                   "fields": {"dam_id": "DWR-2801", "name": "Absaraka"}}, fh)
    os.makedirs(os.path.join(root, "0199"))
    os.makedirs(os.path.join(root, "_archive", "2801"))      # skipped, as by the batch
    return root


def _ras(folder, depth=3.0, unit=None, tag=""):
    """RAS Mapper style export: Depth (Max).vrt reading a tile, Velocity / Arrival tifs."""
    os.makedirs(folder, exist_ok=True)
    tile = _tif(os.path.join(folder, "Depth (Max)", "Depth (Max).Terrain.tif"), depth, unit)
    vrt = os.path.join(folder, "Depth (Max).vrt")
    gdal.BuildVRT(vrt, [tile])
    txt = open(vrt).read()
    if 'relativeToVRT="1"' not in txt:          # BuildVRT writes a relative path normally
        txt = txt.replace('relativeToVRT="0">' + tile,
                          'relativeToVRT="1">Depth (Max)/Depth (Max).Terrain.tif')
        open(vrt, "w").write(txt)
    _tif(os.path.join(folder, "Velocity (Max).tif"), 2.0, unit)
    _tif(os.path.join(folder, "Arrival Time.tif"), 1.5)
    with open(os.path.join(folder, "readme{}.txt".format(tag)), "w") as fh:
        fh.write("export " + tag)
    return folder


# ------------------------------------------------------------------ matching / creating --
def test_dam_list_like_batch():
    """Folders the batch skips (_archive) are never matched."""
    root = _dams()
    labels = dict(AD.dam_folders(root))
    assert os.path.join(root, "_archive") not in labels
    assert labels[os.path.join(root, "Absaraka Dam")] == ["Absaraka Dam", "DWR-2801", "Absaraka"]


def test_match_existing_folder():
    root = _dams()
    absaraka, d199 = os.path.join(root, "Absaraka Dam"), os.path.join(root, "0199")
    for typed in ("absaraka dam", "ABSARAKADAM", " Absaraka-Dam "):
        assert AD.match_dam(root, typed) == absaraka, typed
    for typed in ("199", "0199", "ND199", "nid 0199", "ND-00199"):
        assert AD.match_dam(root, typed) == d199, typed
    assert AD.match_dam(root, "Nowhere") is None
    assert AD.match_dam(root, "2802") is None
    assert "0199" in AD.completions(root) and "Absaraka Dam" in AD.completions(root)


def test_match_reads_dam_json():
    root = _dams()
    # written with the real dam_record module
    from DamSafety_Hazard.damsuite import dam_record as DR
    d = os.path.join(root, "Folder A")
    os.makedirs(d)
    rec = DR.load(d, create=True)
    rec.set("dam_id", "DWR-3055", "test", "0")
    rec.set("name", "Lake Ashtabula Dam", "test", "0")
    rec.save()
    assert AD.match_dam(root, "dwr 3055") == d
    assert AD.match_dam(root, "lake ashtabula dam") == d
    assert "Lake Ashtabula Dam" in AD.completions(root)
    os.makedirs(os.path.join(root, "3055"))
    try:
        AD.match_dam(root, "3055")
        raise AssertionError("two folders fit 3055: must not guess")
    except AD.AddResultsError as e:
        assert "2 dam folders" in str(e), e


def test_new_folder_names():
    assert AD.new_folder_name("dwr-2801") == "DWR-2801"
    assert AD.new_folder_name("  Lake  Darling: Dam ") == "Lake Darling_ Dam"
    for bad in ("", "  ", "..", "_", "CON", "outputs", "///"):
        try:
            AD.new_folder_name(bad)
            raise AssertionError(bad)
        except AD.AddResultsError:
            pass


def test_creates_dam_folder_with_dam_json():
    root = _dams()
    src = _ras(os.path.join(tempfile.mkdtemp(), "RAS export"))
    res = AD.add_results("Lake Darling Dam", src, AD.MODEL_HECRAS, dams_folder=root)
    d = os.path.join(root, "Lake Darling Dam")
    assert res["created"] and res["dam_folder"] == d, res
    rec = json.load(open(os.path.join(d, "dam.json")))
    assert rec["fields"]["name"] == "Lake Darling Dam", rec
    st = [x for x in rec["status"] if x["step"] == "modelled"]
    assert st and st[-1]["tool"] == AD.TOOL and st[-1]["result"]["source_sha256"], rec["status"]
    # typed as an ID: dam_id, folder name upper case; matched again the next time
    res2 = AD.add_results("nd 00777", src, AD.MODEL_HECRAS, dams_folder=root)
    assert os.path.basename(res2["dam_folder"]) == "ND 00777", res2["dam_folder"]
    rec2 = json.load(open(os.path.join(res2["dam_folder"], "dam.json")))
    assert rec2["fields"]["dam_id"] == "ND 00777"
    assert AD.match_dam(root, "ND777") == res2["dam_folder"]
    # the batch lists it like any dam folder
    from DamSafety_Hazard.watch import dam_folders
    assert d in [os.path.normpath(p) for p in dam_folders(root)], dam_folders(root)


# ------------------------------------------------------------------ HEC-RAS rasters --------
def test_hecras_rasters_copied_and_found():
    root = _dams()
    src = _ras(os.path.join(tempfile.mkdtemp(), "Absaraka breach"))
    before = _tree(src)
    res = AD.add_results("DWR-2801", src, dams_folder=root)   # Auto-detect -> HEC-RAS
    assert res["status"] == "added" and res["model"] == AD.MODEL_HECRAS, res
    folder = os.path.join(root, "Absaraka Dam", "Hazard_inputs", "Breach_HECRAS")
    assert res["folder"] == folder, res["folder"]
    assert os.path.isfile(os.path.join(folder, "Depth (Max)", "Depth (Max).Terrain.tif"))
    arr, _nd, _u, _p = _read(os.path.join(folder, "Depth (Max).vrt"))   # tile copied too
    assert abs(float(arr.max()) - 3.0) < 1e-6
    found = find_rasters(res["dam_folder"])
    assert found["depth"] == res["paths"]["depth"], (found, res["paths"])
    assert found["vel"] == os.path.join(folder, "Velocity (Max).tif"), found
    assert found["arr"] == os.path.join(folder, "Arrival Time.tif"), found
    assert res["picked_by_batch"] is True
    prov = json.load(open(os.path.join(folder, AD.PROVENANCE_FILE)))
    for k in ("source", "source_sha256", "model", "when", "plugin_version", "tool"):
        assert prov.get(k), (k, prov)
    assert prov["source_sha256"] == AD.source_sha256(src) and prov["model"] == "HEC-RAS"
    assert _tree(src) == before, "the source was changed"


def test_si_rasters_converted_to_feet():
    root = _dams()
    src = _ras(os.path.join(tempfile.mkdtemp(), "metric"), depth=2.0, unit="m")
    res = AD.add_results("0199", src, AD.MODEL_HECRAS, dams_folder=root)
    a, _nd, unit, _p = _read(res["paths"]["depth"])
    assert res["paths"]["depth"].endswith("Depth (Max)_ft.tif"), res["paths"]
    assert abs(float(a.max()) - 2.0 * AD.FT_PER_M) < 1e-3 and unit == "ft", (a.max(), unit)
    v, _nd, vu, _p = _read(res["paths"]["vel"])
    assert abs(float(v.max()) - 2.0 * AD.FT_PER_M) < 1e-3 and vu == "ft/s"
    assert any("SI units" in n for n in res["notes"]), res["notes"]
    assert find_rasters(res["dam_folder"])["depth"] == res["paths"]["depth"]


def test_hecras_plan_uses_plan_import():
    from DamSafety_Hazard import rasimport as RI
    root = _dams()
    src = os.path.join(tempfile.mkdtemp(), "Model")
    os.makedirs(src)
    plan = os.path.join(src, "Absaraka.p03.hdf")
    open(plan, "wb").write(b"\x89HDF stub")
    seen = {}
    real = RI.import_plan

    def fake(hdf, out, dem_path=None, progress=None, runner_dirs=None):
        seen.update(hdf=hdf, dem=dem_path)
        d = _tif(os.path.join(out, "Depth_Max_ft.tif"), 4.0, "ft")
        return {"paths": {"depth": d}, "notes": ["stub"],
                "depth_basis": RI.DEPTH_BASIS_CELL_MIN}
    RI.import_plan = fake
    try:
        assert AD.detect_model(src) == AD.MODEL_HECRAS
        res = AD.add_results("Absaraka", src, dams_folder=root)
    finally:
        RI.import_plan = real
    assert seen["hdf"] == plan and seen["dem"] is None, seen
    assert any(RI.CELL_MIN_WARNING in n for n in res["notes"]), res["notes"]
    assert find_rasters(res["dam_folder"])["depth"] == res["paths"]["depth"]


# ------------------------------------------------------------------ no overwrite ----------
def test_never_overwrites():
    root = _dams()
    tmp = tempfile.mkdtemp()
    a = _ras(os.path.join(tmp, "run A"), depth=3.0, tag="a")
    b = _ras(os.path.join(tmp, "run B"), depth=5.0, tag="b")
    r1 = AD.add_results("2801", a, AD.MODEL_HECRAS, dams_folder=root)
    first = _tree(r1["folder"])
    r2 = AD.add_results("2801", b, AD.MODEL_HECRAS, dams_folder=root)
    assert r2["folder"].endswith(os.path.join("Hazard_inputs", "Breach_HECRAS_2")), r2["folder"]
    assert any("nothing overwritten" in n for n in r2["notes"]), r2["notes"]
    assert _tree(r1["folder"]) == first, "the first results were changed"
    r3 = AD.add_results("2801", a, AD.MODEL_HECRAS, dams_folder=root)
    assert r3["status"] == "already added" and r3["folder"] == r1["folder"], r3
    assert sorted(os.listdir(os.path.join(root, "Absaraka Dam", "Hazard_inputs"))) == \
        ["Breach_HECRAS", "Breach_HECRAS_2"]
    # the same source as no-breach is a different scenario: added
    r4 = AD.add_results("2801", a, AD.MODEL_HECRAS, AD.SCEN_BASELINE, dams_folder=root)
    assert r4["status"] == "added" and r4["folder"].endswith("No_Breach_HECRAS")


def test_failure_leaves_nothing():
    root = _dams()
    src = os.path.join(tempfile.mkdtemp(), "nodepth")
    _tif(os.path.join(src, "Velocity (Max).tif"), 1.0)
    try:
        AD.add_results("0199", src, AD.MODEL_HECRAS, dams_folder=root)
        raise AssertionError("no depth raster: must fail")
    except AD.AddResultsError as e:
        assert "maximum-depth" in str(e), e
    inputs = os.path.join(root, "0199", "Hazard_inputs")
    assert not os.path.isdir(inputs) or os.listdir(inputs) == [], os.listdir(inputs)


# ------------------------------------------------------------------ DSS-WISE --------------
def test_dsswise_zip():
    root = _dams()
    tmp = tempfile.mkdtemp()
    pkg = os.path.join(tmp, "pkg")
    _tif(os.path.join(pkg, "MaxDepth_in_Ft.tif"), 6.0)
    _tif(os.path.join(pkg, "MaxVelocity_in_Ft_per_Sec.tif"), 3.0)
    _tif(os.path.join(pkg, "ArrivalTime_in_Hr.tif"), 0.5)
    z = os.path.join(tmp, "DSS-WISE_Absaraka.zip")
    with zipfile.ZipFile(z, "w") as zf:
        for n in os.listdir(pkg):
            zf.write(os.path.join(pkg, n), "Absaraka/" + n)
    before = _tree(tmp)
    assert AD.detect_model(z) == AD.MODEL_DSSWISE
    res = AD.add_results("Absaraka", z, dams_folder=root)
    assert res["model"] == AD.MODEL_DSSWISE and res["folder"].endswith("Breach_DSSWISE")
    for role in ("depth", "vel", "arr"):
        assert res["paths"].get(role) and os.path.isfile(res["paths"][role]), (role, res)
    a, _nd, _u, _p = _read(res["paths"]["depth"])
    assert abs(float(a.max()) - 6.0) < 1e-4
    found = find_rasters(res["dam_folder"])
    assert found["depth"] == res["paths"]["depth"], (found, res["paths"])
    assert found["vel"] == res["paths"]["vel"] and found["arr"] == res["paths"]["arr"]
    assert _tree(tmp) == before, "the zip / its folder was changed"


def test_dsswise_assumed_units_need_ok():
    root = _dams()
    pkg = os.path.join(tempfile.mkdtemp(), "DSS-WISE pkg")
    _tif(os.path.join(pkg, "MaxDepth.tif"), 6.0)
    try:
        AD.add_results("0199", pkg, AD.MODEL_DSSWISE, dams_folder=root)
        raise AssertionError("assumed units must be accepted first")
    except AD.AddResultsError as e:
        assert "assumed" in str(e), e
    res = AD.add_results("0199", pkg, AD.MODEL_DSSWISE, dams_folder=root, accept_assumed=True)
    assert res["status"] == "added"


# ------------------------------------------------------------------ LISFLOOD-FP ----------
def test_lisflood_max_converted():
    root = _dams()
    run = os.path.join(tempfile.mkdtemp(), "results")
    os.makedirs(run)
    _asc(os.path.join(run, "absaraka.max"), 2.0, prj=True)
    _asc(os.path.join(run, "absaraka.maxVc"), 1.0)
    _asc(os.path.join(run, "absaraka.inittm"), 0.75)
    _asc(os.path.join(run, "absaraka-0001.wd"), 9.0)          # a snapshot: ignored
    before = _tree(run)
    assert AD.detect_model(run) == AD.MODEL_LISFLOOD
    res = AD.add_results("DWR 2801", run, dams_folder=root)
    assert res["model"] == AD.MODEL_LISFLOOD and res["folder"].endswith("Breach_LISFLOOD")
    d, nd, unit, prj = _read(res["paths"]["depth"])
    assert os.path.basename(res["paths"]["depth"]) == "Depth_Hmax_ft.tif"
    valid = d[d != nd]
    assert abs(float(valid.max()) - 2.0 * AD.FT_PER_M) < 1e-3 and unit == "ft", (valid, unit)
    assert d[0, 0] == nd, "NoData kept"
    s = osr.SpatialReference()
    s.ImportFromWkt(prj)
    assert s.IsSame(UTM14), prj
    v, vnd, vu, _p = _read(res["paths"]["vel"])
    assert abs(float(v[v != vnd].max()) - AD.FT_PER_M) < 1e-3 and vu == "ft/s"
    t, tnd, tu, _p = _read(res["paths"]["arr"])
    assert abs(float(t[t != tnd].max()) - 0.75) < 1e-4 and tu == "h"
    found = find_rasters(res["dam_folder"])
    for role in ("depth", "vel", "arr"):
        assert found[role] == res["paths"][role], (role, found)
    assert _tree(run) == before


def test_lisflood_needs_a_crs():
    root = _dams()
    run = os.path.join(tempfile.mkdtemp(), "noprj")
    os.makedirs(run)
    single = _asc(os.path.join(run, "dam.max"), 1.0)
    try:
        AD.add_results("0199", single, dams_folder=root)
        raise AssertionError("no CRS anywhere: must ask for one")
    except AD.AddResultsError as e:
        assert "coordinate system" in str(e), e
    res = AD.add_results("0199", single, dams_folder=root, crs="EPSG:26914")
    _a, _nd, _u, prj = _read(res["paths"]["depth"])
    s = osr.SpatialReference()
    s.ImportFromWkt(prj)
    assert s.IsSame(UTM14)
    # two runs in one folder: the run's .max must be chosen
    _asc(os.path.join(run, "other.max"), 1.0)
    try:
        AD.add_results("0199", run, AD.MODEL_LISFLOOD, dams_folder=root, crs="EPSG:26914")
        raise AssertionError("two runs: must not guess")
    except AD.AddResultsError as e:
        assert "2 LISFLOOD-FP runs" in str(e), e


# ------------------------------------------------------------------ baseline --------------
def test_baseline_placement():
    root = _dams()
    tmp = tempfile.mkdtemp()
    br = _ras(os.path.join(tmp, "breach"), depth=4.0, tag="b")
    nb = _ras(os.path.join(tmp, "fair weather"), depth=1.0, tag="n")
    rb = AD.add_results("Absaraka", br, AD.MODEL_HECRAS, AD.SCEN_BREACH, dams_folder=root)
    rn = AD.add_results("Absaraka", nb, AD.MODEL_HECRAS, AD.SCEN_BASELINE, dams_folder=root)
    assert rn["folder"].endswith(os.path.join("Hazard_inputs", "No_Breach_HECRAS"))
    found = find_rasters(rb["dam_folder"])
    assert found["depth"] == rb["paths"]["depth"], found
    assert found["base_depth"] == rn["paths"]["depth"], found
    assert rb["picked_by_batch"] and rn["picked_by_batch"], (rb, rn)


# ------------------------------------------------------------------ refusals --------------
def test_refusals():
    root = _dams()
    src = _ras(os.path.join(tempfile.mkdtemp(), "x"))
    pack = os.path.join(tempfile.mkdtemp(), "ND_Dam_Suite_Pack", "Dams")
    for bad_root, word in ((pack, "read-only"), (os.path.join(root, "Absaraka Dam"),
                                                 "dam's own folder"), ("/", "cannot be")):
        try:
            AD.add_results("2801", src, AD.MODEL_HECRAS, dams_folder=bad_root)
            raise AssertionError(bad_root)
        except AD.AddResultsError as e:
            assert word in str(e), (bad_root, e)
    assert not os.path.exists(pack)
    inside = _ras(os.path.join(root, "Absaraka Dam", "RAS"))
    try:
        AD.add_results("2801", inside, AD.MODEL_HECRAS, dams_folder=root)
        raise AssertionError("source in the dam folder")
    except AD.AddResultsError as e:
        assert "already in the dam folder" in str(e), e
    try:
        AD.add_results("2801", os.path.dirname(root), AD.MODEL_HECRAS, dams_folder=root)
        raise AssertionError("source holds the Dams folder")
    except AD.AddResultsError as e:
        assert "holds the Dams folder" in str(e), e
    try:
        AD.add_results("2801", "/nonexistent/x", dams_folder=root)
        raise AssertionError("missing source")
    except AD.AddResultsError:
        pass
    try:
        AD.add_results("New dam", src, AD.MODEL_HECRAS, dams_folder=root, create=False)
        raise AssertionError("create=False")
    except AD.AddResultsError:
        assert not os.path.exists(os.path.join(root, "New dam"))


def test_csv_rows():
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "many.csv")
    with open(p, "w") as fh:
        fh.write("Dam ID,Results,Model,Scenario\nDWR-2801,runs/a,lisflood,\n"
                 "Lake Darling,/abs/b.zip,DSS-WISE,no breach\n,,,\n")
    rows = AD.read_csv(p)
    assert len(rows) == 2, rows
    assert rows[0]["source"] == os.path.join(tmp, "runs/a") and rows[0]["model"] == "lisflood"
    assert rows[0]["scenario"] == AD.SCEN_BREACH
    assert rows[1]["model"] == AD.MODEL_DSSWISE and rows[1]["scenario"] == AD.SCEN_BASELINE


# ------------------------------------------------------------------ window + Processing ---
def test_dialog_offscreen():
    from DamSafety_Hazard.add_results_dialog import AddResultsDialog
    root = _dams()
    src = _ras(os.path.join(tempfile.mkdtemp(), "ras"))
    used = []
    d = AddResultsDialog(on_use=used.append)
    try:
        assert d.windowTitle().startswith("DamSafety ")
        d.root_edit.setText(root)
        d.dam_edit.setText("absaraka")
        assert "Existing dam folder" in d.match_label.text(), d.match_label.text()
        d.dam_edit.setText("Brand New Dam")
        assert "will be made" in d.match_label.text(), d.match_label.text()
        d.src_edit.setText(src)
        res = d.run()
        assert res and res["created"], d.log.toPlainText()
        assert "Added:" in d.log.toPlainText(), d.log.toPlainText()
        assert d.use_btn.isEnabled()
        d._use()
        assert used == [os.path.join(root, "Brand New Dam")], used
        assert "Brand New Dam" in AD.completions(root)
        d.src_edit.setText("/nonexistent")
        assert d.run() is None and "Not added" in d.log.toPlainText()
    finally:
        d.deleteLater()


def test_classify_view_button():
    """The Setup box of the Classify view opens the window; 'Use this dam folder' fills the
    dam folder box."""
    from qgis.core import QgsProject
    from DamSafety_Hazard import settings as ST
    from DamSafety_Hazard import dialog as DLG
    from DamSafety_Hazard import add_results_dialog as ARD
    QgsProject.instance().removeEntry(ST.SCOPE, ST.KEY)
    d = DLG.NDDamHazardDialog()
    root = _dams()
    seen = []

    def fake_exec(self):
        seen.append(self.windowTitle())
        self.on_use(os.path.join(root, "0199"))
        return 0
    old = ARD.AddResultsDialog.exec_ if hasattr(ARD.AddResultsDialog, "exec_") else None
    ARD.AddResultsDialog.exec_ = fake_exec
    ARD.AddResultsDialog.exec = fake_exec
    try:
        btn = d.add_results_btn
        assert btn.text() == "Add model results to a dam\u2026", btn.text()
        btn.click()
        assert seen and seen[0].startswith("DamSafety Hazard"), seen
        assert d.dam_folder_edit.text() == os.path.join(root, "0199")
    finally:
        del ARD.AddResultsDialog.exec
        if old is None:
            del ARD.AddResultsDialog.exec_
        else:
            ARD.AddResultsDialog.exec_ = old
        d._busy = False
        d.close()
        d.deleteLater()
        _app.processEvents()


def test_processing_algorithm():
    from qgis.core import QgsProcessingContext, QgsProcessingFeedback
    from DamSafety_Hazard.processing_provider import AddDamResultsAlgorithm
    root = _dams()
    run = os.path.join(tempfile.mkdtemp(), "lf")
    os.makedirs(run)
    _asc(os.path.join(run, "r.max"), 1.0, prj=True)
    alg = AddDamResultsAlgorithm().create()
    alg.initAlgorithm({})
    ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
    ok_params = {"DAM": "ND123", "SOURCE_FOLDER": run, "MODEL": 0, "SCENARIO": 0,
                 "DAMS_FOLDER": root}
    out = alg.processAlgorithm(ok_params, ctx, fb)
    assert out["DAM_FOLDER"] == os.path.join(root, "ND123"), out
    assert out["RESULTS_FOLDER"].endswith("Breach_LISFLOOD"), out
    assert os.path.isfile(os.path.join(out["RESULTS_FOLDER"], "Depth_Hmax_ft.tif"))


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name, flush=True)
    print("{} passed".format(n), flush=True)
    sys.stdout.flush()
    os._exit(0)
