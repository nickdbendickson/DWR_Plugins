"""Runner 3.12.3: culvert prep (core/culvert_prep.py, the `culvertprep` Processing tool and
`ndbreach_cli.py culvert-prep`): a corrected COPY of a structures layer from the culvert
pre-check, with large / non-native / geometry changes flagged for review and an accept flow.

Fixture: tests/data/culvert_prep_2801.json, 8 culverts of DWR-2801's structures layer (public
NDDOT pipe / NBI derived), the lowest native 1 m ground within 5 m of each end from the
2026-10-24 pre-check, and the inverts after the manual, Nick-approved invfix / invfix2. The DEM
is synthetic: a 21 x 21 m, 1 m tile around each end at ground + 0.8 m with ONE cell at the
recorded ground 2-3 m from the end and a decoy 2 m lower 8 m away (outside the 5 m radius); a
5 m `fill` source is under the native one (composite VRT, top source first), as in DWR-2801.
Expected (as the manual fix): 10 HIGH ends on 7 culverts, all > 1 ft: all flagged with the
default limit; accepting all (or a 10 ft limit) gives invfix2 exactly."""
import csv
import inspect
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test

EPSG = 5070
FT = 0.3048
HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "data", "culvert_prep_2801.json")


def _gdal():
    from osgeo import gdal, ogr, osr
    gdal.UseExceptions()
    ogr.UseExceptions()
    return gdal, ogr, osr


def _fx():
    with open(FIXTURE, encoding="utf-8") as f:
        return json.load(f)


def _srs():
    _g, _o, osr = _gdal()
    s = osr.SpatialReference()
    s.ImportFromEPSG(EPSG)
    return s


def _tif(path, x0, y0, n, cell, a):
    gdal, _o, _s = _gdal()
    ds = gdal.GetDriverByName("GTiff").Create(path, n, n, 1, gdal.GDT_Float32)
    ds.SetProjection(_srs().ExportToWkt())
    ds.SetGeoTransform((x0, cell, 0.0, y0, 0.0, -cell))
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(-9999.0)
    b.WriteArray(a.astype("float32"))
    ds = None


def _ends(fx):
    out = {}
    for f in fx["features"]:
        pts = f["wkt"][f["wkt"].index("(") + 1:f["wkt"].rindex(")")].split(",")
        a, b = [tuple(map(float, p.split())) for p in (pts[0], pts[-1])]
        out["{}:upstream".format(f["fid"])] = a
        out["{}:downstream".format(f["fid"])] = b
    return out


def _dem(d, fx, native_skip=(), fill=None):
    """Composite VRT [native1m (top), fill5m]; native_skip: end keys without a native tile;
    fill: {end key: ground on the 5 m fill}."""
    gdal, _o, _s = _gdal()
    tiles, ftiles = [], []
    for k, (x, y) in sorted(_ends(fx).items()):
        if k in native_skip:
            continue
        g = fx["ground_m"][k]
        n = 21
        x0, y0 = float(np.floor(x)) - 10.0, float(np.floor(y)) + 11.0
        a = np.full((n, n), g + 0.8)
        cx, cy = int(x - x0), int(y0 - y)            # the end's cell
        a[cy - 1, cx + 2] = g                         # 2.2-3.2 m away: inside 5 m
        a[cy, min(cx + 8, n - 1)] = g - 2.0           # 8 m away: outside the 5 m radius
        p = os.path.join(d, "n_{}.tif".format(k.replace(":", "_")))
        _tif(p, x0, y0, n, 1.0, a)
        tiles.append(p)
    for k, g in (fill or {}).items():
        x, y = _ends(fx)[k]
        p = os.path.join(d, "f_{}.tif".format(k.replace(":", "_")))
        _tif(p, float(np.floor(x)) - 25.0, float(np.floor(y)) + 25.0, 10, 5.0, np.full((10, 10), g))
        ftiles.append(p)
    nat = os.path.join(d, "native1m.vrt")
    gdal.BuildVRT(nat, tiles, srcNodata=-9999.0, VRTNodata=-9999.0)
    srcs = [nat]
    if ftiles:
        fl = os.path.join(d, "fill5m.vrt")
        gdal.BuildVRT(fl, ftiles, srcNodata=-9999.0, VRTNodata=-9999.0)
        srcs = [fl, nat]                              # a later source paints over: native on top
    comp = os.path.join(d, "composite_1m_native_over_5m.vrt")
    gdal.BuildVRT(comp, srcs, resolution="highest", srcNodata=-9999.0, VRTNodata=-9999.0)
    return comp


