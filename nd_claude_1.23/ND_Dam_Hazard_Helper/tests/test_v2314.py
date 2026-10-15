"""2.31.4 (Lumo v1.6): imagery probe off in suite / offline-pack mode, describe() lists the
no-breach velocity / arrival, word-boundary 'max' ranking, logged fallbacks.
Run: python3 -m ND_Dam_Hazard_Helper.tests.run_tests (from the folder above the plugin)."""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402


def _env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return old


@test
def v2314_imagery_probe_skipped_in_suite_or_offline_pack_mode():
    from ND_Dam_Hazard_Helper import basemap as B
    from ND_Dam_Hazard_Helper import download as DL
    orig = DL.offline_config
    try:
        for var in ("ND_DAM_SUITE", "ND_DAM_OFFLINE"):
            old = _env(ND_DAM_SUITE=None, ND_DAM_OFFLINE=None)
            os.environ[var] = "1"
            try:
                DL.offline_config = lambda: ("", False)
                assert B.offline_only()
                ok, text = B.probe("https://example.invalid/{z}/{x}/{y}")
                assert not ok and "offline" in text
                assert B.layout_imagery(add_to_project=False)[0] is None
            finally:
                _env(**old)
        old = _env(ND_DAM_SUITE=None, ND_DAM_OFFLINE=None)
        try:
            DL.offline_config = lambda: ("/data/pack.gpkg", False)
            assert B.offline_only()
            DL.offline_config = lambda: ("", True)
            assert B.offline_only()
            DL.offline_config = lambda: ("", False)
            assert not B.offline_only()
        finally:
            _env(**old)
    finally:
        DL.offline_config = orig


@test
def v2314_describe_lists_no_breach_velocity_and_arrival():
    from ND_Dam_Hazard_Helper import autodetect as AD
    root = tempfile.mkdtemp()
    found = {k: os.path.join(root, k + ".tif") for k in
             ("depth", "base_depth", "base_vel", "base_arr")}
    text = AD.describe(found, root)
    assert "no-breach velocity = base_vel.tif" in text, text
    assert "no-breach arrival = base_arr.tif" in text, text


@test
def v2314_max_is_ranked_as_a_word_not_a_substring():
    from ND_Dam_Hazard_Helper import autodetect as AD
    r = "/x"
    for name in ("Depth (Max).tif", "Depth_Hmax.tif", "dmax.tif", "Velocity_Vmax.vrt",
                 "max_depth.tif"):
        assert AD._rank(os.path.join(r, name), r)[0] == 0, name
    for name in ("Maxwell_depth.tif", "Climax_Depth.tif", "Depth.tif"):
        assert AD._rank(os.path.join(r, name), r)[0] == 1, name
    assert not AD._is_time_step("Depth_Hmax.tif") and not AD._is_time_step("Velocity_Vmax.tif")
    assert AD._is_time_step("Depth_0100.tif")


@test
def v2314_swallowed_failures_are_logged():
    for f in ("access.py", "__init__.py"):
        src = open(os.path.join(PLUGIN_DIR, f), encoding="utf-8").read()
        assert "except Exception:\n        pass\n\n\n_install" not in src
    acc = open(os.path.join(PLUGIN_DIR, "access.py"), encoding="utf-8").read()
    assert "area filter not applied" in acc
    ini = open(os.path.join(PLUGIN_DIR, "__init__.py"), encoding="utf-8").read()
    assert "transform guard not installed" in ini
