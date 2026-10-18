"""
Event inflow hydrograph from an nd_pmp hyetograph CSV (pure Python + numpy, no QGIS).

Losses   : SCS Curve Number (optional AMC II->III, Ia ratio)  |  initial + constant
Transform: SCS dimensionless UH (lag or Tc, peak rate factor, optional PMF peaking adjustment)  |  Clark UH
Baseflow : constant  |  exponential recession Q0 * k**(t/24)

Units: depth in inches, area in mi^2, time in hours, flow in cfs.
1 inch over 1 mi^2 = 2,323,200 ft^3 = 53.333 ac-ft.
"""
import csv
import os
from dataclasses import dataclass, asdict

import numpy as np

FT3_PER_IN_MI2 = 5280.0 ** 2 / 12.0          # 2,323,200
ACFT_PER_IN_MI2 = 640.0 / 12.0                # 53.3333

# SCS (NRCS NEH-630 Ch.16 Table 16-1) dimensionless unit hydrograph, t/Tp vs q/qp
SCS_DUH_T = [0, .1, .2, .3, .4, .5, .6, .7, .8, .9, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 2.0, 2.2,
             2.4, 2.6, 2.8, 3.0, 3.2, 3.4, 3.6, 3.8, 4.0, 4.5, 5.0]
SCS_DUH_Q = [0, .03, .1, .19, .31, .47, .66, .82, .93, .99, 1.0, .99, .93, .86, .78, .68, .56, .46, .39, .33, .28,
             .207, .147, .107, .077, .055, .04, .029, .021, .015, .011, .005, 0]


def read_hyetograph(path):
    hdr = {}
    rows = []
    with open(path) as fh:
        for line in fh:
            if line.startswith("#"):
                k, _, v = line[1:].partition(":")
                hdr[k.strip()] = v.strip()
            elif line.strip() and not line.startswith("time_hr"):
                rows.append([float(x) for x in line.strip().split(",")])
    a = np.array(rows, float)
    if a.size == 0:
        raise ValueError("No data rows in %s" % path)
    t = a[:, 0]
    dt = float(t[0] if len(t) == 1 else np.median(np.diff(t)))
    dt = round(dt * 60.0) / 60.0          # snap to whole minutes (CSV times are rounded to 6 dp)
    if not np.allclose(np.diff(np.concatenate([[0.0], t])), dt, atol=1e-4):
        raise ValueError("Hyetograph %s is not on a uniform timestep" % path)
    hdr["storm_id"] = os.path.splitext(os.path.basename(path))[0]
    return hdr, dt, a[:, 1]


# ---------------------------------------------------------------- losses
def cn_amc3(cn2):
    """AMC II -> III (Chow, Maidment & Mays 1988 eq. 5.5.4; NEH-630 Ch.10 Table 10-1 approximation)."""
    return 23.0 * cn2 / (10.0 + 0.13 * cn2)


def scs_cn_excess(precip_incr, cn, ia_ratio=0.2, amc3=False):
    cn = cn_amc3(cn) if amc3 else float(cn)
    if not 0 < cn <= 100:
        raise ValueError("CN must be in (0,100]")
    S = 1000.0 / cn - 10.0
    Ia = ia_ratio * S
    P = np.cumsum(precip_incr)
    Q = np.where(P > Ia, (P - Ia) ** 2 / np.maximum(P - Ia + S, 1e-12), 0.0)
    return np.diff(np.concatenate([[0.0], Q])), {"cn_used": cn, "S_in": S, "Ia_in": Ia}


def init_const_excess(precip_incr, dt, initial_in, rate_in_hr):
    ex = np.zeros_like(precip_incr)
    ia_left = initial_in
    for i, p in enumerate(precip_incr):
        take = min(p, ia_left); ia_left -= take; p -= take
        if ia_left <= 0:
            ex[i] = max(0.0, p - rate_in_hr * dt)
    return ex, {"initial_in": initial_in, "constant_in_hr": rate_in_hr}


