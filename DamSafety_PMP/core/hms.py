"""Export a runnable HEC-HMS 4.x project (single subbasin -> sink) from the ndpmp hyetograph CSVs and a
HydroConfig, plus a Jython script that writes the precipitation to DSS7, computes every run headless
(`hec-hms.sh -s run_hms.py`) and writes hms_peaks.csv.

Mapped: SCS CN (+ Ia ratio as Initial Abstraction), Initial+Constant; SCS UH (STANDARD or PRFnnn) / Clark;
Baseflow None (constant 0). Pothole 'abstraction' -> Surface: Simple with Surface Storage Capacity
(NB: HMS fills surface storage from precipitation BEFORE the loss method; ours abstracts from excess).
Not mapped (warned): pothole 'cascade', nonzero/recession baseflow, AMC-III conversion (CN converted
before export), UH 'average' sampling, noncontrib area (exported as area_mi2 - noncontrib_mi2).
"""
import os, re, json, subprocess, csv
from .hydro import read_hyetograph, cn_amc3, HydroConfig

HMS_DT = [1, 2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60, 120, 180, 240, 360, 480, 720, 1440]
START = ("1 January 2000", "00:00")


def _name(s):
    return re.sub(r"[^A-Za-z0-9_]+", "_", s)[:60]


def _dss_interval(m):
    return {60: "1HOUR", 120: "2HOUR", 180: "3HOUR", 240: "4HOUR", 360: "6HOUR", 480: "8HOUR",
            720: "12HOUR", 1440: "1DAY"}.get(m, "%dMINUTE" % m)


def _end(start_min, minutes):
    import datetime as D
    t = D.datetime(2000, 1, 1) + D.timedelta(minutes=start_min + minutes)
    return "%d %s %d" % (t.day, t.strftime("%B"), t.year), t.strftime("%H:%M")


