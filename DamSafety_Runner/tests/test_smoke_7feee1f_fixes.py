"""Runner 3.11.1: QGIS Dev's smoke test of main 7feee1f (PR #218).

CHANNEL-BED-SLOPE: structures.channel_bed took the lowest ground 0-30 m beyond each culvert
end, which on a sloping channel reads the downstream bed up to slope x 30 m low (2 % -> 0.6 m =
2 ft) and rejected correct inventory inverts ("disagree (-0.1 ft / +2.0 ft): LiDAR inverts
used"). Each thalweg point is now carried back to the end along the channel's own slope.

#209 wording: anuga_model.volume_note named the breach inlet for all of the operators'
fractional-step excess and recommended the first-step inflow cap even for a run that had it.
"""
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_breach_runner.core import anuga_model as AM  # noqa: E402
from nd_breach_runner.core import anuga_worker as AW  # noqa: E402
from nd_breach_runner.core import structures as S  # noqa: E402
from nd_breach_runner.core.grid import Grid  # noqa: E402
from nd_breach_runner.tests.registry import test  # noqa: E402

FT = 0.3048


def _sloped(slope):
    """QGIS Dev's repro (smoke_7feee1f_out/culvert/slope_repro.py): a 3 m channel falling
    `slope` along +x through a road embankment (crown 534 m, toe slopes 0.8)."""
    def ground(x, y):
        bed = 530.0 - slope * x
        g = np.where(np.abs(y - 60.0) <= 1.5, bed, bed + 1.2)
        emb = np.where(x < 52.0, 534.0 - (52.0 - x) * 0.8,
                       np.where(x > 68.0, 534.0 - (x - 68.0) * 0.8, 534.0))
        return np.maximum(g, emb)

    def read(bounds, cell, sub=0.25):
        x0, y0, x1, y1 = bounds
        nx, ny = int(np.ceil((x1 - x0) / cell)), int(np.ceil((y1 - y0) / cell))
        k = max(1, int(round(cell / sub)))
        xs = x0 + (np.arange(nx * k) + 0.5) * cell / k
        ys = y1 - (np.arange(ny * k) + 0.5) * cell / k
        X, Y = np.meshgrid(xs, ys)
        z = ground(X, Y).reshape(ny, k, nx, k).mean(axis=(1, 3))
        return Grid(z.astype(float), x0, y1, cell)
    return read


@test
def channel_bed_on_a_sloping_channel_accepts_true_inverts():
    for slope in (0.0, 0.01, 0.02):
        up, dn = 530.0 - slope * 47.0, 530.0 - slope * 73.0
        st = S.Structure("culvert", np.array([[47.0, 60.0], [73.0, 60.0]]), 1.5, 1.5, 1, "box",
                         up, dn, name="s{}".format(slope))
        out = S.reconcile_inverts([st], _sloped(slope), 1.0 * FT, 3.0)
        txt = " ".join(str(o) for o in out)
        assert "inventory inverts used" in txt and "disagree" not in txt, (slope, txt)
        bed = st.invert_channel_bed
        assert abs(bed[0] - up) < 0.1 and abs(bed[1] - dn) < 0.1, (slope, bed, up, dn)


@test
def channel_bed_carries_the_thalweg_back_along_its_slope():
    # a 1 m grid of a 2 % channel falling away from the end: the old 0-30 m minimum read
    # the bed 0.6 m low; now within a few cm of the bed at the end
    n = 120
    xs = np.arange(n) + 0.5
    X, Y = np.meshgrid(xs, (n - np.arange(n)) - 0.5)
    z = np.where(np.abs(Y - 60.0) <= 1.5, 530.0 - 0.02 * X, 532.0)
    g = Grid(z.astype(float), 0.0, float(n), 1.0)
    line = np.array([[30.0, 60.0], [50.0, 60.0]])         # end 1 at x = 50, outward = +x
    want = 530.0 - 0.02 * 50.0
    b = S.channel_bed(g, line, -1, 1.5)
    assert abs(b - want) < 0.05, (b, want)
    along, bed = S.thalweg_profile(g, line, -1, 1.5)
    assert len(along) >= S.CHANNEL_MIN_BINS
    assert abs(S.channel_slope(along, bed) + 0.02) < 1e-3, S.channel_slope(along, bed)
    # flat ground: the lowest point is still found (the old behaviour on a level channel)
    flat = Grid(np.where(np.abs(Y - 60.0) <= 1.5, 527.0, 529.0).astype(float), 0.0, float(n), 1.0)
    assert abs(S.channel_bed(flat, line, -1, 1.5) - 527.0) < 1e-6