# ---------------------------------------------------------------- unit hydrographs (cfs per inch of excess)
def scs_uh(area_mi2, dt, lag_hr, prf=484.0, peaking_pct=0.0, sampling="point"):
    """SCS UH. Tp = dt/2 + lag. qp = PRF*A/Tp (cfs/in). The curvilinear DUH is scaled from the tabulated
    shape (which corresponds to PRF 484). For a non-484 PRF the time base is rescaled so the volume stays 1 in
    (qp*Tp = PRF*A). PMF peaking adjustment p% raises qp by p% by shortening Tp by 1/(1+p/100) (volume kept).
    Ordinates are sampled at the END of each dt (pulse response) and renormalised to exactly 1 inch."""
    tp = dt / 2.0 + lag_hr
    tp = tp / (1.0 + peaking_pct / 100.0)
    qp = prf * area_mi2 / tp
    tbase_scale = 484.0 / prf            # keeps volume = 1 in for the tabulated shape
    n = int(np.ceil(5.0 * tp * tbase_scale / dt)) + 1
    t = dt * np.arange(1, n + 1)
    if sampling == "point":
        # HEC-HMS style: DUH ordinates interpolated at the end of each dt
        uh = qp * np.interp(t / (tp * tbase_scale), SCS_DUH_T, SCS_DUH_Q, right=0.0)
    else:
        # interval-average of the instantaneous DUH over each dt
        fine = 20
        tt = np.linspace(0, t[-1], n * fine + 1)
        q_inst = qp * np.interp(tt / (tp * tbase_scale), SCS_DUH_T, SCS_DUH_Q, right=0.0)
        uh = np.array([q_inst[i * fine:(i + 1) * fine + 1].mean() for i in range(n)])
    raw_vol_in = uh.sum() * dt * 3600.0 / (area_mi2 * FT3_PER_IN_MI2)
    uh = uh / raw_vol_in
    info = {"Tp_hr": tp, "qp_formula_cfs_per_in": qp, "uh_peak_cfs_per_in": float(uh.max()),
            "uh_raw_volume_in": raw_vol_in, "lag_hr": lag_hr, "prf": prf, "peaking_pct": peaking_pct}
    return uh, info


def clark_uh(area_mi2, dt, tc_hr, r_hr):
    """Clark UH with the HEC-HMS synthetic time-area curve and linear reservoir routing
    (Ca = dt/(R+0.5dt); O2 = Ca*I + (1-Ca)*O1)."""
    def ta(x):
        x = np.clip(x, 0, 1)
        return np.where(x <= 0.5, 1.414 * x ** 1.5, 1 - 1.414 * (1 - x) ** 1.5)
    nt = int(np.ceil(tc_hr / dt))
    cum = ta(dt * np.arange(0, nt + 1) / tc_hr)
    inflow = np.diff(cum) * area_mi2 * FT3_PER_IN_MI2 / (dt * 3600.0)
    ca = dt / (r_hr + 0.5 * dt)
    out, o = [], 0.0
    i = 0
    while True:
        I = inflow[i] if i < len(inflow) else 0.0
        o = ca * I + (1 - ca) * o
        out.append(o); i += 1
        if i > len(inflow) and o < 1e-6 * max(out):
            break
    uh = np.array(out)
    raw = uh.sum() * dt * 3600.0 / (area_mi2 * FT3_PER_IN_MI2)
    return uh / raw, {"tc_hr": tc_hr, "R_hr": r_hr, "uh_peak_cfs_per_in": float((uh / raw).max()),
                      "uh_raw_volume_in": raw}


# ---------------------------------------------------------------- main
@dataclass
class HydroConfig:
    loss: str = "scs_cn"           # scs_cn | init_const
    cn: float = 80.0
    amc3: bool = False
    ia_ratio: float = 0.2
    initial_in: float = 1.0
    constant_in_hr: float = 0.1
    transform: str = "scs"         # scs | clark
    lag_hr: float = None
    tc_hr: float = None            # SCS: lag = 0.6*Tc if lag not given; Clark: Tc
    prf: float = 484.0
    uh_sampling: str = "point"     # point (HEC-HMS-like) | average
    peaking_pct: float = 0.0
    clark_r_hr: float = None
    baseflow: str = "constant"     # constant | recession
    baseflow_cfs: float = 0.0
    recession_k: float = 0.9       # daily recession constant
    area_mi2: float = None         # override header area
    noncontrib_mi2: float = 0.0
    pothole_mode: str = "none"     # none | cascade | abstraction
    pothole_network: str = None    # pothole_network.json from ndpmp:pothole_storage
    pothole_initial_full: float = 0.0   # antecedent fraction of pothole capacity already full (0-1)
    pothole_storage_in: float = None    # abstraction mode: storage depth (in); None = network capacity/area


