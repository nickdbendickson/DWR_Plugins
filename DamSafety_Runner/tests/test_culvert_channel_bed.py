"""Culvert channel bed, the ANUGA bed floor, per-structure losses and the per-operator volume
log (Runner 3.10.96; DWR-3034 REVIEW 9.8 items 3 and 4).

DWR-3034's Boyd culvert: the inventory inverts 527.30 / 526.39 m sit within 3 ft of the
channel on the 1 m LiDAR (527.90-528.07 m upstream, 526.56-527.02 m downstream, 4-28 m out),
but reconcile_inverts read the lowest ground in a disc around each end at a 3 m cell - the
embankment's toe slope averaged with the narrow channel (528.26 / 526.68 m) - and called it
a datum shift. The run's scratch patches (the `losses` field, a bed clamp and an operator
volume log) are upstreamed here, with the clamp floor the enquiry / channel bed, not the
exchange triangles' minimum."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_breach_runner.core import anuga_model as AM  # noqa: E402
from nd_breach_runner.core import anuga_worker as AW  # noqa: E402
from nd_breach_runner.core import structures as S  # noqa: E402
from nd_breach_runner.core.grid import Grid  # noqa: E402
from nd_breach_runner.tests.registry import test, model_test  # noqa: E402

FT = 0.3048
UP_BED, DN_BED, FLOODPLAIN = 527.90, 526.75, 529.00


def _ground(x, y):
    """A 3 m channel along y = 60 (banks 529.0 m upstream, 528.1 m downstream) under a road embankment (crown 532 m, 52 < x < 68) with
    0.8 toe slopes: at the culvert ends (x = 50 / 70) the ground is the slope, 530.4 m."""
    bed = np.where(x <= 60.0, UP_BED, DN_BED)
    g = np.where(np.abs(y - 60.0) <= 1.5, bed, np.where(x <= 60.0, FLOODPLAIN, FLOODPLAIN - 0.9))
    emb = np.where(x < 52.0, 532.0 - (52.0 - x) * 0.8,
                   np.where(x > 68.0, 532.0 - (x - 68.0) * 0.8, 532.0))
    return np.maximum(g, emb)


def _read(bounds, cell, sub=0.25):
    """The ground as a Grid at `cell`, each cell the mean of sub-metre samples (a 3 m read
    averages the 3 m channel with its banks, as the 3 m read of the 1 m LiDAR did)."""
    x0, y0, x1, y1 = bounds
    nx, ny = int(np.ceil((x1 - x0) / cell)), int(np.ceil((y1 - y0) / cell))
    k = max(1, int(round(cell / sub)))
    xs = x0 + (np.arange(nx * k) + 0.5) * cell / k
    ys = y1 - (np.arange(ny * k) + 0.5) * cell / k
    X, Y = np.meshgrid(xs, ys)
    z = _ground(X, Y).reshape(ny, k, nx, k).mean(axis=(1, 3))
    return Grid(z.astype(float), x0, y1, cell)


def _culvert(up=527.304, dn=526.390):
    return S.Structure("culvert", np.array([[50.0, 60.0], [70.0, 60.0]]), 2.13, 2.13, 1, "box",
                       up, dn, name="Boyd")


@test
def the_3034_inverts_are_inventory_inverts_against_the_channel_not_a_datum_shift():
    s = _culvert()
    # the bug: the old disc at a 3 m cell reads the toe slope / smeared channel at both ends,
    # both about 1.1 m above the inventory (more than 3 ft, within 1 ft of each other)
    half = 0.5 * s.corridor_width(3.0)
    r = 2.0 * half + 3.0
    g = _read((50 - r - 6, 60 - r - 6, 70 + r + 6, 60 + r + 6), 3.0)   # reconcile's read
    off = [s.invert_up_m - S.lidar_bed(g, (50.0, 60.0), r),
           s.invert_dn_m - S.lidar_bed(g, (70.0, 60.0), r)]
    assert all(o < -3.0 * FT for o in off) and abs(off[0] - off[1]) <= FT, off
    notes = S.reconcile_inverts([s], _read, 3.0 * FT, 3.0)
    assert s.invert_note.startswith("inventory inverts used"), notes
    assert "channel bed" in s.invert_note and "datum shift" not in s.invert_note, notes
    assert (s.invert_up_m, s.invert_dn_m) == (527.304, 526.390)
    assert abs(s.invert_channel_bed[0] - UP_BED) < 0.01 and \
        abs(s.invert_channel_bed[1] - DN_BED) < 0.01, s.invert_channel_bed


@test
def a_real_datum_shift_is_still_found_against_the_channel():
    s = _culvert(up=UP_BED + 2.0, dn=DN_BED + 2.1)
    S.reconcile_inverts([s], _read, 3.0 * FT, 3.0)
    assert "datum shift" in s.invert_note, s.invert_note
    assert abs(s.invert_up_m - (UP_BED - 0.05)) < 0.02 and \
        abs(s.invert_dn_m - (DN_BED + 0.05)) < 0.02, (s.invert_up_m, s.invert_dn_m)


@test
def channel_bed_looks_outward_only_and_within_the_corridor():
    g = _read((0.0, 20.0, 120.0, 100.0), 1.0)
    line = np.array([[50.0, 60.0], [70.0, 60.0]])
    assert abs(S.channel_bed(g, line, 0, 1.5) - UP_BED) < 1e-6      # x <= 50 only
    assert abs(S.channel_bed(g, line, -1, 1.5) - DN_BED) < 1e-6     # x >= 70 only
    off = np.array([[50.0, 70.0], [70.0, 70.0]])                    # 10 m off the channel
    assert S.channel_bed(g, off, 0, 1.5) >= FLOODPLAIN - 1e-6
    assert S.channel_bed(g, np.array([[5.0, 5.0], [5.0, 5.0]]), 0, 1.5) is None


@test
def the_fine_channel_read_failing_keeps_the_disc():
    calls = []

    def read(bounds, cell):
        calls.append(cell)
        if cell < 3.0:
            raise OSError("no fine terrain")
        return _read(bounds, cell)
    s = _culvert()
    S.reconcile_inverts([s], read, 3.0 * FT, 3.0)
    assert calls == [3.0, 1.0], calls
    assert "datum shift" in s.invert_note, s.invert_note      # as before 3.10.96


# ------------------------------------------------------------------ the ANUGA bed floor --
class _Inlet:
    def __init__(self, tri, enq, inv):
        self.triangle_indices, self.enquiry_index, self.invert_elevation = tri, enq, inv


class _Op:
    def __init__(self, inv):
        self.inlets = [_Inlet([0, 1, 2], 3, inv[0]), _Inlet([4, 5], 6, inv[1])]
        self.invert_elevations = list(inv)


class _Dom:
    def __init__(self):
        # 3034-like: exchange triangles reach onto the road fill; enquiry beds at the channel
        el = np.array([528.2, 531.9, 533.2, 527.93, 527.3, 534.1, 527.04])
        self.quantities = {"elevation": type("Q", (), {"centroid_values": el})()}


@test
def end_beds_record_the_exchange_and_enquiry_beds():
    e = AW.culvert_end_beds(_Dom(), _Op([527.304, 526.390]))
    assert e[0] == {"exchange_bed_min_m": 528.2, "exchange_bed_mean_m": 531.1,
                    "exchange_triangles": 3, "enquiry_bed_m": 527.93,
                    "invert_given_m": 527.304}, e[0]
    assert e[1]["exchange_bed_mean_m"] == 530.7 and e[1]["enquiry_bed_m"] == 527.04, e[1]


@test
def the_floor_is_the_enquiry_or_channel_bed_never_the_exchange_minimum():
    op = _Op([527.304, 526.390])
    e = AW.culvert_end_beds(_Dom(), op)
    assert AW.apply_bed_floor(op, e, "enquiry") == 2
    assert op.invert_elevations == [527.93, 527.04], op.invert_elevations   # not 528.2 / 527.3
    assert e[0]["raised_m"] == 0.626 and e[0]["floor_source"] == "enquiry", e[0]
    op = _Op([527.304, 526.390])
    e = AW.culvert_end_beds(_Dom(), op)
    assert AW.apply_bed_floor(op, e, "channel", [527.90, None]) == 2
    assert op.invert_elevations == [527.90, 527.04], op.invert_elevations
    assert e[0]["floor_source"] == "channel" and e[1]["floor_source"] == "enquiry"
    op = _Op([527.304, 526.390])
    e = AW.culvert_end_beds(_Dom(), op)
    assert AW.apply_bed_floor(op, e, "off") == 0 and op.invert_elevations == [527.304, 526.390]
    op = _Op([527.90, 527.00])                 # within the 0.05 m tolerance: left alone
    e = AW.culvert_end_beds(_Dom(), op)
    assert AW.apply_bed_floor(op, e, "enquiry") == 0 and op.invert_elevations == [527.90, 527.00]
    assert AW.bed_floor_mode({}) == "off" and AW.bed_floor_mode({"bed_floor": "Channel"}) == \
        "channel" and AW.bed_floor_mode({"bed_floor": "exchange"}) == "off"


@test
def bed_floor_setting_and_specs_are_unchanged_when_off():
    assert S.bed_floor_setting({}) == "off"
    assert S.bed_floor_setting({S.BED_FLOOR_ENV: "Enquiry"}) == "enquiry"
    try:
        S.bed_floor_setting({S.BED_FLOOR_ENV: "exchange"})
        raise AssertionError("an unknown floor must be refused")
    except ValueError as e:
        assert "off, enquiry, channel" in str(e), e
    dem = _read((0.0, 0.0, 120.0, 120.0), 3.0)
    s = _culvert()
    s.invert_channel_bed = (527.9, 526.6)
    off, _ = S.anuga_culverts([s], dem)
    assert "bed_floor" not in off[0] and "channel_bed_m" not in off[0], off[0]
    enq, _ = S.anuga_culverts([s], dem, bed_floor="enquiry")
    assert enq[0]["bed_floor"] == "enquiry" and "channel_bed_m" not in enq[0]
    ch, _ = S.anuga_culverts([s], dem, bed_floor="channel")
    assert ch[0]["channel_bed_m"] == [527.9, 526.6], ch[0]
    rec = S.invert_record(ch)
    assert rec[0]["bed_floor"] == "channel" and rec[0]["channel_bed_m"] == [527.9, 526.6]
    assert "bed_floor" not in S.invert_record(off)[0]


@test
def bed_notes_name_phantom_head_raised_ends_and_operator_imbalance():
    st = {"culvert_enquiry": [{"op_index": 0, "ends": [
        {"invert_used_m": 527.304, "enquiry_bed_m": 527.93},
        {"invert_used_m": 526.9, "enquiry_bed_m": 527.04}]}],
        "operator_volumes": [{"op": 0, "name": "Boyd", "removed_m3": 1000.0,
                              "added_m3": 1000.5, "net_m3": 0.5, "calls": 10}]}
    n = AM.culvert_bed_notes(st)
    assert len(n) == 1 and "Boyd: the upstream invert (527.30 m = 1730.00 ft) is 0.63 m (2.1 ft) " \
        "below" in n[0], n
    st["operator_volumes"][0]["added_m3"] = 1100.0
    n = AM.culvert_bed_notes(st)
    assert len(n) == 2 and "added 1,100 m3 and removed 1,000 m3 (+10.00%)" in n[1], n
    st["culvert_enquiry"][0]["ends"][0].update(raised_m=0.626, floor_source="enquiry",
                                                floor_m=527.93, invert_used_m=527.93)
    n = AM.culvert_bed_notes(st)
    assert "upstream invert raised 0.63 m (2.1 ft) to the enquiry bed (527.93 m = 1732.05 ft" \
        in n[0], n
    assert AM.culvert_bed_notes({}) == []


@test
def losses_field_and_values():
    assert S.losses_field(["Name", "LOSSES", "kind"]) == "LOSSES"
    assert S.losses_field(["name", "k_loss"]) == "k_loss" and S.losses_field(["name"]) == ""
    assert S.losses_value(None) == (None, True) and S.losses_value("") == (None, True)
    assert S.losses_value(float("nan")) == (None, True)
    assert S.losses_value(1.5) == (1.5, True) and S.losses_value("0") == (0.0, True)
    assert S.losses_value(-1) == (None, False) and S.losses_value(25) == (None, False)
    assert S.losses_value("abc") == (None, False)
    s = _culvert()
    s.losses = 1.5
    specs, _ = S.anuga_culverts([s], _read((0.0, 0.0, 120.0, 120.0), 3.0))
    assert specs[0]["losses"] == 1.5


@test
def algorithms_reads_the_losses_field():
    src = open(os.path.join(os.path.dirname(HERE), "algorithms.py")).read()
    assert "loss_f = structmod.losses_field(names)" in src
    assert "structmod.losses_value(qio.num(at.get(loss_f)))" in src


# -------------------------------------------------------------- with ANUGA (model tests) --
def _job(d, floor=None):
    """A 400 x 200 m valley with a 4 m road across it at x = 190-210 and a 1.5 m box culvert
    under it whose inverts are 1 m below the valley floor; 5 m3/s for 600 s at x = 60."""
    nx, ny, dx = 80, 40, 5.0
    X, Y = np.meshgrid((np.arange(nx) + 0.5) * dx, (np.arange(ny) + 0.5) * dx)
    dem = 100.0 + 0.002 * (400.0 - X) + 0.01 * np.abs(Y - 100.0)
    dem = np.where((X > 190) & (X < 210), 104.0, dem)
    x0, y0 = 500000.0, 5200200.0
    t = np.array([0.0, 600.0])
    q = np.array([5.0, 5.0])
    zu, zd = 100.0 + 0.002 * 220.0, 100.0 + 0.002 * 180.0
    c = {"end_points": [[x0 + 180.0, y0 - 100.0], [x0 + 220.0, y0 - 100.0]],
         "invert_elevations": [zu - 1.0, zd - 1.0], "width": 1.5, "height": 1.0,
         "barrels": 1, "shape": "box", "manning": 0.013, "losses": 1.5, "apron": 2.0,
         "name": "C1", "invert_source": ["given", "given"], "enquiry_gap": 2.0}
    if floor:
        c["bed_floor"] = floor
    np.savez(os.path.join(d, "j.npz"), dem=dem.astype(np.float32), dem_x0=x0, dem_y0=y0,
             dem_dx=dx, manning_n=0.05, manning=np.zeros(0),
             boundary=np.array([[x0 + 5, y0 - 195], [x0 + 395, y0 - 195],
                                [x0 + 395, y0 - 5], [x0 + 5, y0 - 5]]),
             fine=np.zeros((0, 2)), inlet=np.array([[x0 + 60, y0 - 80], [x0 + 60, y0 - 120]]),
             fine_area=100.0, coarse_area=400.0, hyd_t=t, hyd_q=q, sim_time=600.0,
             yieldstep=300.0, arrival_depth=0.3048, velocity_min_depth=0.03,
             algorithm="DE0", threads=1, out_x0=x0, out_y0=y0, out_dx=dx, out_nx=nx,
             out_ny=ny, culverts_json=json.dumps([c]))
    return zu - 1.0


def _run(d, py):
    env = dict(AM._clean_env(), OMP_NUM_THREADS="1")
    subprocess.run([py, "-u", AW.__file__, "j.npz", "r.npz"], cwd=d, env=env, check=True,
                   stdout=subprocess.DEVNULL, timeout=600)
    with np.load(os.path.join(d, "r.npz")) as z:
        return json.loads(str(z["stats"]))


@model_test
def the_worker_records_end_beds_raises_to_the_floor_and_logs_operator_volumes():
    py = os.environ.get("NDBREACH_ANUGA_PYTHON")
    if not (py and os.path.isfile(py)):
        print("skip: NDBREACH_ANUGA_PYTHON not set")
        return
    moved = {}
    for floor in (None, "enquiry"):
        d = tempfile.mkdtemp()
        try:
            given_up = _job(d, floor)
            st = _run(d, py)
            assert not st["culvert_errors"], st["culvert_errors"]
            w = st["culvert_enquiry"][0]
            e = w["ends"]
            assert len(e) == 2 and all(r["exchange_triangles"] > 0 for r in e), w
            assert abs(e[0]["invert_given_m"] - given_up) < 1e-3, e[0]
            bed = e[0]["enquiry_bed_m"]
            assert bed - given_up > 0.8, e[0]
            if floor is None:
                assert "bed_floor" not in w and e[0]["invert_used_m"] == e[0]["invert_given_m"]
                n = AM.culvert_bed_notes(st)
                assert any("below the bed the operator reads its head on" in x for x in n), n
            else:
                assert w["bed_floor"] == "enquiry" and abs(e[0]["invert_used_m"] - bed) < 1e-3
                assert abs(e[0]["raised_m"] - (bed - given_up)) < 2e-3, e[0]
            ov = st["operator_volumes"]
            assert len(ov) == 1 and ov[0]["name"] == "C1" and ov[0]["calls"] > 0, ov
            assert w["op_index"] == 0 and abs(ov[0]["net_m3"]) < 1e-3 * max(1.0, ov[0]["removed_m3"])
            moved[floor] = ov[0]["removed_m3"]
            rows = open(os.path.join(d, AW.OPVOL_CSV)).read().splitlines()
            assert rows[0] == ",".join(AW.OPVOL_FIELDS) and len(rows) == 4, rows   # t = 0, 300, 600 s
            print("floor {}: ends {} operator {}".format(floor, json.dumps(e), json.dumps(ov)))
        finally:
            shutil.rmtree(d)
    # the phantom head (inverts 1 m below a dry bed) moves water the floor does not
    assert moved[None] > 2.0 * moved["enquiry"], moved


@test
def the_channel_replaces_the_disc_only_when_it_reads_half_a_foot_lower():
    from nd_breach_runner.core import structures as S
    assert S.end_bed(528.26, 527.90) == 527.90                 # 3034 upstream: the toe slope
    assert S.end_bed(99.96, 99.90) == 99.96                    # a 0.2 % channel's own fall
    assert S.end_bed(None, 99.9) == 99.9 and S.end_bed(100.0, None) == 100.0
    assert S.end_bed(None, None) is None
