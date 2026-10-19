"""0.17.28: Download DEM's 'USGS 3DEP 1 m lidar tiles (native, TNM)' source (core/lidar_tnm.py).
No network: the TNM answers, the metadata XML and the tile downloads are served by a fake HTTP
object; the tiles are tiny synthetic GeoTIFFs. Needs GDAL (prints SKIP otherwise).

    python3 DamSafety_Screening/tests/test_lidar_tnm_1m.py   or   python3 -m pytest <this file>"""
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _optional  # noqa: E402
_optional.need("osgeo", "GDAL builds and checks the tiles here")
from osgeo import gdal, osr  # noqa: E402  (the real GDAL, before qgis_mock's stand-in)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

import nd_dam_screening  # noqa: E402,F401
from nd_dam_screening.core import lidar_tnm as lt  # noqa: E402
from nd_dam_screening.damsuite import suitedata as SD  # noqa: E402

gdal.UseExceptions()
TESTS = []


def register(f):
    TESTS.append(f)
    return f


def _tif_bytes(value, x0, y0, n=20, cell=1.0, epsg=26914, nodata=-999999.0, holes=()):
    """A small float32 GeoTIFF (top-left x0, y0), every cell `value` except `holes` (nodata)."""
    path = "/vsimem/t_{}_{}_{}_{}.tif".format(value, x0, y0, epsg)
    ds = gdal.GetDriverByName("GTiff").Create(path, n, n, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, cell, 0, y0, 0, -cell))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    b = ds.GetRasterBand(1)
    b.SetNoDataValue(nodata)
    import struct
    rows = []
    for r in range(n):
        rows.append(b"".join(struct.pack("<f", nodata if (r, c) in holes else value) for c in range(n)))
    b.WriteRaster(0, 0, n, n, b"".join(rows), buf_type=gdal.GDT_Float32)
    ds = None
    f = gdal.VSIFOpenL(path, "rb")
    gdal.VSIFSeekL(f, 0, 2)
    size = gdal.VSIFTellL(f)
    gdal.VSIFSeekL(f, 0, 0)
    data = gdal.VSIFReadL(1, size, f)
    gdal.VSIFCloseL(f)
    gdal.Unlink(path)
    return bytes(data)


def _item(name, project, size, pub, url_root="https://prd-tnm.example/1m/"):
    return {"title": "USGS 1 Meter " + name.replace("USGS_1M_", "").replace(".tif", "").replace("_", " ", 2),
            "downloadURL": url_root + project + "/TIFF/" + name, "sizeInBytes": size,
            "publicationDate": pub, "lastUpdated": pub + "T00:00:00", "format": "GeoTIFF",
            "vendorMetaUrl": "https://meta.example/" + project + "/" + name.replace(".tif", ".xml"),
            "boundingBox": {"minX": -97.3, "maxX": -97.2, "minY": 46.9, "maxY": 47.0}}


class FakeBody(io.BytesIO):
    def __init__(self, data, fail_after=None, feedback=None, cancel_after=None):
        super().__init__(data)
        self.fail_after, self.feedback, self.cancel_after, self.sent = fail_after, feedback, cancel_after, 0

    def read(self, n=-1):
        if self.fail_after is not None and self.sent >= self.fail_after:
            raise ConnectionResetError("connection reset")
        if self.cancel_after is not None and self.sent >= self.cancel_after:
            self.feedback.cancel()
        n = 7 if n is None or n < 0 else min(n, 7)            # small reads: many chunks
        out = super().read(n)
        self.sent += len(out)
        return out


class FakeHttp:
    """TNM pages, metadata XML and tiles from dicts; records every request."""

    def __init__(self, items, files, meta=None, page=None, honour_range=True):
        self.items, self.files, self.meta = items, files, meta or {}
        self.page, self.honour_range = page, honour_range
        self.calls, self.ranges, self.fail = [], [], {}
        self.cancel = None                                  # (name, after_bytes, feedback)

    def get_json(self, url):
        self.calls.append(url)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert q["datasets"] == [lt.DATASET] and q["prodFormats"] == ["GeoTIFF"], q
        off, mx = int(q["offset"][0]), int(q["max"][0])
        mx = min(mx, self.page or mx)
        return {"total": len(self.items), "items": self.items[off:off + mx], "errors": [], "messages": []}

    def get_text(self, url):
        self.calls.append(url)
        if url not in self.meta:
            raise IOError("404")
        return self.meta[url]

    def open_range(self, url, start):
        name = url.rsplit("/", 1)[-1]
        self.ranges.append((name, start))
        n = self.fail.get(name, 0)
        if n:
            self.fail[name] = n - 1
            raise ConnectionError("timed out")
        data = self.files[name]
        if start and self.honour_range:
            body = data[start:]
            status = 206
        else:
            body, status = data, 200
        fb = None
        if self.cancel and self.cancel[0] == name:
            fb = FakeBody(body, cancel_after=self.cancel[1], feedback=self.cancel[2])
            self.cancel = None
        return status, {"content-length": str(len(body))}, fb or FakeBody(body)