def compute(hdr, dt, precip, cfg: HydroConfig):
    area = float(cfg.area_mi2) if cfg.area_mi2 else float(hdr.get("area_mi2") or 0)
    area_eff = area - (cfg.noncontrib_mi2 or 0.0)
    if area_eff <= 0:
        raise ValueError("Contributing area must be > 0 (area %s - non-contributing %s)" % (area, cfg.noncontrib_mi2))
    if cfg.loss == "scs_cn":
        ex, linfo = scs_cn_excess(precip, cfg.cn, cfg.ia_ratio, cfg.amc3)
    elif cfg.loss == "init_const":
        ex, linfo = init_const_excess(precip, dt, cfg.initial_in, cfg.constant_in_hr)
    else:
        raise ValueError(cfg.loss)
    pinfo, cas = {}, None
    if cfg.pothole_mode in ("cascade", "abstraction"):
        from . import cascade as C
        net = C.load_network(cfg.pothole_network) if cfg.pothole_network else None
        if cfg.pothole_mode == "cascade":
            if net is None:
                raise ValueError("cascade mode needs pothole_network (pothole_network.json)")
            cas = C.simulate(net, ex, dt, cfg.pothole_initial_full, scale_area_to=area_eff * C.M2_PER_MI2)
            cas["_excess_in"] = ex.copy()
            ex_out = cas["outlet_excess_in"]
            pinfo = {"pothole_capacity_in": cas["capacity_m3"] / cas["basin_m2"] / 0.0254,
                     "pothole_stored_end_in": float(cas["stored_m3"][-1] / cas["basin_m2"] / 0.0254),
                     "pothole_min_contrib_fraction": float(cas["contrib_m2"].min() / cas["basin_m2"]),
                     "pothole_max_contrib_fraction": float(cas["contrib_m2"].max() / cas["basin_m2"])}
        else:
            if cfg.pothole_storage_in is not None:
                cap = float(cfg.pothole_storage_in)
            elif net is not None:
                cap = sum(p_["volume_m3"] for p_ in net["potholes"]) / net["basin_m2"] / 0.0254
            else:
                raise ValueError("abstraction mode needs pothole_storage_in or pothole_network")
            cap *= (1.0 - cfg.pothole_initial_full)
            cum = np.cumsum(ex)
            ex_out = np.diff(np.concatenate([[0.0], np.maximum(cum - cap, 0.0)]))
            pinfo = {"pothole_capacity_in": cap}
        pinfo["excess_before_potholes_in"] = float(ex.sum())
        ex = ex_out
    if cfg.transform == "scs":
        lag = cfg.lag_hr if cfg.lag_hr else (0.6 * cfg.tc_hr if cfg.tc_hr else None)
        if not lag:
            raise ValueError("SCS UH needs lag_hr or tc_hr")
        uh, tinfo = scs_uh(area_eff, dt, lag, cfg.prf, cfg.peaking_pct, cfg.uh_sampling)
    elif cfg.transform == "clark":
        if not (cfg.tc_hr and cfg.clark_r_hr):
            raise ValueError("Clark UH needs tc_hr and clark_r_hr")
        uh, tinfo = clark_uh(area_eff, dt, cfg.tc_hr, cfg.clark_r_hr)
    else:
        raise ValueError(cfg.transform)
    direct = np.convolve(ex, uh)
    n = len(direct)
    t = dt * np.arange(1, n + 1)
    if cfg.baseflow == "recession":
        base = cfg.baseflow_cfs * cfg.recession_k ** (t / 24.0)
    else:
        base = np.full(n, float(cfg.baseflow_cfs))
    flow = direct + base
    p = np.zeros(n); p[:len(precip)] = precip
    e = np.zeros(n); e[:len(ex)] = ex
    vol_in = direct.sum() * dt * 3600.0 / (area_eff * FT3_PER_IN_MI2)
    ipk = int(np.argmax(flow))
    res = {"storm_id": hdr.get("storm_id"), "storm_type": hdr.get("storm_type"), "duration_hr": hdr.get("duration_hr"),
           "temporal_distribution": hdr.get("temporal_distribution"), "area_mi2": area, "contrib_area_mi2": area_eff,
           "precip_in": float(precip.sum()), "excess_in": float(ex.sum()), "peak_cfs": float(flow[ipk]),
           "time_to_peak_hr": float(t[ipk]), "runoff_volume_in": float(vol_in),
           "runoff_volume_acft": float(vol_in * area_eff * ACFT_PER_IN_MI2), "dt_hr": dt,
           "loss": cfg.loss, "transform": cfg.transform, "baseflow": cfg.baseflow}
    res.update({"loss_" + k: v for k, v in linfo.items()})
    res.update({"uh_" + k: v for k, v in tinfo.items()})
    res.update(pinfo); res["pothole_mode"] = cfg.pothole_mode
    res["_cascade"] = cas
    return res, t, p, e, flow


