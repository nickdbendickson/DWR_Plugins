"""QA-map aerial base map (core/basemap.py, 3.11.0; Docs & QA aerial-basemap spec): the ND DWR
NAIP 2025 WMS -> Esri -> USGS chain, labels, credit with flight dates, offline / pack /
hillshade fallbacks, the 30 s budget, the tile cache never inside a run folder, and the
qa_basemap / qa_basemap_labels settings. Every fetch is faked: no test touches the network."""
import contextlib
import io
import json
import os
import shutil
import socket
import tempfile
import time

import numpy as np

from nd_breach_runner.core import basemap as BM
from nd_breach_runner.core import qa_maps as QA
from nd_breach_runner.core.grid import Grid
from nd_breach_runner.tests.registry import test as register

ENVS = ("ND_DAM_OFFLINE", "NDBREACH_BASEMAP_OFFLINE", "ND_DAM_HAZARD_OFFLINE_ONLY",
        "ND_DAM_SUITE", "NDSUITE_HOME", "ND_DAM_HAZARD_OFFLINE_PACK", "NDBREACH_BASEMAP_PACK",
        "NDBREACH_BASEMAP_ORDER", "NDBREACH_BASEMAP_SOURCES", "NDBREACH_BASEMAP_CACHE",
        "NDBREACH_BASEMAP_CACHE_MB")


@contextlib.contextmanager
def _env(**kw):
    old = {k: os.environ.get(k) for k in ENVS + tuple(kw)}
    try:
        for k in ENVS:
            os.environ.pop(k, None)
        os.environ["NDBREACH_BASEMAP_CACHE"] = "off"
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


@contextlib.contextmanager
def _no_sockets(local_ok=False):
    """Any real connection attempt fails the test (local_ok: 127.0.0.1 is allowed)."""
    real = socket.socket.connect

    def boom(sock, addr, *a, **k):
        if local_ok and isinstance(addr, tuple) and addr[0] == "127.0.0.1":
            return real(sock, addr, *a, **k)
        raise AssertionError("a QA base-map test tried the network: {}".format(addr))
    socket.socket.connect = boom
    try:
        yield
    finally:
        socket.socket.connect = real


def _wkt(code=5070):
    from osgeo import osr
    s = osr.SpatialReference()
    s.ImportFromEPSG(code)
    return s.ExportToWkt()


def _png(w, h, rgba):
    """PNG bytes of a w x h picture of one colour (rgba) through GDAL (no PIL)."""
    from osgeo import gdal
    mem = gdal.GetDriverByName("MEM").Create("", w, h, 4, gdal.GDT_Byte)
    rng = np.random.RandomState(w + h)
    for i, v in enumerate(rgba):
        a = np.full((h, w), v, np.uint8)
        if i < 3 and rgba[3] == 255:
            a = np.clip(a.astype(int) + rng.randint(-20, 20, (h, w)), 0, 255).astype(np.uint8)
        mem.GetRasterBand(i + 1).WriteArray(a)
    name = "/vsimem/qa_basemap_test_{}.png".format(time.time())
    gdal.GetDriverByName("PNG").CreateCopy(name, mem)
    f = gdal.VSIFOpenL(name, "rb")
    gdal.VSIFSeekL(f, 0, 2)
    n = gdal.VSIFTellL(f)
    gdal.VSIFSeekL(f, 0, 0)
    data = gdal.VSIFReadL(1, n, f)
    gdal.VSIFCloseL(f)
    gdal.Unlink(name)
    return data


