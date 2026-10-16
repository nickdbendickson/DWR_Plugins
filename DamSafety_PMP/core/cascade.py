"""
Dynamic fill-and-spill (cascade) of prairie potholes. Pure numpy.

Network (pothole_network.json written by ndpmp:pothole_storage):
  each pothole k has storage capacity V_k (m3), local catchment A_k (m2, cells whose runoff first enters k,
  including k itself) and a spill target (another pothole id, 0 = basin outlet, -1 = other exit).
  direct_to_outlet_m2 drains to the outlet without passing any pothole.

Each timestep: local runoff e_t*A_k enters pothole k; potholes are processed upstream -> downstream; storage
above V_k spills (instantly, within the step) to the target. Runoff reaching the outlet is returned as an
effective excess depth over the TOTAL basin area. No infiltration/evaporation from potholes, no travel time
between potholes (the basin UH provides the timing). Initial storage = initial_full_frac * V_k.
"""
import csv
import json

import numpy as np

M3_PER_ACFT = 1233.48183754752
M2_PER_MI2 = 2589988.110336


def load_network(path):
    with open(path) as fh:
        return json.load(fh)


def _order(pots):
    """Topological order (upstream first): sort by number of hops to a terminal."""
    tgt = {p["id"]: p["target"] for p in pots}
    hops = {}
    for k in tgt:
        n, c, seen = 0, k, set()
        while c in tgt and c not in seen:
            seen.add(c); c = tgt[c]; n += 1
        hops[k] = n
    return sorted(tgt, key=lambda k: -hops[k])


def simulate(net, excess_in, dt_hr, initial_full_frac=0.0, other_exit_to_outlet=True, scale_area_to=None):
    """excess_in: incremental runoff depth (in) per step over the land surface.
    scale_area_to: if given (m2), all network areas are scaled to this basin area (e.g. when the hyetograph
    area differs from the DEM-derived area); storage volumes are scaled by the same ratio.
    Returns dict of arrays."""
    pots = net["potholes"]
    ratio = (scale_area_to / net["basin_m2"]) if scale_area_to else 1.0
    V = {p["id"]: p["volume_m3"] * ratio for p in pots}
    A = {p["id"]: p["catchment_m2"] * ratio for p in pots}
    T = {p["id"]: p["target"] for p in pots}
    basin = net["basin_m2"] * ratio
    direct = net["direct_to_outlet_m2"] * ratio + (net["to_other_exit_m2"] * ratio if other_exit_to_outlet else 0)
    order = _order(pots)
    S = {k: initial_full_frac * V[k] for k in V}
    n = len(excess_in)
    out = {k: np.zeros(n) for k in ("outlet_m3", "stored_m3", "lost_other_exit_m3", "contrib_m2", "n_full")}
    # contributing area = direct + catchments of potholes whose whole chain to the outlet is full
    for t in range(n):
        e_m = excess_in[t] * 0.0254
        inflow = {k: e_m * A[k] for k in V}
        to_outlet = e_m * direct
        lost = 0.0
        for k in order:
            S[k] += inflow[k]
            if S[k] > V[k]:
                sp = S[k] - V[k]; S[k] = V[k]
                j = T[k]
                if j > 0:
                    inflow[j] += sp
                elif j == 0 or other_exit_to_outlet:
                    to_outlet += sp
                else:
                    lost += sp
        full = {k: S[k] >= V[k] * (1 - 1e-9) for k in V}
        conn = {}
        def connected(k):
            if k in conn:
                return conn[k]
            c, ok, seen = k, True, []
            while c > 0:
                if c in conn:
                    ok = ok and conn[c]; break
                seen.append(c)
                if not full[c]:
                    ok = False
                c = T[c]
            if c == -1 and not other_exit_to_outlet:
                ok = False
            for s_ in seen:
                conn[s_] = ok if s_ == seen[0] else conn.get(s_, ok)
            conn[k] = ok
            return ok
        ca = direct + sum(A[k] for k in V if connected(k))
        out["outlet_m3"][t] = to_outlet
        out["stored_m3"][t] = sum(S.values())
        out["lost_other_exit_m3"][t] = lost
        out["contrib_m2"][t] = ca
        out["n_full"][t] = sum(full.values())
    out["outlet_excess_in"] = out["outlet_m3"] / basin / 0.0254
    out["basin_m2"] = basin
    out["capacity_m3"] = sum(V.values())
    return out


def write_timeseries(path, t_hr, excess_in, sim, meta=None):
    with open(path, "w", newline="") as fh:
        for k, v in (meta or {}).items():
            fh.write("# %s: %s\n" % (k, v))
        fh.write("# basin_area_mi2: %.6f\n# pothole_capacity_acft: %.3f\n" % (sim["basin_m2"] / M2_PER_MI2,
                                                                           sim["capacity_m3"] / M3_PER_ACFT))
        w = csv.writer(fh)
        w.writerow(["time_hr", "excess_in", "outlet_excess_in", "storage_loss_in", "stored_acft",
                    "contributing_area_mi2", "contributing_fraction", "noncontributing_area_mi2", "n_full"])
        for i, t in enumerate(t_hr):
            ca = sim["contrib_m2"][i]
            w.writerow(["%.6g" % t, "%.6f" % excess_in[i], "%.6f" % sim["outlet_excess_in"][i],
                        "%.6f" % (excess_in[i] - sim["outlet_excess_in"][i]),
                        "%.4f" % (sim["stored_m3"][i] / M3_PER_ACFT), "%.6f" % (ca / M2_PER_MI2),
                        "%.6f" % (ca / sim["basin_m2"]), "%.6f" % ((sim["basin_m2"] - ca) / M2_PER_MI2),
                        int(sim["n_full"][i])])
    return path
