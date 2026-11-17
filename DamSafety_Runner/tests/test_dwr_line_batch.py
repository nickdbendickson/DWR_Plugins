"""3.20.13: a batch dam's DWR dam line (core/dwr_lines.py) and the dam point check's centre snap.

Nick, 2026-11-17: "dam point should be at the center of the embankment in most all cases"; he
chose "DWR line centre, else found centre". The batch tools (QGIS batch, ndbreach_cli.py batch,
and the chain through the QGIS batch) take each dam's DWR dam line, matched by its DWR structure
id, from the DAM_LINES layer or the offline pack (through the pack's manifest), use it as the
dam line and put the point at its centre. Dams with no line: the dam point check moves by
default and moves a point that is on the embankment but off its centre along to the centre.

The packs here are built in a temporary folder (manifest.json + GeoPackages written with
damsuite.suitegpkg). The DWR line file is deliberately not called DWR_dam_lines.gpkg: the pack
is read through its manifest, never a hard-coded path."""
import contextlib
import csv
import json
import os
import shutil
import sys
import tempfile

import numpy as np

from nd_breach_runner.core import dam_point_check as dpc
from nd_breach_runner.core import dwr_lines as DL
from nd_breach_runner.core import workflow
from nd_breach_runner.tests.registry import test

SRS = 5070            # a projected CRS the writer knows: the fake QGIS transforms are identity
# dam 2645: a 300 m line through (0, 0) along y (its centre at (0, 40)); dam 2700: a line 600 m
# away (refused: farther than NEAR_M from its point); dam 2800: a degenerate 4 m line
LINE_2645 = [(0.0, -110.0), (0.0, 0.0), (0.0, 190.0)]
LINE_FAR = [(600.0, -100.0), (600.0, 100.0)]
LINE_SHORT = [(0.0, 0.0), (0.0, 4.0)]
LINE_OTHER = [(30.0, -100.0), (30.0, 100.0)]      # another dam's line, 30 m east


@contextlib.contextmanager
def _env(**kw):
    """Set (value) or remove (None) environment variables for the block."""
    old = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _write_gpkg(path, table, gtype, fields, rows, srs=SRS):
    from nd_breach_runner.damsuite import suitegpkg as G
    os.makedirs(os.path.dirname(path), exist_ok=True)
    w = G.GpkgWriter(path)
    lyr = w.layer(table, gtype, fields, srs)
    for geom, attrs in rows:
        lyr.add(geom, attrs)
    w.close()


def _pack(root, lines=None, dams=None, srs=SRS):
    """A format-2 pack folder: manifest.json naming dwr/lines_2026.gpkg as DWR_dam_lines (not
    the usual file name) and dwr/dams.gpkg as DWR_dams."""
    pack = os.path.join(root, "ND_Dam_Suite_Pack")
    lines = lines if lines is not None else [
        (2645, LINE_2645), (2700, LINE_FAR), (2800, LINE_SHORT), (2900, LINE_OTHER)]
    rows = [({"type": "MultiLineString", "coordinates": [ln]}, {"structure_id": sid,
                                                              "feature_id": k})
            for k, (sid, ln) in enumerate(lines)]
    _write_gpkg(os.path.join(pack, "dwr", "lines_2026.gpkg"), "DWR_dam_lines",
                "MULTILINESTRING", [("feature_id", "int"), ("structure_id", "int")], rows, srs)
    dams = dams if dams is not None else [(2645, "ND01234"), (2700, "ND05678")]
    _write_gpkg(os.path.join(pack, "dwr", "dams.gpkg"), "DWR_dams", "POINT",
                [("structure_id", "int"), ("national_id", "text")],
                [({"type": "Point", "coordinates": (0.0, 0.0)},
                  {"structure_id": s, "national_id": n}) for s, n in dams], srs)
    m = {"format": "nd-dam-hazard-offline-pack", "format_version": 2, "layout": "gpkg",
         "datasets": {"DWR_dam_lines": {"file": "dwr/lines_2026.gpkg", "count": len(lines),
                                        "geometry": "MULTILINESTRING", "status": "complete"},
                      "DWR_dams": {"file": "dwr/dams.gpkg", "count": len(dams),
                                   "geometry": "POINT", "status": "complete"}}}
    with open(os.path.join(pack, "manifest.json"), "w") as fh:
        json.dump(m, fh)
    return pack


@contextlib.contextmanager
def _active_pack(pack, data=None):
    """The suite reads `pack` (NDSUITE_PACK) and DWR lines are on; no drop-in folder."""
    d = data or tempfile.mkdtemp(prefix="ndbr_dl_data_")
    try:
        with _env(NDSUITE_PACK=pack, NDSUITE_SOURCE="pack", NDSUITE_DATA=d,
                  ND_DAM_HAZARD_OFFLINE_PACK=None, NDBREACH_DWR_LINES=None):
            yield
    finally:
        if data is None:
            shutil.rmtree(d, ignore_errors=True)


