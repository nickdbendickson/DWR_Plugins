"""2.31.3 (Lumo v1.5): settings-not-applied warning, cover letter 'Not enclosed', series vintage
flags and raster notes, stale-package warning, settings-grid cosmetics.
Run: python3 -m ND_Dam_Hazard_Helper.tests.run_tests (from the folder above the plugin)."""
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402


@test
def v2313_settings_aware_says_when_the_file_was_not_applied():
    from ND_Dam_Hazard_Helper import suite_settings_tools as ST

    class Base(object):
        def initAlgorithm(self, config=None):
            pass

        def shortHelpString(self):
            return "help"

        def processAlgorithm(self, parameters, context, feedback):
            return {"ran": True}

        def name(self):
            return "demo"
    orig = ST.apply_file_defaults
    ST.apply_file_defaults = lambda *a, **k: (_ for _ in ()).throw(ValueError("broken file"))
    try:
        W = ST.settings_aware(Base, "ndhazard", lambda *a: "")
        w = W()
        w.initAlgorithm()
    finally:
        ST.apply_file_defaults = orig
    assert "NOT applied" in w._nd_problem and "broken file" in w._nd_problem
    assert w.shortHelpString().startswith("<p><b>WARNING:") and w.shortHelpString().endswith("help")

    class FB(object):
        msgs = []

        def reportError(self, m, fatal):
            FB.msgs.append(m)
    assert w.processAlgorithm({}, None, FB()) == {"ran": True}
    assert FB.msgs and "NOT applied" in FB.msgs[0]
    ST.apply_file_defaults = lambda *a, **k: ([], [])
    try:
        ok = W()
        ok.initAlgorithm()
    finally:
        ST.apply_file_defaults = orig
    assert ok._nd_problem == "" and ok.shortHelpString() == "help"


@test
def v2313_cover_letter_lists_what_is_not_enclosed():
    from ND_Dam_Hazard_Helper import submittal as SB
    rows = [{"Path": "01_Report/Hazard_Classification_Summary.txt"}]
    miss = SB.missing_items(rows, ["NSI points (not at hand)"])
    assert any("PDF report" in m for m in miss) and miss[-1] == "NSI points (not at hand)"
    text = SB.cover_letter({"nd": "Low"}, "Test Dam", ["Summary"], not_enclosed=miss)
    assert "Not enclosed:" in text and "  - NSI points (not at hand)" in text
    assert "Not enclosed:" not in SB.cover_letter({"nd": "Low"}, "Test Dam", ["Summary"])


@test
def v2313_series_flags_vintage_and_versions():
    from ND_Dam_Hazard_Helper import series as SE
    a = {"name": "A", "class": {"time": "2024-01-02 10:00", "plugin_version": "2.31.3"}}
    b = {"name": "B", "class": {"time": "2026-03-01 09:00", "plugin_version": "2.31.3"}}
    c = {"name": "C", "class": {"time": "2026-02-01 09:00", "plugin_version": "2.20.0"}}
    assert "days apart" in SE.vintage_problem(a, b)
    assert SE.vintage_problem(b, c).startswith("classified by different plugin versions")
    assert SE.vintage_problem(b, {"name": "D", "class": {}}) == ""
    got = {"depth": 1.0, "max_depth": 1.0, "arrival": None, "velocity": None, "dist_ft": 0.0}
    a["raster_warnings"] = ["velocity raster v.tif could not be read"]
    row = SE._row(a, b, got, 2.0, "nearest")
    assert row["Vintage_Flag"] == "YES" and "VINTAGE" in row["Flags"]
    assert row["Upstream_Snapshot"] == "2024-01-02 10:00"
    assert "velocity raster v.tif could not be read" in row["Message"]
    assert set(row) <= set(SE.SERIES_FIELDS)
    dams = SE._dam_rows([dict(a, depth="d.tif", status="screened", point=(-100.0, 47.0))], [])
    assert dams[0]["Snapshot_Date"] == "2024-01-02 10:00" and "velocity raster" in dams[0]["Status"]
    assert set(dams[0]) <= set(SE.DAM_FIELDS)


@test
def v2313_stale_package_is_warned():
    from ND_Dam_Hazard_Helper import suite_tab as STB
    out = tempfile.mkdtemp()
    os.makedirs(os.path.join(out, "QA"))
    pkg = os.path.join(out, "pkg.zip")
    open(pkg, "w").close()
    snap = os.path.join(out, "QA", "Last_Run_Snapshot.json")
    open(snap, "w").close()
    now = time.time()
    os.utime(pkg, (now - 3600, now - 3600))
    os.utime(snap, (now, now))
    assert "OLDER than the last classification run" in STB.package_staleness(out, pkg)
    os.utime(pkg, (now + 60, now + 60))
    assert STB.package_staleness(out, pkg) == ""
    assert STB.package_staleness(out, "") == ""


@test
def v2313_suite_styles_has_no_dead_entry():
    from ND_Dam_Hazard_Helper import suite_styles as SS
    assert all(q for _p, q in SS.BY_NAME)
    assert SS.style_for_path("HECRAS depth (max).tif") == SS.HECRAS["depth"]