class Feedback:
    def __init__(self):
        self.log, self._c, self.errors, self.warnings, self.pct = [], False, [], [], []

    def pushInfo(self, m): self.log.append(m)
    def pushWarning(self, m): self.warnings.append(m); self.log.append(m)
    def reportError(self, m, fatal=False): self.errors.append(m)
    def setProgress(self, p): self.pct.append(p)
    def isCanceled(self): return self._c
    def cancel(self): self._c = True


def _meta(beg, end):
    return "<metadata><timeperd><rngdates><begdate>{}</begdate><enddate>{}</enddate></rngdates></timeperd></metadata>".format(beg, end)


def _world(tmp):
    """Three cells: x62y521 from two projects (old 2019 = 1.0, new 2024 = 2.0, the new one with
    a nodata hole), x63y521 from the old project only (3.0). Tiles are 20 x 20 m here."""
    t_old_a = _tif_bytes(1.0, 500000, 5200020)
    t_new_a = _tif_bytes(2.0, 500000, 5200020, holes={(0, 0)})
    t_old_b = _tif_bytes(3.0, 500020, 5200020)
    names = {"old_a": "USGS_1M_14_x62y521_ND_Old_2019.tif", "new_a": "USGS_1M_14_x62y521_ND_New_C24.tif",
             "old_b": "USGS_1M_14_x63y521_ND_Old_2019.tif"}
    files = {names["old_a"]: t_old_a, names["new_a"]: t_new_a, names["old_b"]: t_old_b}
    items = [_item(names["old_a"], "ND_Old_2019", len(t_old_a), "2021-03-01"),
             _item(names["new_a"], "ND_New_C24", len(t_new_a), "2026-07-15"),
             _item(names["old_b"], "ND_Old_2019", len(t_old_b), "2021-03-01")]
    meta = {items[0]["vendorMetaUrl"]: _meta("20190501", "20190601"),
            items[1]["vendorMetaUrl"]: _meta("20230504", "20240516")}
    return names, files, items, meta


@register
def tiles_for_a_bbox_are_listed_from_every_tnm_page():
    tmp = tempfile.mkdtemp()
    try:
        names, files, items, meta = _world(tmp)
        http = FakeHttp(items, files, meta, page=2)             # two pages
        plan = lt.make_plan((-97.3, 46.9, -97.2, 47.0), tmp, http)
        tnm = [c for c in http.calls if c.startswith(lt.TNM_URL)]
        assert len(tnm) == 2 and "offset=2" in tnm[1], tnm
        q = urllib.parse.parse_qs(urllib.parse.urlparse(tnm[0]).query)
        assert q["bbox"] == ["-97.300000,46.900000,-97.200000,47.000000"], q
        got = sorted(t["tile"] for t in plan["tiles"])
        assert got == sorted([names["new_a"], names["old_b"]]), got
        assert plan["projects"] == ["ND_New_C24", "ND_Old_2019"]
        assert plan["total_bytes"] == len(files[names["new_a"]]) + len(files[names["old_b"]])
        assert plan["need_bytes"] == plan["total_bytes"]
        text = "\n".join(lt.describe_plan(plan))
        assert "ND_New_C24" in text and "collected 2023-05-04 to 2024-05-16" in text and "published 2026-07-15" in text
        assert "free in" in text
        # one metadata XML per project, never per tile
        assert sum(1 for c in http.calls if c.startswith("https://meta.example/ND_Old_2019")) == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@register