@contextlib.contextmanager
def _no_pack():
    d = tempfile.mkdtemp(prefix="ndbr_dl_data_")
    try:
        with _env(NDSUITE_PACK=None, NDSUITE_SOURCE="download", NDSUITE_DATA=d,
                  ND_DAM_HAZARD_OFFLINE_PACK=None, NDBREACH_DWR_LINES=None):
            yield
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------------- the module -------
@test
def dwr_lines_structure_ids_switch_and_guard():
    assert DL.norm_id(2645) == DL.norm_id("2645") == DL.norm_id(2645.0) == "2645"
    assert DL.norm_id("2645.0") == DL.norm_id(" 02645 ") == "2645"
    assert DL.norm_id(None) == DL.norm_id("") == DL.norm_id("NULL") == ""
    assert DL.dwr_number("DWR-2645") == DL.dwr_number("dwr_02645") == "2645"
    assert DL.dwr_number("ND01234") == ""
    # the order: a chosen field, a structure id field, the DWR-NNNN dam ID, dam.json
    ids = DL.structure_ids("DWR-2645", {"STRUCTURE_ID": "2646"}, {"dwr_structure_id": 2647},
                           field="sid_x")
    assert [s for s, _h in ids] == ["2646", "2645", "2647"], ids
    ids = DL.structure_ids("DWR-2645", {"sid_x": "9", "structure_id": "2645"}, None,
                           field="sid_x")
    assert [s for s, _h in ids] == ["9", "2645"], ids
    # Nick's kit: dam_id DWR-2645 and structure_id '2645' (text): one id
    assert [s for s, _h in DL.structure_ids("DWR-2645", {"dam_id": "DWR-2645",
                                                          "structure_id": "2645"})] == ["2645"]
    # a NID only: looked up in the DWR dams
    look = {"ND01234": "2645"}.get
    ids = DL.structure_ids("ND01234", {"NIDID": "ND01234"}, None,
                           nid_lookup=lambda n: look(n, ""))
    assert ids and ids[0][0] == "2645" and "NID ND01234" in ids[0][1], ids
    assert DL.structure_ids("ND09999", {}, None, nid_lookup=lambda n: "") == []
    # the per-dam switch
    for v, want in ((0, False), ("0", False), ("no", False), ("False", False), (1, True),
                    ("yes", True), ("", None), (None, None)):
        assert DL.row_switch({"Use_DWR_Line": v}) is want, (v, want)
    assert DL.row_switch({"use_dwr_li": "0"}) is False and DL.row_switch({}) is None
    assert DL.env_off({DL.ENV: "off"}) and DL.env_off({DL.ENV: "0"})
    assert not DL.env_off({}) and not DL.env_off({DL.ENV: "on"})
    # the guard: the nearest point within NEAR_M and the centre within CENTRE_M
    line, info = DL.choose([LINE_2645], (0.0, 0.0))
    assert line == LINE_2645 and info["length_m"] == 300.0 and info["centre_m"] == 40.0, info
    assert DL.midpoint(LINE_2645) == (0.0, 40.0)
    line, info = DL.choose([LINE_FAR], (0.0, 0.0))
    assert line is None and "600 m from the inventory point" in info["refused"], info
    # DWR-2899: the correct line passes 426 m from the point, its centre 569 m away: used
    l2899 = [(426.0, 0.0), (712.0, 0.0)]
    line, info = DL.choose([l2899], (0.0, 0.0))
    assert line is not None and info["nearest_m"] < DL.NEAR_M < info["centre_m"], info
    line, info = DL.choose([[(400.0, 0.0), (2000.0, 0.0)]], (0.0, 0.0))
    assert line is None and "centre is 1200 m" in info["refused"], info
    for bad in ([(0.0, 0.0), (0.0, 0.0)], LINE_SHORT):
        line, info = DL.choose([bad], (0.0, 0.0))
        assert line is None and "degenerate" in info["refused"], (bad, info)
    # pieces of one line are joined; of two lines the one nearest the point (DWR-2665)
    a, b = [(0.0, -100.0), (0.0, 0.0)], [(0.0, 50.0), (0.0, 0.5)]
    line, info = DL.choose([a, b, [(5.0, 0.0), (5.0, 20.0)]], (0.0, 0.0))
    assert len(line) == 3 and DL.length(line) > 149.0 and info["parts"] == 2, (line, info)
    near, far = [(15.0, -180.0), (15.0, 180.0)], [(258.0, -260.0), (258.0, 270.0)]
    line2, info2 = DL.choose([far, near], (0.0, 0.0))
    assert line2 == near and info2["nearest_m"] == 15.0, info2
    t = DL.describe(dict(info, source="the pack", structure_id="2645"))
    assert t.startswith("DWR dam line from the pack, structure 2645 (150 m long"), t


