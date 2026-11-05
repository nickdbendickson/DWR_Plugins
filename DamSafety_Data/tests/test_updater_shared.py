"""Suite 1.18.8: ONE data updater engine and source list for both suites. DamSafety_Data's
core.py / nsi_merge.py are shims: after the import they ARE damsuite/updatecore.py /
nsi_merge.py, the same modules the ND Data Updater uses; sources.py takes the list from
damsuite/updatesources.py (the ND Updater adds its offline pack source to the same list).

    python3 -m pytest DamSafety_Data/tests/test_updater_shared.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from DamSafety_Data import core                      # noqa: E402
from DamSafety_Data import nsi_merge                 # noqa: E402
from DamSafety_Data import sources                   # noqa: E402
from DamSafety_Data.damsuite import updatecore as UC  # noqa: E402
from DamSafety_Data.damsuite import updatesources as US  # noqa: E402

ALL = ["nsi", "microsoft", "osm", "osm_layers", "usa_structures", "overture", "address_points",
       "nsi_generator"]


def test_the_shims_are_the_shared_modules():
    from DamSafety_Data.damsuite import nsi_merge as NM
    assert core is UC and nsi_merge is NM


def test_one_source_list_with_the_same_defaults():
    # 1.7.0: DamSafety Data adds its own offline pack source, in the same place as the ND one
    assert list(sources.SOURCE_INFO) == ALL[:-1] + ["pack", "nsi_generator"]
    assert {k: v for k, v in sources.SOURCE_INFO.items() if k != "pack"} == US.SOURCE_INFO
    on = {k for k, v in US.SOURCE_INFO.items() if v[2]}
    assert on == {"nsi", "microsoft", "osm", "osm_layers", "address_points"}
    nd = US.source_info(("pack", "the ND pack", True))
    assert list(nd) == ALL[:-1] + ["pack", "nsi_generator"]


def test_build_sources_makes_every_enabled_source():
    got = {s.id: s for s in sources.build_sources(
        {"enabled": {k: True for k in ALL}, "usa_structures_url": "https://x.test/ND.zip"})}
    assert list(got) == ALL
    assert isinstance(got["osm_layers"], core.OsmLayersSource)
    assert got["osm"].version_mode == "md5"                  # the ND Updater's checksum check
    assert isinstance(got["overture"], core.OvertureSource)
    assert isinstance(got["address_points"], core.ArcGISFeatureSource)
    assert got["address_points"].url == US.ADDRESS_POINTS_URL
    # the defaults: Overture and nsi-generator off, USA Structures needs a link
    assert [s.id for s in sources.build_sources({})] == [
        "nsi", "microsoft", "osm", "osm_layers", "address_points"]


def test_pack_source_goes_before_nsi_generator():
    made = []

    def pack(config, info):
        made.append(info["pack"][0])
        return core.Source.__new__(core.Source)
    info = US.source_info(("ND pack", "x", True))
    got = US.build_sources({"enabled": {"nsi_generator": True}}, pack=pack, info=info)
    assert made == ["ND pack"] and isinstance(got[-2], core.Source)
    assert got[-1].id == "nsi_generator"


def test_feedback_status_and_the_lock_says_why():
    seen = []
    fb = core.Feedback(status=seen.append)
    fb.status("layer 1 of 2")
    assert seen == ["layer 1 of 2"]
    d = tempfile.mkdtemp()
    up = core.Updater(d, [], http=None)
    path, why = up._acquire_lock()
    assert path and why == ""
    with open(path) as f:
        pid = f.read().split()[0]
    assert pid == str(os.getpid())
    # a fresh lock of this same process is a live run: refused, not stolen (M23)
    again, why = up._acquire_lock()
    assert again is None and "already running" in why, why
    os.remove(path)


def test_in_place_sources_are_not_staged():
    class InPlace(core.Source):
        id, name, folder, in_place, config_key = "p", "Pack", "pk", True, None
        min_check_interval_hours = 0

        def update(self, http, target, feedback, force, entry):
            os.makedirs(target, exist_ok=True)
            return "updated", "built", "v1"
    d = tempfile.mkdtemp()
    res = core.Updater(d, [InPlace()], http=None).check_and_update()
    assert [r.status for r in res] == ["updated"] and os.path.isdir(os.path.join(d, "pk"))
    assert core.Updater(d, [], http=None).load_manifest()["p"]["version"] == "v1"


def test_osm_layers_build_from_an_osm_file_without_gdal_exceptions():
    """The Geofabrik OSM pack layers (damsuite/osm_layers.py, now in DamSafety Data too): read
    with GDAL's OSM driver, no process-wide gdal.UseExceptions(), each layer its own GeoPackage."""
    import pytest
    gdal = pytest.importorskip("osgeo.gdal")
    from DamSafety_Data.damsuite import osm_layers as OL
    d = tempfile.mkdtemp()
    osm = os.path.join(d, "t.osm")
    with open(osm, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6">\n'
                '<node id="1" lat="46.8" lon="-100.8" version="1"><tag k="amenity" '
                'v="hospital"/><tag k="name" v="St A"/></node>\n'
                '<node id="2" lat="46.81" lon="-100.81" version="1"><tag k="place" '
                'v="town"/><tag k="name" v="Townsville"/></node>\n</osm>\n')
    before = gdal.GetUseExceptions()
    counts = OL.build_layers(osm, os.path.join(d, "out"))
    assert gdal.GetUseExceptions() == before
    assert counts.get("OSM_lifelines") == 1 and counts.get("OSM_places") == 1, counts
    assert os.path.isfile(os.path.join(d, "out", "OSM_lifelines.gpkg"))
