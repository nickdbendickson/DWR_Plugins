"""Data 1.5.2: the format-1 pack gets the NLCD land cover, and a format-1 zip.

    python -m pytest -q DamSafety_Data/tests/test_pack_build_v1_nlcd.py   (from the repository root)

Needs GDAL (osgeo); skipped without it. Every download mocked (test_pack_build's fixture).
Checks: pack_build.nlcd_format1 copies the format-2 NLCD GeoPackage into nd_offline_pack/landcover/
and lists it in the format-1 manifest "rasters" (a second run keeps the copy; without the raster
the manifest names it as not included); verify() fails when the format-1 copy is missing or
different; zip_format1 writes nd_offline_pack/{sqlite, rasters, manifest.json last}, refuses a zip
inside the pack and a missing raster; offline_pack unpacks that zip with the raster, and the
suite's lookups (damsuite.suitedata, Setup's suite_fill) find DWR, NID and NLCD in format 1."""
import json
import os
import shutil
import zipfile

import pytest

pytest.importorskip("osgeo.gdal")

from DamSafety_Data import offline_pack as OP  # noqa: E402
from DamSafety_Data import pack_build as PB  # noqa: E402
from DamSafety_Data.tests.test_pack_build import _build, mocked  # noqa: E402,F401

NLCD = "NLCD_2021_landcover"
REL = "landcover/NLCD_2021_landcover.gpkg"


@pytest.fixture
def built(tmp_path, mocked):  # noqa: F811
    _calls, nlcd = mocked
    out = tmp_path / "out"
    out.mkdir()
    r, _ = _build(out, nlcd, only=["DWR_dams", "NID_dams", "ND_counties", NLCD])
    assert not r["failed"], r["failed"]
    return out, out / PB.PACK1, out / PB.PACK2


def _man(p):
    return json.load(open(str(p / "manifest.json")))


def test_format1_holds_nlcd(built):
    out, p1, p2 = built
    m1, m2 = _man(p1), _man(p2)
    assert m1["rasters"][NLCD]["file"] == REL and "rasters_not_included" not in m1
    assert m1["rasters"][NLCD]["sha256"] == m2["rasters"][NLCD]["sha256"]
    f1 = p1 / "landcover" / "NLCD_2021_landcover.gpkg"
    assert f1.read_bytes() == (p2 / "landcover" / "NLCD_2021_landcover.gpkg").read_bytes()
    ok, lines = PB.verify(str(out))
    assert ok and any("the same 1 raster" in ln for ln in lines), lines
    # a second run keeps the copy (same size and sha256)
    t = os.path.getmtime(str(f1))
    logs = []
    PB.nlcd_format1(str(p1), str(p2), m1, m2, logs.append)
    assert os.path.getmtime(str(f1)) == t and not any("copied" in ln for ln in logs), logs
    # verify: a different or missing copy fails
    f1.write_bytes(b"not the raster")
    ok, lines = PB.verify(str(out))
    assert not ok and any(NLCD in ln and "not in format 1" in ln for ln in lines), lines
    f1.unlink()
    assert not PB.verify(str(out), deep=False)[0]
    # re-copied from format 2
    m1 = PB.nlcd_format1(str(p1), str(p2), _man(p1), m2, logs.append)
    assert f1.is_file() and PB.verify(str(out))[0]
    # without the raster in format 2 the format-1 manifest says so
    m1b = PB.nlcd_format1(str(p1), str(p2), m1, dict(m2, rasters={}))
    assert NLCD not in m1b["rasters"] and NLCD in m1b["rasters_not_included"]
    assert _man(p1) == m1b


def test_zip_format1_and_lookups(built, tmp_path, monkeypatch):
    out, p1, p2 = built
    with pytest.raises(PB.PackBuildError):
        PB.zip_format1(str(p1), str(p1 / "x.zip"))
    zp = PB.zip_format1(str(p1), str(tmp_path / "nd_offline_pack.zip"))
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
        assert names[-1] == "nd_offline_pack/manifest.json", names
        assert "nd_offline_pack/" + OP.DB_NAME in names and "nd_offline_pack/" + REL in names
        assert z.getinfo("nd_offline_pack/" + REL).compress_type == zipfile.ZIP_STORED
    # unpacked by the plugins' reader, the raster with it
    got = OP._unzip(zp, lambda m: None)
    here = os.path.dirname(got)
    assert os.path.basename(got) == OP.DB_NAME and os.path.isfile(os.path.join(here, *REL.split("/")))
    assert OP.open_pack(got).has("DWR_dams")
    # the suite's lookups on format 1: DWR, NID, NLCD (data sets written to the cache)
    from DamSafety_Data.damsuite import suitedata as SD
    monkeypatch.setenv(SD.ENV_PACK_CACHE, str(tmp_path / "cache"))
    for pk in (str(p1), here):
        dams = SD.pack_file("DWR_dams", pk)
        assert dams.startswith(str(tmp_path / "cache")) and SD.pack_file("NID_dams", pk)
        assert SD.nlcd_raster(pk) == os.path.join(pk, *REL.split("/"))
    from DamSafety_Setup import suite_fill as SF
    v1 = SF._pack_dwr({"pack": str(p1)}, [])
    assert v1["dwr"].endswith("|layername=DWR_dams") and v1["nid"].endswith("|layername=NID_dams")
    # a missing raster: no zip
    os.remove(os.path.join(str(p1), *REL.split("/")))
    with pytest.raises(PB.PackBuildError):
        PB.zip_format1(str(p1), str(tmp_path / "again.zip"))
    shutil.rmtree(here, ignore_errors=True)
