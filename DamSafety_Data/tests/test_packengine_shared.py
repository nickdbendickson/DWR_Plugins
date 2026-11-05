"""Suite 1.18.6: ONE offline pack builder for both suites (damsuite/packengine.py). The plugins'
build_nd_pack.py / dwr_files.py are shims: after the import they ARE the shared modules, bound
to the plugin's constants and osm_parse. The DamSafety layer list is unchanged; the ND list is
there too (picked by constants.PACK_LAYERS).

    python3 DamSafety_Data/tests/test_packengine_shared.py
"""
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from DamSafety_Data import build_nd_pack as BP          # noqa: E402
from DamSafety_Data import dwr_files as DFS             # noqa: E402
from DamSafety_Data import constants as C               # noqa: E402
from DamSafety_Data import osm_parse as O               # noqa: E402
from DamSafety_Data.damsuite import packengine as E     # noqa: E402
from DamSafety_Data.damsuite import dwrfiles as DF      # noqa: E402

TESTS = []


def case(f):
    TESTS.append(f)
    return f


# the DamSafety layer list before 1.18.6 (build_nd_pack 1.3), in order
DWR_NAMES = ["NSI_structures", "NDGISHUB_address_points", "USA_Structures", "NID_dams",
             "NARN_rail_lines", "FRA_grade_crossings", "ND_roads_NG911", "HPMS_ND",
             "NDDOT_traffic_counts", "ND_county_roads", "lifeline_nd_hospitals",
             "lifeline_nd_skilled_nursing", "lifeline_nd_basic_care",
             "lifeline_nd_assisted_living", "lifeline_eia_power_plants", "NBI_bridges",
             "NHD_flowlines", "NHD_waterbodies", "OSM_lifelines", "OSM_crossings",
             "OSM_campgrounds", "OSM_trailer_parks", "OSM_places"]
# packbuild 1.1 (suite 1.18.7): the layers both suites hold the same way, appended to each list
SHARED_NAMES = ["ABI_dams", "ABI_small_barriers_surveyed", "ABI_road_crossings_unsurveyed",
                "USGS_stream_crossings", "NDDEQ_landfills",
                "NDDEQ_source_water_protection_community",
                "NDDEQ_source_water_protection_noncommunity"]


@case
def test_the_shims_are_the_shared_modules():
    assert BP is E, BP
    assert DFS is DF
    assert E.C.__dict__["_mod"] is C and E.O.__dict__["_mod"] is O
    assert E.pack_layers() == "dwr" == C.PACK_LAYERS
    # a test or the data-source file that changes a constant reaches the engine at call time
    old = C.NID_URL
    try:
        C.NID_URL = "https://example.org/nid/FeatureServer/0"
        spec = dict((t[0], t[4]) for t in E.datasets())["NID_dams"]
        assert spec["urls"][0] == "https://example.org/nid/FeatureServer/0", spec
    finally:
        C.NID_URL = old


@case
def test_dwr_layer_list_is_unchanged():
    names = [t[0] for t in E.datasets()]
    assert names == DWR_NAMES + SHARED_NAMES, names
    assert not [n for n in names if n.startswith(("3DHP_", "SARP_"))], names
    # the old group names still pick (no ND aliases here: nhd stays nhd)
    table = E.datasets()
    assert E._pick("nhd", table) == {"nhd"}
    assert E._pick("NHD_flowlines", table) == {"NHD_flowlines"}


@case
def test_nd_layer_list_is_there_too():
    """The ND list reads the ND constants (3DHP): with a stand-in that has them it builds."""
    class NDC(object):
        def __getattr__(self, k):
            return getattr(C, k) if hasattr(C, k) else "https://example.org/{}".format(k)
    NDC.PACK_LAYERS = "nd"
    real = E.C.__dict__["_mod"]
    E.C.__dict__["_mod"] = NDC()
    try:
        assert E.pack_layers() == "nd"
        nd = [t[0] for t in E.datasets()]
        assert E._pick("nhd", E.datasets()) == {"hydro"}       # the ND group aliases
    finally:
        E.C.__dict__["_mod"] = real
    for n in ["3DHP_flowlines", "3DHP_waterbodies"] + SHARED_NAMES:
        assert n in nd, (n, nd)
    assert not [n for n in nd if n.startswith("SARP_")], nd


