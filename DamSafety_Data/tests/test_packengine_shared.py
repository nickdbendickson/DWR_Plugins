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
    assert names == DWR_NAMES, names
    assert not [n for n in names if n.startswith(("3DHP_", "SARP_"))], names
    # the old group names still pick (no ND aliases here: nhd stays nhd)
    table = E.datasets()
    assert E._pick("nhd", table) == {"nhd"}
    assert E._pick("NHD_flowlines", table) == {"NHD_flowlines"}


@case
def test_nd_layer_list_is_there_too():
    """The ND list reads the ND constants (SARP_*, 3DHP): with a stand-in that has them it
    builds; the next merge step gives both suites one list."""
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
    for n in ("3DHP_flowlines", "3DHP_waterbodies", "SARP_dams", "SARP_surveyed_crossings",
              "SARP_unsurveyed_crossings", "USGS_stream_crossings"):
        assert n in nd, (n, nd)


@case
def test_kinds_the_engine_runs():
    import inspect
    src = inspect.getsource(E._run_one)
    for kind in ("nsi", "arcgis", "osm", "arcgis_tiled", "sarp_zip", "local_gpkg", "first_of"):
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
