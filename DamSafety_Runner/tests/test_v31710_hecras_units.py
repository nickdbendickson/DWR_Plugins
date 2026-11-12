"""3.17.10: a low-elevation feet-as-metres mix is fatal, a plan with no units is not read
as US customary, and cms/cfs are recognized. A few feet of relief stays a warning."""
import numpy as np

from nd_breach_runner.core import hecras
from nd_breach_runner.core.units import FT
from nd_breach_runner.tests.registry import test


class _DS:
    def __init__(self, arr, attrs):
        self._a = np.asarray(arr)
        self.attrs = dict(attrs)
        self.shape = self._a.shape
        self.dtype = self._a.dtype

    def __getitem__(self, k):
        return self._a[k]


class _Grp:
    def __init__(self):
        self.attrs = {}


class _FakeH5:
    def __init__(self, layout, root_attrs):
        self.layout, self.attrs = layout, dict(root_attrs)

    def visititems(self, fn):
        groups = set()
        for p in self.layout:
            parts = p.split("/")
            groups.update("/".join(parts[:i]) for i in range(1, len(parts)))
        for g in sorted(groups):
            fn(g, _Grp())
        for p in sorted(self.layout):
            arr, at = self.layout[p]
            fn(p, _DS(arr, at))


def _bare():
    """One wet cell, no Units attribute anywhere."""
    elev = np.array([100.0])
    wse = np.array([105.0])
    L = {
        "Geometry/2D Flow Areas/A/Cells Center Coordinate": (np.array([[1005.0, 2005.0]]), {}),
        "Geometry/2D Flow Areas/A/Cells Minimum Elevation": (elev, {}),
        "Results/Unsteady/Output/Output Blocks/Base Output/Summary Output/2D Flow Areas/A/"
        "Maximum Water Surface": (np.vstack([wse, np.array([0.1])]), {}),
    }
    return _FakeH5(L, {"File Type": "HEC-RAS Results"})


@test
def v31710_cms_is_si_and_cfs_is_us():
    assert hecras.parse_units("cms") == "si"
    assert hecras.parse_units("cfs") == "us"
    assert hecras.parse_units("Feet") == "us"
    assert hecras.parse_units("Meter") == "si"
    assert hecras.parse_units("") is None


@test
def v31710_low_elevation_feet_as_metres_is_fatal():
    elev = 40.0
    ground = np.full(20, elev / FT)          # feet numbers read as metres, then to feet
    cmin = np.full(20, elev)
    rec = hecras.datum_check(ground, cmin)
    assert rec["status"] == "mismatch" and rec["fatal"], rec


@test
def v31710_a_few_feet_of_relief_stays_a_warning():
    cmin = np.full(20, 1600.0)
    ground = cmin + 2.0
    rec = hecras.datum_check(ground, cmin)
    assert rec["status"] == "suspect" and not rec["fatal"], rec


@test
def v31710_a_plan_with_no_units_is_refused():
    try:
        hecras.read_mesh(_bare(), want_arrival=False)
    except hecras.HecRasError as e:
        assert "Refusing to assume US customary" in str(e), e
    else:
        raise AssertionError("a plan HDF with no units was read")
    mesh = hecras.read_mesh(_bare(), units="us", want_arrival=False)
    assert abs(float(mesh.raw["wse"][0]) - 105.0) < 1e-6