@test
def dwr_lines_pack_found_through_its_manifest():
    """The pack's DWR_dam_lines and DWR_dams through manifest.json (the file names are the
    manifest's), the active pack first, else ND_DAM_HAZARD_OFFLINE_PACK; none: the old way."""
    root = tempfile.mkdtemp(prefix="ndbr_dl_")
    try:
        pack = _pack(root)
        with _active_pack(pack):
            path, label = DL.pack_source()
            assert path == os.path.join(pack, "dwr", "lines_2026.gpkg") and label == "the pack"
            idx = DL.line_index(path, label)
            assert idx.count == 4 and idx.crs == "EPSG:{}".format(SRS) and idx.has("2645")
            assert idx.parts("2645") == [LINE_2645]
            assert DL.line_index(path, label) is idx                      # cached
            look = DL.nid_lookup_for(DL.dams_source())
            assert look("nd01234") == "2645" and look("ND00000") == ""
        # only the Hazard plugin's pack variable (the batch kit's): found through its manifest
        with _no_pack(), _env(ND_DAM_HAZARD_OFFLINE_PACK=pack):
            assert DL.pack_source()[0] == os.path.join(pack, "dwr", "lines_2026.gpkg")
        with _no_pack():
            path, why = DL.pack_source()
            assert path == "" and "no DWR dam lines" in why, why
        # lon/lat lines labelled EPSG:4326 are taken as NAD83 (holdout.py)
        p2 = _pack(os.path.join(root, "ll"), lines=[(1, [(-98.6, 46.0), (-98.59, 46.0)])],
                   srs=4326)
        assert DL.LineIndex(os.path.join(p2, "dwr", "lines_2026.gpkg")).crs == DL.LINE_CRS
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------------------- the QGIS batch ---
def _batch(feats, extra=None):
    """The QGIS batch tool (GIS side stubbed, run_dam faked) on `feats`; returns (calls, rows,
    feedback info). calls: one dict per dam run_dam was given."""
    from nd_breach_runner.tests.preflight_stub import without_dem_and_smoke_checks
    from nd_breach_runner.tests.run_tests import _patched_algs
    from nd_breach_runner.tests.test_v320 import _FakeRaster
    from nd_breach_runner.tests.test_cli import _FakeResult
    algs, fq = _patched_algs()
    calls = []

    def fake_run_dam(dam, xy, st, *a, **k):
        info = getattr(dam, "_dam_line_info", None)
        calls.append({"dam": dam.dam_id, "xy": (float(xy[0]), float(xy[1])),
                      "line": getattr(dam, "crest_line_xy", None),
                      "orig": getattr(dam, "_point_original", None),
                      "source": getattr(dam, "_crest_line_source", None),
                      "info": info, "midcrest_line": st.midcrest_line,
                      "crest_refused": getattr(dam, "_crest_line_refused", None)})
        r = _FakeResult(dam.dam_id)
        r.summary["dam_line"] = DL.batch_text(info) if info else ""
        r.modelled_dam_xy = None
        return r
    keep = workflow.run_dam
    workflow.run_dam = fake_run_dam
    d = tempfile.mkdtemp(prefix="ndbr_dl_b_")
    fb = fq.QgsProcessingFeedback()
    try:
        @without_dem_and_smoke_checks
        def go():
            b = algs.BatchAlgorithm()
            b.initAlgorithm()
            params = {"DEM": _FakeRaster(), "CELL": 20.0, "LENGTH_MI": 1.0,
                      "HALF_WIDTH_MI": 0.5, "MAX_RUNS": 1, "LISFLOOD_EXE": sys.executable,
                      "DAMS_FOLDER": d, "DAMS": fq.FakeSource(feats), "F_ID": "ID",
                      "F_HEIGHT": "H", "F_STORAGE": "S", "F_TOP": "TOP", "SKIP_DONE": False}
            params.update(extra or {})
            return b.processAlgorithm(params, fq.QgsProcessingContext(), fb)
        r = go()
        with open(r["SUMMARY"], newline="", encoding="utf-8") as fh:
            rd = csv.DictReader(fh)
            assert tuple(rd.fieldnames) == algs.BATCH_FIELDS
            rows = {row["Dam"]: row for row in rd}
    finally:
        workflow.run_dam = keep
        shutil.rmtree(d, ignore_errors=True)
    return calls, rows, list(fb.info)


def _feats(fq, specs):
    """FakeFeatures at (x, y) with the given attributes."""
    out = []
    for (x, y), attrs in specs:
        out.append(fq.FakeFeature(fq.FakeGeometry("point", [[(x, y)]]),
                                  **dict(dict(H=25.0, S=500.0, TOP=1850.0), **attrs)))
    return out