def _f(v, nd=6):
    if v is None or v == "":
        return ""
    if isinstance(v, str):
        return v
    return ("%.*f" % (nd, v)).rstrip("0").rstrip(".") if v != int(v) else str(int(v))


def write_hydrograph(out_dir, res, t, p, e, q):
    d = os.path.join(out_dir, "hydrographs"); os.makedirs(d, exist_ok=True)
    path = os.path.join(d, res["storm_id"] + ".csv")
    with open(path, "w", newline="") as fh:
        for k in ("storm_id", "storm_type", "duration_hr", "temporal_distribution", "peak_cfs", "time_to_peak_hr",
                  "runoff_volume_in", "runoff_volume_acft", "area_mi2", "contrib_area_mi2", "dt_hr", "loss",
                  "transform", "baseflow", "pothole_mode", "excess_before_potholes_in", "pothole_capacity_in"):
            if k not in res:
                continue
            fh.write("# %s: %s\n" % (k, _f(res.get(k), 4)))
        w = csv.writer(fh)
        w.writerow(["time_hr", "flow_cfs", "precip_in", "excess_in"])
        for row in zip(t, q, p, e):
            w.writerow([_f(row[0]), _f(row[1], 3), _f(row[2]), _f(row[3])])
    return path


def _write_cas(cd, res, dt, n):
    from . import cascade as C
    cas = res["_cascade"]
    ex_in = cas.get("_excess_in")
    return C.write_timeseries(os.path.join(cd, res["storm_id"] + ".csv"), dt * np.arange(1, n + 1), ex_in, cas,
                              {"storm_id": res["storm_id"], "pothole_mode": "cascade"})


SUMMARY_COLS = ["storm_id", "storm_type", "duration_hr", "temporal_distribution", "area_mi2", "contrib_area_mi2",
                "precip_in", "excess_in", "runoff_volume_in", "runoff_volume_acft", "peak_cfs", "time_to_peak_hr",
                "loss", "transform", "baseflow", "pothole_mode", "excess_before_potholes_in", "pothole_capacity_in",
                "pothole_stored_end_in", "pothole_min_contrib_fraction", "pothole_max_contrib_fraction", "hydrograph_csv"]


def run_folder(hyeto_paths, out_dir, cfg: HydroConfig, log=print):
    rows = []
    for pth in sorted(hyeto_paths):
        hdr, dt, precip = read_hyetograph(pth)
        res, t, p, e, q = compute(hdr, dt, precip, cfg)
        res["hydrograph_csv"] = write_hydrograph(out_dir, res, t, p, e, q)
        if res.get("_cascade") is not None:
            from . import cascade as C
            cd = os.path.join(out_dir, "pothole_cascade"); os.makedirs(cd, exist_ok=True)
            n = len(precip)
            res["cascade_csv"] = _write_cas(cd, res, dt, n)
        res.pop("_cascade", None)
        rows.append(res)
        if log:
            log("%s: peak %.0f cfs at %.2f h, runoff %.2f in" % (res["storm_id"], res["peak_cfs"],
                                                                 res["time_to_peak_hr"], res["runoff_volume_in"]))
    rows.sort(key=lambda r: -r["peak_cfs"])
    sp = os.path.join(out_dir, "inflow_hydrograph_summary.csv")
    with open(sp, "w", newline="") as fh:
        w = csv.writer(fh); w.writerow(SUMMARY_COLS)
        for r in rows:
            w.writerow([_f(r.get(c), 4) for c in SUMMARY_COLS])
    return rows, sp
