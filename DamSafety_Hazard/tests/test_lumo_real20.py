"""Lumo vReal.20 BP.1-3 / XT.1-2 and vReal.21 SM.2/4/5/6 (Hazard 2.18.31).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_lumo_real20.py
"""
import os
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem,  # noqa: E402
                       QgsCoordinateTransformContext, QgsGeometry, QgsPointXY, QgsRectangle)
_app = QgsApplication([], False)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard import build_nd_pack as BP  # noqa: E402
from DamSafety_Hazard import offline_pack as OP  # noqa: E402
from DamSafety_Hazard import extent as XT  # noqa: E402
from DamSafety_Hazard import submittal as SB  # noqa: E402
import test_lumo_real18b as R18B  # noqa: E402


class Svc(object):
    """A paged ArcGIS service: n features, page size from the caller."""
    def __init__(self, n, count_ok=True, fail_at=None):
        self.n, self.count_ok, self.fail_at, self.offsets = n, count_ok, fail_at, []

    def __call__(self, url, params=None, post=False, retries=None, what=""):
        p = params or {}
        if p.get("returnCountOnly"):
            if not self.count_ok:
                raise BP.BuildError("count not supported")
            return {"count": self.n}
        if p.get("outStatistics"):
            return {"features": [{"attributes": {"MX": self.n}}]}
        off, size = int(p["resultOffset"]), int(p["resultRecordCount"])
        if self.fail_at is not None and off >= self.fail_at:
            raise RuntimeError("interrupted")
        self.offsets.append(off)
        feats = [{"type": "Feature", "properties": {"OBJECTID": i + 1},
                  "geometry": {"type": "Point", "coordinates": [-100.0, 46.0]}}
                 for i in range(off, min(self.n, off + size))]
        return {"features": feats, "exceededTransferLimit": off + size < self.n}


def _run(db, svc, logs):
    w = OP.PackWriter(db, (-101, 45, -99, 47))
    w.begin("pages", "Pages", "http://svc", resume=True, signature="s")
    ctx = BP._Ctx(w, 1, logs.append, lambda *a, **k: None)
    old = BP.http_json
    BP.http_json = svc
    try:
        note = BP._fetch_arcgis_pages(ctx, "http://svc", (-101, 45, -99, 47), "OBJECTID", 2,
                                      "*", None, BP._uid_arcgis("OBJECTID"))
    finally:
        BP.http_json = old
    return w, ctx, note


def _interrupted(db, svc):
    try:
        _run(db, svc, [])
    except RuntimeError:
        return
    raise AssertionError("not interrupted")


def test_bp1_changed_count_restarts_pages():
    db = os.path.join(tempfile.mkdtemp(), "w.sqlite")
    _interrupted(db, Svc(5, fail_at=2))
    logs = []
    w, ctx, note = _run(db, Svc(6), logs)
    assert any("service changed" in m for m in logs), logs
    assert w.count == 6 and ctx.status == "complete" and note == "", (w.count, note)


def test_bp1_same_service_resumes():
    db = os.path.join(tempfile.mkdtemp(), "w.sqlite")
    _interrupted(db, Svc(5, fail_at=2))
    svc, logs = Svc(5), []
    w, ctx, note = _run(db, svc, logs)
    assert 0 not in svc.offsets and w.count == 5 and ctx.status == "complete", svc.offsets
    assert any("resuming: 1 page" in m for m in logs), logs


def test_bp1_unknown_anchor_partial():
    db = os.path.join(tempfile.mkdtemp(), "w.sqlite")
    _interrupted(db, Svc(5, count_ok=False, fail_at=2))
    w, ctx, note = _run(db, Svc(5), [])
    assert ctx.status == OP.STATUS_PARTIAL and "could not be checked" in note, note


def test_bp2_bp3_started_and_count():
    db = os.path.join(tempfile.mkdtemp(), "w.sqlite")
    w = OP.PackWriter(db, (-101, 45, -99, 47))
    assert w.count == 0
    w.begin("x", "X", "http://x")
    assert w.count == 0 and len(w.stage_started("x")) == 19


class _BadXf(object):
    def transform(self, pt):
        raise RuntimeError("no grid")


def _clip(logs):
    XT.log_message = lambda m, level=None: logs.append(m)
    return XT.ExtentClip(None, 0, QgsCoordinateTransformContext(),
                         rect=QgsRectangle(0, 0, 10, 10),
                         crs=QgsCoordinateReferenceSystem("EPSG:26914"))


def test_xt1_failures_counted_logged_once_loud_warning():
    logs = []
    c = _clip(logs)
    assert c.contains_point(QgsPointXY(1, 1), xform=_BadXf()) is False
    assert sum("transformation failed" in m for m in logs) == 1
    assert not any("FAILED" in m for m in logs)
    for _ in range(25):
        c.contains_point(QgsPointXY(1, 1), xform=_BadXf())
    assert sum("transformation failed" in m for m in logs) == 1
    assert sum("FAILED" in m for m in logs) == 1, logs
    assert c.transform_failures() == (26, 26)
    assert c.contains_point(QgsPointXY(1, 1)) is True          # single point unchanged


def test_xt2_invalid_crs_warned():
    logs = []
    c = _clip(logs)
    assert c.transform_from(QgsCoordinateReferenceSystem()) is None
    c.transform_from(QgsCoordinateReferenceSystem())
    assert sum("is invalid" in m for m in logs) == 1, logs
    assert c.intersects_geometry(QgsGeometry.fromPointXY(QgsPointXY(1, 1)))


def test_sm2_sm6_zip_atomic_and_readme_line():
    out = R18B._outputs(multi=False)
    r = SB.build(out, dam_name="Test", zip_it=True)
    assert os.path.isfile(r["zip"]) and not os.path.exists(r["zip"] + ".part")
    assert zipfile.ZipFile(r["zip"]).testzip() is None
    txt = open(os.path.join(r["folder"], SB.README_NAME), encoding="utf-8").read()
    assert "keep the zip secured" in txt


def test_sm5_profile_info_import_error():
    saved = sys.modules.get("DamSafety_Hazard.reproduce")
    sys.modules["DamSafety_Hazard.reproduce"] = None          # import -> ImportError
    try:
        assert SB._profile_info(tempfile.mkdtemp(), None) == (None, None)
    finally:
        if saved is not None:
            sys.modules["DamSafety_Hazard.reproduce"] = saved
        else:
            sys.modules.pop("DamSafety_Hazard.reproduce", None)


def test_sm4_same_basename_not_overwritten():
    out = R18B._outputs(multi=False)
    other = os.path.join(tempfile.mkdtemp(), "Hazard_Classification_Run_Log.txt")
    R18B._touch(other, "other\n")
    old = SB.collect
    SB.collect = lambda o, include_gis=False: old(o, include_gis) + [
        ("02_Method_and_record", other, "x"), ("02_Method_and_record", other + "", "y")]
    try:
        items = SB.collect(out)
        same = [p for g, p, d in items if os.path.basename(p) == os.path.basename(other)]
        r = SB.build(out, dam_name="T", zip_it=False)
    finally:
        SB.collect = old
    paths = [x["Path"] for x in r["files"]]
    assert len(paths) == len(set(paths)), paths
    assert len(same) >= 2


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