def _structures(path, fx, fids=None):
    _g, ogr, _s = _gdal()
    types = {"String": ogr.OFTString, "Real": ogr.OFTReal, "Integer": ogr.OFTInteger,
             "Integer64": ogr.OFTInteger64}
    ds = ogr.GetDriverByName("GPKG").CreateDataSource(path)
    lyr = ds.CreateLayer("runner_culverts", _srs(), ogr.wkbLineString)
    for n, t in fx["fields"]:
        lyr.CreateField(ogr.FieldDefn(n, types.get(t, ogr.OFTString)))
    for f in fx["features"]:
        if fids and f["fid"] not in fids:
            continue
        o = ogr.Feature(lyr.GetLayerDefn())
        o.SetFID(f["fid"])
        for k, v in f["attrs"].items():
            if v is not None:
                o.SetField(k, v)
        o.SetGeometry(ogr.CreateGeometryFromWkt(f["wkt"]))
        lyr.CreateFeature(o)
    info = ds.CreateLayer("runner_culverts_info", None, ogr.wkbNone)
    for n in ("key", "value"):
        info.CreateField(ogr.FieldDefn(n, ogr.OFTString))
    o = ogr.Feature(info.GetLayerDefn())
    o.SetField("key", "created_by")
    o.SetField("value", "ND Dam Hazard (runnerculverts.py) - test fixture")
    info.CreateFeature(o)
    ds = None


def _read(path):
    _g, ogr, _s = _gdal()
    ds = ogr.Open(path)
    lyr = ds.GetLayerByName("runner_culverts")
    out = {}
    for f in lyr:
        g = f.GetGeometryRef()
        out[f.GetFID()] = dict(up=f.GetField("invert_up"), dn=f.GetField("invert_dn"),
                               notes=f.GetField("notes") or "", n=g.GetPointCount(),
                               wkt=g.ExportToWkt(), line_len_ft=f.GetField("line_len_ft"))
    info = {}
    il = ds.GetLayerByName("runner_culverts_info")
    for f in il:
        info.setdefault(f.GetField("key"), []).append(f.GetField("value"))
    return out, info


def _setup(**k):
    fx = _fx()
    d = tempfile.mkdtemp(prefix="cprep_")
    st = os.path.join(d, "structures_T.gpkg")
    _structures(st, fx, k.pop("fids", None))
    dem = _dem(d, fx, **k)
    return fx, d, st, dem


def _ok():
    try:
        _gdal()
        return True
    except ImportError:
        return False


def _by_status(log):
    out = {}
    for it in log["items"]:
        out.setdefault(it["status"], set()).add(it["item_id"])
    return out


HIGH10 = {"254:invert_up", "254:invert_dn", "257:invert_dn", "263:invert_dn", "267:invert_up",
          "267:invert_dn", "277:invert_up", "280:invert_dn", "281:invert_up", "281:invert_dn"}