@test
def dwr_line_batch_uses_the_pack_line_by_structure_id():
    from nd_breach_runner.tests import fake_qgis as fq
    root = tempfile.mkdtemp(prefix="ndbr_dl_")
    try:
        pack = _pack(root)
        specs = [((0.0, 0.0), {"ID": "DWR-2645"}),                         # by its dam ID
                 ((0.0, 0.0), {"ID": "Pheasant", "structure_id": "2645"}),  # by structure_id
                 ((0.0, 0.0), {"ID": "N1", "NIDID": "ND01234"}),            # NID -> DWR dams
                 ((0.0, 0.0), {"ID": "DWR-2700"}),                         # line too far
                 ((0.0, 0.0), {"ID": "DWR-2800"}),                         # degenerate
                 ((0.0, 0.0), {"ID": "DWR-5555"}),                         # no line
                 ((0.0, 0.0), {"ID": "DWR-2645B", "structure_id": 2645, "Use_DWR_Line": 0})]
        with _active_pack(pack):
            calls, rows, info = _batch(_feats(fq, specs))
        by = {c["dam"]: c for c in calls}
        for did in ("DWR-2645", "Pheasant", "N1"):
            c = by[did]
            assert c["line"] == LINE_2645, (did, c)
            assert np.allclose(c["xy"], (0.0, 40.0)) and c["orig"] == (0.0, 0.0), c
            assert c["source"].startswith("DWR dam line from the pack, structure 2645"), c
            assert c["info"]["used"] and c["info"]["centre_m"] == 40.0, c
            assert rows[did]["Dam_Line"].startswith("DWR line (the pack, structure 2645)"), \
                rows[did]
        assert "NID ND01234" in by["N1"]["info"]["matched_by"], by["N1"]
        for did, why in (("DWR-2700", "600 m from the inventory point"),
                         ("DWR-2800", "degenerate")):
            c = by[did]
            assert c["line"] is None and c["xy"] == (0.0, 0.0), c
            assert c["info"]["used"] is False and why in c["info"]["refused"], c
            assert rows[did]["Dam_Line"].startswith("not used (the pack, {})".format(
                did[4:])), rows[did]
            assert any(did in m and "not used" in m for m in info), info
        assert by["DWR-5555"]["line"] is None and by["DWR-5555"]["xy"] == (0.0, 0.0)
        assert rows["DWR-5555"]["Dam_Line"] == "no DWR line for structure 5555"
        assert by["DWR-2645B"]["line"] is None
        assert rows["DWR-2645B"]["Dam_Line"] == "off (Use_DWR_Line = 0)"
        assert any("DWR dam lines: 4 lines from the pack" in m for m in info), info
        # switched off: the points as given
        with _active_pack(pack):
            calls, rows, info = _batch(_feats(fq, specs[:1]), {"USE_DWR_LINES": False})
        assert calls[0]["line"] is None and calls[0]["xy"] == (0.0, 0.0), calls
        assert rows["DWR-2645"]["Dam_Line"] == "off (USE_DWR_LINES)"
        with _active_pack(pack), _env(NDBREACH_DWR_LINES="0"):
            calls, rows, _i = _batch(_feats(fq, specs[:1]))
        assert calls[0]["line"] is None and rows["DWR-2645"]["Dam_Line"] == \
            "off (NDBREACH_DWR_LINES)", rows
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def dwr_line_batch_crest_line_and_dam_lines_layer():
    """A CREST_LINE wins over the DWR line (and is now matched by the dam's structure id in a
    batch: before, the nearest line); a DAM_LINES layer is used instead of the pack."""
    from nd_breach_runner.tests import fake_qgis as fq
    root = tempfile.mkdtemp(prefix="ndbr_dl_")
    try:
        pack = _pack(root)
        own = [(5.0, -80.0), (5.0, 80.0)]
        crest = fq.FakeSource([
            fq.FakeFeature(fq.FakeGeometry("line", [LINE_OTHER]), structure_id=2900),
            fq.FakeFeature(fq.FakeGeometry("line", [own]), structure_id=2645)])
        with _active_pack(pack):
            calls, rows, _i = _batch(_feats(fq, [((20.0, 0.0), {"ID": "DWR-2645"})]),
                                     {"CREST_LINE": crest})
        c = calls[0]
        assert c["line"] is None and c["midcrest_line"] == own, c   # its own, not the nearest
        assert rows["DWR-2645"]["Dam_Line"] == "a crest line (CREST_LINE) is given"
        # DAM_LINES: matched by structure id; no pack needed
        lay = fq.FakeSource([
            fq.FakeFeature(fq.FakeGeometry("line", [LINE_OTHER]), STRUCTURE_ID=2900),
            fq.FakeFeature(fq.FakeGeometry("line", [own]), STRUCTURE_ID=2645)])
        with _no_pack():
            calls, rows, info = _batch(_feats(fq, [((20.0, 0.0), {"ID": "DWR-2645"}),
                                                   ((20.0, 0.0), {"ID": "DWR-7777"})]),
                                       {"DAM_LINES": lay})
        by = {c["dam"]: c for c in calls}
        assert by["DWR-2645"]["line"] == own and np.allclose(by["DWR-2645"]["xy"], (5.0, 0.0))
        assert by["DWR-2645"]["source"].startswith(
            "DWR dam line from the DAM_LINES layer, structure 2645"), by["DWR-2645"]
        assert by["DWR-7777"]["line"] is None              # never a neighbour's line
        # a layer without a structure id field: the nearest line (within NEAR_M)
        lay2 = fq.FakeSource([fq.FakeFeature(fq.FakeGeometry("line", [own])),
                              fq.FakeFeature(fq.FakeGeometry("line", [LINE_FAR]))])
        with _no_pack():
            calls, rows, info = _batch(_feats(fq, [((20.0, 0.0), {"ID": "X1"})]),
                                       {"DAM_LINES": lay2})
        assert calls[0]["line"] == own, calls
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def dwr_line_batch_without_a_pack_is_as_before():
    from nd_breach_runner.tests import fake_qgis as fq
    with _no_pack():
        calls, rows, info = _batch(_feats(fq, [((0.0, 0.0), {"ID": "DWR-2645"})]))
    c = calls[0]
    assert c["line"] is None and c["xy"] == (0.0, 0.0) and c["orig"] is None, c
    assert rows["DWR-2645"]["Dam_Line"].startswith("no DWR dam lines"), rows
    assert any("DWR dam lines: none" in m for m in info), info


# ------------------------------------------------------------------------- the CLI ----------
def _cli_batch(d, table_rows, batch=None, layers=None, pre=None):
    from nd_breach_runner.tests import test_cli as tc
    c = tc.cli()
    p, cfg = tc.base_config(d)
    cfg["dem"]["crs"] = "EPSG:{}".format(SRS)
    cfg["dam"] = {"point": None}
    cfg["batch"] = dict({"crs": None}, **(batch or {}))
    if layers:
        cfg["layers"] = layers
    cfg["scenario"] = {"type": "sunny"}
    tc.dump(p, cfg)
    table = os.path.join(d, "dams.csv")
    heads = ["dam_id", "structure_id", "Use_DWR_Line", "Dam Height (ft)",
             "Max Storage (acre-feet)", "Top of Dam (ft)", "x", "y"]
    with open(table, "w") as f:
        f.write(",".join(heads) + "\n")
        for r in table_rows:
            f.write(",".join(str(r.get(h, "")) for h in heads) + "\n")
    exe = os.path.join(d, "lisflood")
    with open(exe, "w") as f:
        f.write("x")
    if pre is not None:
        pre(d)
    calls = []

    def fake_run_dam(dam, dam_xy, st, *a, **k):
        info = getattr(dam, "_dam_line_info", None)
        calls.append({"dam": dam.dam_id, "xy": tuple(map(float, dam_xy)),
                      "line": getattr(dam, "crest_line_xy", None), "info": info})
        r = tc._FakeResult(dam.dam_id)
        r.summary["dam_line"] = DL.batch_text(info) if info else ""
        return r
    keep = c.workflow.run_dam
    c.workflow.run_dam = fake_run_dam
    try:
        code, out, err = tc.run_cli("batch", table, p, "--lisflood", exe)
    finally:
        c.workflow.run_dam = keep
    with open(os.path.join(d, "out", "Batch_Breach_Runs.csv")) as fh:
        rd = csv.DictReader(fh)
        assert tuple(rd.fieldnames) == tuple(c.BATCH_FIELDS)
        rows = {r["Dam"]: r for r in rd}
    return c, calls, rows, out + err


