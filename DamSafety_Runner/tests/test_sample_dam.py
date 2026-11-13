"""Core 1.2.0 / Data 1.11.0: the suite's sample dam (damsuite.sample_dam).

* The offline pack carries one ready-made dam: its record (dam.json) and 10 m terrain named as
  'Download DEM' names it, so the Runner finds the terrain in the dam folder by itself.
* "Try the sample dam" (Dam suite home) copies it to the Dams folder, or downloads the terrain
  itself when the pack has none; a user's own record or terrain there is never replaced.
* The terrain is checked before it is kept: the sample's grid, plausible ground in metres, no
  holes. A wrong answer from the elevation service is refused, never written.

The elevation service is a double here (GeoTIFF parts made with GDAL); the tests that need GDAL
skip on a plain Python."""
import json
import os
import shutil
import tempfile
import urllib.parse

from nd_breach_runner import wizard_logic as wl
from nd_breach_runner.damsuite import dam_record as DR
from nd_breach_runner.damsuite import sample_dam as SD
from nd_breach_runner.tests.registry import need_osgeo, test

TINY = "T-SAMPLE"


class _Tiny:
    """A small sample (250 x 130 cells, asked in parts of 100) in place of the real one, and no
    waiting between download tries."""

    def __enter__(self):
        self.keep = (SD.TILE_PX, SD.RETRY_WAIT_S, SD.MIN_DEM_BYTES)
        SD.TILE_PX, SD.RETRY_WAIT_S, SD.MIN_DEM_BYTES = 100, 0.0, 1000
        SD.SAMPLES[TINY] = SD.Sample(
            dam_id=TINY, name="Tiny Test Dam",
            fields={"dam_id": TINY, "name": "Tiny Test Dam", "lon": -100.0, "lat": 47.0,
                    "x": 1200.0, "y": 5700.0, "crs": "EPSG:5070", "height_ft": 20.0,
                    "storage_acft": 100.0},
            box=(1000.0, 5000.0, 3500.0, 6300.0), z_range_m=(200.0, 500.0),
            about="a test dam.", note="test sample")
        self.tmp = tempfile.mkdtemp(prefix="sample_dam_")
        return self.tmp

    def __exit__(self, *exc):
        SD.TILE_PX, SD.RETRY_WAIT_S, SD.MIN_DEM_BYTES = self.keep
        SD.SAMPLES.pop(TINY, None)
        shutil.rmtree(self.tmp, ignore_errors=True)


def _ground(x, y):
    """The test terrain: a plane, so a part put in the wrong place shows."""
    return 300.0 + 0.01 * (x - 1000.0) + 0.02 * (y - 5000.0)


