"""Runner 3.10.55 (run queue 2026-10-21, a_culvert_anuga): the ANUGA mesh's outer edges were
anuga.Transmissive_boundary, which copies the interior momentum to the edge, so water moving
inward next to an edge is fed in from outside. The NDDOT culvert run (mesh clipped by the
LISFLOOD-FP grid at its SE corner) ended with 4.15 x the breach volume in the domain. The
outer edges are now outflow-only. Pure logic and a numpy stand-in for the domain: no ANUGA."""
import types

import numpy as np

from nd_breach_runner.core import anuga_model as am, anuga_worker as aw
from nd_breach_runner.tests.registry import test


@test
def anuga_outflow_only_momentum_mirrors_inward_keeps_outward():
    # outward normals: east, north, west, (0.6, 0.8)
    n1 = np.array([1.0, 0.0, -1.0, 0.6, 1.0])
    n2 = np.array([0.0, 1.0, 0.0, 0.8, 0.0])
    xm = np.array([2.0, 1.0, 3.0, -0.6, 0.0])     # out, along, in, in, still
    ym = np.array([0.5, -2.0, 1.0, -0.8, 0.0])    # ...,  in (north edge, moving south) ...
    x2, y2, inward = aw.outflow_only_momentum(xm, ym, n1, n2)
    assert inward.tolist() == [False, True, True, True, False], inward
    # outward and still edges are untouched (transmissive)
    assert (x2[0], y2[0]) == (2.0, 0.5) and (x2[4], y2[4]) == (0.0, 0.0)
    # inward: the normal component flips, the tangential one stays
    assert np.allclose([x2[1], y2[1]], [1.0, 2.0]) and np.allclose([x2[2], y2[2]], [-3.0, 1.0])
    assert np.allclose([x2[3], y2[3]], [0.6, 0.8])
    # never an inward normal flux afterwards; the inputs are not changed
    assert np.all(x2 * n1 + y2 * n2 >= -1e-12)
    assert xm[2] == 3.0 and ym[1] == -2.0


class _Q:
    def __init__(self, edge, nb):
        self.edge_values = np.asarray(edge, float)
        self.centroid_values = self.edge_values.mean(axis=1)
        self.boundary_values = np.zeros(nb)


def _domain():
    """Two triangles, one boundary edge each: triangle 0 edge 1 (outward normal +x), triangle 1
    edge 2 (outward normal -y). Water 2 m deep moving +x (out of the east edge) and moving
    -y in triangle 1 (out of its south edge), then the same with the flow reversed."""
    nb = 2
    normals = np.zeros((2, 6))
    normals[0, 2:4] = [1.0, 0.0]
    normals[1, 4:6] = [0.0, -1.0]
    q = {"stage": _Q([[5, 5, 5], [6, 6, 6]], nb), "elevation": _Q([[3, 3, 3], [4, 4, 4]], nb),
         "height": _Q([[2, 2, 2], [2, 2, 2]], nb),
         "xmomentum": _Q([[0, 4, 0], [0, 0, 1]], nb), "ymomentum": _Q([[0, 1, 0], [0, 0, -3]], nb),
         "xvelocity": _Q([[0, 2, 0], [0, 0, .5]], nb), "yvelocity": _Q([[0, .5, 0], [0, 0, -1.5]], nb)}
    return types.SimpleNamespace(quantities=q, normals=normals,
                                 boundary_cells=np.array([0, 1]), boundary_edges=np.array([1, 2]),
                                 centroid_transmissive_bc=False)