def export_hms(hyeto_paths, cfg: HydroConfig, out_dir, project="NDPMP", tail_hr=None, log=print):
    log = log or (lambda *a: None)
    os.makedirs(out_dir, exist_ok=True)
    warn = []
    storms = []
    area = None
    for p in hyeto_paths:
        hdr, dt, pr = read_hyetograph(p)
        m = int(round(dt * 60))
        if m not in HMS_DT:
            raise ValueError("%s: timestep %d min is not a HEC-HMS interval" % (p, m))
        a = float(cfg.area_mi2) if cfg.area_mi2 else float(hdr.get("area_mi2") or 0)
        area = a if area is None else area
        storms.append((_name(hdr["storm_id"]), m, pr, hdr))
    area_eff = area - (cfg.noncontrib_mi2 or 0.0)
    cn = cn_amc3(cfg.cn) if cfg.amc3 else cfg.cn
    if cfg.uh_sampling != "point":
        warn.append("uh_sampling=%s: HMS always uses its own UH; compare with point sampling" % cfg.uh_sampling)
    if cfg.baseflow_cfs:
        warn.append("baseflow %s %.1f cfs not exported (HMS baseflow: None)" % (cfg.baseflow, cfg.baseflow_cfs))
    if cfg.pothole_mode == "cascade":
        warn.append("pothole cascade has no HMS equivalent here; not exported (use abstraction or HMS reservoirs)")
    surf = ""
    if cfg.pothole_mode == "abstraction":
        if cfg.pothole_storage_in is not None:
            cap = cfg.pothole_storage_in
        else:
            net = json.load(open(cfg.pothole_network))
            cap = sum(x["volume_m3"] for x in net["potholes"]) / net["basin_m2"] / 0.0254
        cap *= (1 - cfg.pothole_initial_full)
        surf = ("     Surface: Simple\n     Initial Surface Storage Percent: 0\n"
                "     Surface Storage Capacity: %.4f\n     End Surface:\n\n" % cap)
        warn.append("pothole abstraction exported as Simple Surface storage %.3f in (HMS applies it to "
                    "precipitation before the loss method; ndpmp applies it to excess)" % cap)
    if cfg.loss == "scs_cn":
        S = 1000.0 / cn - 10.0
        loss = ("     LossRate: SCS\n     Percent Impervious Area: 0.0\n     Curve Number: %.4f\n"
                "     Initial Abstraction: %.4f\n" % (cn, cfg.ia_ratio * S))
    else:
        loss = ("     LossRate: Initial+Constant\n     Percent Impervious Area: 0.0\n     Initial Loss: %.4f\n"
                "     Constant Loss Rate: %.4f\n" % (cfg.initial_in, cfg.constant_in_hr))
    if cfg.transform == "scs":
        lag = cfg.lag_hr if cfg.lag_hr else 0.6 * cfg.tc_hr
        ug = "STANDARD" if abs(cfg.prf - 484) < 1e-6 else "PRF%d" % int(round(cfg.prf))
        tr = "     Transform: SCS\n     Unitgraph Type: %s\n     Lag: %.4f\n" % (ug, lag * 60.0)
        if cfg.peaking_pct:
            warn.append("peaking_pct %.1f not exported" % cfg.peaking_pct)
        lag_hr = lag
    else:
        tr = ("     Transform: Clark\n     Clark Method: Specified\n     Time of Concentration: %.4f\n"
              "     Storage Coefficient: %.4f\n     Time Area Method: Default\n" % (cfg.tc_hr, cfg.clark_r_hr))
        lag_hr = cfg.tc_hr
    tail = tail_hr if tail_hr else max(24.0, 10 * lag_hr)
    basin = "Basin_" + project
    hd = "     Version: 4.13\n     Filepath Separator: /\n"
    with open(os.path.join(out_dir, basin + ".basin"), "w") as f:
        f.write("Basin: %s\n%s     Unit System: English\n     Missing Flow To Zero: No\n     Enable Flow Ratio: No\n"
                "     Compute Local Flow At Junctions: No\n     Unregulated Output Required: No\n\n"
                "     Enable Sediment Routing: No\nEnd:\n\n" % (basin, hd))
        f.write("Subbasin: Basin\n     Canvas X: 0.0\n     Canvas Y: 100.0\n     Area: %.6f\n     Downstream: Outlet\n\n"
                "     Discretization: None\n\n     Canopy: None\n     Allow Simultaneous Precip Et: No\n"
                "     Plant Uptake Method: None\n\n%s%s\n%s\n     Baseflow: None\nEnd:\n\n" % (area_eff, surf, loss, tr))
        f.write("Sink: Outlet\n     Canvas X: 0.0\n     Canvas Y: 0.0\nEnd:\n\n")
    hms = ["Project: %s\n     Description: ND PMP prototype export (internal)\n%s     DSS File Name: %s.dss\n"
           "     Time Zone ID: UTC\nEnd:\n\n" % (project, hd, project),
           "Basin: %s\n     Filename: %s.basin\n     Description: \nEnd:\n\n" % (basin, basin)]
    gage = ["Gage Manager: %s\n     Gage Manager: %s\n%sEnd:\n\n" % (project, project, hd)]
    runs = ["", ]
    runs = []
    jy_series = []
    for sid, m, pr, hdr in storms:
        n = len(pr)
        pend = _end(0, m * n)
        gname = "G_" + sid
        path = "/NDPMP/%s/PRECIP-INC/01JAN2000/%s/PMP/" % (sid[:40].upper(), _dss_interval(m))
        gage.append("Gage: %s\n     Gage: %s\n     Gage Type: Precipitation\n     Data Source Type: External DSS\n"
                    "     Filename: %s.dss\n     Pathname: %s\n     Variant: Variant-1\n"
                    "       Start Time: 1 January 2000, 00:00\n       End Time: %s, %s\n     End Variant: Variant-1\nEnd:\n\n"
                    % (gname, gname, project, path, pend[0], pend[1]))
        met = "M_" + sid; ctl = "C_" + sid; run = "R_" + sid
        with open(os.path.join(out_dir, met + ".met"), "w") as f:
            f.write("Meteorology: %s\n%s     Unit System: English\n     Set Missing Data to Default: Yes\n"
                    "     Precipitation Method: Specified Average\n     Air Temperature Method: None\n"
                    "     Atmospheric Pressure Method: None\n     Dew Point Method: None\n     Wind Speed Method: None\n"
                    "     Shortwave Radiation Method: None\n     Longwave Radiation Method: None\n"
                    "     Snowmelt Method: None\n     Evapotranspiration Method: No Evapotranspiration\n"
                    "     Use Basin Model: %s\nEnd:\n\nPrecip Method Parameters: Specified Average\nEnd:\n\n"
                    "Subbasin: Basin\n     Gage: %s\nEnd:\n\n" % (met, hd, basin, gname))
        e = _end(0, int(round(m * n + tail * 60)))
        with open(os.path.join(out_dir, ctl + ".control"), "w") as f:
            f.write("Control: %s\n%s     Start Date: %s\n     Start Time: %s\n     End Date: %s\n     End Time: %s\n"
                    "     Time Interval: %d\nEnd:\n" % (ctl, hd, START[0], START[1], e[0], e[1], m))
        hms.append("Precipitation: %s\n     Filename: %s.met\n     Description: \nEnd:\n\n" % (met, met))
        hms.append("Control: %s\n     FileName: %s.control\n     Description: \nEnd:\n\n" % (ctl, ctl))
        runs.append("Run: %s\n     Log File: %s.log\n     DSS File: %s.dss\n     Basin: %s\n     Precip: %s\n"
                    "     Control: %s\n     Save State Type: None\n     Time-Series Output: Save All\nEnd:\n\n"
                    % (run, run, run, basin, met, ctl))
        jy_series.append({"sid": sid, "run": run, "path": path, "minutes": m, "values": [float(x) for x in pr]})
    hms.append("Gage Manager: %s\n     Filename: %s.gage\nEnd:\n\n" % (project, project))   # harmless if ignored
    open(os.path.join(out_dir, project + ".hms"), "w").write("".join(hms))
    open(os.path.join(out_dir, project + ".gage"), "w").write("".join(gage))
    open(os.path.join(out_dir, project + ".run"), "w").write("".join(runs))
    json.dump(jy_series, open(os.path.join(out_dir, "precip_series.json"), "w"))
    jy = '''# Jython for HEC-HMS 4.13:  hec-hms.sh -s run_hms.py   (cwd = this folder)
import os, json
from hec.heclib.dss import HecDss
from hec.io import TimeSeriesContainer
from hec.heclib.util import HecTime
from hms.model import Project
from hms import Hms
here = os.path.dirname(os.path.abspath(%r))
S = json.load(open(os.path.join(here, "precip_series.json")))
d = HecDss.open(os.path.join(here, "%s.dss"))
for s in S:
    t = TimeSeriesContainer(); t.fullName = s["path"]
    st = HecTime("01Jan2000", "0000")
    t.times = [st.value() + s["minutes"] * (i + 1) for i in range(len(s["values"]))]
    t.values = s["values"]; t.numberValues = len(s["values"]); t.units = "IN"; t.type = "PER-CUM"
    d.put(t)
d.done()
p = Project.open(os.path.join(here, "%s.hms"))
out = open(os.path.join(here, "hms_peaks.csv"), "w")
out.write("storm_id,run,hms_peak_cfs,hms_time_to_peak_hr,hms_volume_in,status\\n")
for s in S:
    status = "ok"
    try:
        p.computeRun(s["run"])
    except Exception, e:
        status = "compute error: %%s" %% e
    pk = tp = vol = ""
    try:
        r = HecDss.open(os.path.join(here, s["run"] + ".dss"))
        names = [n for n in r.getCatalogedPathnames() if "/OUTLET/FLOW/" in n.upper() and "RUN:" in n.upper()]
        if names:
            tsc = r.get(names[0], True)
            v = list(tsc.values); i = v.index(max(v))
            pk = "%%.3f" %% v[i]
            tp = "%%.4f" %% ((tsc.times[i] - HecTime("01Jan2000", "0000").value()) / 60.0)
        else:
            status += "; no outlet flow path in " + str(list(r.getCatalogedPathnames())[:20])
        r.done()
    except Exception, e:
        status += "; read error: %%s" %% e
    out.write("%%s,%%s,%%s,%%s,%%s,%%s\\n" %% (s["sid"], s["run"], pk, tp, vol, status.replace(",", ";")))
out.close()
p.close()
Hms.shutdownEngine()
''' % (os.path.join(os.path.abspath(out_dir), "run_hms.py"), project, project)
    open(os.path.join(out_dir, "run_hms.py"), "w").write(jy)
    for w in warn:
        log("WARNING: " + w)
    return {"project": os.path.join(out_dir, project + ".hms"), "script": os.path.join(out_dir, "run_hms.py"),
            "warnings": warn, "storms": [s[0] for s in storms]}


def run_hms(out_dir, hms_home=None, timeout=1800):
    hms_home = hms_home or os.environ.get("ND_PMP_HMS_HOME", "")
    if not hms_home:
        raise ValueError("HEC-HMS folder not set (algorithm setting nd_pmp/hms_home or ND_PMP_HMS_HOME)")
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":6")
    p = subprocess.run([os.path.join(hms_home, "hec-hms.sh"), "-s", os.path.join(os.path.abspath(out_dir), "run_hms.py")],
                       cwd=out_dir, env=env, capture_output=True, text=True, timeout=timeout)
    peaks = []
    pk = os.path.join(out_dir, "hms_peaks.csv")
    if os.path.exists(pk):
        peaks = list(csv.DictReader(open(pk)))
    return p.returncode, p.stdout[-4000:], p.stderr[-4000:], peaks