def overlapping_projects_prefer_the_newest_collection_and_say_so():
    t = [lt.parse_item(_item("USGS_1M_14_x1y1_A.tif", "A", 10, "2025-01-01")),
         lt.parse_item(_item("USGS_1M_14_x1y1_B.tif", "B", 10, "2021-01-01")),
         lt.parse_item(_item("USGS_1M_14_x2y1_A.tif", "A", 10, "2025-01-01"))]
    # B was collected later although A was published later: the collection date wins
    for x in t:
        x["collection_end"] = "2019-06-01" if x["project"] == "A" else "2020-11-05"
    chosen, dropped, notes = lt.select_tiles(t)
    assert [c["tile"] for c in chosen if c["key"] == "14_x1y1"] == ["USGS_1M_14_x1y1_B.tif"]
    assert [d["tile"] for d in dropped] == ["USGS_1M_14_x1y1_A.tif"]
    assert len(notes) == 1 and "B (2020-11-05) is the newest collection" in notes[0] and "not used" in notes[0]
    # without collection dates the publication date decides
    for x in t:
        x["collection_end"] = ""
    chosen, dropped, _n = lt.select_tiles(t)
    assert "USGS_1M_14_x1y1_A.tif" in [c["tile"] for c in chosen] and dropped[0]["project"] == "B"
    # keep_older: both kept, oldest first (the VRT order: newest on top)
    for x in t:
        x["collection_end"] = "2019-06-01" if x["project"] == "A" else "2020-11-05"
    chosen, dropped, notes = lt.select_tiles(t, keep_older=True)
    assert not dropped and len(chosen) == 3 and chosen[-1]["project"] == "B" and "kept underneath" in notes[0]


@register
def size_and_disk_check_refuse_too_big_or_too_full():
    plan = {"need_bytes": 3 * 1024 ** 3, "free_bytes": 100 * 1024 ** 3, "folder": "/x"}
    assert lt.disk_check(plan)[0]
    ok, msg = lt.disk_check(plan, max_bytes=2 * 1024 ** 3)
    assert not ok and "over the 2.00 GB limit" in msg, msg
    plan["free_bytes"] = 4 * 1024 ** 3                           # 1 GB would be left, 2 GB must stay
    ok, msg = lt.disk_check(plan)
    assert not ok and "only 4.00 GB free" in msg, msg
    plan["free_bytes"] = 6 * 1024 ** 3
    assert lt.disk_check(plan)[0]


@register
def download_resumes_a_part_file_and_skips_tiles_already_here():
    tmp = tempfile.mkdtemp()
    try:
        names, files, items, meta = _world(tmp)
        http = FakeHttp(items, files, meta)
        a, b = names["new_a"], names["old_b"]
        half = len(files[a]) // 2
        with open(os.path.join(tmp, a + ".part"), "wb") as f:
            f.write(files[a][:half])
        plan = lt.make_plan((0, 0, 1, 1), tmp, http)
        ta = [t for t in plan["tiles"] if t["tile"] == a][0]
        assert ta["partial_bytes"] == half
        assert plan["need_bytes"] == len(files[a]) - half + len(files[b])
        entries, failed = lt.fetch(plan, http, sleep=lambda s: None)
        assert not failed, failed
        assert (a, half) in http.ranges, http.ranges            # resumed from the .part
        assert open(os.path.join(tmp, a), "rb").read() == files[a]
        assert not os.path.exists(os.path.join(tmp, a + ".part"))
        # second run: both present -> nothing to download, no request
        http.ranges.clear()
        plan2 = lt.make_plan((0, 0, 1, 1), tmp, http)
        assert all(t["present"] for t in plan2["tiles"]) and plan2["need_bytes"] == 0
        fb = Feedback()
        entries2, failed2 = lt.fetch(plan2, http, fb)
        assert http.ranges == [] and len(entries2) == 2 and not failed2
        assert sum("already here, skipped" in m for m in fb.log) == 2
        # a file of the wrong size is not "present"
        with open(os.path.join(tmp, b), "ab") as f:
            f.write(b"x")
        plan3 = lt.make_plan((0, 0, 1, 1), tmp, http)
        assert [t["tile"] for t in plan3["tiles"] if not t["present"]] == [b]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@register