SUP, VIN, EXCESS = 88186.4, 92251.1, 4064.8


def _stats(cap=None, culverts=None, rows=None):
    vb = AW.volume_balance(0.0, SUP, VIN, boundary_in=0.0, operators=SUP + EXCESS,
                           solver_volume=VIN)
    if cap is not None:
        vb["inflow_step_cap"] = {"applied": cap, "max_dt_s": 30.0}
    st = {"volume_balance": vb, "boundary": "outflow", "injected_m3": SUP}
    if culverts:
        st["culverts"] = culverts
    if rows:
        st["operator_volumes"] = rows
    return st


@test
def volume_note_does_not_recommend_the_cap_a_run_already_used():
    n = AM.volume_note(_stats(cap=True))
    assert "the breach inlet added it" in n, n
    assert "was on in this run" in n and "should remove" not in n, n
    n = AM.volume_note(_stats(cap=False))
    assert "it was off in this run" in n and "should remove the inlet's part" in n, n
    n = AM.volume_note(_stats())
    assert "not recorded for this run" in n, n


@test
def volume_note_does_not_credit_the_inlet_with_culvert_water():
    # culverts in the run, their volumes not logged: the inlet is not named alone
    n = AM.volume_note(_stats(culverts=1))
    assert "the breach inlet added it" not in n, n
    assert "ANUGA's operators added it" in n and "culvert / bridge operators" in n, n
    assert "was not logged" in n, n
    # logged: the culverts made 3,000 m3 of the 4,065 - split, and the cap is not offered
    # for a gain that is mostly the culverts'
    rows = [{"op": "c1", "t": 100.0, "added_m3": 500.0, "removed_m3": 400.0},
            {"op": "c1", "t": 600.0, "added_m3": 13000.0, "removed_m3": 10000.0}]
    st = _stats(cap=False, culverts=1, rows=rows)
    sh = AM.operator_share(st, st["volume_balance"])
    assert abs(sh["structures_net_m3"] - 3000.0) < 1e-6
    assert abs(sh["inlet_excess_m3"] - (EXCESS - 3000.0)) < 1e-6
    n = AM.volume_note(st)
    assert "1,065 m3 from the breach inlet, +3,000 m3 net from the culvert" in n, n
    assert "should remove" not in n, n
    # a small culvert net: the inlet is named, with the culverts' net beside it
    rows = [{"op": "c1", "t": 600.0, "added_m3": 10010.0, "removed_m3": 10000.0}]
    n = AM.volume_note(_stats(culverts=1, rows=rows))
    assert "the breach inlet added it" in n and "(culvert operators: +10 m3 net)" in n, n


@test
def culvert_bed_notes_give_feet():
    st = {"culvert_enquiry": [{"op_index": 0, "ends": [
        {"invert_used_m": 527.30, "enquiry_bed_m": 528.20},
        {"raised_m": 0.30, "floor_source": "enquiry", "floor_m": 526.70}]}],
          "operator_volumes": [{"op": 0, "name": "c1", "added_m3": 0.0, "removed_m3": 0.0}]}
    notes = AM.culvert_bed_notes(st)
    txt = " ".join(notes)
    assert "527.30 m = 1729.99 ft" in txt and "0.90 m (3.0 ft)" in txt, notes
    assert "raised 0.30 m (1.0 ft)" in txt and "526.70 m = 1728.02 ft" in txt, notes