@test
def dwr_line_cli_batch_matches_the_plugin():
    from nd_breach_runner.tests import test_cli as tc
    from nd_breach_runner.tests import fake_qgis as fq
    from nd_breach_runner.tests.run_tests import _patched_algs
    algs, _fq = _patched_algs()
    root = tempfile.mkdtemp(prefix="ndbr_dl_")
    try:
        pack = _pack(root)
        base = {"Dam Height (ft)": 25, "Max Storage (acre-feet)": 20, "Top of Dam (ft)": 1850,
                "x": 0, "y": 0}
        table = [dict(base, dam_id="DWR-2645"), dict(base, dam_id="P1", structure_id=2645),
                 dict(base, dam_id="DWR-2700"), dict(base, dam_id="P2", structure_id=2645,
                                                     Use_DWR_Line=0)]
        with tc.Tmp() as d, _active_pack(pack):
            c, calls, rows, out = _cli_batch(d, table)
        assert tuple(c.BATCH_FIELDS) == tuple(algs.BATCH_FIELDS)
        assert c.BATCH_FIELDS[-1] == "Dam_Line"
        by = {x["dam"]: x for x in calls}
        for did in ("DWR-2645", "P1"):
            assert by[did]["line"] == LINE_2645 and np.allclose(by[did]["xy"], (0.0, 40.0)), \
                by[did]
        assert by["DWR-2700"]["line"] is None and by["DWR-2700"]["xy"] == (0.0, 0.0)
        assert by["P2"]["line"] is None and rows["P2"]["Dam_Line"] == "off (Use_DWR_Line = 0)"
        # the same dams through the QGIS batch: the same Dam_Line text
        with _active_pack(pack):
            _c, prow, _i = _batch(_feats(fq, [((0.0, 0.0), {"ID": "DWR-2645"}),
                                              ((0.0, 0.0), {"ID": "DWR-2700"})]))
        for did in ("DWR-2645", "DWR-2700"):
            assert rows[did]["Dam_Line"] == prow[did]["Dam_Line"] != "", (rows[did], prow[did])
        # batch.use_dwr_lines = false; layers.dam_lines (GeoJSON) instead of the pack
        with tc.Tmp() as d, _active_pack(pack):
            c, calls, rows, out = _cli_batch(d, table[:1], batch={"use_dwr_lines": False})
        assert calls[0]["line"] is None and rows["DWR-2645"]["Dam_Line"] == \
            "off (batch.use_dwr_lines)"
        own = [(205.0, -80.0), (205.0, 80.0)]     # (x > 180: not taken for lon/lat)
        with tc.Tmp() as d, _no_pack():
            gj = os.path.join(d, "lines.geojson")
            tc.dump(gj, tc.fc([{"type": "Feature", "properties": {"structure_id": 2645},
                                "geometry": {"type": "LineString", "coordinates": own}}]))
            c, calls, rows, out = _cli_batch(d, [dict(table[0], x=200)],
                                             layers={"dam_lines": gj})
        assert calls and calls[0]["line"] == own, (rows, out)
        assert np.allclose(calls[0]["xy"], (205.0, 0.0)), calls
        assert rows["DWR-2645"]["Dam_Line"].startswith("DWR line (the DAM_LINES layer, "
                                                       "structure 2645)"), rows
        # no pack: as before
        with tc.Tmp() as d, _no_pack():
            c, calls, rows, out = _cli_batch(d, table[:1])
        assert calls[0]["line"] is None and calls[0]["xy"] == (0.0, 0.0), calls
        assert "DWR dam lines: none" in out, out
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ------------------------------------------------------------------------- the run ---------
def _run(dam_setup):
    """run_dam (LISFLOOD-FP stubbed) on the synthetic dam of the dam point check's tests, the
    dam prepared by dam_setup(dam); (report, md, warnings)."""
    from nd_breach_runner.tests import synthetic_dams as S
    from nd_breach_runner.tests.synthetic import npy_writer
    from nd_breach_runner.tests.test_dam_point_check import _dam, _st, _fake_models
    dpc.cache_clear()
    d = tempfile.mkdtemp(prefix="ndbr_dl_r_")
    try:
        dam = _dam()
        xy = dam_setup(dam)
        with _fake_models():
            res = workflow.run_dam(dam, xy, _st(qa_maps="off"), S.DamTerrain(S.enclosed),
                                   npy_writer, os.path.join(d, "T1"))
        with open(res.report_json) as f:
            rep = json.load(f)
        with open(res.report_md) as f:
            md = f.read()
        return rep, md, list(res.warnings)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def dwr_line_reaches_the_run_report_and_a_refused_line_warns():
    line = [(0.0, -100.0), (0.0, 100.0)]

    def used(dam):
        info = {"source": "the pack", "structure_id": "2645", "matched_by": "dam ID DWR-2645",
                "parts": 1, "length_m": 200.0, "nearest_m": 30.0, "centre_m": 30.0,
                "used": True}
        info["text"] = DL.describe(info)
        return DL.apply(dam, (-30.0, 0.0), line, info)
    rep, md, warnings = _run(used)
    assert rep["dam_line"]["used"] and rep["dam_line"]["structure_id"] == "2645", rep["dam_line"]
    assert rep["summary"]["dam_line"].startswith("DWR line (the pack, structure 2645), 200 m")
    assert "| Dam line | DWR dam line from the pack, structure 2645 (200 m long, centre 30 m " \
           "from the inventory point) |" in md, md[:3000]
    assert rep["dam_point_check"]["outcome"] == "skipped" and \
        rep["dam_point_check"]["skipped_because"] == "dam line given"
    assert rep["crest_line_xy"] and rep["dam_point"]["original_xy"] == [-30.0, 0.0]
    assert not any(w.startswith(dpc.MOVED_PREFIX) for w in warnings)

    def refused(dam):
        info = {"source": "the pack", "structure_id": "2700", "matched_by": "dam ID DWR-2700",
                "parts": 1, "length_m": 200.0, "nearest_m": 600.0, "centre_m": 600.0,
                "used": False, "refused": "the line is 600 m from the inventory point (more "
                                          "than 500 m)"}
        info["text"] = DL.describe(info)
        DL.note_only(dam, info)
        return (0.0, 0.0)
    rep, md, warnings = _run(refused)
    want = "DWR dam line from the pack, structure 2700, not used: the line is 600 m"
    assert any(w.startswith(want) for w in warnings), warnings
    assert rep["summary"]["dam_line"].startswith("not used (the pack, 2700)"), rep["summary"]
    assert "crest_line_xy" not in rep or not rep.get("crest_line_xy")


