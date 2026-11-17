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
                      "crest_refused": getattr(dam, "_crest_line_refused", None),
                      "dam_point_lines": st.dam_point_lines})
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
def _cli_batch(d, table_rows, batch=None, layers=None, pre=None, extra_heads=()):
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
             "Max Storage (acre-feet)", "Top of Dam (ft)", "x", "y"] + list(extra_heads)
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
    fingerprint) is run again once on a resume; with the DWR lines off the term is left out and
    the fingerprint is the earlier Runner's (review 2)."""
    from nd_breach_runner.core import chain as CH
    with _env(NDBREACH_DWR_LINES=None, NDBREACH_DAM_POINT_CHECK=None,
              DAM_POINT_CHECK=None):
        inp = CH.stage_inputs("runner", {}, {})
        assert inp["dam_line_rule"] == {"placement": DL.PLACEMENT_RULE, "dwr_lines": "on",
                                        "dam_point_check_env": ""}, inp
        new = CH.fingerprint(inp)
        old = CH.fingerprint({k: v for k, v in inp.items() if k != "dam_line_rule"})
        assert new != old
        rec = {"status": "done", "fingerprint": old, "stage": "runner", "outputs": {}}
        assert not CH.stage_kept(rec, new)
    with _env(NDBREACH_DWR_LINES="off"):
        assert CH.fingerprint(CH.stage_inputs("runner", {}, {})) == old != new
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


# ------------------------------------------------------------------------- second review ---
def _relabel_crs(path, srs_id, org, org_id, definition):
    """Give a GeoPackage's feature tables another gpkg_spatial_ref_sys row (an ESRI or a custom
    CRS, as GDAL writes them); the coordinates are left as they are."""
    import sqlite3
    con = sqlite3.connect(path)
    try:
        con.execute("INSERT OR REPLACE INTO gpkg_spatial_ref_sys VALUES (?,?,?,?,?,?)",
                    ("test", srs_id, org, org_id, definition, ""))
        con.execute("UPDATE gpkg_geometry_columns SET srs_id = ?", (srs_id,))
        con.execute("UPDATE gpkg_contents SET srs_id = ? WHERE data_type = 'features'",
                    (srs_id,))
        con.commit()
    finally:
        con.close()


