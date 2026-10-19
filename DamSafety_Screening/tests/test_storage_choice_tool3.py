"""Tool 3's STORAGE_CHOICE (0.17.27) through the mock, on the self-test valley (core/selftest):
Max (the default), Normal and Larger of the two, each fallback when a value is missing or zero,
dams without a water body, and an older call without the parameter. The window sends the
choice to tool 3 only (core/workflow.defaults)."""
import csv
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
from _optional import need  # noqa: E402
need("scipy", "storage.estimate_storage uses scipy.ndimage")
from qgis.core import (QgsProcessingContext, QgsProcessingFeedback, QgsGeometry, QgsPointXY,  # noqa: E402
                       make_layer)
from qgis import processing  # noqa: E402
import nd_dam_screening as pkg  # noqa: E402
from nd_dam_screening.core import selftest as st, storage as S, workflow as wf  # noqa: E402

plug = pkg.classFactory(None); plug.initGui()
algs = {a.name(): a for a in plug.provider.algs}
for a in algs.values():
    a.initAlgorithm()
processing.REGISTRY.update(algs)
TYPES = {"string": "string", "double": "double", "int": "int"}


def mock_layers(specs):
    out = {}
    for name, (kind, fields, feats) in specs.items():
        rows = []
        for (gkind, coords), attrs in feats:
            if gkind == "point":
                g = QgsGeometry.fromPointXY(QgsPointXY(*coords))
            elif gkind == "line":
                g = QgsGeometry.fromPolylineXY([QgsPointXY(*p) for p in coords])
            else:
                g = QgsGeometry.fromPolygonXY([[QgsPointXY(*p) for p in coords]])
            rows.append((g, attrs))
        out[name] = make_layer([(f, TYPES[t]) for f, t in fields], rows, kind, name)
    return out


# ---- 1. the parameter: the window's labels and values, Max by default, tool 3 only
p3 = algs["estimatestorage"]._params["STORAGE_CHOICE"]
assert list(p3.options) == ["Max storage", "Normal storage", "Larger of the two"] == list(S.STORAGE_CHOICES)
assert p3.defaultValue == 0
assert "STORAGE_CHOICE" not in algs["preparedams"]._params
W0 = tempfile.mkdtemp(prefix="sc3_w_")
for c in (0, 1, 2):
    assert wf.defaults("3", W0, {"storage_choice": c})["STORAGE_CHOICE"] == c
    assert "STORAGE_CHOICE" not in wf.defaults("1", W0, {"storage_choice": c})
assert wf.defaults("3", W0, {})["STORAGE_CHOICE"] == 0
shutil.rmtree(W0)

# ---- 2. resolve_storage_choice: every choice and every fallback
N, M = 1000.0, 5000.0
R = S.resolve_storage_choice
cases = [  # (normal, maximum, choice) -> (volume, kind, fallback words or "")
    ((N, M, 0), (M, "maximum", "")),
    ((N, M, 1), (N, "normal", "")),
    ((N, M, 2), (M, "maximum", "")),
    ((M * 2, M, 2), (M * 2, "normal", "")),
    ((N, 0.0, 0), (N, "normal", "maximum storage missing or zero, so normal storage was used")),
    ((N, None, 0), (N, "normal", "maximum storage missing or zero")),
    ((0.0, M, 1), (M, "maximum", "normal storage missing or zero, so maximum storage was used")),
    (("", M, 1), (M, "maximum", "normal storage missing or zero")),
    ((None, M, 2), (M, "maximum", "normal storage missing or zero, so the larger of the two is the maximum")),
    ((N, 0.5, 2), (N, "normal", "maximum storage missing or zero")),
    ((0.0, 0.0, 0), (None, "none", "DEM estimate is kept")),
    ((None, None, 1), (None, "none", "DEM estimate is kept")),
    (("x", float("nan"), 2), (None, "none", "DEM estimate is kept")),
    ((N, M, 7), (M, "maximum", "unknown storage choice 7, used Max storage")),
]
for (n, m, c), (vol, kind, fb_) in cases:
    r = R(n, m, c)
    assert r["volume_m3"] == vol and r["kind"] == kind, ((n, m, c), r)
    assert (fb_ in r["fallback"]) if fb_ else r["fallback"] == "", ((n, m, c), r)
    assert r["text"].startswith("storage choice " + S.STORAGE_CHOICES[r["choice"]]), r
    assert (fb_ in r["text"]) if fb_ else "(" not in r["text"], r
# the older helper is unchanged
assert S.storage_choice_volume(N, M, 0) == (M, "maximum storage")
assert S.storage_choice_volume(N, M, 1) == (N, "normal storage")

# ---- 3. tool 3 end to end on the self-test valley
work = tempfile.mkdtemp(prefix="sc3_")
layers = mock_layers(st.layer_specs())
dem_folder, _p = st.write_dem(os.path.join(work, "dem"))
ctx, fb = QgsProcessingContext(), QgsProcessingFeedback()
prev = {}
for step, alg, mk, check in st.steps(work, layers, dem_folder)[:2]:          # tools 1 and 2
    prev[step] = processing.run("nddam:" + alg, mk(prev), ctx, fb)
    assert not check(prev[step]), (step, check(prev[step]))
dams1 = prev["1"]["OUT"]
src = ctx.layers.get(dams1, dams1)
fields = [(f.name(), f._t) for f in src.fields()]
base = [(f.geometry(), {k: f[k] for k, _ in fields}) for f in src.getFeatures()]
assert len(base) == 1
DAM = base[0][1]["dam_id"]
no_wb = make_layer([("name", "string")], [], "polygon", "no_waterbodies")


