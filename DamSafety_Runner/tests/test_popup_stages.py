"""3.10.15: terrain / submittal status rows, stage states and per-tool status (pure logic)."""
import os
import shutil
import tempfile
import time

from nd_breach_runner.tests.registry import test


def _touch(*parts, mtime=None):
    p = os.path.join(*parts)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").close()
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


@test
def popup_terrain_and_submittal_rows_stage_states_and_tool_status():
    from nd_breach_runner import popup_logic as pl
    from nd_breach_runner.core import workflow as wf
    d = tempfile.mkdtemp()
    try:
        f = os.path.join(d, "D1")
        os.makedirs(f)
        st = lambda: {k: v for k, _l, v, _d in pl.dam_status(f)}           # noqa: E731
        # empty folder: nothing yet
        assert st()["terrain"] == "no" and st()["submittal"] == "no"
        assert pl.stage_states(pl.dam_status(f), True, False, "", "D1") == \
            {"dam": "part", "terrain": "no", "run": "no", "products": "no"}
        # a DEM chosen only lifts terrain to part
        assert pl.stage_states(pl.dam_status(f), True, False, "dem1", "D1")["terrain"] == "part"
        # no folder: the DEM is the fallback
        assert pl.stage_states([], False, False, "dem1", "D1") == \
            {"dam": "part", "terrain": "part", "run": "no", "products": "no"}
        assert pl.stage_states([], False, False, "", "") == \
            {"dam": "no", "terrain": "no", "run": "no", "products": "no"}
        # a model folder whose clip was cleaned up -> part; a kept clip -> yes
        _touch(f, wf.MODEL_FOLDER, "run_report.json", mtime=time.time() - 100)
        assert st()["terrain"] == "part"
        _touch(f, wf.MODEL_FOLDER, "lisflood", "dam.dem.asc")
        assert st()["terrain"] == "yes"
        for k in ("depth", "velocity", "arrival"):
            _touch(f, wf.FINAL_NAMES[k])
        rows = pl.dam_status(f)
        assert pl.tool_status("rundam", rows) == "yes" and pl.tool_status("submittal", rows) == "no"
        assert pl.tool_status("estimate", rows) is None and pl.tool_status("rundam", []) is None
        # a submittal older than the run report is stale (part); a newer one is yes
        _touch(f, "Dam D1 submittal.docx", mtime=time.time() - 1000)
        assert st()["submittal"] == "part"
        assert pl.stage_states(pl.dam_status(f), True, False, "", "D1")["products"] == "part"
        _touch(f, "Dam D1 submittal.docx", mtime=time.time())
        assert st()["submittal"] == "yes" and pl.tool_status("submittal", pl.dam_status(f)) == "yes"
        _touch(f, "Flood Extent.geojson")
        _touch(f, "_classification", "x.json")
        _touch(f, "dam.json")
        s = pl.stage_states(pl.dam_status(f), True, True, "", "D1")
        assert s == {"dam": "yes", "terrain": "yes", "run": "yes", "products": "yes"}, s
        # dam_record_info / dam_record_text agree; no folder = no dam.json
        assert pl.dam_record_info("") == ("dam.json: no dam folder", "", False)
        assert pl.dam_record_text("") == ("dam.json: no dam folder", "")
        g = os.path.join(d, "D2")
        os.makedirs(g)
        text, code, has = pl.dam_record_info(g)
        assert not has and text.startswith("dam.json: none yet") and "DamSafety Hazard" in text
        assert pl.dam_record_text(g) == (text, code)
        assert set(pl.STAGE_KEYS) == set(pl.STAGE_LABELS)
    finally:
        shutil.rmtree(d, ignore_errors=True)