# ------------------------------------------------------------------------- the check -------
@test
def dam_point_check_default_move_and_centre_snap():
    """3.20.13: move is the default; a point on the embankment but off the found line's centre
    moves along to the centre (C1) in move mode only, when every gate and V pass; a point at
    the centre stays."""
    from nd_breach_runner.tests import synthetic_dams as S
    from nd_breach_runner.tests.test_dam_point_check import _survey, _check
    assert dpc.DEFAULT_MODE == "move" == workflow.RunSettings().dam_point_check
    sv, rp = _survey(S.enclosed, (0.0, 60.0))                 # on the crest, 60 m off centre
    d = dpc.decide(sv, rp, S.H, mode="move")
    assert d["outcome"] == "move" and d.get("centre_snap") and not d["gates_failed"], d
    assert [t[0] for t in d["triggers"]] == ["C1"] and d["centre_m"] > 55.0, d
    w = dpc.decide(sv, rp, S.H, mode="warn")
    assert w["outcome"] == "kept" and not w["warn"] and not w["would_move"], w
    assert "\"move\" would put it there" in w["kept_why"], w
    rec = _check(S.enclosed, (0.0, 60.0))
    assert rec["outcome"] == "moved" and rec["centre_snap"], rec
    assert abs(rec["_mid_xy"][1]) < 5.0 and abs(rec["_mid_xy"][0]) < 5.0, rec["_mid_xy"]
    assert dpc.batch_text(rec) == "moved {:.0f} m (to the embankment's centre)".format(
        rec["moved_m"])
    assert "the point given is on the embankment" in dpc.warning_text(rec)
    # at the centre (closer than CENTRE_MIN_M): kept
    sv0, rp0 = _survey(S.enclosed, (0.0, 4.0))
    d0 = dpc.decide(sv0, rp0, S.H, mode="move")
    assert d0["outcome"] == "kept" and d0["centre_m"] < dpc.CENTRE_MIN_M, d0
    # a gate that fails keeps the point (no REVIEW warning)
    dg = dpc.decide(sv, rp, S.H, mode="move", crest_length_m=20.0)        # G7d
    assert dg["outcome"] == "kept" and not dg["warn"] and "G7d" in {g[0] for g in
                                                                  dg["gates_failed"]}, dg
    assert "not moved there" in dg["kept_why"], dg