class _Answer(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FakeNet(object):
    """opener(url, timeout) answering like the real services; `down` = url parts that fail."""

    def __init__(self, down=(), delay=0.0, wms_html=False):
        self.down, self.delay, self.wms_html = tuple(down), delay, wms_html
        self.urls = []

    def __call__(self, url, timeout):
        self.urls.append(url)
        if self.delay:
            time.sleep(self.delay)
        for d in self.down:
            if d in url:
                raise OSError("connection refused (fake {})".format(d))
        if "Dates_Imagery" in url:
            return _Answer(json.dumps({"features": [{"attributes": {"IDATE": "2025-07-13"}},
                                                    {"attributes": {"IDATE": "2025-07-13"}}]}).encode())
        if "World_Imagery/MapServer/identify" in url:
            return _Answer(json.dumps({"results": [{"attributes": {
                "DATE (YYYYMMDD)": "20240922", "SRC_DATE2": "9/22/2024"}}]}).encode())
        if "dwrwms" in url:
            if self.wms_html:
                return _Answer(b"<!DOCTYPE html><html>login</html>")
            q = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
            return _Answer(_png(int(q["WIDTH"]), int(q["HEIGHT"]), (40, 120, 40, 255)))
        if "Reference/" in url:
            return _Answer(_png(256, 256, (255, 255, 255, 120)))
        if "/tile/" in url:
            return _Answer(_png(256, 256, (150, 110, 70, 255)))
        raise OSError("fake: unknown url " + url)


def _bbox():
    """A 1.2 km map near Casselton, ND, in EPSG:5070."""
    x, y = -92780.0, 2657270.0
    return (x - 600.0, y - 600.0, x + 600.0, y + 600.0)


def _grid():
    b = _bbox()
    n, cell = 60, 20.0
    xs = b[0] + (np.arange(n) + 0.5) * cell
    X = np.meshgrid(xs, xs)[0] - (b[0] + b[2]) / 2
    z = 280.0 + 0.002 * -X
    z = np.where(np.abs(X) < 15, 290.0, z)
    z = np.where(X > 15, z - 4.0, z)
    return Grid(z, b[0], b[3], cell, crs_wkt=_wkt())


def _have_mpl():
    try:
        import matplotlib  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# 1. the chain: NAIP first, its flight date, labels, credit --------------------------------
@register
def qa_basemap_naip_first_with_dates_labels_and_credit():
    with _env(), _no_sockets():
        net = FakeNet()
        bm = BM.render(_bbox(), _wkt(), 300, 300, opener=net)
        assert bm.source == "nd_naip_2025", bm.info
        assert bm.rgb.shape == (300, 300, 3) and bm.rgb.dtype == np.uint8
        assert bm.labels is not None and bm.labels.shape == (300, 300, 4)
        assert bm.info["dates"] == ["2025-07-13"] and bm.info["labels"] is True
        wms = [u for u in net.urls if "dwrwms" in u][0]
        assert "LAYERS=NorthDakota_NAIP_2025_col_air" in wms and "SRS=EPSG:5070" in wms, wms
        assert any("World_Transportation" in u for u in net.urls)
        assert any("World_Boundaries_and_Places" in u for u in net.urls)
        c = bm.credit(_wkt(), "DamSafety Runner 9.9.9", "dem.tif")
        assert c.startswith("Imagery: ") and "NAIP 2025" in c and "flown 2025-07-13" in c, c
        assert "labels: Esri reference (Esri, HERE, Garmin, OSM contributors)" in c, c
        assert "EPSG:5070" in c and c.endswith("DamSafety Runner 9.9.9"), c
        no_lab = BM.render(_bbox(), _wkt(), 300, 300, want_labels=False, opener=FakeNet())
        assert no_lab.labels is None and "labels:" not in no_lab.credit(_wkt())


# 1b. a real local tile server: mosaic + 3857 -> 5070 warp lands a known pixel; cache hits ----
TARGET = (-97.2125, 46.9005)            # lon, lat of the red mark


def _tile_with_mark(z, x, y):
    """Grey 256 px tile; the 3 x 3 px around TARGET painted red when it falls in this tile."""
    import math as m
    n = 2 ** z
    gx = (TARGET[0] + 180.0) / 360.0 * n * 256
    r = m.radians(TARGET[1])
    gy = (1.0 - m.log(m.tan(r) + 1.0 / m.cos(r)) / m.pi) / 2.0 * n * 256
    from osgeo import gdal
    mem = gdal.GetDriverByName("MEM").Create("", 256, 256, 3, gdal.GDT_Byte)
    rgb = np.full((3, 256, 256), 90, np.uint8)
    rgb[:, ::16, :] = 140                                  # texture (not "blank")
    px, py = int(gx - x * 256), int(gy - y * 256)
    if 0 <= px < 256 and 0 <= py < 256:
        rgb[:, max(0, py - 1):py + 2, max(0, px - 1):px + 2] = np.array([255, 0, 0])[:, None, None]
    for i in range(3):
        mem.GetRasterBand(i + 1).WriteArray(rgb[i])
    name = "/vsimem/mark_{}_{}_{}.png".format(z, x, y)
    gdal.GetDriverByName("PNG").CreateCopy(name, mem)
    f = gdal.VSIFOpenL(name, "rb")
    gdal.VSIFSeekL(f, 0, 2)
    size = gdal.VSIFTellL(f)
    gdal.VSIFSeekL(f, 0, 0)
    data = gdal.VSIFReadL(1, size, f)
    gdal.VSIFCloseL(f)
    gdal.Unlink(name)
    return data


@contextlib.contextmanager
def _tile_server():
    import http.server
    import threading
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            parts = self.path.strip("/").split("/")
            try:
                z, y, x = (int(v) for v in parts[-3:])
                body, ctype = _tile_with_mark(z, x, y), "image/png"
            except ValueError:
                body, ctype = b"<html>not a tile</html>", "text/html"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield "http://127.0.0.1:{}".format(srv.server_address[1]), hits
    finally:
        srv.shutdown()
        srv.server_close()


@register
def qa_basemap_local_tile_server_known_pixel_and_cache():
    from osgeo import osr
    root = tempfile.mkdtemp(prefix="qa_srv_")
    try:
        with _tile_server() as (base, hits):
            cfg = os.path.join(root, "sources.json")
            with open(cfg, "w") as f:
                json.dump({"sources": [{"id": "local", "kind": "xyz", "zmax": 18,
                                        "url": base + "/tile/{z}/{y}/{x}", "name": "Local tiles",
                                        "res": "1 m", "credit": "test"}],
                           "order": ["local"], "labels": []}, f)
            with _env(NDBREACH_BASEMAP_SOURCES=cfg, NDBREACH_BASEMAP_CACHE=os.path.join(
                    root, "cache"), no_proxy="127.0.0.1", NO_PROXY="127.0.0.1"), \
                    _no_sockets(local_ok=True):
                w = osr.SpatialReference()
                w.ImportFromEPSG(4326)
                w.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                t = osr.SpatialReference()
                t.ImportFromEPSG(5070)
                t.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                X, Y, _ = osr.CoordinateTransformation(w, t).TransformPoint(*TARGET)
                bbox = (X - 437.0, Y - 311.0, X + 563.0, Y + 689.0)      # 1 km, 250 px
                bm = BM.render(bbox, _wkt(), 250, 250)
                assert bm.source == "local" and bm.rgb.shape == (250, 250, 3), bm.info
                n1 = len(hits)
                assert n1 > 0 and bm.info["requests"] == n1, (n1, bm.info)
                red = (bm.rgb[:, :, 0] > 170) & (bm.rgb[:, :, 1] < 90)
                assert red.any(), "mark not found"
                rr, cc = np.where(red)
                col, row = cc.mean(), rr.mean()
                ecol, erow = 437.0 / 4.0, 689.0 / 4.0                   # 4 m pixels
                assert abs(col - ecol) <= 1.0 and abs(row - erow) <= 1.0, (col, row, ecol, erow)
                again = BM.render(bbox, _wkt(), 250, 250)
                assert len(hits) == n1 and again.info["requests"] == 0, (len(hits), again.info)
                assert again.info["cache_hits"] == n1 and np.array_equal(again.rgb, bm.rgb)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# 2. a service that fails or sends a login page: the next source; all down: hillshade --------
@register
def qa_basemap_probe_failure_falls_through_the_chain():
    with _env(), _no_sockets():
        for net in (FakeNet(down=("dwrwms",)), FakeNet(wms_html=True)):
            bm = BM.render(_bbox(), _wkt(), 200, 200, opener=net)
            assert bm.source == "esri_world_imagery", bm.info
            assert bm.info["dates"] == ["2024-09-22"], bm.info
            assert any(f.startswith("nd_naip_2025:") for f in bm.info["failures"]), bm.info
        bm = BM.render(_bbox(), _wkt(), 200, 200,
                       opener=FakeNet(down=("dwrwms", "World_Imagery")))
        assert bm.source == "usgs_imagery", bm.info
        bm = BM.render(_bbox(), _wkt(), 200, 200, opener=FakeNet(down=("http",)))
        assert bm.rgb is None and bm.source == "hillshade" and bm.labels is None, bm.info
        assert bm.info["fallback"] == "imagery services did not answer", bm.info
        assert "hillshade" in bm.credit(_wkt(), "", "dem.tif")


# 3. the per-map network budget ------------------------------------------------------------
@register
def qa_basemap_network_budget_caps_the_time():
    with _env(), _no_sockets():
        t = time.time()
        bm = BM.render(_bbox(), _wkt(), 200, 200, budget_s=1.0,
                       opener=FakeNet(down=("http",), delay=0.4))
        assert time.time() - t < 3.0, time.time() - t
        assert bm.rgb is None and any("budget" in f for f in bm.info["failures"]), bm.info
    assert BM.BUDGET_S == 30


# 4. offline: no request at all, pack imagery else hillshade, said in the credit ------------
@register
def qa_basemap_offline_makes_no_request():
    from osgeo import gdal
    for env in ({"ND_DAM_OFFLINE": "1"}, {"NDBREACH_BASEMAP_OFFLINE": "yes"},
                {"ND_DAM_SUITE": tempfile.gettempdir()}):
        with _env(**env), _no_sockets():
            net = FakeNet()
            bm = BM.render(_bbox(), _wkt(), 120, 120, opener=net, dirs=[])
            assert net.urls == [] and bm.info["requests"] == 0, net.urls
            assert bm.rgb is None and "offline" in bm.info["fallback"], bm.info
            c = bm.credit(_wkt(), "v", "dem.tif")
            assert c.startswith("Imagery not available (offline): hillshade of dem.tif"), c
    d = tempfile.mkdtemp(prefix="qa_pack_")
    try:
        img = os.path.join(d, "imagery")
        os.makedirs(img)
        b = _bbox()
        ds = gdal.GetDriverByName("GTiff").Create(os.path.join(img, "casselton.tif"), 64, 64, 3,
                                                  gdal.GDT_Byte)
        ds.SetGeoTransform((b[0] - 100, (b[2] - b[0] + 200) / 64.0, 0, b[3] + 100, 0,
                            -(b[3] - b[1] + 200) / 64.0))
        ds.SetProjection(_wkt())
        rng = np.random.RandomState(1)
        for i in range(3):
            ds.GetRasterBand(i + 1).WriteArray(rng.randint(30, 220, (64, 64)).astype(np.uint8))
        ds.SetMetadataItem("ACQUISITION_DATE", "2023-06-01")
        ds = None
        before = sorted(os.listdir(img))
        with _env(ND_DAM_OFFLINE="1", NDBREACH_BASEMAP_PACK=d), _no_sockets():
            bm = BM.render(_bbox(), _wkt(), 100, 100, opener=FakeNet())
            assert bm.source == "pack" and bm.rgb.shape == (100, 100, 3), bm.info
            assert bm.info["dates"] == ["2023-06-01"], bm.info
            assert "offline pack imagery casselton.tif" in bm.credit(_wkt())
        assert sorted(os.listdir(img)) == before, os.listdir(img)       # no .aux.xml
    finally:
        shutil.rmtree(d, ignore_errors=True)


# 5. the PNGs: imagery under the flood, scale bar, north arrow, credit in the record ---------
@register
def qa_basemap_pngs_have_imagery_scale_north_and_credit():
    if not _have_mpl():
        print("   (skipped: no matplotlib)")
        return
    g = _grid()
    d = tempfile.mkdtemp(prefix="qa_bm_")
    try:
        cx = (g.x0 + g.x0 + g.nx * g.dx) / 2
        cy = g.y0 - g.ny * g.dx / 2
        depth = np.where(g.x_centers()[None, :] > cx + 15, 1.2, 0.0) * np.ones((g.ny, 1))
        args = (d, "BM1", g, (cx, cy), (cx - 30.0, cy), 290.0)
        kw = dict(radius_m=60.0, model_grid=g, depth_m=depth)
        with _env(), _no_sockets():
            info, problems = QA.write_qa_maps("inflow+depth", *args, basemap_opts={
                "opener": FakeNet()}, **kw)
            assert problems == [], problems
            for k in ("inflow_map", "depth_map"):
                rec = info["basemap"]["maps"][k]
                assert rec["source"] == "nd_naip_2025" and rec["north_arrow"], rec
                assert rec["scale_bar_m"] > 0 and "flown 2025-07-13" in rec["credit"], rec
            assert info["basemap"]["mode"] == "auto" and "NAIP 2025" in info["basemap"]["credit"]
            with open(info["depth_map"], "rb") as f:
                aerial = f.read()
            d2 = os.path.join(d, "hs")
            info2, problems2 = QA.write_qa_maps("inflow+depth", d2, *args[1:], basemap="hillshade",
                                                **kw)
            assert problems2 == [] and info2["basemap"]["maps"]["depth_map"]["source"] == "hillshade"
            assert "qa_basemap = hillshade" in info2["basemap"]["credit"], info2
            with open(info2["depth_map"], "rb") as f:
                assert f.read() != aerial
            # imagery asked for but nothing answers: drawn anyway, with a warning
            info3, problems3 = QA.write_qa_maps("inflow", os.path.join(d, "x"), *args[1:],
                                                basemap="imagery", basemap_opts={
                                                    "opener": FakeNet(down=("http",))}, **kw)
            assert os.path.isfile(info3["inflow_map"]), info3
            assert len(problems3) == 1 and "no aerial imagery" in problems3[0], problems3
        from osgeo import gdal
        for name in ("qa_inflow_map.png", "qa_depth_map.png"):
            ds = gdal.Open(os.path.join(d, name))
            a = ds.ReadAsArray()[:3].astype(float)
            ds = None
            assert a.std() > 5
    finally:
        shutil.rmtree(d, ignore_errors=True)


# 6. the tile cache: capped, TTL, never inside a run / input folder; no files left ----------
@register
def qa_basemap_cache_is_capped_and_never_in_run_folders():
    root = tempfile.mkdtemp(prefix="qa_cache_")
    try:
        run = os.path.join(root, "run")
        os.makedirs(run)
        with _env(NDBREACH_BASEMAP_CACHE=os.path.join(run, "cache")), _no_sockets():
            assert BM.Cache(avoid=(run,)).root == ""
            info, _p = QA.write_qa_maps("inflow", run, "C1", _grid(), (-92780.0, 2657270.0),
                                        None, 290.0, basemap_opts={"opener": FakeNet()})
            assert info["basemap"]["maps"]["inflow_map"]["source"] == "nd_naip_2025"
        assert sorted(os.listdir(run)) == ["qa_inflow_map.png"], os.listdir(run)
        cdir = os.path.join(root, "user_cache")
        c = BM.Cache(root=cdir, cap_mb=0.01)                  # ~10 kB
        for i in range(8):
            c.put("u{}".format(i), b"x" * 3000)
            os.utime(c._path("u{}".format(i)), (1000 + i, time.time() - 100 + i))
        assert c.get("u0") == b"x" * 3000                     # touched: most recently used
        c.trim()
        left = sorted(n for _d, _s, f in os.walk(cdir) for n in f)
        assert len(left) * 3000 <= 0.9 * 0.01 * 1024 * 1024 + 3000, left
        assert c.get("u0") is not None and c.get("u1") is None
        old = BM.Cache(root=cdir, ttl_days=1)
        old.put("stale", b"y")
        os.utime(old._path("stale"), (0, time.time() - 2 * 86400))
        assert old.get("stale") is None
        with _env(NDBREACH_BASEMAP_CACHE="off"):
            assert BM.Cache().root == ""
    finally:
        shutil.rmtree(root, ignore_errors=True)


# 7. no CRS: the hillshade without asking anything ---------------------------------------
@register
def qa_basemap_grid_without_crs_keeps_the_hillshade():
    with _env(), _no_sockets():
        net = FakeNet()
        bm = BM.render((0, 0, 100, 100), None, 50, 50, opener=net)
        assert net.urls == [] and bm.rgb is None and bm.info["fallback"] == "the grid has no CRS"
        assert BM.scale_text(500) == "500 m (1,640 ft)", BM.scale_text(500)
        assert abs(BM.true_north_deg(-92780.0, 2657270.0, _wkt())) < 3.0


# 8. settings: RunSettings, old JSON, CLI keys, run key, mode names --------------------------
@register
def qa_basemap_settings_defaults_cli_and_run_key():
    from nd_breach_runner.core import workflow, probabilistic
    st = workflow.RunSettings()
    assert st.qa_basemap == "auto" and st.qa_basemap_labels is True
    d = st.as_dict()
    assert d["qa_basemap"] == "auto" and d["qa_basemap_labels"] is True
    assert workflow.RunSettings(qa_basemap="hillshade", qa_basemap_labels=False).qa_basemap == "hillshade"
    assert "qa_basemap" in probabilistic._KEY_SKIP and "qa_basemap_labels" in probabilistic._KEY_SKIP
    from nd_breach_runner.tests.test_cli import cli as _cli
    cli = _cli()
    assert cli.MODEL_KEYS["qa_basemap"] == ("qa_basemap", ("enum", ("auto", "imagery", "hillshade")))
    assert cli.MODEL_KEYS["qa_basemap_labels"] == ("qa_basemap_labels", ("bool",))
    assert BM.normalize_mode(None) == "auto" and BM.normalize_mode("") == "auto"   # old JSON
    assert BM.normalize_mode(2) == "hillshade" and BM.normalize_mode("Aerial") == "imagery"
    for bad in ("sideways", 9):
        try:
            BM.normalize_mode(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
