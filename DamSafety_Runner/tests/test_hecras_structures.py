"""3.10.56: a run's culverts and bridges (as ANUGA used them) in the HEC-RAS geometry
(core/hecras_structures): the connections, breaklines built into the computation points so
cell faces lie on every connection line, the weir / culvert invert no lower than the cells they
connect, ANUGA's own inverts from anuga_job.npz, and adding the structures to an already built
package. Synthetic terrain (GDAL), no HEC-RAS, no solver."""
import json
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.core import hecras_breaklines as hb
from nd_breach_runner.core import hecras_model as hm
from nd_breach_runner.core import hecras_structures as hs
from nd_breach_runner.core import structures as sm
from nd_breach_runner.tests.registry import test

EPSG = 2266                       # ND South, international ft (the model CRS and units)
X0, Y0 = 2700000.0, 480000.0
HALF = 3000.0                     # the 2D area: a 6,000 ft square
CELL = 100.0
PIX = 16.0
FLOOR, ROAD, CHANNEL = 1000.0, 1006.0, 996.0


def _gdal():
    from osgeo import gdal, osr
    gdal.UseExceptions()
    return gdal, osr


def _terrain(path):
    """Flat ground at FLOOR; a road along y = Y0 (crest 30 ft wide at ROAD, 3:1 side slopes);
    a channel along x = X0 (40 ft wide, CHANNEL) that stops at the road (a culvert crossing)."""
    gdal, osr = _gdal()
    n = int(2 * (HALF + 400) / PIX)
    xs = X0 - HALF - 400 + (np.arange(n) + 0.5) * PIX
    ys = Y0 + HALF + 400 - (np.arange(n) + 0.5) * PIX
    X, Y = np.meshgrid(xs, ys)
    z = np.full(X.shape, FLOOR)
    dy = np.abs(Y - Y0)
    road = np.clip(ROAD - np.maximum(dy - 15.0, 0.0) / 3.0, FLOOR, ROAD)
    z = np.maximum(z, road)
    ch = (np.abs(X - X0) <= 20.0) & (dy > 45.0)
    z[ch] = CHANNEL
    ds = gdal.GetDriverByName("GTiff").Create(path, n, n, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((X0 - HALF - 400, PIX, 0, Y0 + HALF + 400, 0, -PIX))
    s = osr.SpatialReference()
    s.ImportFromEPSG(EPSG)
    ds.SetProjection(s.ExportToWkt())
    ds.GetRasterBand(1).WriteArray(z.astype(np.float32))
    ds.GetRasterBand(1).SetNoDataValue(-9999)
    ds = None
    return path


def _perimeter():
    return np.array([[X0 - HALF, Y0 - HALF], [X0 + HALF, Y0 - HALF], [X0 + HALF, Y0 + HALF],
                     [X0 - HALF, Y0 + HALF]])


def _points():
    """A 100 ft grid, offset so no row lies on the road line (the faces are off it)."""
    g = np.arange(-HALF + 0.5 * CELL, HALF, CELL)
    X, Y = np.meshgrid(X0 + g + 13.0, Y0 + g + 37.0)
    p = np.column_stack([X.ravel(), Y.ravel()])
    per = _perimeter()
    return p[(p[:, 0] > per[0, 0]) & (p[:, 0] < per[1, 0]) & (p[:, 1] > per[0, 1]) &
             (p[:, 1] < per[2, 1])]


def _srs():
    s = hm.srs_from("EPSG:{}".format(EPSG))
    return s


def _structs():
    ft = hm.FT
    culv = sm.Structure("culvert", np.array([[X0, Y0 + 60.0], [X0, Y0 - 60.0]]), width_m=1.2192,
                        height_m=1.2192, shape="pipe", invert_up_m=997.0 * ft,
                        invert_dn_m=996.5 * ft, name="test culvert")
    brg = sm.Structure("bridge", np.array([[X0 + 1500.0, Y0 + 60.0], [X0 + 1500.0, Y0 - 60.0]]),
                       width_m=12.0, height_m=2.0, name="test bridge")
    out = sm.Structure("culvert", np.array([[X0 + 9000.0, Y0 + 60.0], [X0 + 9000.0, Y0 - 60.0]]),
                       name="far culvert")
    return [culv, brg, out]


def _build(tmp):
    tif = _terrain(os.path.join(tmp, "t.tif"))
    units = hm.Units(False)
    per = _perimeter()
    s = hs.connections(_structs(), lambda a: np.asarray(a, float).reshape(-1, 2), per, CELL, tif,
                       _srs(), units)
    return tif, units, per, s


@test
def hecras_structures_connections_culvert_bridge_and_outside():
    tmp = tempfile.mkdtemp()
    try:
        tif, units, per, s = _build(tmp)
        names = {c["source_name"]: c for c in s["exported"]}
        assert set(names) == {"test culvert", "test bridge"}, names.keys()
        assert [k["name"] for k in s["skipped"]] == ["far culvert"], s["skipped"]
        assert "outside" in s["skipped"][0]["why"]
        cv = names["test culvert"]["culvert"]
        assert cv["shape"] == 1 and abs(cv["rise"] - 4.0) < 0.01, cv
        assert abs(cv["inv_up"] - 997.0) < 0.01 and abs(cv["inv_dn"] - 996.5) < 0.01, cv
        assert abs(names["test culvert"]["crest_elev"] - ROAD) < 0.2
        gap = names["test bridge"]["gap"]
        assert gap and abs(gap["width"] - 12.0 / hm.FT) < 0.5, gap
        blk = "\n".join(hm.structure_block(names["test culvert"], __import__("datetime").datetime(
            2026, 10, 21)))
        assert "Conn Routing Type= 1 " in blk and "Conn Routing Type= 3" not in blk and "Connection Culv=1," in blk, blk
        assert "Connection Up SA=Downstream" in blk and "Connection Dn SA=Downstream" in blk
    finally:
        shutil.rmtree(tmp)


@test
def hecras_structures_breaklines_put_faces_on_the_lines_and_keep_the_rest():
    tmp = tempfile.mkdtemp()
    try:
        tif, units, per, s = _build(tmp)
        pts0 = _points()
        conns = s["exported"]
        for c in conns:
            assert hb.share_on_faces(pts0, c["line"]) < 0.5      # the grid is off the road
        pts, info = hs.connection_breaklines(pts0, per, conns, CELL, units,
                                             clip=hm.perimeter_clip(per))
        assert info["lines"] == 2 and info["points_added"] > 0, info
        for c in conns:
            assert info["on_faces"][c["name"]] >= 0.95, info["on_faces"]
        assert info["check"]["max_sides"] <= hb.MAX_FACES
        # points farther than the reach from every line are exactly the old ones
        far0 = np.ones(len(pts0), bool)
        for c in conns:
            far0 &= hb.poly_dist(pts0[:, 0], pts0[:, 1], c["line"]) > hs.BREAKLINE_REACH_FT + 1
        from scipy.spatial import cKDTree
        assert cKDTree(pts).query(pts0[far0])[0].max() < 1e-9
        # a connection next to the dam line gets no breakline
        dam = np.array([[X0 - HALF, Y0 + 50.0], [X0 + HALF, Y0 + 50.0]])
        _, info2 = hs.connection_breaklines(pts0, per, conns, CELL, units, dam_line=dam,
                                            clip=hm.perimeter_clip(per))
        assert info2["lines"] == 0 and len(info2["skipped"]) == 2, info2
    finally:
        shutil.rmtree(tmp)


@test
def hecras_structures_cells_check_and_fit_raise_weir_and_inverts():
    tmp = tempfile.mkdtemp()
    try:
        tif, units, per, s = _build(tmp)
        conns = s["exported"]
        pts, _ = hs.connection_breaklines(_points(), per, conns, CELL, units,
                                          clip=hm.perimeter_clip(per))
        raster = hs.read_raster(tif)
        culv = [c for c in conns if c["culvert"]][0]
        # an invert below the cell, and a weir stretch 2 ft under the ground beside the road
        culv["culvert"]["inv_dn"] = CHANNEL - 5.0
        w = culv["weir"]
        culv["weir"] = [w[0], (250.0, ROAD), (260.0, FLOOR - 2.0), (440.0, FLOOR - 2.0),
                        (450.0, ROAD)] + [p for p in w[1:] if p[0] > 450.0]
        ck = hs.cells_check(pts, per, conns, raster)
        sm_ = hs.check_summary(ck)["_total"]
        assert sm_["inverts_below_cells"] >= 1 and ck[culv["name"]]["weir_below_cells"] >= 1, ck
        b = ck[culv["name"]]["barrels"][0]
        assert not b["same_cell"] and b["cell_min_up"] <= 997.0 + 0.01, b
        changes = hs.fit_to_cells(conns, ck, units)
        assert any(x["what"] == "inv_dn" for x in changes)
        assert any(x["what"] == "weir" for x in changes)
        after = hs.check_summary(hs.cells_check(pts, per, conns, raster))["_total"]
        assert after["inverts_below_cells"] == 0 and after["weir_below_cells"] == 0, after
        assert len(culv["weir"]) <= hm.MAX_WEIR_POINTS
        assert all(isinstance(hs.change_text(x, units), str) for x in changes)
    finally:
        shutil.rmtree(tmp)


@test
def hecras_structures_load_run_takes_anuga_inverts():
    tmp = tempfile.mkdtemp()
    try:
        bm = os.path.join(tmp, "DWR-1", "_breach_model")
        os.makedirs(os.path.join(bm, "anuga"))
        used = [{"kind": "culvert", "line": [[0, 10], [0, -10]], "width_m": 1.2, "height_m": 0.9,
                 "shape": "box", "barrels": 2, "invert_up_m": None, "invert_dn_m": None,
                 "manning": 0.013, "losses": 1.0, "name": "c1"},
                {"kind": "bridge", "line": [[50, 10], [50, -10]], "width_m": 8.0,
                 "height_m": 1.8, "name": "b1"}]
        with open(os.path.join(bm, "run_report.json"), "w") as f:
            json.dump({"structures_used": used, "work_crs": "EPSG:5070"}, f)
        job = [{"name": "c1", "invert_elevations": [301.5, 301.2], "end_points": [[0, 10], [0, -10]]}]
        np.savez(os.path.join(bm, "anuga", "anuga_job.npz"), culverts_json=np.array(json.dumps(job)))
        st, src, crs, notes = hs.load_run_structures(os.path.join(tmp, "DWR-1"))
        assert crs == "EPSG:5070" and len(st) == 2 and "run_report.json" in src
        assert (st[0].invert_up_m, st[0].invert_dn_m) == (301.5, 301.2), st[0]
        assert st[0].barrels == 2 and st[1].kind == "bridge" and st[1].invert_up_m is None
        assert notes and "anuga_job.npz" in notes[0]
        try:
            hs.load_run_structures(os.path.join(tmp, "nothing"))
        except hm.HecRasModelError:
            pass
        else:
            raise AssertionError("no report: an error")
    finally:
        shutil.rmtree(tmp)


def _package(tmp, tif):
    """A minimal Runner-style package: .g01 with the 2D area, its points, a dam connection and
    the BC line; the terrain; GIS/Projection.prj."""
    pk = os.path.join(tmp, "PKG")
    os.makedirs(os.path.join(pk, "Terrain"))
    os.makedirs(os.path.join(pk, "GIS"))
    shutil.copy(tif, os.path.join(pk, "Terrain", "T Terrain.tif"))
    with open(os.path.join(pk, "GIS", "Projection.prj"), "w") as f:
        f.write(_srs().ExportToWkt())
    per, pts = _perimeter(), _points()
    dam = [(X0 - 500.0, Y0 + HALF), (X0 + 500.0, Y0 + HALF)]
    L = ["Geom Title=t", "Program Version=6.10", "",
         "Storage Area=Downstream      ,{},{}".format(X0, Y0),
         "Storage Area Surface Line= {} ".format(len(per))] + hm.lines_xy(per, 1) + [
        "Storage Area Is2D=-1", "Storage Area 2D Points= {} ".format(len(pts))] + \
        hm.lines_xy(pts, 2) + ["Storage Area Mannings=0.06", "",
                               "Connection=Dam             ,{},{}".format(X0, Y0 + HALF),
                               "Connection Desc=dam", "Connection Line=2"] + \
        hm.lines_xy(dam, 2) + ["Conn Routing Type= 1 ", "Conn Outlet Rating Curve= 0 ,False,,",
                               "BC Line Name=DS Normal Depth                 ",
                               "BC Line Storage Area=Downstream      ", ""]
    with open(os.path.join(pk, "T.g01"), "w", newline="") as f:
        f.write("\r\n".join(L))
    with open(os.path.join(pk, "README_HECRAS.md"), "w") as f:
        f.write("# t\n")
    with open(os.path.join(pk, "PACKAGE_MANIFEST.json"), "w") as f:
        json.dump({"cell": CELL}, f)
    return pk


@test
def hecras_structures_add_to_package_round_trip():
    tmp = tempfile.mkdtemp()
    try:
        tif = _terrain(os.path.join(tmp, "t.tif"))
        pk = _package(tmp, tif)
        bm = os.path.join(tmp, "RUN", "_breach_model")
        os.makedirs(bm)
        used = [{"kind": "culvert", "line": [[X0, Y0 + 60.0], [X0, Y0 - 60.0]],
                 "width_m": 1.2192, "height_m": 1.2192, "shape": "pipe",
                 "invert_up_m": 997.0 * hm.FT, "invert_dn_m": 996.5 * hm.FT, "name": "KO-x"}]
        with open(os.path.join(bm, "run_report.json"), "w") as f:
            json.dump({"structures_used": used, "work_crs": "EPSG:{}".format(EPSG)}, f)
        out = os.path.join(tmp, "OUT")
        info = hs.add_to_package(pk, os.path.join(tmp, "RUN"), out)
        assert [c["source_name"] for c in info["exported"]] == ["KO-x"], info["skipped"]
        assert info["check_after"]["_total"]["weir_below_cells"] == 0
        assert info["check_after"]["_total"]["inverts_below_cells"] == 0
        g = hs.parse_g01(os.path.join(out, "T.g01"))
        assert g["connections"] == ["Dam", info["exported"][0]["name"]], g["connections"]
        assert len(g["points"]) == info["points_after"] != info["points_before"]
        assert np.allclose(g["perimeter"], _perimeter())
        txt = open(os.path.join(out, "T.g01"), newline="").read()
        assert txt.index("Connection=KO-x") < txt.index("BC Line Name=")
        assert "Conn Routing Type= 3" not in txt and "Conn Routing Type= 1 " in txt and "\r\n" in txt
        assert "## Culverts, bridges and notches" in open(os.path.join(out, "README_HECRAS.md")).read()
        assert open(os.path.join(pk, "T.g01"), newline="").read().count("Connection=") == 1
        try:
            hs.add_to_package(out, os.path.join(tmp, "RUN"), os.path.join(tmp, "OUT2"))
        except hm.HecRasModelError as e:
            assert "already has structure connections" in str(e)
        else:
            raise AssertionError("a package with structures: an error")
    finally:
        shutil.rmtree(tmp)


@test
def hecras_structures_bridge_on_a_culvert_crossing_becomes_a_gap_in_its_weir():
    tmp = tempfile.mkdtemp()
    try:
        tif = _terrain(os.path.join(tmp, "t.tif"))
        units = hm.Units(False)
        culv = _structs()[0]
        brg = sm.Structure("bridge", np.array([[X0 + 40.0, Y0 + 60.0], [X0 + 40.0, Y0 - 60.0]]),
                           width_m=9.0, height_m=1.8, invert_up_m=CHANNEL * hm.FT,
                           invert_dn_m=CHANNEL * hm.FT, name="NBI on the crossing")
        s = hs.connections([culv, brg], lambda a: np.asarray(a, float).reshape(-1, 2),
                           _perimeter(), CELL, tif, _srs(), units)
        assert len(s["exported"]) == 1 and not s["skipped"], s["skipped"]
        c = s["exported"][0]
        assert c["culvert"] and c["gaps"][0]["structure"] == "NBI on the crossing", c.get("gaps")
        gp = c["gaps"][0]
        assert abs(gp["width"] - 9.0 / hm.FT) < 0.1 and abs(gp["floor"] - CHANNEL) < 0.01, gp
        w = np.asarray(c["weir"])
        assert float(np.interp(gp["station"], w[:, 0], w[:, 1])) <= CHANNEL + 0.01, w
        assert w[:, 1].max() >= ROAD - 0.2
        assert any("shares this crossing" in n for n in c["notes"])
    finally:
        shutil.rmtree(tmp)


@test
def hecras_structures_build_model_takes_them_from_another_run():
    """End to end: build_model on the synthetic run of test_hecras_p0 (no structures) with
    structures_from = an 'ANUGA run' folder holding one culvert and one bridge."""
    from nd_breach_runner.tests import test_hecras_p0 as P
    if P._skip():
        return
    d = tempfile.mkdtemp()
    try:
        P._run(d)
        an = os.path.join(d, "anuga_run")
        bm = os.path.join(an, "_breach_model")
        os.makedirs(os.path.join(bm, "anuga"))
        yc = P.Y0 - 1500.0
        used = [{"kind": "culvert", "line": [[P.X0, yc + 15.0], [P.X0, yc - 15.0]],
                 "width_m": 1.2, "height_m": 0.9, "shape": "box", "barrels": 2,
                 "invert_up_m": None, "invert_dn_m": None, "manning": 0.013, "losses": 1.0,
                 "name": "guess: road 1 crosses channel 1"},
                {"kind": "bridge", "line": [[P.X0 + 40.0, yc - 1000.0 + 15.0],
                                            [P.X0 + 40.0, yc - 1000.0 - 15.0]],
                 "width_m": 10.0, "height_m": 1.8, "name": "NBI 1 over CREEK"}]
        with open(os.path.join(bm, "run_report.json"), "w") as f:
            json.dump({"structures_used": used, "work_crs": "EPSG:26914"}, f)
        z = P.FLOOR_M + 0.002 * (yc - P.Y0)
        job = [{"name": used[0]["name"], "invert_elevations": [z - 0.2, z - 0.3]}]
        np.savez(os.path.join(bm, "anuga", "anuga_job.npz"),
                 culverts_json=np.array(json.dumps(job)))
        out = os.path.join(d, "model")
        r = hm.build_model(d, out_folder=out, options={"structures_from": an})
        m, info = r["model"], r["info"]
        si = info["structures"]
        assert len(si["exported"]) == 2, (si["exported"], si["skipped"])
        cv = [c for c in si["exported"] if c["culvert"]][0]["culvert"]
        assert cv["barrels"] == 2 and cv["inverts_from"] == "run / run", cv
        assert si["breaklines"]["lines"] == 2 and min(si["breaklines"]["on_faces"].values()) >= 0.95
        tot = si["cells_check"]["_total"]
        assert tot["weir_below_cells"] == 0 and tot["inverts_below_cells"] == 0, tot
        g = os.path.join(out, m.name + ".g01")
        assert P.unknown_keywords(g) == set(), P.unknown_keywords(g)
        txt = open(g).read()
        assert txt.count("Connection=") == 3 and "Conn Routing Type= 3" not in txt
        assert "Storage Area 2D Points= {} ".format(len(m.points)) in txt
        assert any("Structure breaklines" in n for n in r["notes"]), r["notes"]
        rd = open(os.path.join(out, "README_HECRAS.md")).read()
        assert "Mapping limits" in rd and "breaklines built into the computation points" in rd
    finally:
        shutil.rmtree(d)


@test
def hecras_structures_internal_culvert_connection_is_routing_1():
    """Regression (Absaraka abs2801v2egrcv, HEC-RAS 6.6: "The SA 2D Connection at BNSF KO KO-01
    cu has an error with the connection. Check that the "To:" is set correctly"): a culvert
    connection inside the 2D area was written with 'Conn Routing Type= 3', which HEC-RAS reads as
    a family of rating curves ('Use RC Family' 1). Now routing type 1, as HEC's own internal
    connections (BaldEagleDamBrk g01/g03/g09; this alone did not clear the error, see
    hecras_structures_connection_ends_two_cells_inside_the_perimeter); the barrel ends as HEC-RAS
    lays them (barrel_ends, checked against KO-01 in that run's geometry HDF) are in two cells on
    opposite sides, headwater end on the right; connection_problems() flags what HEC-RAS would
    refuse."""
    import datetime
    tmp = tempfile.mkdtemp()
    try:
        tif, units, per, s = _build(tmp)
        c = {k["source_name"]: k for k in s["exported"]}["test culvert"]
        blk = hm.structure_block(c, datetime.datetime(2026, 10, 21))
        assert "Conn Routing Type= 1 " in blk and not any("Routing Type= 3" in x for x in blk)
        assert "Conn Culv Bottom n=0.013" in blk
        up, dn = (np.array(p) for p in hm.barrel_ends(c)[0])
        assert abs(np.hypot(*(dn - up)) - c["culvert"]["length"]) < 0.01
        A, B = np.asarray(c["line"][0]), np.asarray(c["line"][-1])
        t = B - A

        def side(p):
            return t[0] * (p[1] - A[1]) - t[1] * (p[0] - A[0])
        assert side(up) < 0 < side(dn), (side(up), side(dn))      # headwater on the right
        assert up[1] > dn[1], (up, dn)       # the run's culvert flows north to south
        # as HEC-RAS 6.6 laid KO-01's barrel in abs2801v2egrcv (its geometry HDF)
        ko = {"line": [(2771988.8219505, 467795.23050863), (2771029.7799253, 468222.38717843)],
              "culvert": {"length": 131.56, "stations": [738.19]}}
        (ku, kd), = hm.barrel_ends(ko)
        assert np.allclose(ku, (2771341.25906267, 468155.66432044), atol=0.02), ku
        assert np.allclose(kd, (2771287.73167693, 468035.48590335), atol=0.02), kd
        pts, _ = hs.connection_breaklines(_points(), _perimeter(), s["exported"], CELL, units, None)
        raster = hs.read_raster(tif)
        ck = hs.cells_check(pts, _perimeter(), s["exported"], raster)
        hs.fit_to_cells(s["exported"], ck, units)
        ck = hs.cells_check(pts, _perimeter(), s["exported"], raster)
        b = ck[c["name"]]["barrels"][0]
        assert b["up_cell"] != b["dn_cell"] and b["up_on_headwater_side"] and b["ends_inside"], b
        assert hs.connection_problems(s["exported"], ck, _perimeter(), [hm.AREA_NAME]) == []
        # what HEC-RAS would refuse is reported
        bad = dict(c, name=hm.AREA_NAME)
        assert any("used twice" in p for p in hs.connection_problems([bad], {}, _perimeter(),
                                                                       [hm.AREA_NAME]))
        rev = json.loads(json.dumps(ck))
        rb = rev[c["name"]]["barrels"][0]
        rb.update(dn_cell=rb["up_cell"], same_cell=True, up_on_headwater_side=False)
        msg = "; ".join(hs.connection_problems([c], rev, _perimeter()))
        assert "both ends are in cell" in msg and "headwater end first" in msg, msg
        assert "used twice" not in msg
    finally:
        shutil.rmtree(tmp)


@test
def hecras_structures_connection_ends_two_cells_inside_the_perimeter():
    """Regression (Absaraka abs2801v2egrcv / cv2, HEC-RAS 6.6 still: "The SA 2D Connection at
    BNSF KO KO-01 cu has an error with the connection. Check that the "To:" is set correctly"
    with routing type 1): KO-01's line ended 114 ft (about one cell) from the 2D perimeter, all
    the others 466 ft or more. structure_connection now keeps both ends STRUCT_EDGE_CELLS (2)
    cells inside, connection_problems(cell=) refuses an end closer than that, an adverse culvert
    is noted, and add_to_package(exclude=) leaves named structures out."""
    tmp = tempfile.mkdtemp()
    try:
        tif = _terrain(os.path.join(tmp, "t.tif"))
        units = hm.Units(False)
        per = _perimeter()
        closed = [tuple(p) for p in per] + [tuple(per[0])]
        # a culvert on the road 250 ft from the east edge: the road runs on to the edge
        xe = X0 + HALF - 250.0
        near = sm.Structure("culvert", np.array([[xe, Y0 + 60.0], [xe, Y0 - 60.0]]),
                            width_m=0.9144, height_m=0.9144, shape="pipe",
                            invert_up_m=997.0 * hm.FT, invert_dn_m=998.74 * hm.FT,
                            name="edge culvert")
        s = hs.connections([near], lambda a: np.asarray(a, float).reshape(-1, 2), per, CELL,
                           tif, _srs(), units)
        assert len(s["exported"]) == 1, s["skipped"]
        c = s["exported"][0]
        ln = np.asarray(c["line"], float)
        d = hm.point_segment_distance(ln[:, 0], ln[:, 1], closed)
        assert d.min() >= hm.STRUCT_EDGE_CELLS * CELL - 1e-6, d
        assert any("adverse culvert" in n for n in c["notes"]), c["notes"]
        assert c["culvert"]["inv_up"] < c["culvert"]["inv_dn"]          # kept as the run had it
        pts, _ = hs.connection_breaklines(_points(), per, [c], CELL, units, None)
        ck = hs.cells_check(pts, per, [c], hs.read_raster(tif))
        hs.fit_to_cells([c], ck, units)
        ck = hs.cells_check(pts, per, [c], hs.read_raster(tif))
        assert hs.connection_problems([c], ck, per, cell=CELL) == []
        # an end 114 ft from the edge (KO-01) is refused; without cell= it is not checked
        ko = dict(c, line=[tuple(ln[0]), (X0 + HALF - 114.0, Y0)])
        msg = "; ".join(hs.connection_problems([ko], ck, per, cell=CELL))
        assert "end of its line is 114 from the edge" in msg and "needs 2 cells" in msg, msg
        assert not any("edge" in p for p in hs.connection_problems([ko], ck, per))
        # add_to_package: exclude a structure by name; an unknown name stops the build
        pk = _package(tmp, tif)
        bm = os.path.join(tmp, "RUN", "_breach_model")
        os.makedirs(bm)
        used = [{"kind": "culvert", "line": [[X0, Y0 + 60.0], [X0, Y0 - 60.0]], "width_m": 1.2192,
                 "height_m": 1.2192, "shape": "pipe", "invert_up_m": 997.0 * hm.FT,
                 "invert_dn_m": 996.5 * hm.FT, "name": "KO-x"},
                {"kind": "culvert", "line": [[xe, Y0 + 60.0], [xe, Y0 - 60.0]], "width_m": 0.9144,
                 "height_m": 0.9144, "shape": "pipe", "invert_up_m": 997.0 * hm.FT,
                 "invert_dn_m": 996.9 * hm.FT, "name": "KO-edge"}]
        with open(os.path.join(bm, "run_report.json"), "w") as f:
            json.dump({"structures_used": used, "work_crs": "EPSG:{}".format(EPSG)}, f)
        info = hs.add_to_package(pk, os.path.join(tmp, "RUN"), os.path.join(tmp, "A"))
        assert sorted(x["source_name"] for x in info["exported"]) == ["KO-edge", "KO-x"]
        for x in info["exported"]:
            e = np.asarray(x["line"], float)
            assert hm.point_segment_distance(e[:, 0], e[:, 1], closed).min() >= 200.0 - 1e-6
        info = hs.add_to_package(pk, os.path.join(tmp, "RUN"), os.path.join(tmp, "B"),
                                 exclude=["ko-edge"])
        assert [x["source_name"] for x in info["exported"]] == ["KO-x"]
        assert any("left out on request: KO-edge" in n for n in info["notes"]), info["notes"]
        txt = open(os.path.join(tmp, "B", "T.g01"), newline="").read()
        assert "KO-edge" not in txt and "Connection=KO-x" in txt
        try:
            hs.add_to_package(pk, os.path.join(tmp, "RUN"), os.path.join(tmp, "C"),
                              exclude=["nope"])
        except hm.HecRasModelError as e:
            assert "no structure named nope" in str(e) and not os.path.exists(
                os.path.join(tmp, "C"))
        else:
            raise AssertionError("an unknown exclude name: an error")
    finally:
        shutil.rmtree(tmp)