# ------------------------------------------------------------------------- review fixes ----
@test
def dwr_lines_damaged_file_falls_back_to_the_pack():
    """A zero-byte or junk DWR lines file (an interrupted copy) never stops a batch: it gives
    no lines, and the next source (the active pack, then ND_DAM_HAZARD_OFFLINE_PACK) is used."""
    from nd_breach_runner.tests import fake_qgis as fq
    root = tempfile.mkdtemp(prefix="ndbr_dl_")
    try:
        pack = _pack(root)
        empty = os.path.join(root, "empty_lines.gpkg")
        open(empty, "wb").close()
        junk = os.path.join(root, "junk_lines.gpkg")
        with open(junk, "wb") as fh:
            fh.write(b"not a database" * 100)
        for bad in (empty, junk):
            idx = DL.line_index(bad)
            assert idx is not None and idx.count == 0 and idx.error, (bad, idx.error)
        assert DL.line_index(os.path.join(root, "missing.gpkg")) is None
        # the engineer's dwr/ file is damaged: the active pack is used
        data = os.path.join(root, "data")
        os.makedirs(os.path.join(data, "dwr"))
        open(os.path.join(data, "dwr", "DWR_dam_lines.gpkg"), "wb").close()
        with _active_pack(pack, data):
            path, label = DL.pack_source()
            assert path == os.path.join(pack, "dwr", "lines_2026.gpkg"), (path, label)
            idx, p2, lab = DL.open_lines()
            assert idx is not None and idx.count == 4 and p2 == path, (p2, lab)
            calls, rows, info = _batch(_feats(fq, [((0.0, 0.0), {"ID": "DWR-2645"})]))
        assert calls[0]["line"] == LINE_2645, calls
        # only the damaged file and no pack: no lines, the batch runs as before
        with _no_pack(), _env(NDSUITE_DATA=data):
            path, why = DL.pack_source()
            assert path == "" and "gave no lines" in why, why
            assert DL.open_lines()[0] is None
            calls, rows, info = _batch(_feats(fq, [((0.0, 0.0), {"ID": "DWR-2645"})]))
        assert calls[0]["line"] is None and rows["DWR-2645"]["Status"] != "failed", rows
        assert rows["DWR-2645"]["Dam_Line"].startswith("no DWR dam lines"), rows
        # the active pack's file damaged, ND_DAM_HAZARD_OFFLINE_PACK's good: that one
        bad_pack = _pack(os.path.join(root, "bad"))
        with open(os.path.join(bad_pack, "dwr", "lines_2026.gpkg"), "wb") as fh:
            fh.write(b"x")
        with _active_pack(bad_pack), _env(ND_DAM_HAZARD_OFFLINE_PACK=pack):
            assert DL.pack_source()[0] == os.path.join(pack, "dwr", "lines_2026.gpkg")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def dwr_lines_ids_from_a_dwr_field_and_the_next_id_after_a_refusal():
    ids = DL.structure_ids("Pheasant", {"dwr_id": "DWR-3034"})
    assert [s for s, _h in ids] == ["3034"], ids
    ids = DL.structure_ids("Pheasant", {"sid": "DWR 3034"}, field="sid")
    assert [s for s, _h in ids] == ["3034"], ids

    class Idx(object):
        label = "the pack"
        lines = {"2700": [LINE_FAR], "2645": [LINE_2645]}

        def has(self, sid):
            return sid in self.lines

        def parts(self, sid):
            return self.lines[sid]
    ident = lambda pts: [tuple(p) for p in pts]
    # a wrong structure_id field (2700, 600 m away) and the right DWR-2645 dam ID: 2645's line
    line, info = DL.find([("2700", "field structure_id"), ("2645", "dam ID DWR-2645")], Idx(),
                         ident, (0.0, 0.0))
    assert line == LINE_2645 and info["structure_id"] == "2645" and info["used"], info
    # only refused lines: the first refusal
    line, info = DL.find([("2700", "field structure_id")], Idx(), ident, (0.0, 0.0))
    assert line is None and info["structure_id"] == "2700" and "600 m" in info["refused"], info
    # the same in a DAM_LINES layer
    lay = [(np.asarray(LINE_FAR), {"sid": 2700}), (np.asarray(LINE_2645), {"sid": 2645})]
    line, info = DL.find_in_lines(lay, "sid", [("2700", "a"), ("2645", "b")], (0.0, 0.0))
    assert info["structure_id"] == "2645" and info["used"], info


@test
def dwr_line_batch_crest_line_matched_by_id_is_guarded():
    """3.20.13 review: a CREST_LINE matched by structure id far from the dam (DWR-2659's line is
    7.2 km from its point) is refused with a warning, never replaced by a neighbour's nearer
    line; the DWR line (the pack's) then places the dam."""
    from nd_breach_runner.tests import fake_qgis as fq
    from nd_breach_runner.tests.run_tests import _patched_algs
    algs, _fq = _patched_algs()
    root = tempfile.mkdtemp(prefix="ndbr_dl_")
    try:
        pack = _pack(root)
        far = [(2000.0, -100.0), (2000.0, 100.0)]
        crest = fq.FakeSource([
            fq.FakeFeature(fq.FakeGeometry("line", [LINE_OTHER]), structure_id=2900),
            fq.FakeFeature(fq.FakeGeometry("line", [far]), structure_id=2645)])
        with _active_pack(pack):
            calls, rows, info = _batch(_feats(fq, [((0.0, 0.0), {"ID": "DWR-2645"})]),
                                       {"CREST_LINE": crest})
        c = calls[0]
        assert c["midcrest_line"] is None, c                  # not 2645's far line, not 2900's
        assert c["crest_refused"].startswith("Crest line (CREST_LINE) of structure 2645 not "
                                             "used: the line is 2000 m from the inventory "
                                             "point"), c
        assert any("Crest line (CREST_LINE) of structure 2645 not used" in m for m in info)
        assert c["line"] == LINE_2645, c                      # the pack's line places it
        # with DWR lines off: no line at all, the point as given
        with _active_pack(pack):
            calls, rows, info = _batch(_feats(fq, [((0.0, 0.0), {"ID": "DWR-2645"})]),
                                       {"CREST_LINE": crest, "USE_DWR_LINES": False})
        assert calls[0]["midcrest_line"] is None and calls[0]["line"] is None, calls
        assert calls[0]["xy"] == (0.0, 0.0) and calls[0]["crest_refused"], calls
        # the helper: a match within the guard is used; a refused one names why
        ln, _d, how = algs._pick_crest_line([(np.asarray(far), {"sid": "2645"})], (0.0, 0.0),
                                            {"2645"}, "sid")
        assert ln is None and "2000 m" in how, how
        ln, _d, how = algs._pick_crest_line([(np.asarray(LINE_2645), {"sid": "2645"})],
                                            (0.0, 0.0), {"2645"}, "sid")
        assert ln is not None and how == "DWR structure 2645", how
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def dwr_line_run_warns_for_a_refused_crest_line():
    def setup(dam):
        dam._crest_line_refused = "Crest line (CREST_LINE) of structure 2659 not used: the " \
                                  "line is 7148 m from the inventory point (more than 500 m)"
        return (0.0, 0.0)
    rep, md, warnings = _run(setup)
    assert any(w.startswith("Crest line (CREST_LINE) of structure 2659 not used") for w in
               warnings), warnings
    assert rep["crest_line_refused"].endswith("(more than 500 m)"), rep.get("crest_line_refused")