def tool3(normal, maximum, choice=None, wb=True, tag="x"):
    """Run tool 3 on the dam with these inventory values; returns (curve top storage m3, the
    output row, the warnings, the log lines of this run)."""
    rows = [(g, dict(a, inv_norm_m3=normal, inv_max_m3=maximum)) for g, a in base]
    dams = ctx.add(make_layer(fields, rows, "point", "dams_" + tag))
    curves = os.path.join(work, "curves_" + tag)
    params = {"DAMS": dams, "WB": layers["waterbodies"] if wb else no_wb,
              "DEMDIR": os.path.join(work, "2_terrain"), "SEARCH_FT": 492.126, "SHAPE": 1.0,
              "CURVES": curves, "OUT": "TEMPORARY_OUTPUT"}
    if choice is not None:
        params["STORAGE_CHOICE"] = choice
    f3 = QgsProcessingFeedback()
    res = processing.run("nddam:estimatestorage", params, ctx, f3)
    out = ctx.layers.get(res["OUT"], res["OUT"])
    row = [{k.name(): f[k.name()] for k in out.fields()} for f in out.getFeatures()][0]
    top = None
    cp = os.path.join(curves, "curve_{}.csv".format(DAM))
    if os.path.isfile(cp):
        with open(cp, encoding="utf-8") as fh:
            top = float(list(csv.DictReader(fh))[-1]["storage_m3"])
    return top, row, f3.warnings, f3.log


# the DEM's own storage at the top of dam with the inventory normal storage below the pool
NORM = float(base[0][1]["inv_norm_m3"] or 0) or 50000.0
dem_top, row, warn, log = tool3(NORM, None, 1, tag="dem")
assert dem_top and dem_top > NORM, (dem_top, row["stor_notes"])
BIG = dem_top * 5.0                                    # an inventory maximum well above the DEM

# Max (the default, also without the parameter: a model from before 0.17.27)
for choice, tag in ((0, "max"), (None, "absent")):
    top, row, warn, log = tool3(NORM, BIG, choice, tag=tag)
    assert abs(top - BIG) < 1.0, (top, BIG, row["stor_notes"])
    assert row["stor_notes"].startswith("storage choice Max storage: maximum storage"), row["stor_notes"]
    assert "raised" in row["stor_notes"] and not warn, (row["stor_notes"], warn)
    assert any(m == "Storage to use: Max storage" for m in log), log
    assert abs(row["cal_total_m3"] - BIG) < 1.0
# Normal: the DEM above the normal pool, not raised
top, row, warn, log = tool3(NORM, BIG, 1, tag="normal")
assert abs(top - dem_top) < 1.0 and top < BIG, (top, dem_top)
assert row["stor_notes"].startswith("storage choice Normal storage: normal storage") and not warn
assert "raised" not in row["stor_notes"]
# Larger of the two: the maximum here; the normal when that is the larger (no raise)
top, row, warn, _ = tool3(NORM, BIG, 2, tag="larger")
assert abs(top - BIG) < 1.0 and "Larger of the two: maximum storage" in row["stor_notes"] and not warn
top, row, warn, _ = tool3(NORM, NORM / 2.0, 2, tag="larger_n")
assert abs(top - dem_top) < 1.0 and "Larger of the two: normal storage" in row["stor_notes"]
# fallbacks: logged as a warning and written first in stor_notes
top, row, warn, _ = tool3(0.0, BIG, 1, tag="normal_missing")                  # Normal -> Max
assert abs(top - BIG) < 1.0, (top, row["stor_notes"])
assert "normal storage missing or zero, so maximum storage was used" in row["stor_notes"]
assert any("normal storage missing or zero" in w for w in warn), warn
top, row, warn, _ = tool3(NORM, 0.0, 0, tag="max_missing")                    # Max -> Normal
assert abs(top - dem_top) < 1.0 and "maximum storage missing or zero, so normal storage was used" in row["stor_notes"]
assert any("maximum storage missing" in w for w in warn), warn
top, row, warn, _ = tool3(NORM, None, 2, tag="larger_missing")
assert abs(top - dem_top) < 1.0 and "maximum storage missing or zero" in row["stor_notes"] and warn
top, row, warn, _ = tool3(None, None, 0, tag="none")                          # neither: DEM estimate
assert top and "no inventory storage" in row["stor_notes"] and "DEM estimate is kept" in row["stor_notes"]
assert row["stor_method"] == "estimated" and warn

# ---- 4. no water body: the full-height inventory curve uses the chosen value
for (n, m, c), want in (((NORM, BIG, 0), BIG), ((NORM, BIG, 1), NORM), ((NORM, BIG, 2), BIG),
                        ((0.0, BIG, 1), BIG), ((NORM, 0.0, 0), NORM)):
    top, row, warn, _ = tool3(n, m, c, wb=False, tag="dry_{}_{}_{}".format(int(bool(n)), int(bool(m)), c))
    assert top is not None and abs(top - want) < 1.0, ((n, m, c), top, want, row["stor_notes"])
    # stor_method is "inventory" when the record has a normal storage (as before 0.17.27)
    assert row["stor_method"] == ("inventory" if n else "estimated"), row["stor_method"]
    assert "dry-dam inventory curve" in row["stor_notes"]
    assert row["stor_notes"].count("missing or zero") <= 1, row["stor_notes"]
    assert row["stor_notes"].startswith("storage choice " + S.STORAGE_CHOICES[c])
    if (n and m):
        assert "missing" not in row["stor_notes"], row["stor_notes"]
    else:
        assert "missing or zero" in row["stor_notes"] and warn, row["stor_notes"]
top, row, warn, _ = tool3(None, None, 1, wb=False, tag="dry_none")             # nothing to use
assert top is None and row["stor_method"] == "none" and "no waterbody" in row["stor_notes"]

shutil.rmtree(work)
print("OK (tool 3 storage choice)")