def _service(calls=None, scale=1.0, hole=False):
    """A double of the elevation service: the GeoTIFF for the box and size the URL asks."""
    import numpy as np
    from osgeo import gdal, osr

    def fetch(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        x0, y0, x1, y1 = (float(v) for v in q["bbox"][0].split(","))
        w, h = (int(v) for v in q["size"][0].split(","))
        assert q["bboxSR"] == ["5070"] and q["imageSR"] == ["5070"] and q["format"] == ["tiff"]
        assert q["pixelType"] == ["F32"]
        if calls is not None:
            calls.append((x0, y0, x1, y1, w, h))
        cx = x0 + (np.arange(w) + 0.5) * (x1 - x0) / w
        cy = y1 - (np.arange(h) + 0.5) * (y1 - y0) / h
        a = (_ground(cx[None, :], cy[:, None]) * scale).astype("float32")
        name = "/vsimem/sample_part_{}.tif".format(len(calls or []))
        ds = gdal.GetDriverByName("GTiff").Create(name, w, h, 1, gdal.GDT_Float32)
        ds.SetGeoTransform((x0, (x1 - x0) / w, 0.0, y1, 0.0, -(y1 - y0) / h))
        sr = osr.SpatialReference()
        sr.ImportFromEPSG(5070)
        ds.SetProjection(sr.ExportToWkt())
        if hole:
            a[: h // 2, :] = -9999.0
            ds.GetRasterBand(1).SetNoDataValue(-9999.0)
        ds.GetRasterBand(1).WriteArray(a)
        ds = None
        f = gdal.VSIFOpenL(name, "rb")
        gdal.VSIFSeekL(f, 0, 2)
        n = gdal.VSIFTellL(f)
        gdal.VSIFSeekL(f, 0, 0)
        raw = bytes(gdal.VSIFReadL(1, n, f))
        gdal.VSIFCloseL(f)
        gdal.Unlink(name)
        return raw
    return fetch


@test
def sample_dam_table_is_complete_and_the_dam_is_inside_its_terrain():
    assert SD.DEFAULT in SD.SAMPLES
    for sid, sm in SD.SAMPLES.items():
        f = sm.fields
        assert sm.dam_id == sid == f["dam_id"] and sm.name == f["name"]
        for key in ("lon", "lat", "x", "y", "crs", "height_ft", "storage_acft",
                    "top_of_dam_ft", "top_of_dam_datum"):
            assert f.get(key) not in (None, ""), (sid, key)
        assert f["crs"] == "EPSG:5070"
        unknown = [k for k in f if k not in DR.FIELDS]
        assert not unknown, unknown                       # dam.json would refuse them
        x0, y0, x1, y1 = sm.box
        assert x0 + 3000 < f["x"] < x1 - 3000 and y0 + 3000 < f["y"] < y1 - 3000, sid
        for a, b in ((x0, x1), (y0, y1)):                 # whole 10 m cells on the 10 m grid
            assert a % SD.CELL_M == 0 and b % SD.CELL_M == 0 and b > a
        nx, ny = SD.grid(sid)
        assert nx * SD.CELL_M == x1 - x0 and ny * SD.CELL_M == y1 - y0
        top_m = f["top_of_dam_ft"] * 0.3048
        assert sm.z_range_m[0] < top_m < sm.z_range_m[1], "top of dam inside the ground range"
        assert 1 <= SD.download_mb(sid) <= 60, "a download a new user will wait for"
        assert "determination" in sm.note and sm.about.endswith(".")
    # the name is what 'Download DEM' writes, so the Runner's own lookup finds it
    assert SD.dem_name().endswith(wl.DEM_SUFFIXES) and SD.dem_name().startswith(SD.DEFAULT)


@test
def sample_dam_parts_cover_the_terrain_once():
    for sid in SD.SAMPLES:
        nx, ny = SD.grid(sid)
        x0, y0, x1, y1 = SD.get(sid).box
        parts = SD.tiles(sid)
        assert sum(t[4] * t[5] for t in parts) == nx * ny
        assert min(t[0] for t in parts) == x0 and max(t[2] for t in parts) == x1
        assert min(t[1] for t in parts) == y0 and max(t[3] for t in parts) == y1
        for t in parts:
            assert 0 < t[4] <= SD.TILE_PX and 0 < t[5] <= SD.TILE_PX
            assert abs((t[2] - t[0]) / t[4] - SD.CELL_M) < 1e-9
            assert abs((t[3] - t[1]) / t[5] - SD.CELL_M) < 1e-9
    url = SD.tile_url(SD.tiles()[0])
    assert url.startswith(SD.SERVICE + "/exportImage?") and "f=image" in url
    assert "interpolation=RSP_BilinearInterpolation" in url
    try:
        SD.get("NO-SUCH")
        raise AssertionError("an unknown sample must be refused")
    except SD.SampleError as e:
        assert "NO-SUCH" in str(e)


@test
def sample_dam_record_is_written_once_and_a_users_record_is_kept():
    d = tempfile.mkdtemp(prefix="sample_rec_")
    try:
        assert SD.write_record(d) is True
        rec = DR.load(d)
        sm = SD.get()
        for k, v in sm.fields.items():
            assert rec.get(k) == v, k
        raw = json.load(open(os.path.join(d, DR.FILE_NAME), encoding="utf-8"))
        src = raw["sources"]["height_ft"]
        assert src["tool"] == SD.TOOL and "not a determination" in src["note"]
        # a user changed a value: trying the sample again leaves the record alone
        rec.set("height_ft", 31.0, "me", "1", "surveyed", overwrite=True)
        rec.save()
        assert SD.write_record(d) is False
        assert DR.load(d).get("height_ft") == 31.0
        assert SD.write_record(d, overwrite=True) is True
        assert DR.load(d).get("height_ft") == sm.fields["height_ft"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def sample_dam_is_built_from_the_service_joined_checked_and_tagged_metres():
    need_osgeo()
    from osgeo import gdal
    with _Tiny() as root:
        calls, log = [], []
        folder = SD.build(root, TINY, fetch=_service(calls), log=log.append)
        assert folder == os.path.join(root, SD.FOLDER, TINY) == SD.find(root, sample_id=TINY)
        assert len(calls) == 6 and len(calls) == len(SD.tiles(TINY))
        assert sorted(os.listdir(folder)) == sorted(
            [SD.README_NAME, DR.FILE_NAME, SD.INFO_NAME, SD.dem_name(TINY)]), os.listdir(folder)
        ds = gdal.Open(os.path.join(folder, SD.dem_name(TINY)))
        assert (ds.RasterXSize, ds.RasterYSize) == (250, 130)
        gt = ds.GetGeoTransform()
        assert (gt[0], gt[1], gt[3], gt[5]) == (1000.0, 10.0, 6300.0, -10.0)
        band = ds.GetRasterBand(1)
        assert band.GetUnitType() == "metre"              # 'Detect from the raster' reads this
        assert "5070" in (ds.GetSpatialRef().GetAuthorityCode(None) or "")
        a = band.ReadAsArray()
        for row, col in ((0, 0), (129, 249), (64, 99), (64, 100), (99, 180), (100, 180)):
            want = _ground(1000.0 + (col + 0.5) * 10.0, 6300.0 - (row + 0.5) * 10.0)
            assert abs(float(a[row, col]) - want) < 1e-3, (row, col)     # every part in its place
        info = json.load(open(os.path.join(folder, SD.INFO_NAME), encoding="utf-8"))
        assert info["dam_id"] == TINY and info["terrain"]["units"] == "metres"
        assert "USGS" in info["terrain"]["source"]
        assert "not a hazard determination" in open(os.path.join(folder, SD.README_NAME)).read()
        assert DR.load(folder).get("name") == "Tiny Test Dam"
        # a second build downloads nothing
        assert SD.build(root, TINY, fetch=_service(calls)) == folder and len(calls) == 6
        assert any("already there" in m for m in [SD.build(root, TINY, log=log.append)] + log)
        # refresh downloads it again
        SD.build(root, TINY, fetch=_service(calls), refresh=True)
        assert len(calls) == 12


@test
def sample_dam_terrain_that_is_wrong_is_refused_not_written():
    need_osgeo()
    for kind, kw, word in (("feet, not metres", {"scale": 3.28084}, "outside the ground range"),
                           ("holes", {"hole": True}, "")):
        with _Tiny() as root:
            try:
                SD.build(root, TINY, fetch=_service([], **kw))
                raise AssertionError("terrain with {} must be refused".format(kind))
            except SD.SampleError as e:
                assert "not usable" in str(e) and word in str(e), str(e)
            folder = SD.sample_folder(root, TINY)
            assert not SD.complete(folder, TINY) and SD.find(root, sample_id=TINY) == ""
            assert not os.path.isfile(os.path.join(folder, SD.dem_name(TINY)))
            assert not os.path.isfile(os.path.join(folder, DR.FILE_NAME))
    # an error page, or no network: tried RETRIES times, then a plain message
    with _Tiny() as root:
        tries = []

        def html(url):
            tries.append(url)
            return b"<html>Service unavailable</html>"
        try:
            SD.build(root, TINY, fetch=html)
            raise AssertionError("an error page must be refused")
        except SD.SampleError as e:
            assert "could not be downloaded" in str(e) and "internet" in str(e), str(e)
        assert len(tries) == SD.RETRIES

        def down(url):
            raise OSError("network is unreachable")
        try:
            SD.build(root, TINY, fetch=down)
            raise AssertionError
        except SD.SampleError as e:
            assert "network is unreachable" in str(e)


@test
def sample_dam_download_goes_on_from_the_parts_it_has():
    need_osgeo()
    with _Tiny() as root:
        calls, good = [], _service([])

        def flaky(url):
            calls.append(url)
            if len(calls) > 4:                    # the connection drops after four parts
                raise OSError("connection reset")
            return good(url)
        try:
            SD.build(root, TINY, fetch=flaky)
            raise AssertionError
        except SD.SampleError:
            pass
        assert len(calls) == 4 + SD.RETRIES
        again = []
        SD.build(root, TINY, fetch=_service(again))
        assert len(again) == 2, "only the two missing parts are asked again"
        assert SD.complete(SD.sample_folder(root, TINY), TINY)
        assert not os.path.isdir(os.path.join(SD.sample_folder(root, TINY), "_sample_download"))
        # cancel stops before the next part
        other = tempfile.mkdtemp(prefix="sample_cancel_")
        try:
            n = []
            try:
                SD.build(other, TINY, fetch=_service(n), cancel=lambda: len(n) >= 2)
                raise AssertionError
            except SD.SampleError as e:
                assert str(e) == "cancelled"
            assert len(n) == 2
        finally:
            shutil.rmtree(other, ignore_errors=True)


@test
def sample_dam_installs_into_the_dams_folder_where_the_runner_finds_its_terrain():
    need_osgeo()
    with _Tiny() as root:
        src = SD.build(root, TINY, fetch=_service([]))
        dams = os.path.join(root, "Dams")
        os.makedirs(dams)
        assert SD.installed(dams, TINY) == "" and SD.installed("", TINY) == ""
        done = SD.install(src, dams, TINY)
        target = os.path.join(dams, TINY)
        assert done == {"folder": target, "dem": os.path.join(target, SD.dem_name(TINY)),
                        "record": "written", "dem_copied": True}
        assert SD.installed(dams, TINY) == target
        # the Runner wizard's own lookup of a dam's terrain
        assert wl.dam_dem_candidates(dams, TINY) == [done["dem"]]
        assert DR.load(target).get("storage_acft") == 100.0
        # the user works on it, then tries the sample again: nothing of theirs is replaced
        rec = DR.load(target)
        rec.set("storage_acft", 140.0, "me", "1", "my survey", overwrite=True)
        rec.save()
        again = SD.install(src, dams, TINY)
        assert again["record"] == "kept" and again["dem_copied"] is False
        assert DR.load(target).get("storage_acft") == 140.0
        # the pack sample is never changed by an install
        assert DR.load(src).get("storage_acft") == 100.0
        for bad_dams in ("", "   "):
            try:
                SD.install(src, bad_dams, TINY)
                raise AssertionError
            except SD.SampleError as e:
                assert "Dams folder" in str(e)
        try:
            SD.install(os.path.join(root, "nothing"), dams, TINY)
            raise AssertionError
        except SD.SampleError as e:
            assert "not complete" in str(e)


@test
def sample_dam_can_be_built_straight_into_a_dam_folder_in_the_background():
    need_osgeo()
    with _Tiny() as root:
        dams = os.path.join(root, "Dams")
        target = os.path.join(dams, TINY)
        # a dam folder the user already made a record in: the terrain is added, the record kept
        os.makedirs(target)
        rec = DR.load(target, create=True)
        rec.set_many({"dam_id": TINY, "height_ft": 22.0}, "me", "1", "mine", overwrite=True)
        rec.save()
        job = SD.Job(folder=target, sample_id=TINY, fetch=_service([])).start()
        assert job.wait(60) and job.error == "" and job.folder == target, job.error
        assert job.messages and "part 6 of 6" in " ".join(job.messages)
        assert SD.installed(dams, TINY) == target
        assert DR.load(target).get("height_ft") == 22.0
        assert wl.dam_dem_candidates(dams, TINY) == [os.path.join(target, SD.dem_name(TINY))]

        # a failure is reported in words, and nothing half-made is listed as a dam
        other = os.path.join(root, "Dams2")

        def down(url):
            raise OSError("no route to host")
        job = SD.Job(folder=os.path.join(other, TINY), sample_id=TINY, fetch=down).start()
        assert job.wait(60) and "no route to host" in job.error and job.folder == ""
        assert SD.installed(other, TINY) == ""
        assert not os.path.isfile(os.path.join(other, TINY, DR.FILE_NAME))
        job = SD.Job(folder=os.path.join(other, TINY), sample_id=TINY, fetch=_service([]))
        job.cancel()
        job.start()
        assert job.wait(60) and job.error == "cancelled" and job.cancelled


@test
def sample_dam_is_looked_for_in_the_pack_and_beside_it():
    need_osgeo()
    with _Tiny() as root:
        pack = os.path.join(root, "data", "ND_Dam_Suite_Pack")
        src = SD.build(pack, TINY, fetch=_service([]))
        assert SD.find("", None, os.path.join(root, "x"), pack, sample_id=TINY) == src
        assert SD.find(os.path.join(root, "data"), sample_id=TINY) == ""
        assert SD.find(sample_id=TINY) == ""
        os.remove(os.path.join(src, DR.FILE_NAME))
        assert SD.find(pack, sample_id=TINY) == "", "a sample without its record is not used"
    assert isinstance(SD.pack_roots(), list)


@test
def sample_dam_record_gives_the_runner_its_inputs_as_given():
    """The Runner fills its dam inputs from the sample's dam.json and uses the top of dam as it
    is: datum "DEM" is neither a local datum (not used) nor an earlier run's estimate (estimated
    again), and no NGVD29 shift is applied."""
    from nd_breach_runner.core import damjson
    from nd_breach_runner.damsuite import vdatum as VD
    d = tempfile.mkdtemp(prefix="sample_run_")
    try:
        SD.write_record(d)
        rec = damjson.load(d)[0]
        f = SD.get().fields
        vals, _notes, used = damjson.fill({p: None for p, _f, _l in damjson.PARAM_FIELDS}, rec)
        assert vals["HEIGHT_FT"] == f["height_ft"] == 27.5
        assert vals["STORAGE_ACFT"] == f["storage_acft"] and vals["TOP_FT"] == f["top_of_dam_ft"]
        assert vals["AREA_AC"] == 154.5, "the DWR maximum-pool area, as the reference runs use"
        assert used["TOP_FT"] == "top_of_dam_ft"
        assert not damjson.local_datum(rec) and not damjson.dem_estimate(rec)
        plan = VD.plan(f["lon"], f["lat"], stated=rec.get("top_of_dam_datum"))
        assert plan["applied"] is False and plan["shift_ft"] == 0.0 and not plan["warnings"]
        point = damjson.point_from_record(rec)[0]
        assert point == ("xy", f["x"], f["y"], "EPSG:5070")
    finally:
        shutil.rmtree(d, ignore_errors=True)