def a_server_that_ignores_range_restarts_and_failures_are_retried():
    tmp = tempfile.mkdtemp()
    try:
        names, files, items, meta = _world(tmp)
        http = FakeHttp(items, files, meta, honour_range=False)
        a = names["new_a"]
        with open(os.path.join(tmp, a + ".part"), "wb") as f:
            f.write(b"garbage-not-the-start")
        http.fail[names["old_b"]] = 2                            # two time-outs, then fine
        plan = lt.make_plan((0, 0, 1, 1), tmp, http)
        slept = []
        entries, failed = lt.fetch(plan, http, sleep=slept.append)
        assert not failed and open(os.path.join(tmp, a), "rb").read() == files[a]
        assert len(slept) == 2 and [r for r in http.ranges if r[0] == names["old_b"]] == [(names["old_b"], 0)] * 3
        # always failing: reported, others kept, the .part stays for the next run
        tmp2 = tempfile.mkdtemp()
        http2 = FakeHttp(items, files, meta)
        http2.fail[names["old_b"]] = 99
        plan2 = lt.make_plan((0, 0, 1, 1), tmp2, http2)
        entries2, failed2 = lt.fetch(plan2, http2, sleep=lambda s: None)
        assert len(entries2) == 1 and len(failed2) == 1 and "after 4 attempts" in failed2[0]
        shutil.rmtree(tmp2, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@register
def manifest_records_tile_project_dates_url_sha256_and_bytes():
    tmp = tempfile.mkdtemp()
    try:
        names, files, items, meta = _world(tmp)
        http = FakeHttp(items, files, meta)
        plan = lt.make_plan((0, 0, 1, 1), tmp, http)
        lt.fetch(plan, http, sleep=lambda s: None)
        m = json.load(open(os.path.join(tmp, lt.MANIFEST), encoding="utf-8"))
        e = m["tiles"][names["new_a"]]
        import hashlib
        assert e["sha256"] == hashlib.sha256(files[names["new_a"]]).hexdigest()
        assert e["bytes"] == len(files[names["new_a"]]) and e["project"] == "ND_New_C24"
        assert e["collection_begin"] == "2023-05-04" and e["collection_end"] == "2024-05-16"
        assert e["publication_date"] == "2026-07-15" and e["url"].endswith("/" + names["new_a"])
        assert e["epsg"] == "26914" and e["downloaded"]
        # the old project's metadata was not served: its dates fall back to empty, publication kept
        assert m["tiles"][names["old_b"]]["collection_end"] == "2019-06-01"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@register
def bad_tiles_fail_the_check():
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "ten_m.tif")
        open(p, "wb").write(_tif_bytes(1.0, 0, 0, cell=10.0))
        try:
            lt.verify_tile(p)
            raise AssertionError("a 10 m grid passed")
        except lt.TileError as e:
            assert "not 1 m" in str(e)
        try:
            lt.verify_tile(p, expected_bytes=os.path.getsize(p) + 1)
            raise AssertionError("wrong size passed")
        except lt.TileError as e:
            assert "expected" in str(e)
        q = os.path.join(tmp, "junk.tif")
        open(q, "wb").write(b"<html>Access Denied</html>")
        try:
            lt.verify_tile(q)
            raise AssertionError("junk passed")
        except lt.TileError:
            pass
        ok = os.path.join(tmp, "ok.tif")
        open(ok, "wb").write(_tif_bytes(1.0, 500000, 5200000))
        assert lt.verify_tile(ok)["epsg"] == "26914"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _read(vrt, x, y):
    ds = gdal.Open(vrt)
    gt = ds.GetGeoTransform()
    col, row = int((x - gt[0]) / gt[1]), int((y - gt[3]) / gt[5])
    v = ds.GetRasterBand(1).ReadAsArray(col, row, 1, 1) if hasattr(ds.GetRasterBand(1), "ReadAsArray") else None
    if v is None:
        import struct
        v = struct.unpack("<f", ds.GetRasterBand(1).ReadRaster(col, row, 1, 1, buf_type=gdal.GDT_Float32))[0]
    else:
        v = float(v[0][0])
    ds = None
    return v