@case
def test_one_barrier_list_for_both_suites():
    """packbuild 1.1: the barrier and ND DEQ layers are the same entries in both lists; the old
    ND names (SARP_*) answer to the ABI_* layers in the pack readers and in --only."""
    from DamSafety_Data.damsuite import hydro3dhp as H
    from DamSafety_Data.damsuite import abi as A
    shared = E.shared_datasets()
    assert [t[0] for t in shared] == SHARED_NAMES
    dwr = {t[0]: t for t in E.datasets(layers="dwr")}
    for t in shared:
        assert dwr[t[0]] == t
    kinds = {t[0]: (t[2], t[3]) for t in shared}
    assert kinds["ABI_dams"] == ("barriers", "abi")
    assert kinds["NDDEQ_landfills"] == ("nddeq", "arcgis")
    assert A.OLD_NAMES == {"SARP_dams": "ABI_dams",
                           "SARP_surveyed_crossings": "ABI_small_barriers_surveyed",
                           "SARP_unsurveyed_crossings": "ABI_road_crossings_unsurveyed"}
    for old, new in A.OLD_NAMES.items():
        assert H.real_name(old, {new}) == new and H.real_name(new, {old}) == old
    assert H.real_name("3DHP_flowlines", {"NHD_flowlines"}) == "NHD_flowlines"
    # --only with an old name (the ND list's aliases)
    assert E._GROUP_ALIASES["SARP_dams"] == "ABI_dams"
    assert E._signature("abi", {"mode": "zip", "kind": "road_crossings"}, None) == \
        "abi|zip|road_crossings"


@case
def test_fetch_abi_writes_points():
    """kind "abi": the public API CSV through the engine's resumable download."""
    import tempfile
    from DamSafety_Data import offline_pack as OP
    d = tempfile.mkdtemp()
    w = OP.PackWriter(os.path.join(d, "work.sqlite"), (-104.1, 45.9, -96.5, 49.1))
    got = []

    def fake_dl(url, dest, max_age=None, refresh=False, beat=None):
        got.append(url)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w") as f:
            if url.endswith("/metadata"):
                f.write('{"data_version": "4.4.0"}')
            else:
                f.write('"sarpid","lat","lon","hasnetwork","removed","huc8"\n'
                        '"a1","46.8","-100.8","true","no","09030001"\n'
                        '"a2","46.8","-100.7","false","yes","09030001"\n'
                        '"a3","40.0","-90.0","true","no","07"\n')
        return dest, True
    real = E._download_resume
    E._download_resume = fake_dl

    class Ctx(object):
        writer, refresh = w, False

        def log(self, m):
            pass

        def sub(self, p, m=""):
            pass
    try:
        w.begin("ABI_dams", "x", "u", resume=False, signature="abi|api|dams")
        note = E.fetch_abi(Ctx(), (-104.1, 45.9, -96.5, 49.1), {"mode": "api", "kind": "dams"})
        n = w.finish(note)
    finally:
        E._download_resume = real
        w.close()
    assert n == 2, n
    assert "state?id=ND" in got[0] and got[1].endswith("/dams/metadata"), got
    assert "data version 4.4.0" in note, note


@case
def test_kinds_the_engine_runs():
    import inspect
    src = inspect.getsource(E._run_one)
    for kind in ("nsi", "arcgis", "osm", "arcgis_tiled", "abi", "local_gpkg", "first_of"):
        assert '"{}"'.format(kind) in src, kind
    sig = inspect.signature(E.build).parameters
    for p in ("dwr_folder", "files", "box"):
        assert p in sig, p


@case
def test_dwr_build_still_writes_a_zip():
    import tempfile
    d = tempfile.mkdtemp()
    calls = []

    def fake_run_one(ctx, name, label, kind, spec, bbox):
        calls.append(name)
        ctx.writer.begin(name, label, "x", resume=False, signature=kind)
        ctx.writer.add([{"type": "Feature", "geometry": {"type": "Point",
                                                         "coordinates": [-100.0, 47.0]},
                         "properties": {"OBJECTID": 1}, "id": 1}], E._uid_arcgis("OBJECTID"))
        return ctx.writer.finish("")
    real = E._run_one
    E._run_one = fake_run_one
    try:
        man = E.build(os.path.join(d, "pack"), only="nid", log=lambda m: None)
    finally:
        E._run_one = real
    assert calls == ["NID_dams"], calls
    assert os.path.isfile(os.path.join(d, "pack.zip")), os.listdir(d)
    assert man["datasets"]["NID_dams"]["count"] == 1, man["datasets"]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print("ok   ", t.__name__)
        except Exception:                                        # noqa
            failed += 1
            print("FAIL ", t.__name__)
            traceback.print_exc()
    print("{} tests, {} failed".format(len(TESTS), failed))
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