@test
def dwr_line_batch_crest_line_of_another_structure_is_never_taken():
    """Review 2: a CREST_LINE layer with structure ids but no line for this dam gives no crest
    line (it took the nearest line, another structure's, which then also blocked the dam's own
    DWR line); a line with a blank id is still the fallback. The dam's own CREST_LINE that the
    guard refuses is not 'another dam's line' for the dam point check."""
    from nd_breach_runner.tests import fake_qgis as fq
    from nd_breach_runner.tests.run_tests import _patched_algs
    algs, _fq = _patched_algs()
    root = tempfile.mkdtemp(prefix="ndbr_dl_")
    try:
        pack = _pack(root)
        crest = fq.FakeSource([fq.FakeFeature(fq.FakeGeometry("line", [LINE_OTHER]),
                                              structure_id=2900)])
        with _active_pack(pack):
            calls, rows, info = _batch(_feats(fq, [((0.0, 0.0), {"ID": "DWR-2645"})]),
                                       {"CREST_LINE": crest})
        c = calls[0]
        assert c["midcrest_line"] is None and c["line"] == LINE_2645, c
        assert np.allclose(c["xy"], (0.0, 40.0)), c
        assert rows["DWR-2645"]["Dam_Line"].startswith("DWR line (the pack, structure 2645)"), \
            rows
        # 2900's line is another dam's line for the dam point check
        assert c["dam_point_lines"] == [LINE_OTHER], c
        # the helper: another structure's line is never the fallback; a blank id is
        own = [(-5.0, -80.0), (-5.0, 80.0)]
        ln, _d, how = algs._pick_crest_line([(np.asarray(LINE_OTHER), {"sid": 2900})],
                                            (0.0, 0.0), {"2645"}, "sid")
        assert ln is None and how.startswith("no crest line for structure 2645"), how
        for blank in (None, "", "NULL"):
            ln, _d, how = algs._pick_crest_line([(np.asarray(LINE_OTHER), {"sid": 2900}),
                                                 (np.asarray(own), {"sid": blank})],
                                                (0.0, 0.0), {"2645"}, "sid")
            assert ln is not None and np.allclose(ln, own) and how == "nearest line", (blank,
                                                                                       how)
        # no structure id for the dam (or no id field): the nearest line, as before
        ln, _d, how = algs._pick_crest_line([(np.asarray(LINE_OTHER), {"sid": 2900})],
                                            (0.0, 0.0), set(), "sid")
        assert ln is not None and how == "nearest line", how
        # a 'DWR-2645' id on the line side matches dam 2645 (one key on both sides)
        ln, _d, how = algs._pick_crest_line([(np.asarray(LINE_OTHER), {"sid": "2900"}),
                                             (np.asarray(LINE_2645), {"sid": "DWR-2645"})],
                                            (0.0, 0.0), {"2645"}, "sid")
        assert ln is not None and np.allclose(ln, LINE_2645) and how == "DWR structure 2645"
        # the dam's own crest line refused by the guard is not in the dam point check's other
        # dams' lines (it blocked moves onto its own embankment as 'another dam's crest line')
        far = [(-50.0, 0.0), (-50.0, 2600.0)]           # 50 m off, its centre 1300 m away
        crest2 = fq.FakeSource([
            fq.FakeFeature(fq.FakeGeometry("line", [far]), structure_id=2645),
            fq.FakeFeature(fq.FakeGeometry("line", [LINE_OTHER]), structure_id=2900)])
        with _no_pack():
            calls, rows, info = _batch(_feats(fq, [((0.0, 0.0), {"ID": "DWR-2645"})]),
                                       {"CREST_LINE": crest2})
        c = calls[0]
        assert c["midcrest_line"] is None and c["crest_refused"], c
        assert c["dam_point_lines"] == [LINE_OTHER], c["dam_point_lines"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def dwr_line_refused_crest_line_warning_says_what_placed_the_dam():
    """Review 2: the warning of a refused CREST_LINE said 'The dam point is used as given' when
    the DWR dam line then placed the dam."""
    crf = "Crest line (CREST_LINE) of structure 2645 not used: the line is 2000 m from the " \
          "inventory point (more than 500 m)"
    line = [(0.0, -100.0), (0.0, 100.0)]

    def with_line(dam):
        dam._crest_line_refused = crf
        info = {"source": "the pack", "structure_id": "2645", "matched_by": "dam ID DWR-2645",
                "parts": 1, "length_m": 200.0, "nearest_m": 30.0, "centre_m": 30.0,
                "used": True}
        info["text"] = DL.describe(info)
        return DL.apply(dam, (-30.0, 0.0), line, info)
    rep, md, warnings = _run(with_line)
    mine = [w for w in warnings if w.startswith(crf)]
    assert len(mine) == 1 and ". The DWR dam line is used instead (DWR dam line from the " \
        "pack, structure 2645" in mine[0], warnings
    assert not any("used as given" in w for w in mine), mine
    assert rep["dam_point"]["original_xy"] == [-30.0, 0.0] and rep["crest_line_xy"]

    def without(dam):
        dam._crest_line_refused = crf
        return (0.0, 0.0)
    rep, md, warnings = _run(without)
    assert crf + ". The dam point is used as given (the dam point check may still move it)." \
        in warnings, warnings
    # the batch's own message says the same (the pack's line places the dam)
    from nd_breach_runner.tests import fake_qgis as fq
    root = tempfile.mkdtemp(prefix="ndbr_dl_")
    try:
        pack = _pack(root)
        far = [(2000.0, -100.0), (2000.0, 100.0)]
        crest = fq.FakeSource([fq.FakeFeature(fq.FakeGeometry("line", [far]),
                                              structure_id=2645)])
        with _active_pack(pack):
            calls, rows, info = _batch(_feats(fq, [((0.0, 0.0), {"ID": "DWR-2645"})]),
                                       {"CREST_LINE": crest})
        msg = [m for m in info if "Crest line (CREST_LINE) of structure 2645" in m]
        assert msg and "The DWR dam line is used instead" in msg[0], info
        with _active_pack(pack):
            calls, rows, info = _batch(_feats(fq, [((0.0, 0.0), {"ID": "DWR-2645"})]),
                                       {"CREST_LINE": crest, "USE_DWR_LINES": False})
        msg = [m for m in info if "Crest line (CREST_LINE) of structure 2645" in m]
        assert msg and "The dam point is used as given" in msg[0], info
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def dwr_lines_dwr_text_ids_on_the_line_side():
    """Review 2: one key for the dam and the line side: a lines layer or file whose structure
    id holds 'DWR-2645' matches dam 2645 (LineIndex, find_in_lines, the NID table, the QGIS
    DAM_LINES layer); the id field is chosen by SID_FIELDS priority, never by set order."""
    from nd_breach_runner.tests import fake_qgis as fq
    for v, want in ((2645, "2645"), ("02645", "2645"), (2645.0, "2645"), ("DWR-2645", "2645"),
                    ("dwr_02645", "2645"), ("", ""), (None, ""), ("A12", "")):
        assert DL.sid_key(v) == want, (v, want)
    ids = DL.structure_ids("DWR-2645", {})
    line, info = DL.find_in_lines([(np.asarray(LINE_2645), {"dwr_id": "DWR-2645"})], "dwr_id",
                                  ids, (0.0, 0.0))
    assert line is not None and info["used"], info
    # the field priority (the CLI's GeoJSON properties came as a set)
    assert DL.id_field_of(["dwr_id", "structure_id"]) == "structure_id"
    assert DL.id_field_of({"DWR_ID", "name"}) == "DWR_ID" and DL.id_field_of(["x"]) == ""
    root = tempfile.mkdtemp(prefix="ndbr_dl_")
    try:
        p = os.path.join(root, "lines.gpkg")
        _write_gpkg(p, "DWR_dam_lines", "MULTILINESTRING", [("dwr_id", "text")],
                    [({"type": "MultiLineString", "coordinates": [LINE_2645]},
                      {"dwr_id": "DWR-2645"})])
        idx = DL.LineIndex(p)
        assert idx.has("2645") and idx.parts("2645") == [LINE_2645], idx._blobs.keys()
        d = os.path.join(root, "dams.gpkg")
        _write_gpkg(d, "DWR_dams", "POINT", [("dwr_id", "text"), ("national_id", "text")],
                    [({"type": "Point", "coordinates": (0.0, 0.0)},
                      {"dwr_id": "DWR-2645", "national_id": "ND01234"})])
        assert DL.nid_lookup_for(d)("ND01234") == "2645"
        # the QGIS batch's DAM_LINES layer with text ids
        lay = fq.FakeSource([
            fq.FakeFeature(fq.FakeGeometry("line", [LINE_OTHER]), dwr_id="DWR-2900"),
            fq.FakeFeature(fq.FakeGeometry("line", [LINE_2645]), dwr_id="DWR-2645")])
        with _no_pack():
            calls, rows, info = _batch(_feats(fq, [((0.0, 0.0), {"ID": "DWR-2645"})]),
                                       {"DAM_LINES": lay})
        assert calls[0]["line"] == LINE_2645, calls
        # a DAM_LINES layer with dwr_id before structure_id: structure_id (the priority)
        lay = fq.FakeSource([fq.FakeFeature(fq.FakeGeometry("line", [LINE_2645]),
                                            dwr_id="DWR-9999", structure_id=2645)])
        with _no_pack():
            calls, rows, info = _batch(_feats(fq, [((0.0, 0.0), {"ID": "DWR-2645"})]),
                                       {"DAM_LINES": lay})
        assert calls[0]["line"] == LINE_2645, (calls, info)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def dwr_line_cli_geojson_id_field_by_priority():
    """Review 2: a GeoJSON layers.dam_lines with structure_id and dwr_id: structure_id is used
    (SID_FIELDS priority), every run."""
    from nd_breach_runner.tests import test_cli as tc
    base = {"Dam Height (ft)": 25, "Max Storage (acre-feet)": 20, "Top of Dam (ft)": 1850,
            "x": 200, "y": 0}
    own = [(205.0, -80.0), (205.0, 80.0)]
    with tc.Tmp() as d, _no_pack():
        gj = os.path.join(d, "lines.geojson")
        tc.dump(gj, tc.fc([{"type": "Feature",
                            "properties": dict({f: 9999 for f in DL.SID_FIELDS},
                                               dwr_id="DWR-9999", structure_id=2645),
                            "geometry": {"type": "LineString", "coordinates": own}}]))
        c, calls, rows, out = _cli_batch(d, [dict(base, dam_id="DWR-2645")],
                                         layers={"dam_lines": gj})
    assert calls and calls[0]["line"] == own, (rows, out)


@test
def dwr_lines_crs_of_an_esri_or_custom_crs():
    """Review 2: a GeoPackage srs_id is not always an EPSG code: the gpkg_spatial_ref_sys row's
    organization and code (ESRI:102039), else its WKT (a custom CRS); OGR's authority name too.
    A CRS that cannot be read is said once, not as a distance refusal per dam."""
    wkt = 'PROJCS["custom",GEOGCS["NAD83",DATUM["North_American_Datum_1983",' \
          'SPHEROID["GRS 1980",6378137,298.257222101]],PRIMEM["Greenwich",0],' \
          'UNIT["degree",0.0174532925199433]],PROJECTION["Albers_Conic_Equal_Area"],' \
          'PARAMETER["latitude_of_center",23],PARAMETER["longitude_of_center",-96],' \
          'PARAMETER["standard_parallel_1",29.5],PARAMETER["standard_parallel_2",45.5],' \
          'PARAMETER["false_easting",0],PARAMETER["false_northing",0],UNIT["metre",1]]'
    assert DL._crs_of(102039, "ESRI", 102039, wkt) == "ESRI:102039"
    assert DL._crs_of(100000, "NONE", 100000, wkt) == wkt
    assert DL._crs_of(5070, "EPSG", 5070, "x") == "EPSG:5070"
    assert DL._crs_of(4326, "EPSG", 4326, "x") == DL._crs_of(4269, "epsg", 4269, "") == \
        DL.LINE_CRS
    assert DL._crs_of(5070) == "EPSG:5070" and DL._crs_of(0, "NONE", 0, "undefined") == \
        DL.LINE_CRS
    assert DL._crs_of(100001, "NONE", 100001, "undefined") == ""
    from nd_breach_runner.tests import fake_qgis as fq
    from nd_breach_runner.tests import test_cli as tc
    root = tempfile.mkdtemp(prefix="ndbr_dl_")
    try:
        pack = _pack(root)
        p = os.path.join(pack, "dwr", "lines_2026.gpkg")
        _relabel_crs(p, 102039, "ESRI", 102039, wkt)
        assert DL.LineIndex(p).crs == "ESRI:102039"
        _relabel_crs(p, 100000, "NONE", 100000, wkt)
        assert DL.LineIndex(p).crs == wkt
        # OGR (a shapefile with an ESRI .prj): not 'EPSG:102039'
        try:
            from osgeo import ogr, osr
        except ImportError:
            ogr = None
        if ogr is not None:
            sr = osr.SpatialReference()
            if sr.SetFromUserInput("ESRI:102039") == 0:
                shp = os.path.join(root, "lines.shp")
                ds = ogr.GetDriverByName("ESRI Shapefile").CreateDataSource(shp)
                lyr = ds.CreateLayer("lines", sr, ogr.wkbLineString)
                lyr.CreateField(ogr.FieldDefn("struct_id", ogr.OFTString))
                f = ogr.Feature(lyr.GetLayerDefn())
                f.SetField("struct_id", "DWR-2645")
                f.SetGeometry(ogr.CreateGeometryFromWkt("LINESTRING (0 -110,0 0,0 190)"))
                lyr.CreateFeature(f)
                f = lyr = ds = None
                idx = DL.LineIndex(shp)
                assert idx.has("2645") and idx.crs != "EPSG:102039", idx.crs
                chk = osr.SpatialReference()
                assert chk.SetFromUserInput(idx.crs) == 0 and chk.IsSame(sr), idx.crs
        # no usable definition: the QGIS batch says so once, the dams have no line
        _relabel_crs(p, 100001, "NONE", 100001, "undefined")
        DL._INDEX.clear()
        with _active_pack(pack):
            calls, rows, info = _batch(_feats(fq, [((0.0, 0.0), {"ID": "DWR-2645"}),
                                                   ((0.0, 0.0), {"ID": "DWR-2700"})]))
        assert all(c["line"] is None and not (c["info"] or {}).get("refused") for c in calls)
        for did in ("DWR-2645", "DWR-2700"):
            assert "coordinate system that cannot be read" in rows[did]["Dam_Line"], rows[did]
        assert sum("coordinate system that cannot be read" in m for m in info) == 1, info
        # an authority GDAL does not know: the CLI says so once
        _relabel_crs(p, 100002, "FOO", 1, wkt)
        DL._INDEX.clear()
        assert DL.LineIndex(p).crs == "FOO:1"
        base = {"Dam Height (ft)": 25, "Max Storage (acre-feet)": 20, "Top of Dam (ft)": 1850,
                "x": 0, "y": 0}
        if ogr is not None:
            with tc.Tmp() as d, _active_pack(pack):
                c, calls, rows, out = _cli_batch(d, [dict(base, dam_id="DWR-2645"),
                                                     dict(base, dam_id="DWR-2700")])
            assert all(x["line"] is None for x in calls), calls
            assert out.count("coordinate system that cannot be read (FOO:1)") == 1, out
            assert "cannot be read" in rows["DWR-2700"]["Dam_Line"], rows
    finally:
        DL._INDEX.clear()
        shutil.rmtree(root, ignore_errors=True)


@test
def dwr_lines_read_from_a_folder_with_uri_characters():
    """Review 2: a '#', '%' or '?' in the pack's path (a suite data folder 'Dam Projects #2')
    gave no lines and no NID table (the SQLite URI was not encoded)."""
    root = tempfile.mkdtemp(prefix="ndbr_dl_")
    try:
        for name in ("Dam Projects #2", "100%25", "q?x"):
            pack = _pack(os.path.join(root, name))
            idx = DL.LineIndex(os.path.join(pack, "dwr", "lines_2026.gpkg"))
            assert idx.count == 4 and not idx.error and idx.has("2645"), (name, idx.error)
            look = DL.nid_lookup_for(os.path.join(pack, "dwr", "dams.gpkg"))
            assert look("ND01234") == "2645", name
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def cli_frame_transform_cache_survives_recycled_ids():
    """Review 2: Frame._ct cached transformations by id(); an SRS freed after use (a layer's)
    left its id behind, and the next SRS made at that address (the DWR lines', per dam) got the
    other CRS's transformation."""
    from nd_breach_runner.tests import test_cli as tc
    try:
        from osgeo import osr
    except ImportError:
        from nd_breach_runner.tests.registry import Skip
        raise Skip("no GDAL")
    c = tc.cli()
    fr = c.Frame(osr_module=osr)
    work = fr.srs("EPSG:26914")
    fr = c.Frame(dem_srs=work, work_srs=work, osr_module=osr, dem_epsg="EPSG:26914")
    ref = osr.CoordinateTransformation(fr.srs("EPSG:4269"), work)
    want = ref.TransformPoint(-98.5, 47.0)[:2]
    for _k in range(40):
        lay = fr.srs("EPSG:3857")
        fr.array([(-1.09e7, 5.9e6)], lay)
        del lay
        ll = fr.srs("EPSG:4269")
        got = fr.array([(-98.5, 47.0)], ll)[0]
        del ll
        assert abs(got[0] - want[0]) < 1e-6 and abs(got[1] - want[1]) < 1e-6, (got, want)


def _chain_ops(placement):
    """Fake chain ops: every stage writes its output; the Runner also writes run_report.json
    with the dam point and dam line placement[dam] (a dict dam_xy / crest_line_xy)."""
    from nd_breach_runner.core import chain as CH
    calls = []

    class Ops(object):
        def run(self, stage, dam):
            calls.append(stage)
            os.makedirs(dam.folder, exist_ok=True)
            f = os.path.join(dam.folder, stage + ".out")
            open(f, "w").close()
            if stage == "runner":
                rp = os.path.join(dam.folder, CH.RUN_REPORT_REL)
                os.makedirs(os.path.dirname(rp), exist_ok=True)
                with open(rp, "w") as fh:
                    json.dump(dict(placement[dam.dam_id], summary={}), fh)
            if stage == "hec_run":
                raise CH.Waiting("waiting for HEC-RAS: run the plan by hand")
            out = {CH.REQUIRED_OUTPUT[stage]: f}
            if stage == "hec_build":
                out["built_at"] = 1.0
            return out
    return Ops(), calls


@test
def chain_runner_rerun_for_the_dam_line_rule_keeps_unchanged_dams():
    """Review 2: a dam the chain finished under an earlier Runner reruns its Runner stage once
    (the dam_line_rule term); when the dam point and dam line come out where the recorded run
    put them, HEC-RAS build / run, hazard import and classify are kept (a HEC wait dam does not
    go back to waiting for a manual plan run). A dam that moves reruns them. With
    NDBREACH_DWR_LINES=off the old fingerprint is kept and nothing reruns."""
    from nd_breach_runner.core import chain as CH
    assert CH.RUN_REPORT_REL == workflow.RUN_REPORT_REL
    K = lambda key: (CH.PLUGIN, CH.TOOL, key)        # noqa: E731
    root = tempfile.mkdtemp(prefix="ndbr_chain_")
    try:
        vals = {K("OUT_FOLDER"): root, K("SCREENING"): False}
        cv, _p = CH.chain_values(vals)

        def dams():
            return [CH.Dam(i, os.path.join(root, i), vals, cv, mode="wait") for i in ("A", "B")]
        line = [[0.0, -100.0], [0.0, 100.0]]
        place = {"A": {"dam_xy": [0.0, 0.0], "crest_line_xy": line},
                 "B": {"dam_xy": [10.0, 0.0], "crest_line_xy": None}}
        with _env(NDBREACH_DWR_LINES=None, NDBREACH_DAM_POINT_CHECK=None,
                  DAM_POINT_CHECK=None):
            # the dams as an earlier Runner finished them: every stage done, the runner's
            # fingerprint without the term
            ops, calls = _chain_ops(place)
            for d in dams():
                CH.run_dam(d, ops)
            for d in dams():
                sp = os.path.join(CH.chain_dir(d.folder), CH.STATE_NAME)
                st = json.load(open(sp))
                for s in ("hec_run", "hazard_import", "classify"):
                    f = os.path.join(d.folder, s + ".out")
                    open(f, "w").close()
                    st["stages"][s] = {"stage": s, "status": "done", "outputs": {
                        CH.REQUIRED_OUTPUT[s]: f}, "fingerprint": CH.fingerprint(
                        CH.stage_inputs(s, vals, cv))}
                r = st["stages"]["runner"]
                r["fingerprint"] = r.pop("fingerprint_base")
                json.dump(st, open(sp, "w"))
            assert "dam_line_rule" in CH.stage_inputs("runner", vals, cv)
            # with the DWR lines off: the old fingerprint, nothing reruns
            with _env(NDBREACH_DWR_LINES="off"):
                assert "dam_line_rule" not in CH.stage_inputs("runner", vals, cv)
                ops, calls = _chain_ops(place)
                rows = [CH.run_dam(d, ops) for d in dams()]
                assert calls == [] and all(r["Status"] == "done" for r in rows), (calls, rows)
            assert "dam_line_rule" not in CH.stage_inputs(
                "runner", dict(list(vals.items()) + [(CH.USE_DWR_ROW, "false")]), cv)
            # A: placed as before (Runner only); B: moved by the new rules (all again)
            moved = dict(place, B={"dam_xy": [10.0, 40.0], "crest_line_xy": line})
            ops, calls = _chain_ops(moved)
            ra = CH.run_dam(dams()[0], ops)
            assert calls == ["runner"], calls
            assert ra["Status"] == "done" and ra["Stage_runner"] == "done", ra
            assert all(ra["Stage_" + s] == "kept" for s in ("hec_build", "hec_run",
                                                              "hazard_import", "classify")), ra
            rb = CH.run_dam(dams()[1], ops)
            assert calls[1:] == ["runner", "hec_build", "hec_run"], calls
            assert rb["Status"] == "waiting for HEC-RAS", rb
            # the next resume: A is kept entirely
            ops, calls = _chain_ops(moved)
            CH.run_dam(dams()[0], ops)
            assert calls == [], calls
            # the term's value changes (the dam point check override): the Runner again, the
            # rest kept when the dam stays put
            with _env(NDBREACH_DAM_POINT_CHECK="warn"):
                ops, calls = _chain_ops(moved)
                ra = CH.run_dam(dams()[0], ops)
                assert calls == ["runner"] and ra["Stage_classify"] == "kept", (calls, ra)
            # placement helpers
            assert CH.same_placement({"dam_xy": [1.0, 2.0]}, {"dam_xy": [1.004, 2.0],
                                                               "crest_line_xy": None})
            assert not CH.same_placement({"dam_xy": [1.0, 2.0]}, {"dam_xy": [1.0, 2.5]})
            assert not CH.same_placement(None, {"dam_xy": [1.0, 2.0]})
            assert not CH.same_placement({"dam_xy": [0, 0], "crest_line_xy": line},
                                         {"dam_xy": [0, 0]})
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _with_row(vals, v):
    """The chain values with the batch row USE_DWR_LINES set to v."""
    from nd_breach_runner.core import chain as CH
    out = dict(vals)
    out[CH.USE_DWR_ROW] = v
    return out


@test
def chain_dwr_lines_off_fingerprints_and_kept_stages():
    """Review 3: the batch row USE_DWR_LINES is not a fingerprinted row (a 3.20.12 settings file
    given the row false keeps its fingerprint; given it true, the placement comparison runs);
    with the DWR lines off a dam point check override set through either name is still in the
    term (a change reruns the Runner), unset it is not; the later stages are kept after a rerun
    only in the HEC Wait / Windows Run modes and only when the inflow point did not move."""
    from nd_breach_runner.core import chain as CH
    K = lambda key: (CH.PLUGIN, CH.TOOL, key)        # noqa: E731
    clean = dict(NDBREACH_DWR_LINES=None, NDBREACH_DAM_POINT_CHECK=None, DAM_POINT_CHECK=None)
    with _env(**clean):
        vals = {K("OUT_FOLDER"): "x", CH.USE_DWR_ROW[:2] + ("DEM",): "dem.tif"}
        cv, _p = CH.chain_values(vals)
        old = CH.fingerprint({k: v for k, v in CH.stage_inputs("runner", vals, cv).items()
                              if k != CH.RULE_TERM})
        f_vals = _with_row(vals, "false")
        t_vals = _with_row(vals, "true")
        assert CH.USE_DWR_ROW[2] not in CH.stage_inputs("runner", t_vals, cv)["rows"]
        assert CH.fingerprint(CH.stage_inputs("runner", f_vals, cv)) == old
        assert CH.base_fingerprint("runner", t_vals, cv) == old
        assert CH.fingerprint(CH.stage_inputs("runner", t_vals, cv)) != old
        # an override with the DWR lines off: in the term (both names, as the check reads them)
        for name, v, want in (("NDBREACH_DAM_POINT_CHECK", "warn", "warn"),
                              ("DAM_POINT_CHECK", "on", "move"),
                              ("DAM_POINT_CHECK", "Off", "off")):
            with _env(**{name: v}):
                inp = CH.stage_inputs("runner", f_vals, cv)
                assert inp[CH.RULE_TERM] == {"dam_point_check_env": want}, (name, v, inp)
                assert CH.fingerprint(inp) != old
                assert CH.base_fingerprint("runner", f_vals, cv) == old
                with _env(NDBREACH_DWR_LINES="off"):
                    assert CH.stage_inputs("runner", vals, cv)[CH.RULE_TERM] == {
                        "dam_point_check_env": want}
            with _env(**{name: v}):
                assert CH.stage_inputs("runner", vals, cv)[CH.RULE_TERM][
                    "dam_point_check_env"] == want
        with _env(DAM_POINT_CHECK="not a mode"):
            assert CH.fingerprint(CH.stage_inputs("runner", f_vals, cv)) == old
        # DWR lines on: DAM_POINT_CHECK changes the fingerprint as NDBREACH_DAM_POINT_CHECK does
        on = CH.fingerprint(CH.stage_inputs("runner", vals, cv))
        with _env(DAM_POINT_CHECK="warn"):
            assert CH.fingerprint(CH.stage_inputs("runner", vals, cv)) != on
    # resumes of dams finished by an earlier Runner (every stage done)
    root = tempfile.mkdtemp(prefix="ndbr_chain3_")
    line = [[0.0, -100.0], [0.0, 100.0]]
    try:
        def finish(name, vals, cv, mode, place):
            d = CH.Dam(name, os.path.join(root, name), vals, cv, mode=mode)
            ops, _c = _chain_ops({name: place})
            CH.run_dam(d, ops)
            sp = os.path.join(CH.chain_dir(d.folder), CH.STATE_NAME)
            st = json.load(open(sp))
            for s in d_stages(cv, mode):
                if s == "runner":
                    continue
                f = os.path.join(d.folder, s + ".out")
                open(f, "w").close()
                st["stages"][s] = {"stage": s, "status": "done", "outputs": {
                    CH.REQUIRED_OUTPUT[s]: f}, "fingerprint": CH.fingerprint(
                    CH.stage_inputs(s, vals, cv))}
            r = st["stages"]["runner"]
            r["fingerprint"] = r.pop("fingerprint_base")
            json.dump(st, open(sp, "w"))

        def d_stages(cv, mode):
            return CH.plan_stages(cv, mode)

        def resume(name, vals, cv, mode, place):
            ops, calls = _chain_ops({name: place})
            row = CH.run_dam(CH.Dam(name, os.path.join(root, name), vals, cv, mode=mode), ops)
            return calls, row
        with _env(**clean):
            vals = {K("OUT_FOLDER"): root, K("SCREENING"): False}
            cv, _p = CH.chain_values(vals)
            p0 = {"dam_xy": [0.0, 0.0], "crest_line_xy": line,
                  "inflow_point": {"xy": [30.0, 0.0], "how": "toe band"}}
            # a 3.20.12 settings file given USE_DWR_LINES false: nothing reruns
            finish("rowF", vals, cv, "wait", p0)
            calls, row = resume("rowF", _with_row(vals, "false"), cv, "wait", p0)
            assert calls == [] and row["Status"] == "done", (calls, row)
            # ... given it true: the Runner only (the placement is the same)
            finish("rowT", vals, cv, "wait", p0)
            calls, row = resume("rowT", _with_row(vals, "true"), cv, "wait", p0)
            assert calls == ["runner"] and row["Stage_classify"] == "kept", (calls, row)
            # the inflow point moved: the later stages again
            finish("inflow", vals, cv, "wait", p0)
            moved = dict(p0, inflow_point={"xy": [-30.0, 0.0]})
            calls, row = resume("inflow", vals, cv, "wait", moved)
            assert calls == ["runner", "hec_build", "hec_run"], calls
            # HEC Skip: the classification reads the Runner's rasters, so it runs again
            finish("skip", vals, cv, "skip", p0)
            calls, row = resume("skip", vals, cv, "skip", p0)
            assert calls == ["runner", "classify"], calls
            # HEC Run on Linux: the Runner runs HEC-RAS itself, so the later stages run again
            finish("lnx", vals, cv, "run_linux", p0)
            calls, row = resume("lnx", vals, cv, "run_linux", p0)
            assert calls == ["runner", "hec_run"], calls
            # DWR lines off and a dam point check override set: the Runner once, the rest kept
            finish("ovr", vals, cv, "wait", p0)
            with _env(NDBREACH_DWR_LINES="off", DAM_POINT_CHECK="warn"):
                calls, row = resume("ovr", vals, cv, "wait", p0)
                assert calls == ["runner"] and row["Stage_classify"] == "kept", (calls, row)
                calls, row = resume("ovr", vals, cv, "wait", p0)
                assert calls == [], calls
            # the helpers
            assert CH.placement_of(os.path.join(root, "ovr"))["inflow_xy"] == [30.0, 0.0]
            a = {"dam_xy": [0, 0], "inflow_xy": [1.0, 1.0]}
            assert CH.same_placement(a, {"dam_xy": [0, 0], "inflow_xy": [1.004, 1.0]})
            assert not CH.same_placement(a, {"dam_xy": [0, 0], "inflow_xy": [1.0, 2.0]})
            assert not CH.same_placement(a, {"dam_xy": [0, 0]})
            assert CH.same_placement({"dam_xy": [0, 0]}, {"dam_xy": [0, 0], "inflow_xy": None})
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def crest_line_without_a_structure_id_is_the_fallback():
    """Review 3: when the dam has a DWR structure id and no CREST_LINE matches it, a line whose
    id field holds no structure id (a name in a 'structure' field, 0, a NID) is the nearest-line
    fallback as before; only a line carrying another structure id is never taken. Also in the
    single-dam tool (RunDam: _crest_ids from a DWR-NNNN dam ID)."""
    from nd_breach_runner.tests.run_tests import _patched_algs
    algs, fq = _patched_algs()
    near = [(5.0, -60.0), (5.0, 60.0)]
    for field, v in (("structure", "Main dam"), ("structure_id", 0), ("structure_id", "0"),
                     ("structure_id", "ND01234"), ("structure_id", None)):
        ln, d, how = algs._pick_crest_line([(np.asarray(near), {field: v})], (0.0, 0.0),
                                           {"2645"}, field)
        assert ln is not None and how == "nearest line" and abs(d - 5.0) < 1e-9, (field, v, how)
        # beside another structure's nearer line: still this one, never 2900's
        ln, d, how = algs._pick_crest_line([(np.asarray(LINE_OTHER), {field: 2900}),
                                            (np.asarray(near), {field: v})], (0.0, 0.0),
                                           {"2645"}, field)
        assert ln is not None and np.allclose(ln, near), (field, v, how)
    ln, _d, how = algs._pick_crest_line([(np.asarray(near), {"structure": "DWR-2900"})],
                                        (0.0, 0.0), {"2645"}, "structure")
    assert ln is None and "other structures" in how, how
    # RunDam: a CREST_LINE whose 'structure' field holds a name
    a = algs.RunDamAlgorithm()
    a.initAlgorithm()
    a._crest_ids = {"2645"}
    lines = fq.FakeSource([fq.FakeFeature(fq.FakeGeometry("line", [near]),
                                          structure="Main dam")])
    st = workflow.RunSettings()
    a.apply_features(st, {"CREST_LINE": lines}, fq.QgsProcessingContext(),
                     fq.QgsCoordinateReferenceSystem("EPSG:26914"), (0.0, 0.0))
    assert st.midcrest_line == near and not a._crest_refused, (st.midcrest_line,
                                                                 a._crest_refused)


@test
def dwr_lines_source_order_cache_and_chosen_id_fields():
    """Review 2 (coverage): the active pack before ND_DAM_HAZARD_OFFLINE_PACK's; line_index
    reads a changed file again; F_STRUCTURE (QGIS) and batch.structure_id_field (CLI) win over
    structure_id; the NID is looked up only when the dam has no other id."""
    from nd_breach_runner.tests import fake_qgis as fq
    from nd_breach_runner.tests import test_cli as tc
    root = tempfile.mkdtemp(prefix="ndbr_dl_")
    try:
        pack = _pack(os.path.join(root, "active"))
        other = _pack(os.path.join(root, "hazard"), lines=[(2645, LINE_OTHER)])
        with _active_pack(pack), _env(ND_DAM_HAZARD_OFFLINE_PACK=other):
            cands = [p for p, _l in DL.pack_candidates()]
            assert cands == [os.path.join(pack, "dwr", "lines_2026.gpkg"),
                             os.path.join(other, "dwr", "lines_2026.gpkg")], cands
            assert DL.pack_source()[0] == cands[0]
        # line_index: cached by modification time, read again when the file changes
        p = os.path.join(root, "own", "lines.gpkg")
        rows1 = [({"type": "MultiLineString", "coordinates": [LINE_2645]}, {"structure_id": 1})]
        _write_gpkg(p, "DWR_dam_lines", "MULTILINESTRING", [("structure_id", "int")], rows1)
        a = DL.line_index(p)
        assert DL.line_index(p) is a and a.parts("1") == [LINE_2645]
        rows2 = [({"type": "MultiLineString", "coordinates": [LINE_OTHER]}, {"structure_id": 1})]
        _write_gpkg(p, "DWR_dam_lines", "MULTILINESTRING", [("structure_id", "int")], rows2)
        t = os.path.getmtime(p) + 10.0
        os.utime(p, (t, t))
        b = DL.line_index(p)
        assert b is not a and b.parts("1") == [LINE_OTHER], b.parts("1")
        # the NID only when there is no other id
        look = lambda n: {"ND01234": "2645"}.get(n, "")             # noqa: E731
        ids = DL.structure_ids("X", {"structure_id": "2700", "NIDID": "ND01234"},
                               nid_lookup=look)
        assert [s for s, _h in ids] == ["2700"], ids
        # F_STRUCTURE: the chosen column (2645) wins over structure_id (2700, refused); the
        # NID (2645 too) is not looked up for a dam that has an id
        specs = [((0.0, 0.0), {"ID": "Pheasant", "sid_x": "2645", "structure_id": "2700"}),
                 ((0.0, 0.0), {"ID": "N2", "structure_id": "2700", "NIDID": "ND01234"})]
        with _active_pack(pack):
            calls, rows, info = _batch(_feats(fq, specs), {"F_STRUCTURE": "sid_x"})
        by = {c["dam"]: c for c in calls}
        assert by["Pheasant"]["line"] == LINE_2645, by["Pheasant"]
        assert by["Pheasant"]["info"]["matched_by"] == "field sid_x", by["Pheasant"]["info"]
        assert by["N2"]["line"] is None and by["N2"]["info"]["structure_id"] == "2700", by["N2"]
        # the CLI's batch.structure_id_field
        base = {"Dam Height (ft)": 25, "Max Storage (acre-feet)": 20, "Top of Dam (ft)": 1850,
                "x": 0, "y": 0}
        with tc.Tmp() as d, _active_pack(pack):
            c, calls, rows, out = _cli_batch(
                d, [dict(base, dam_id="P1", structure_id=2700, sid_x=2645)],
                batch={"structure_id_field": "sid_x"}, extra_heads=("sid_x",))
        assert calls[0]["line"] == LINE_2645, (calls, out)
        assert calls[0]["info"]["matched_by"] == "field sid_x", calls[0]["info"]
    finally:
        DL._INDEX.clear()
        shutil.rmtree(root, ignore_errors=True)


@test
def crest_raised_warning_is_added_once_per_run():
    # Nick's 2026-11-16 two-dam run listed it once per LISFLOOD-FP pass (six times on DWR-4031)
    w = []
    assert workflow.add_crest_raised_warning(w, 12, "at the crest point")
    assert not workflow.add_crest_raised_warning(w, 12, "at the crest point")
    assert not workflow.add_crest_raised_warning(w, 15, "along the dam line")
    assert not workflow.add_crest_raised_warning([], 0, "at the crest point")
    assert len(w) == 1 and w[0].startswith("Crest raised to the top of dam") and "12 cells" in w[0]