@register
def vrt_puts_the_newest_on_top_and_the_coarser_dem_underneath():
    tmp = tempfile.mkdtemp()
    try:
        names, files, items, meta = _world(tmp)
        tiles = os.path.join(tmp, "tiles")
        http = FakeHttp(items, files, meta)
        plan = lt.make_plan((0, 0, 1, 1), tiles, http, keep_older=True)   # old A underneath new A
        entries, _f = lt.fetch(plan, http, sleep=lambda s: None)
        assert len(entries) == 3
        # a 10 m fallback in another CRS (EPSG:5070) covering the tiles and more
        fb = os.path.join(tmp, "DWR-1_10m_5070.tif")
        src = osr.SpatialReference(); src.ImportFromEPSG(26914)
        dst = osr.SpatialReference(); dst.ImportFromEPSG(5070)
        for s_ in (src, dst):
            s_.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        x, y, _z = osr.CoordinateTransformation(src, dst).TransformPoint(500020.0, 5200010.0)
        tmpfb = "/vsimem/fb.tif"
        ds = gdal.GetDriverByName("GTiff").Create(tmpfb, 30, 30, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((x - 150, 10, 0, y + 150, 0, -10))
        ds.SetProjection(dst.ExportToWkt())
        ds.GetRasterBand(1).Fill(9.0)
        ds = None
        gdal.Translate(fb, tmpfb)
        gdal.Unlink(tmpfb)
        vrt = os.path.join(tmp, "DWR-1_1m_lidar.vrt")
        vrt, js = lt.build_vrt(entries, vrt, fb, meta={"dam_id": "DWR-1"})
        info = gdal.Info(vrt, format="json")
        assert abs(info["geoTransform"][1] - 1.0) < 1e-9 and "26914" in json.dumps(info["coordinateSystem"])
        assert _read(vrt, 500005.5, 5200005.5) == 2.0          # newest collection on top
        assert _read(vrt, 500000.5, 5200019.5) == 1.0          # its nodata hole: the older tile shows
        assert _read(vrt, 500030.5, 5200010.5) == 3.0          # the second cell
        # without the older tile the hole shows the fallback
        only_new = [e for e in entries if e["project"] == "ND_New_C24" or e["key"] != "14_x62y521"]
        vrt2, _ = lt.build_vrt(only_new, os.path.join(tmp, "b.vrt"), fb)
        assert abs(_read(vrt2, 500000.5, 5200019.5) - 9.0) < 1e-4
        assert abs(_read(vrt2, 499990.5, 5200010.5) - 9.0) < 1e-4    # outside every tile
        meta_js = json.load(open(js, encoding="utf-8"))
        assert [p["project"] for p in meta_js["projects"]] == ["ND_Old_2019", "ND_New_C24"]
        assert meta_js["projects"][1]["collection_end"] == "2024-05-16"
        assert meta_js["fallback"] == fb and meta_js["dam_id"] == "DWR-1" and len(meta_js["tiles"]) == 3
        assert all(t["sha256"] for t in meta_js["tiles"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@register
def a_tile_in_another_utm_zone_is_warped_into_the_main_one():
    tmp = tempfile.mkdtemp()
    try:
        es = []
        for i, (val, x0, epsg) in enumerate(((1.0, 500000, 26914), (1.0, 500020, 26914), (5.0, 280000, 26913))):
            p = os.path.join(tmp, "USGS_1M_{}_x{}y1_P.tif".format(epsg - 26900, i))
            open(p, "wb").write(_tif_bytes(val, x0, 5200020, epsg=epsg))
            es.append({"tile": os.path.basename(p), "path": p, "project": "P"})
        vrt, js = lt.build_vrt(es, os.path.join(tmp, "z.vrt"))
        meta_js = json.load(open(js, encoding="utf-8"))
        assert meta_js["crs"] == "EPSG:26914" and len(meta_js["warped_helpers"]) == 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@register
def tile_folder_guards_never_the_pack_or_a_read_only_folder():
    tmp = tempfile.mkdtemp()
    try:
        suite = os.path.join(tmp, "suite")
        os.makedirs(os.path.join(suite, "ND_Dam_Suite_Pack"))
        os.makedirs(os.path.join(suite, "DEM"))                  # an existing DEM/ is used as it is
        out = os.path.join(tmp, "out")
        f, how, notes = lt.tile_folder("", suite, out)
        assert (f, how) == (os.path.join(suite, "DEM", "lidar_1m"), "suite") and not notes
        chosen = os.path.join(tmp, "mine")
        assert lt.tile_folder(chosen, suite, out)[:2] == (chosen, "chosen")
        # a chosen folder inside the pack is skipped (with a note), the suite folder is used
        f, how, notes = lt.tile_folder(os.path.join(suite, "ND_Dam_Suite_Pack", "x"), suite, out)
        assert how == "suite" and "offline pack" in notes[0]
        # the suite folder *is* a pack (format-1 manifest) -> the output folder
        pack = os.path.join(tmp, "packlike")
        os.makedirs(pack)
        json.dump({"format": "nd-dam-hazard-offline-pack"}, open(os.path.join(pack, "manifest.json"), "w"))
        f, how, notes = lt.tile_folder("", pack, out)
        assert (f, how) == (os.path.join(out, "lidar_1m"), "output") and "offline pack" in notes[0]
        # read-only
        ro = os.path.join(tmp, "ro")
        os.makedirs(ro)
        os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)
        try:
            if not os.access(ro, os.W_OK):                       # (root can write anyway)
                f, how, notes = lt.tile_folder(os.path.join(ro, "t"), "", out)
                assert how == "output" and "Can't write" in notes[0]
                try:
                    lt.tile_folder(os.path.join(ro, "t"), "", "")
                    raise AssertionError("no folder should be an error")
                except lt.TileError:
                    pass
        finally:
            os.chmod(ro, stat.S_IRWXU)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@register
def the_tile_cache_is_never_taken_for_your_own_dem():
    tmp = tempfile.mkdtemp()
    try:
        own = os.path.join(tmp, "dem", "my_county.tif")
        os.makedirs(os.path.join(tmp, "dem", "lidar_1m"))
        open(own, "wb").write(b"x")
        for n in ("USGS_1M_14_x1y1_A.tif", "USGS_1M_14_x2y1_A.tif", "some.vrt"):
            open(os.path.join(tmp, "dem", "lidar_1m", n), "wb").write(b"x")
        assert SD.local_dem(tmp) == own, SD.local_dem(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@register
def cancel_keeps_finished_tiles_and_the_part_of_the_current_one():
    tmp = tempfile.mkdtemp()
    try:
        names, files, items, meta = _world(tmp)
        http = FakeHttp(items, files, meta)
        plan = lt.make_plan((0, 0, 1, 1), tmp, http)
        first, second = plan["tiles"][0]["tile"], plan["tiles"][1]["tile"]
        fb = Feedback()
        http.cancel = (second, 50, fb)
        try:
            lt.fetch(plan, http, fb, sleep=lambda s: None)
            raise AssertionError("not cancelled")
        except lt.Cancelled:
            pass
        assert os.path.isfile(os.path.join(tmp, first))
        assert os.path.isfile(os.path.join(tmp, second + ".part")) and not os.path.isfile(os.path.join(tmp, second))
        m = lt.load_manifest(tmp)
        assert list(m["tiles"]) == [first]
        # next run: resumes the second from its .part
        plan2 = lt.make_plan((0, 0, 1, 1), tmp, http)
        part = os.path.getsize(os.path.join(tmp, second + ".part"))
        assert part >= 50
        lt.fetch(plan2, http, sleep=lambda s: None)
        assert (second, part) in http.ranges and open(os.path.join(tmp, second), "rb").read() == files[second]
        # cancelled during the TNM query
        fb2 = Feedback()
        fb2.cancel()
        try:
            lt.make_plan((0, 0, 1, 1), tmp, http, fb2)
            raise AssertionError("query not cancelled")
        except lt.Cancelled:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@register
def dam_folder_gives_the_runner_domain_else_the_dam_point():
    tmp = tempfile.mkdtemp()
    try:
        json.dump({"format": "nd_dam_suite/dam_record", "fields": {"dam_id": "DWR-2662", "lon": -97.44, "lat": 46.81}},
                  open(os.path.join(tmp, "dam.json"), "w"))
        box, did, how = lt.domain_of_dam_folder(tmp, 500, 2)
        assert did == "DWR-2662" and "dam point" in how and box[0] < -97.44 < box[2]
        os.makedirs(os.path.join(tmp, "_breach_model"))
        json.dump({"final_bounds": [-112120.0, 2648100.0, -95633.9, 2652190.0]},
                  open(os.path.join(tmp, "_breach_model", "state.json"), "w"))
        box, did, how = lt.domain_of_dam_folder(tmp, 500, 2)
        assert "Runner model bounds" in how and -97.6 < box[0] < box[2] < -97.0 and 46.7 < box[1] < box[3] < 46.95, box
        assert lt.parse_bbox("") is None and lt.parse_bbox("-97.5,46.7,-97.1,46.9") == (-97.5, 46.7, -97.1, 46.9)
        try:
            lt.parse_bbox("-97.1,46.7,-97.5,46.9")
            raise AssertionError("west > east accepted")
        except ValueError:
            pass
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@register
def download_dem_tool_lists_first_then_downloads_and_writes_the_vrt():
    from qgis.core import QgsProcessingContext
    from nd_dam_screening.algorithms import download_dem as mod
    tmp = tempfile.mkdtemp()
    old_http, old_suite = lt.UrlHttp, os.environ.get("NDSUITE_DATA")
    try:
        names, files, items, meta = _world(tmp)
        http = FakeHttp(items, files, meta)
        lt.UrlHttp = lambda *a, **k: http
        suite = os.path.join(tmp, "suite")
        os.makedirs(suite)
        os.environ["NDSUITE_DATA"] = suite
        out = os.path.join(tmp, "out")
        alg = mod.DownloadDem()
        alg.initAlgorithm()
        assert alg._params["SOURCE"].options[2] == "USGS 3DEP 1 m lidar tiles (native, TNM)"
        params = {"SOURCE": 2, "RES": 1, "SCOPE": 0, "DAM_POINT": "-97.25,46.95", "BUFFER_MI": 1.0,
                  "DAM_ID": "DWR-2801", "OUT": out, "COUNTY": 0}
        fb = Feedback()
        res = alg.processAlgorithm(dict(params), QgsProcessingContext(), fb)
        assert res["VRT"] == "" and http.ranges == [], http.ranges          # listed only
        assert any("Listed only" in m for m in fb.log) and any("free in" in m for m in fb.log)
        assert any("newest collection" in w for w in fb.warnings)
        # too big for the limit: refused
        params2 = dict(params, LIDAR_DOWNLOAD=True, LIDAR_MAX_GB=0.000000001)
        try:
            alg.processAlgorithm(params2, QgsProcessingContext(), Feedback())
            raise AssertionError("over the limit")
        except Exception as e:                                             # noqa
            assert "limit" in str(e), e
        # download
        fb = Feedback()
        res = alg.processAlgorithm(dict(params, LIDAR_DOWNLOAD=True), QgsProcessingContext(), fb)
        vrt = res["VRT"]
        assert vrt == os.path.join(out, "DWR-2801_1m_lidar.vrt") and os.path.isfile(vrt), fb.log
        tiles = os.path.join(suite, "dem", "lidar_1m")
        assert sorted(os.listdir(tiles)) == sorted([lt.MANIFEST, names["new_a"], names["old_b"]])
        assert os.path.isfile(os.path.join(out, "DWR-2801_1m_lidar.json"))
        side = open(os.path.join(out, "DWR-2801_1m_lidar_dem.txt"), encoding="utf-8").read()
        assert "USGS 3DEP 1 m lidar DEM tiles (native" in side and "EPSG:26914" in side
        # third run: everything present, nothing downloaded, VRT rebuilt
        http.ranges.clear()
        res = alg.processAlgorithm(dict(params), QgsProcessingContext(), Feedback())
        assert http.ranges == [] and res["VRT"] == vrt
        # the state is refused
        try:
            alg.processAlgorithm(dict(params, SCOPE=2, DAM_POINT=""), QgsProcessingContext(), Feedback())
            raise AssertionError("state accepted")
        except Exception as e:                                             # noqa
            assert "whole state" in str(e)
    finally:
        lt.UrlHttp = old_http
        if old_suite is None:
            os.environ.pop("NDSUITE_DATA", None)
        else:
            os.environ["NDSUITE_DATA"] = old_suite
        shutil.rmtree(tmp, ignore_errors=True)


@register
def nothing_downloads_on_open():
    """The window and the plugin only add the choice; the tool lists before it downloads."""
    root = os.path.dirname(HERE)
    for name in ("plugin.py", "dialog.py", "provider.py", "__init__.py"):
        src = open(os.path.join(root, name), encoding="utf-8").read()
        assert "lidar_tnm" not in src and "tnmaccess" not in src, name
    from nd_dam_screening.algorithms import download_dem as mod
    alg = mod.DownloadDem()
    alg.initAlgorithm()
    assert alg._params["LIDAR_DOWNLOAD"].defaultValue is False


def main():
    import traceback
    failed = 0
    for t in TESTS:
        try:
            t()
            print("ok    ", t.__name__)
        except Exception:                                    # noqa: BLE001
            failed += 1
            print("FAIL  ", t.__name__)
            traceback.print_exc()
    print("\n{} tests, {} failed".format(len(TESTS), failed))
    return 1 if failed else 0


def _pytest_names():
    for t in TESTS:
        globals()["test_" + t.__name__] = t


_pytest_names()

if __name__ == "__main__":
    sys.exit(main())