@test
def dwr_line_cli_gpkg_layer_dam_json_and_bad_layers():
    """The CLI's layers.dam_lines may be a GeoPackage (as the plugin's DAM_LINES); a file that
    cannot be read never fails the dams; the structure id comes from dam.json as in the plugin."""
    from nd_breach_runner.tests import test_cli as tc
    from nd_breach_runner.damsuite import dam_record
    root = tempfile.mkdtemp(prefix="ndbr_dl_")
    try:
        pack = _pack(root)
        base = {"Dam Height (ft)": 25, "Max Storage (acre-feet)": 20, "Top of Dam (ft)": 1850,
                "x": 0, "y": 0}
        gp = os.path.join(pack, "dwr", "lines_2026.gpkg")
        with tc.Tmp() as d, _no_pack():
            c, calls, rows, out = _cli_batch(d, [dict(base, dam_id="DWR-2645")],
                                             layers={"dam_lines": gp})
        assert calls[0]["line"] == LINE_2645 and np.allclose(calls[0]["xy"], (0.0, 40.0)), \
            (calls, out)
        assert rows["DWR-2645"]["Dam_Line"].startswith("DWR line (the DAM_LINES layer, "
                                                       "structure 2645)"), rows
        # a junk GeoPackage and a junk GeoJSON: the dams run on their points, with a note
        for name, data in (("junk.gpkg", b"junk"), ("junk.geojson", b"{not json")):
            with tc.Tmp() as d, _no_pack():
                bad = os.path.join(d, name)
                with open(bad, "wb") as fh:
                    fh.write(data)
                c, calls, rows, out = _cli_batch(d, [dict(base, dam_id="DWR-2645")],
                                                 layers={"dam_lines": bad})
            assert calls and calls[0]["line"] is None and calls[0]["xy"] == (0.0, 0.0), \
                (name, rows, out)
            assert rows["DWR-2645"]["Status"] != "failed", (name, rows)
            assert "not read" in rows["DWR-2645"]["Dam_Line"] or \
                "no lines read" in rows["DWR-2645"]["Dam_Line"], (name, rows)
        # dam.json's dwr_structure_id (no id in the row), as the QGIS batch

        def pre(d):
            rec = dam_record.load(os.path.join(d, "out", "Pheasant"), create=True)
            rec.set("dwr_structure_id", "2645", "test", "1")
            rec.save()
        with tc.Tmp() as d, _active_pack(pack):
            c, calls, rows, out = _cli_batch(d, [dict(base, dam_id="Pheasant")], pre=pre)
        assert calls[0]["line"] == LINE_2645, (calls, out)
        assert "dam.json" in calls[0]["info"]["matched_by"], calls[0]["info"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def chain_runner_fingerprint_has_the_dam_line_rule():
    """A dam the chain finished under an earlier Runner (no dam_line_rule in its runner-stage
    fingerprint) is run again once on a resume; the switch changes it too."""
    from nd_breach_runner.core import chain as CH
    with _env(NDBREACH_DWR_LINES=None, NDBREACH_DAM_POINT_CHECK=None):
        inp = CH.stage_inputs("runner", {}, {})
        assert inp["dam_line_rule"] == {"placement": DL.PLACEMENT_RULE, "dwr_lines": "on",
                                        "dam_point_check_env": ""}, inp
        new = CH.fingerprint(inp)
        old = CH.fingerprint({k: v for k, v in inp.items() if k != "dam_line_rule"})
        assert new != old
        rec = {"status": "done", "fingerprint": old, "stage": "runner", "outputs": {}}
        assert not CH.stage_kept(rec, new)
    with _env(NDBREACH_DWR_LINES="off"):
        assert CH.fingerprint(CH.stage_inputs("runner", {}, {})) != new
    assert "dam_line_rule" not in CH.stage_inputs("classify", {}, {})


@test
def inflow_side_check_on_an_arched_dam_line():
    """3.20.13 review (DWR-3034): the line normal of check (f) is taken at the segment nearest
    the inflow, not from the end-to-end chord. An inflow below an arched line but on the
    reservoir side of its chord, the trace running downstream: no problem."""
    import math
    from nd_breach_runner.core import terrain
    arc = [(x, 60.0 * math.sin(math.pi * x / 170.0)) for x in np.linspace(0.0, 170.0, 18)]
    inflow = (56.0, 15.0)
    path = [(56.0, 15.0 - 10.0 * k) for k in range(30)]
    chk = terrain.inflow_side_check((85.0, 60.0), inflow, path, (0.0, -1.0), crest_line=arc)
    assert chk["ok"] and chk["trace_vs_dam_line_normal"] > 0.5, chk
    # a trace that runs back up to the arc is still caught
    back = [(56.0, 15.0 + 5.0 * k) for k in range(30)]
    chk = terrain.inflow_side_check((85.0, 60.0), inflow, back, None, crest_line=arc)
    assert not chk["ok"] and chk["trace_vs_dam_line_normal"] < 0, chk
    # a straight line: as before (the chord)
    line = [(0.0, 0.0), (170.0, 0.0)]
    chk = terrain.inflow_side_check((85.0, 0.0), (56.0, -15.0), [(56.0, -15.0 - 10.0 * k)
                                                                 for k in range(30)],
                                    (0.0, -1.0), crest_line=line)
    assert chk["ok"] and chk["trace_vs_dam_line_normal"] == 1.0, chk