@test
def culvert_prep_default_flags_all_2801_changes_and_never_writes_the_input():
    if not _ok():
        return
    from nd_breach_runner.core import culvert_precheck as cp, culvert_prep as P
    fx, d, st, dem = _setup()
    try:
        before = cp.sha256(st)
        mtime = os.path.getmtime(st)
        out = os.path.join(d, "out", "structures_T_prep.gpkg")
        log = P.prepare(st, dem, out, dam="T", z_choice="meters")
        assert cp.sha256(st) == before and os.path.getmtime(st) == mtime
        assert log["input_sha256"] == before and log["output_sha256"] == cp.sha256(out)
        assert log["coverage"] == [], log["coverage"]       # a single-source VRT: no split
        s = _by_status(log)
        assert s.get(P.ST_FLAGGED) == HIGH10, s
        assert not s.get(P.ST_APPLIED) and not s.get(P.ST_ACCEPTED), s
        assert log["precheck"]["ends_high"] == 10 and log["precheck"]["culverts_high"] == 7
        # the proposals are the manual invfix / invfix2 values, to the 0.01 ft
        prop = {i["item_id"]: i["proposed_ft"] for i in log["items"] if i["rule"] == P.RULE_INVERT}
        for fid, (up, dn) in fx["invfix2"].items():
            for fld, v in (("invert_up", up), ("invert_dn", dn)):
                iid = "{}:{}".format(fid, fld)
                if iid in prop:
                    assert abs(prop[iid] - v) < 0.0051, (iid, prop[iid], v)
        # flagged items are left unchanged: the copy's features equal the input's
        a, _i = _read(st)
        b, info = _read(out)
        assert a == b, "flagged items must be left unchanged"
        assert "culvert_prep" in info and before in info["culvert_prep"][0]
        assert log["post_check"]["ends_high"] == 10
        # sha256 of input and output in every log
        for k in ("md", "csv", "json"):
            assert os.path.isfile(log["files"][k]), k
        md = open(log["files"]["md"], encoding="utf-8").read()
        assert before in md and log["output_sha256"] in md
        j = json.load(open(log["files"]["json"], encoding="utf-8"))
        assert j["input_sha256"] == before and j["output_sha256"] == log["output_sha256"]
        rows = list(csv.DictReader(open(log["files"]["csv"], encoding="utf-8")))
        assert {r["item_id"] for r in rows if r["status"] == "flagged"} == HIGH10
        assert all(r["accept"] == "" for r in rows)
        # never the input, never an existing file without overwrite
        for bad in (st, out):
            try:
                P.prepare(st, dem, bad, dam="T", z_choice="meters")
            except ValueError:
                pass
            else:
                raise AssertionError("must refuse " + bad)
        assert cp.sha256(st) == before
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def culvert_prep_accept_flow_reproduces_invfix2():
    if not _ok():
        return
    from nd_breach_runner.core import culvert_precheck as cp, culvert_prep as P
    fx, d, st, dem = _setup()
    try:
        before = cp.sha256(st)
        out1 = os.path.join(d, "prep1.gpkg")
        log1 = P.prepare(st, dem, out1, dam="T", z_choice="meters", post_check=False)
        # the engineer accepts everything but 281's downstream end, and one item twice-checked
        rows = list(csv.DictReader(open(log1["files"]["csv"], encoding="utf-8")))
        for r in rows:
            if r["status"] == "flagged" and r["item_id"] != "281:invert_dn":
                r["accept"] = "yes"
        acc = os.path.join(d, "accept.csv")
        with open(acc, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        out2 = os.path.join(d, "prep2.gpkg")
        log2 = P.prepare(st, dem, out2, dam="T", z_choice="meters", accept_path=acc,
                         log_dir=os.path.join(d, "log2"))
        assert cp.sha256(st) == before
        s = _by_status(log2)
        assert s[P.ST_ACCEPTED] == HIGH10 - {"281:invert_dn"} and s[P.ST_FLAGGED] == {"281:invert_dn"}
        b, _i = _read(out2)
        a, _i = _read(st)
        for fid, (up, dn) in fx["invfix2"].items():
            fid = int(fid)
            want_dn = a[fid]["dn"] if fid == 281 else dn
            assert abs(b[fid]["up"] - up) < 1e-6 and abs(b[fid]["dn"] - want_dn) < 1e-6, \
                (fid, b[fid], up, dn)
            changed = (up, dn) != (a[fid]["up"], a[fid]["dn"])
            if changed:
                assert b[fid]["notes"].startswith(a[fid]["notes"]) and "PREP " in b[fid]["notes"]
                assert "engineer-accepted" in b[fid]["notes"] and "NOT surveyed" in b[fid]["notes"]
            else:
                assert b[fid] == a[fid], fid
        assert log2["post_check"]["ends_high"] == 1          # 281 downstream, not accepted
        assert log2["accept_file_sha256"] == cp.sha256(acc)
        # a text list of ids works too; an id that is not proposed is reported, not applied
        txt = os.path.join(d, "ids.txt")
        with open(txt, "w") as f:
            f.write("# Nick 2026-10-28\n281:invert_dn\n999:invert_up\n")
        log3 = P.prepare(st, dem, os.path.join(d, "prep3.gpkg"), dam="T", z_choice="meters",
                         accept_path=txt, apply_auto=False, post_check=False,
                         log_dir=os.path.join(d, "log3"))
        assert _by_status(log3)[P.ST_ACCEPTED] == {"281:invert_dn"}
        assert log3["accepted_ids_not_found"] == ["999:invert_up"]
        # a stale accept (the proposal changed since the review) is not applied
        for r in rows:
            if r["item_id"] == "254:invert_up":
                r["proposed_ft"] = "913.00"
        with open(acc, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        log4 = P.prepare(st, dem, os.path.join(d, "prep4.gpkg"), dam="T", z_choice="meters",
                         accept_path=acc, post_check=False, log_dir=os.path.join(d, "log4"))
        assert _by_status(log4)[P.ST_STALE] == {"254:invert_up"}
        b4, _i = _read(os.path.join(d, "prep4.gpkg"))
        assert b4[254]["up"] == a[254]["up"] and abs(b4[254]["dn"] - 912.18) < 1e-6
        assert cp.sha256(st) == before
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def culvert_prep_limit_auto_applies_and_geometry_is_only_proposed():
    if not _ok():
        return
    from nd_breach_runner.core import culvert_precheck as cp, culvert_prep as P
    fx, d, st, dem = _setup()
    try:
        before = cp.sha256(st)
        out = os.path.join(d, "prep10.gpkg")
        log = P.prepare(st, dem, out, dam="T", z_choice="meters", limit_ft=10.0,
                        simplify_ratio=1.02, post_check=False)
        s = _by_status(log)
        assert s[P.ST_APPLIED] == HIGH10, s
        # 253: 71.2 m line, 68.9 m chord; 254's real 6-vertex line is also > 1.02 x its chord
        assert "253:geometry" in s[P.ST_FLAGGED], s
        assert all(i.endswith(":geometry") for i in s[P.ST_FLAGGED]), s
        g = [i for i in log["items"] if i["item_id"] == "253:geometry"][0]
        assert abs(g["proposed_ft"] - 225.9) < 0.05 and abs(g["old_m"] - 71.2) < 0.01, g
        b, _i = _read(out)
        a, _i = _read(st)
        for fid, (up, dn) in fx["invfix2"].items():
            assert abs(b[int(fid)]["up"] - up) < 1e-6 and abs(b[int(fid)]["dn"] - dn) < 1e-6
        assert b[253] == a[253] and b[253]["n"] == 10, "geometry is never changed unaccepted"
        assert "engineer-accepted" not in b[254]["notes"] and "PREP " in b[254]["notes"]
        # accepted: the 2-vertex chord between the same ends, line_len_ft = the chord
        txt = os.path.join(d, "ids.txt")
        with open(txt, "w") as f:
            f.write("253:geometry\n")
        out2 = os.path.join(d, "prep_geom.gpkg")
        P.prepare(st, dem, out2, dam="T", z_choice="meters", simplify_ratio=1.02,
                  accept_path=txt, apply_auto=False, post_check=False,
                  log_dir=os.path.join(d, "lg"))
        b2, _i = _read(out2)
        assert b2[253]["n"] == 2 and abs(b2[253]["line_len_ft"] - 225.9) < 0.05, b2[253]
        assert b2[253]["up"] == a[253]["up"] and "straightened" in b2[253]["notes"]
        assert all(b2[f] == a[f] for f in a if f != 253), "apply_auto=False: accepted only"
        assert cp.sha256(st) == before
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def culvert_prep_flags_fill_ground_and_lists_fallback_ends():
    if not _ok():
        return
    from nd_breach_runner.core import culvert_prep as P
    # 257 downstream only on the 5 m fill (2 ft below its NDDOT invert: within a 10 ft limit
    # but not native); 263 upstream has no fine ground at all (FALLBACK)
    fx, d, st, dem = _setup(native_skip=("257:downstream", "263:upstream"),
                            fill={"257:downstream": 269.0})
    try:
        log = P.prepare(st, dem, os.path.join(d, "p.gpkg"), dam="T", z_choice="meters",
                        limit_ft=10.0, post_check=False)
        it = {i["item_id"]: i for i in log["items"]}
        assert len(log["coverage"]) == 2, log["coverage"]
        a = it["257:invert_dn"]
        assert a["status"] == P.ST_FLAGGED and a["on_top_source"] == "no", a
        assert "fill5m" in a["fine_source"] and "not the composite's top source" in a["reason"]
        f = it["263:invert_up"]
        assert f["status"] == P.ST_REVIEW and f["rule"] == P.RULE_FALLBACK, f
        assert f["proposed_ft"] is None
        b, _i = _read(os.path.join(d, "p.gpkg"))
        assert b[257]["dn"] == 884.95 and b[263]["up"] == 883.18
        assert abs(b[254]["up"] - 912.05) < 1e-6          # native, within 10 ft: applied
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def culvert_prep_cli_writes_a_copy():
    if not _ok():
        return
    from nd_breach_runner.core import culvert_precheck as cp, culvert_prep as P
    fx, d, st, dem = _setup(fids=[254, 263])
    try:
        before = cp.sha256(st)
        out = os.path.join(d, "c.gpkg")
        rc = P.main(["--structures", st, "--fine-dem", dem, "--output", out, "--dam", "T",
                     "--dem-z", "meters", "--limit-ft", "5"])
        assert rc == 0 and os.path.isfile(out) and cp.sha256(st) == before
        assert os.path.isfile(os.path.join(d, "culvert_prep_T.md"))
        assert P.main(["--structures", st, "--fine-dem", dem, "--output", st]) == 2
        assert cp.sha256(st) == before
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def culvert_prep_processing_tool_copies_and_builds_through_hazard():
    if not _ok():
        return
    from nd_breach_runner.tests import fake_qgis as fq
    fq.install()
    import importlib
    import nd_breach_runner.algorithms as algs
    importlib.reload(algs)
    import nd_breach_runner.algorithms_tools as at
    importlib.reload(at)
    from nd_breach_runner.core import culvert_precheck as cp, handoff
    assert at.CulvertPrepAlgorithm in at.TOOL_ALGORITHMS
    a = at.CulvertPrepAlgorithm()
    a.initAlgorithm()
    assert a.name() == "culvertprep" and "ALG_NO_THREADING" in inspect.getsource(type(a).flags)
    assert a.parameterDefinition("LIMIT_FT").kwargs["defaultValue"] == 1.0
    assert a.parameterDefinition("APPLY_AUTO").kwargs["defaultValue"] is True
    assert a.parameterDefinition("SIMPLIFY_RATIO").kwargs["defaultValue"] == 0.0  # off
    assert a.parameterDefinition("OVERWRITE").kwargs["defaultValue"] is False
    fx, d, st, dem = _setup(fids=[254, 263])
    saved = (handoff.check_version, handoff._reg, handoff._run)
    try:
        before = cp.sha256(st)
        out = os.path.join(d, "copy.gpkg")
        ctx, fb = fq.QgsProcessingContext(), fq.QgsProcessingFeedback()
        r = a.processAlgorithm({"STRUCTURES": st, "DEM": dem, "DEM_Z": 3, "DAM_ID": "T",
                                "OUTPUT": out}, ctx, fb)
        assert cp.sha256(st) == before and os.path.isfile(out) and out in ctx.loaded
        assert r["LOG_MD"].endswith("culvert_prep_T.md") and before in r["RESULT"]
        assert any("need review" in e for e in fb.errors), fb.errors
        # no structures layer: built by ND Dam Hazard's runnerculverts first
        calls = []

        class _R:
            def __init__(self, alg):
                self.alg = alg

            def algorithmById(self, _id):
                return self.alg

        handoff.check_version = lambda _id: None
        handoff._reg = lambda: _R(None)
        b = at.CulvertPrepAlgorithm()
        b.initAlgorithm()
        p = {"DEM": dem, "DEM_Z": 3, "DAM_ID": "T2", "NBI": "nbi.shp",
             "OUTPUT": os.path.join(d, "c2.gpkg")}
        try:
            b.processAlgorithm(p, ctx, fb)
            raise AssertionError("no Hazard: must fail")
        except fq.QgsProcessingException as e:
            assert "not installed" in str(e)

        def run(alg, params, feedback):
            calls.append(params)
            shutil.copy(st, params["OUTPUT"])
            return {"OUTPUT": params["OUTPUT"]}
        handoff._reg, handoff._run = (lambda: _R(object())), run
        r = b.processAlgorithm(p, ctx, fb)
        assert calls[0]["NBI"] == "nbi.shp" and "NDDOT" not in calls[0]
        assert r["BUILT"] == os.path.join(d, "c2_built.gpkg") and os.path.isfile(r["CORRECTED"])
    finally:
        handoff.check_version, handoff._reg, handoff._run = saved
        shutil.rmtree(d, ignore_errors=True)