@test
def anuga_outflow_boundary_segment_is_transmissive_for_outflow_and_a_wall_for_inflow():
    d = _domain()
    ids = np.array([0, 1])
    n = aw.apply_outflow_boundary(d, ids, d.boundary_cells, d.boundary_edges)
    q = d.quantities
    assert n == 0                                         # both edges carry water out
    assert q["stage"].boundary_values.tolist() == [5.0, 6.0]
    assert q["elevation"].boundary_values.tolist() == [3.0, 4.0]
    assert q["xmomentum"].boundary_values.tolist() == [4.0, 1.0]
    assert q["ymomentum"].boundary_values.tolist() == [1.0, -3.0]
    # reversed flow: both edges would take water in -> the normal momentum is mirrored
    for k in ("xmomentum", "ymomentum", "xvelocity", "yvelocity"):
        q[k].edge_values = -q[k].edge_values
    n = aw.apply_outflow_boundary(d, ids, d.boundary_cells, d.boundary_edges)
    assert n == 2
    assert q["xmomentum"].boundary_values.tolist() == [4.0, -1.0]     # east edge: -4 -> +4
    assert q["ymomentum"].boundary_values.tolist() == [-1.0, -3.0]    # south edge: +3 -> -3
    assert q["xvelocity"].boundary_values.tolist() == [2.0, -0.5]
    assert q["yvelocity"].boundary_values.tolist() == [-0.5, -1.5]
    assert q["stage"].boundary_values.tolist() == [5.0, 6.0]           # stage unchanged


@test
def anuga_outer_boundary_choice_and_fallback():
    calls = []

    class FakeAnuga:
        class Boundary:
            def __init__(self):
                pass

        @staticmethod
        def Transmissive_boundary(dom):
            calls.append("transmissive")
            return "T"

        @staticmethod
        def Dirichlet_boundary(v):
            calls.append(("dirichlet", v))
            return "D"
    aw.__dict__.pop("Outflow_boundary", None)
    try:
        b, kind = aw.make_outer_boundary(FakeAnuga, object(), None, 280.0)
        assert kind == "outflow" and type(b).__name__ == "Outflow_boundary", (b, kind)
        assert type(b).__module__ == aw.__name__ and aw.Outflow_boundary is type(b)  # picklable
        assert aw.make_outer_boundary(FakeAnuga, object(), "Transmissive", 280.0) == \
            ("T", "transmissive")
        b, kind = aw.make_outer_boundary(FakeAnuga, None, "outflow", 280.0)   # cannot be made
        assert b == "D" and kind.startswith("dirichlet stage 230.0 m"), kind
        assert calls[-1] == ("dirichlet", [230.0, 0.0, 0.0]), calls
    finally:
        aw.__dict__.pop("Outflow_boundary", None)
    assert am.AnugaSettings("py").edge_bc == "outflow"


@test
def anuga_volume_balance_flags_the_culvert_run():
    # a_culvert_anuga (2026-10-21), pass 2 (1,000 m buffer): QA report numbers
    vb = aw.volume_balance(0.0, 1989255.65, 8248992.54)
    assert vb["ratio"] == round(8248992.54 / 1989255.65, 4) and 4.14 < vb["ratio"] < 4.15, vb
    vb2 = aw.volume_balance(0.0, 1989255.65, 8248992.54, boundary_in=6259000.0)
    assert abs(vb2["residual_m3"] - (8248992.54 - 1989255.65 - 6259000.0)) < 0.2, vb2
    st = {"volume_balance": vb, "boundary": "transmissive"}
    note = am.volume_note(st)
    assert note and note.startswith("ANUGA volume check FAILED: the domain ends with 4.15 x"), note
    assert "5,075 acre-ft more" in note and "transmissive boundary" in note, note
    # results written before 3.10.55 (no volume_balance): computed from the plain stats
    old = {"initial_volume_m3": 0.0, "injected_m3": 1989255.65, "volume_in_domain_m3": 1522089.6}
    assert am.volume_note(old) is None and old["volume_balance"]["ratio"] == 0.7652, old
    assert am.volume_note({"injected_m3": 0.0, "volume_in_domain_m3": 5.0}) is None
    # the refined 72 h run closed at 1.000: no note
    assert am.volume_note({"volume_balance": aw.volume_balance(0, 100.0, 100.0)}) is None
