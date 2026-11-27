"""Road-crossing carving, triage envelope and domain helpers on synthetic terrain."""
import os, sys, math
import numpy as np
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core import gridutil as gu, hydrocond as hc, triage as tg, domain as dm  # noqa: E402

cell = 5.0
ny, nx = 300, 200                       # 1.5 km long (north->south), 1 km wide
gt = (0.0, cell, 0.0, ny * cell, 0.0, -cell)
yy, xx = np.mgrid[0:ny, 0:nx]
x_m = (xx - nx / 2) * cell
dem = 100.0 - yy * cell * 0.002 + 0.02 * np.abs(x_m) ** 1.2     # valley draining south, slope 0.002
thalweg_x = 0.0

# road embankment across the valley at row 150 (4 m fill, 6 cells wide)
road_rows = slice(148, 154)
dem_road = dem.copy()
crown = dem[151, nx // 2] + 4.0
dem_road[road_rows, :] = np.maximum(dem_road[road_rows, :], crown)
# a "dam" (protected) at row 60
dem_road[58:62, :] = np.maximum(dem_road[58:62, :], dem[60, nx // 2] + 5.0)

# ---------- polyline cells are 4-connected
diag = [(10.0, 10.0), (500.0, 800.0)]
cells = gu.polyline_cells(diag, gt, dem.shape)
assert all(abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1 for a, b in zip(cells[:-1], cells[1:])), "not 4-connected"

# ---------- carving: stream line down the thalweg (digitised upstream -> downstream)
stream = [(thalweg_x + nx / 2 * cell + 2.5, ny * cell - 2.5), (thalweg_x + nx / 2 * cell + 2.5, 2.5)]
protect = np.zeros(dem.shape, bool); protect[56:64, :] = True
cond, info = hc.breach_barriers(dem_road, [stream], gt, protect=protect)
col = nx // 2
print("carving:", info)
assert info["cuts"] == 1 and info["skipped_protected"] == 1
assert cond[151, col] < dem_road[151, col] - 3.0              # road cut
assert cond[60, col] == dem_road[60, col]                     # dam kept
assert np.all(np.diff(cond[62:, col]) <= 1e-9)               # monotonic below the dam
# reversed digitising gives the same result
cond2, _ = hc.breach_barriers(dem_road, [stream[::-1]], gt, protect=protect)
assert np.allclose(cond, cond2)
# roads mask required but absent at the crossing -> skipped
noroad = np.zeros(dem.shape, bool)
_, i3 = hc.breach_barriers(dem_road, [stream], gt, protect=protect, roads=noroad)
assert i3["cuts"] == 0 and i3["skipped_no_road"] == 1

# ---------- normal depth: wide rectangular channel, strip method is exact
w = 21; xs = np.full(w, 50.0); xs[:] = 10.0; xs[0] = xs[-1] = 60.0
n, S, d_true = 0.04, 0.001, 2.0
q = (w - 2) * cell * (1 / n) * d_true ** (5 / 3) * math.sqrt(S)
wse, width, area = tg.normal_depth_stage(xs, q, S, n, cell)
assert abs((wse - 10.0) - d_true) < 1e-3, wse

# ---------- trace path escapes the road pit on the raw DEM
path, end = tg.trace_path(dem_road, (70, col))
assert end == "edge" and path[-1][0] >= ny - 2, (end, path[-1])
assert any(r > 154 for r, _ in path)

# ---------- envelope: small vs big release, terrace structure stays dry
res_small = tg.envelope(cond, gt, (70, col), q_peak=20.0, volume_m3=2.0e3,
                        n=0.05, half_width_m=400)
res_big = tg.envelope(cond, gt, (70, col), q_peak=800.0, volume_m3=5.0e6,
                      n=0.05, half_width_m=400)
print("small: reach %.0f m end %s | big: reach %.0f m end %s edge %s"
      % (res_small["reach_m"], res_small["end"], res_big["reach_m"], res_big["end"], res_big["edge"]))
assert res_small["end"] == "volume" and res_small["reach_m"] < res_big["reach_m"]
assert res_big["edge"]                                       # big flood leaves the clip -> needs 2D / bigger clip
assert res_small["mask"][100, col] and not res_small["mask"][250, col]
terrace = (100, col + 90)                                    # 450 m off the thalweg, ~ +32 m
assert not res_big["mask"][terrace]
wider = res_big["mask"].sum() > res_small["mask"].sum()
assert wider

# ---------- coarse screen matches the full-resolution envelope
scr = tg.screen(cond, gt, (70, col), q_peak=800.0, volume_m3=5.0e6, target_cell=10.0, n=0.05, half_width_m=400)
ratio = scr["mask"].sum() / res_big["mask"].sum()
print("coarse screen wet area / full-res: %.2f (factor %d)" % (ratio, scr["factor"]))
assert 0.8 < ratio < 1.25 and scr["edge"] and not scr["mask"][terrace]

# ---------- domain helpers
assert dm.auto_cell(50 * dm.AF_M3) == 3.0 and dm.auto_cell(5e4 * dm.AF_M3) == 20.0
assert dm.auto_cell(5e4 * dm.AF_M3, bavg_m=12.0, dem_cell=1.0) == 6.0
path_c, _ = tg.trace_path(cond, (70, col))           # conditioned terrain: straight down the valley
assert all(c == col for _, c in path_c)
m = dm.corridor_mask(dem.shape, cell, path_c, (70, col), 200.0)
print("corridor keeps %.0f%% of the square clip" % (100 * m.mean()))
assert m.mean() < 0.6 and m[200, col] and not m[200, 5]
depth = np.zeros(dem.shape); depth[200:220, col - 45:col + 45] = 1.0
e, wl = dm.touches_wall(depth, m)
assert wl and not e
depth2 = np.zeros(dem.shape); depth2[-1, col] = 1.0
assert dm.touches_wall(depth2, m)[0]
arr = np.full(dem.shape, 0.5); d = np.ones(dem.shape)
assert not dm.still_growing(arr, d, 3600)
arr[:10, :] = 0.95
assert dm.still_growing(arr, d, 3600)

# ================= 0.52.0 downstream corridor (numpy only, no scipy)
rng = np.random.default_rng(7)
# ---------- PointIndex == brute force
P = rng.uniform(0, 3000, (500, 2)); Q = rng.uniform(-200, 3200, (300, 2))
dq, jq = tg.PointIndex(P, 100.0).nearest(Q, 250.0)
bf = np.hypot(Q[:, None, 0] - P[None, :, 0], Q[:, None, 1] - P[None, :, 1])
near = bf.min(1) <= 250.0
assert np.allclose(np.where(near, bf.min(1), np.inf), dq) and np.all(jq[near] == bf.argmin(1)[near])
assert np.all(jq[~near] == -1)
assert 0 in tg.PointIndex(P, 100.0).within(P[0], 0.0)

# ---------- densify: ends kept, no step over the spacing
xy3 = np.array([[0, 0], [95, 0], [95, 333]], float)
dd = tg.densify(xy3, 10.0)
assert np.hypot(*np.diff(dd, axis=0).T).max() <= 10.0 + 1e-9
assert np.allclose(dd[0], xy3[0]) and np.allclose(dd[-1], xy3[-1])

# ---------- network: main stem, divergence, 60 m digitising gap, open end, cut at d_max
L1 = np.array([[0, -100], [0, -3000]], float)
L2 = np.array([[0, -3000], [0, -6000]], float)
L3 = np.array([[0, -6000], [-2000, -9000]], float)             # branch west
L4 = np.array([[0, -6000], [2000, -9000]], float)              # branch east: ends 9.2 km out (open end)
L5 = np.array([[-2000, -9060], [-2000, -20000]], float)        # starts 60 m below L3's end (gap)
tpath = np.column_stack([np.zeros(151), -np.arange(151) * 10.0])  # tool 6 path: toe (0,0) -> (0,-1500)
net = tg.StreamNet([L1, L2, L3, L4, L5])
cor = tg.corridor_axis(tpath, "edge", net, waterbodies=None, d_max=10000.0)
ax = cor["axis"]
assert (ax[:, 0] < -1500).any() and (ax[:, 0] > 1500).any(), "both branches of the divergence"
assert (ax[:, 1] < -9500).any() and (ax[:, 0][ax[:, 1] < -9500] < -1900).all(), "gap bridged onto L5"
assert cor["line_m"].max() <= 10000.0 + 1e-6 and not cor["flags"]
assert len(cor["open_ends"]) == 1 and np.allclose(cor["open_ends"][0][0], [2000, -9000]), cor["open_ends"]
assert {k for _, k in cor["parts"]} == {"p", "n"} and np.allclose(cor["toe"], [0, 0])
# D shorter than the east branch: no open end, every branch that ends or is cut beyond d_max is complete
cor5 = tg.corridor_axis(tpath, "edge", net, d_max=5000.0)
assert not cor5["open_ends"] and not cor5["flags"] and cor5["line_m"].max() <= 5000.0 + 1e-6
assert cor5["stats"]["reach_m"] <= 5000.0 + 1e-6
# a reach that leaves the D circle and comes back inside it is still checked: a creek runs to 15.5 km and
# joins a river that turns back past a house 8 km from the dam (cut at the first exit, the house was 6.3 km
# from the axis and the dam cleared)
trib = np.array([[0, -1490], [0, -15500]], float)
riv = np.array([[0, -15500], [6000, -12000], [6000, 20000]], float)
cre = tg.corridor_axis(tpath, "edge", tg.StreamNet([trib, riv]), d_max=15000.0)
dh, _ = tg.PointIndex(cre["axis"], 500.0).nearest(np.array([[6300.0, -5000.0]]), 500.0)
assert not cre["flags"] and not cre["open_ends"] and dh[0] <= 300.0 + 1e-6, (cre["flags"], cre["open_ends"], dh)
assert cre["line_m"].max() <= 15000.0 + 1e-6 and ((cre["axis"][:, 0] > 5900) & (cre["axis"][:, 1] > 13000)).any()

# ---------- unlinked exit / stuck path / no path -> flags (needs 2D)
far = tg.StreamNet([Lk + [1000.0, 0.0] for Lk in (L1, L2, L3, L4, L5)])
assert any("leaves the terrain clip" in f for f in tg.corridor_axis(tpath, "edge", far, d_max=10000.0)["flags"])
assert any("stuck" in f for f in tg.corridor_axis(tpath, "stuck", net, d_max=10000.0)["flags"])
assert tg.corridor_axis(tpath[:1], "edge", net)["flags"]
# terminal basin with no stream line near the path end -> an open end (a closed-basin proof needed)
cb = tg.corridor_axis(tpath, "terminal basin", far, d_max=10000.0)
assert not cb["flags"] and [k for _, _, k in cb["open_ends"]] == ["path"]

# ---------- along-path cap: a meander that never gets 2 km from the toe
t_ = np.linspace(0, 60 * math.pi, 20000)
mea = np.column_stack([600 * np.sin(t_), -np.linspace(100, 1900, len(t_))])
capped = tg.corridor_axis(tpath, "edge", tg.StreamNet([mea]), d_max=2000.0, along_cap=5.0)
assert any("channel followed" in f for f in capped["flags"]), capped["flags"]

# ---------- waterbodies: the shore joins the axis; an outlet that leaves the lake resolves the end
lake = np.array([[1800, -9200], [2200, -9200], [2200, -8800], [1800, -8800], [1800, -9200]], float)
L6 = np.array([[2000, -9200], [2000, -15000]], float)          # starts on the south shore, leaves the lake
wbf = lambda bbox: [(("lake", 0), lake)]
c2 = tg.corridor_axis(tpath, "edge", tg.StreamNet([L1, L2, L3, L4, L5, L6]), waterbodies=wbf, d_max=10000.0)
on_l6 = lambda c: ((c["axis"][:, 1] < -9500) & (c["axis"][:, 0] > 1900)).any()   # below the lake, on the outlet
assert not c2["open_ends"] and (c2["src"] == "w").any() and on_l6(c2), "the outlet is walked, not only flagged"
assert "w" in {k for _, k in c2["parts"]}
c3 = tg.corridor_axis(tpath, "edge", net, waterbodies=wbf, d_max=10000.0)          # no outlet: still open
assert len(c3["open_ends"]) == 1 and (c3["src"] == "w").any() and not on_l6(c3)
# an artificial path that starts on the shore and ends inside the lake is NOT an outlet
L7 = np.array([[2000, -9200], [2000, -9000]], float)
c4 = tg.corridor_axis(tpath, "edge", tg.StreamNet([L1, L2, L3, L4, L5, L7]), waterbodies=wbf, d_max=10000.0)
assert len(c4["open_ends"]) >= 1
# a 200 m wide river polygon whose banks are > 30 m from the axis still counts (axis inside it)
river = np.array([[-100, -4000], [100, -4000], [100, -2000], [-100, -2000], [-100, -4000]], float)
c5 = tg.corridor_axis(tpath, "edge", net, waterbodies=lambda b: [(("river", 0), river)], d_max=10000.0)
assert (c5["src"] == "w").any()

# ---------- closed basin proof
bc = 10.0
by, bx = np.mgrid[0:200, 0:200]
r2 = ((by - 100) ** 2 + (bx - 100) ** 2) * bc ** 2
bowl = 100.0 + 2.0 * np.minimum(r2 / 400.0 ** 2, 1.0) + 0.001 * bx    # 2 m deep, 400 m radius, tilted rim
res = tg.closed_basin(bowl, (100, 100), 1.0e5, bc)
assert res["holds"] and res["volume_m3"] > 4.0e5, res
assert not tg.closed_basin(bowl, (100, 100), 5.0e6, bc)["holds"]
assert "runs off" in tg.closed_basin(bowl[:, 60:], (100, 40), 1.0, bc)["why"]    # cut by the window edge
assert "no closed depression" in tg.closed_basin(100.0 - 0.01 * by, (100, 100), 1.0, bc)["why"]

# ---------- state margin: + inside, - outside
sq = np.array([[0, 0], [10000, 0], [10000, 10000], [0, 10000], [0, 0]], float)
sm = tg.state_margin(np.array([[5000, 5000], [9800, 5000], [10200, 5000]]), sq)
assert abs(sm[0] - 5000) < 1e-6 and abs(sm[1] - 200) < 1e-6 and abs(sm[2] + 200) < 1e-6

# ---------- the DWR-2636 pattern in miniature: a volume-cut envelope misses a house the corridor sees
vny, vnx, vc = 600, 200, 5.0
vgt = (0.0, vc, 0.0, vny * vc, 0.0, -vc)
VY, VX = np.mgrid[0:vny, 0:vnx]
vdem = 100.0 - VY * vc * 0.002 + 0.02 * np.abs((VX - vnx / 2) * vc) ** 1.2
venv = tg.envelope(vdem, vgt, (20, vnx // 2), q_peak=20.0, volume_m3=2.0e3, n=0.05, half_width_m=400)
assert venv["end"] == "volume" and venv["reach_m"] < 1000
assert venv.get("trace_end") not in (None, "volume"), venv.get("trace_end")
house = gu.rc_to_xy(vgt, 500, vnx // 2 + 20)                          # 100 m off the valley floor, 2.4 km down
assert not venv["mask"][500, vnx // 2 + 20]
assert venv["path"][-1][0] == vny - 1                                  # the path runs on to the grid edge
pxy = np.array([gu.rc_to_xy(vgt, r, c) for r, c in venv["path"]])     # the FULL path, not cut at the volume stop
# the stream line starts 1.5 km down, below the volume stop: only the full path reaches it
vnet = tg.StreamNet([np.array([gu.rc_to_xy(vgt, 300, vnx // 2), gu.rc_to_xy(vgt, vny - 1, vnx // 2)])])
vcor = tg.corridor_axis(pxy, venv["trace_end"], vnet, d_max=10000.0)
dist, _ = tg.PointIndex(vcor["axis"], 500.0).nearest(np.array([house]), 500.0)
assert np.isfinite(dist[0]) and dist[0] <= 110.0 and not vcor["flags"], (dist, vcor["flags"])
palong = np.concatenate(([0.0], np.cumsum(np.hypot(*np.diff(pxy, axis=0).T))))
vcut = tg.corridor_axis(pxy[:int(np.searchsorted(palong, venv["reach_m"])) + 1], venv["trace_end"], vnet,
                        d_max=10000.0)                                # the path cut at reach_m would miss it
assert vcut["flags"] and not np.isfinite(tg.PointIndex(vcut["axis"], 500.0).nearest(np.array([house]), 500.0)[0][0])
print("corridor: divergence, gap, D cut and re-entry, flags, along cap, waterbodies, closed basin, "
      "state margin OK")
print("OK")
