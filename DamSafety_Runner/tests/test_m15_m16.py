"""M15: the Runner reads the LISFLOOD-FP path that DamSafety Setup writes (ndsuite/lisflood_exe).
M16: the crest line for the breach is picked by the dam's DWR structure id, else the nearest line
within CREST_SNAP_M, so the breach is never placed on a neighbouring dam's line."""
import os
import tempfile

import numpy as np

from nd_breach_runner.tests import fake_qgis as _fq
_fq.install()

from nd_breach_runner.algorithms import (  # noqa: E402
    _Base, _suite_setting, _dwr_ids, _pick_crest_line, _point_along_line, CREST_SNAP_M)
from nd_breach_runner.damsuite import suite_config  # noqa: E402
from nd_breach_runner.tests.fake_qgis import QgsSettings  # noqa: E402
from nd_breach_runner.tests.registry import test  # noqa: E402


# ------------------------------------------------------------------- M15 (LISFLOOD path)
@test
def find_lisflood_reads_the_suite_setting_setup_writes():
    fd, path = tempfile.mkstemp(prefix="lisflood_"); os.close(fd)
    orig_get = suite_config.get
    try:
        # DamSafety Setup stores the path in the one suite store (ndsuite/lisflood_exe); emulate
        # the suite accessor returning it (the fake qgis.core has no QgsSettings of its own).
        suite_config.get = lambda key, project=None: path if key == "lisflood_exe" else ""
        assert _suite_setting("lisflood_exe") == path
        # No LISFLOOD_EXE parameter and no provider ProcessingConfig key: the Runner must still
        # return the Setup-configured executable. Before M15, find_lisflood never consulted the
        # suite store, so it fell through to auto-detection and ignored this path.
        assert _Base().find_lisflood({}, None) == path
    finally:
        suite_config.get = orig_get
        os.remove(path)


@test
def suite_setting_empty_when_unset():
    QgsSettings._store.clear()
    os.environ.pop("NDBREACH_LISFLOOD", None)
    assert _suite_setting("lisflood_exe") == ""


# ------------------------------------------------------------------- M16 (crest line pick)
def _ln(pts, **attrs):
    return (np.asarray(pts, float), attrs)


@test
def dwr_ids_from_dam_id_and_record():
    class _Rec:
        def get(self, k, d=None):
            return {"dwr_structure_id": "02801"}.get(k, d)
    assert _dwr_ids("DWR-2801", None) == {"2801"}
    assert _dwr_ids("DWR_02801", None) == {"2801"}
    assert _dwr_ids("whatever", _Rec()) == {"2801"}
    assert _dwr_ids("", None) == set()


@test
def pick_crest_line_prefers_dwr_id_match_over_a_nearer_neighbour():
    dam = (0.0, 0.0)
    neighbour = _ln([(30.0, -40.0), (30.0, 40.0)], structure_id="9999")   # very close, wrong dam
    mine = _ln([(300.0, -40.0), (300.0, 40.0)], structure_id="2801")      # farther, this dam
    line, dist, how = _pick_crest_line([neighbour, mine], dam, {"2801"}, "structure_id")
    assert line is mine[0]                       # the id-matched line wins over the nearer one
    assert "DWR structure 2801" in how
    assert _point_along_line(line, 0.5) == (300.0, 0.0)


@test
def pick_crest_line_falls_back_to_nearest_within_snap_when_no_id():
    dam = (0.0, 0.0)
    neighbour = _ln([(30.0, -40.0), (30.0, 40.0)], structure_id="9999")
    mine = _ln([(300.0, -40.0), (300.0, 40.0)], structure_id="2801")
    line, dist, how = _pick_crest_line([neighbour, mine], dam, set(), "structure_id")
    assert line is neighbour[0]
    assert how == "nearest line"


@test
def pick_crest_line_refuses_lines_beyond_snap():
    dam = (0.0, 0.0)
    far = _ln([(600.0, 0.0), (700.0, 0.0)])
    line, dist, how = _pick_crest_line([far], dam, set(), "")
    assert line is None
    assert dist > CREST_SNAP_M
    assert _pick_crest_line([], dam, set(), "")[0] is None
