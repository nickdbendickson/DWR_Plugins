#!/usr/bin/env python3
"""Manning's n against the 2020 flood stage at the Tittabawassee River at Midland, Michigan.

    <QGIS python> midland_reach.py build --out FOLDER
    <QGIS python> midland_reach.py run FOLDER --n 0.04 [--lisflood EXE]
    <QGIS python> midland_reach.py run FOLDER --nlcd
    <QGIS python> midland_reach.py score ROOT

The Edenville and Sanford dams failed on 19 May 2020; the flood was gauged downstream at USGS
04156000 (Tittabawassee River at Midland), which read a peak of 35.14 ft gage height (614.61 ft
NAVD88) at a computed 51,800 cfs. There are no surveyed high-water marks anywhere downstream of
the two dams, so this gauge peak is the one observed stage to test roughness against.

This is the North Dakota streamgage rating test (tools/validation/nd_gauges/reach.py) applied to
one Michigan gauge: the measured peak discharge is run steady through a 17 km reach around the
gauge (3DEP 1 m terrain averaged to 10 m cells) with LISFLOOD-FP through the Runner's own code,
and the model stage at the gauge is compared with the observed 35.14 ft across a range of n and
with n from NLCD land cover. It isolates roughness at a large, real dam-break flood; it does not
model the breaches (that is cascade_case.py). The gauge's discharge is rating-derived, so the
rating already embeds a roughness: read how the stage MOVES across n, not the absolute fit."""
import argparse
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ND = os.path.join(os.path.dirname(HERE), "nd_gauges")
sys.path.insert(0, ND)
import reach  # noqa: E402  (tools/validation/nd_gauges/reach.py)

# reach.py hardcodes EPSG:26914 (UTM 14N) for North Dakota. Midland, MI sits at ~84.2 W, in
# UTM 16N, so the reach is built and run in EPSG:26916 instead. reach.py reads EPSG as a module
# global at call time (to_utm, the NHD/3DEP queries, the NLCD warp), so setting it here reprojects
# the whole Midland build; _use_crs() applies it right before each entry point.
EPSG = 26916


def _use_crs():
    reach.EPSG = EPSG

# USGS 04156000, Tittabawassee River at Midland MI. Gage datum 579.47 ft NAVD88; 2020 annual peak
# 51,800 cfs at 35.14 ft gage height -> 614.61 ft NAVD88. gnis_name of the river in the NHD.
MIDLAND = {
    "id": "USGS-04156000",
    "name": "Tittabawassee River at Midland (2020 Edenville-Sanford dam-break flood)",
    "river": "Tittabawassee River",
    "lat": 43.5953047686325,
    "lon": -84.2355439496881,
    "datum_ft": 579.47,
    # 2,400 sq mi at Midland (geoconnex gage record 6,216 km2 / NHD 6,205.6 km2 = ~2,400 sq mi).
    # Sizes the sub-grid channel below the inflow when --sgc is used.
    "drainage_mi2": 2400,
    "measured": {
        "q_cfs": 51800.0,
        "gage_height_ft": 35.14,
        "time_utc": "2020-05-20T16:30",        # peak 11:30 EST May 20 = 16:30 UTC (USGS 04156000)
        "control": "2020 Edenville/Sanford dam-break flood peak (open water)",
        "source": "USGS 04156000, water-year 2020 annual peak",
    },
}


def build(out, say=print):
    _use_crs()
    return reach.build(MIDLAND["id"], out, say=say, gauge=MIDLAND)


def run(folder, n=None, nlcd=False, exe=None, threads=1, sim_hr=None, say=print,
        sgc=False, sgc_min_area_km2=None, sgc_channel_n=None, sgc_start="auto", sgc_curve=None,
        tag_suffix=""):
    _use_crs()
    return reach.run(folder, n, nlcd, exe, threads, sim_hr, say,
                     sgc=sgc, sgc_min_area_km2=sgc_min_area_km2,
                     sgc_channel_n=sgc_channel_n, sgc_start=sgc_start, sgc_curve=sgc_curve,
                     tag_suffix=tag_suffix)


def score(root, say=print):
    """Model minus observed stage, ft, at each n, for every reach under ROOT (here just Midland)."""
    rows = []
    for rj in sorted(glob.glob(os.path.join(root, "*", "reach.json"))):
        with open(rj, encoding="utf-8") as f:
            meta = json.load(f)
        for res in sorted(glob.glob(os.path.join(os.path.dirname(rj), "runs", "*", "result.json"))):
            with open(res, encoding="utf-8") as f:
                r = json.load(f)
            rows.append((meta["gauge"]["name"], meta["gauge"]["measured"]["q_cfs"], r["tag"],
                         r.get("diff_ft"), r.get("qout_over_qin")))
    tags = sorted({r[2] for r in rows}, key=lambda t: (t == "nlcd", t))
    lines = ["# Manning's n at the Tittabawassee River at Midland (2020 dam-break flood)", "",
             "Model minus observed stage, ft, at the 2020 peak discharge (LISFLOOD-FP, 1 m 3DEP "
             "terrain averaged to 10 m cells, steady inflow). Outflow over inflow at the end of "
             "each run in brackets (1.00 = steady).", "",
             "| Gauge | Peak, cfs | " + " | ".join(tags) + " |",
             "|---|---|" + "---|" * len(tags)]
    names = []
    for r in rows:
        if r[0] not in names:
            names.append(r[0])
    for nm in names:
        q, cells = None, []
        for t in tags:
            hit = [r for r in rows if r[0] == nm and r[2] == t]
            q = q or (hit[0][1] if hit else None)
            cells.append("{:+.2f} ({:.2f})".format(hit[0][3], hit[0][4] or float("nan"))
                         if hit and hit[0][3] is not None else "")
        lines.append("| {} | {:,.0f} | ".format(nm, q or 0) + " | ".join(cells) + " |")
    text = "\n".join(lines) + "\n"
    with open(os.path.join(root, "Midland_Reach_Score.md"), "w", encoding="utf-8") as f:
        f.write(text)
    say(text)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", required=True)
    r = sub.add_parser("run")
    r.add_argument("folder")
    r.add_argument("--n", type=float, default=None)
    r.add_argument("--nlcd", action="store_true")
    r.add_argument("--lisflood", default=None)
    r.add_argument("--threads", type=int, default=1)
    r.add_argument("--hours", type=float, default=None)
    r.add_argument("--sgc", action="store_true",
                   help="add a sub-grid river channel below the inflow (needs the SGC-patched "
                        "LISFLOOD-FP); the run is written to a separate n<..>+sgc folder")
    r.add_argument("--sgc-min-area-km2", type=float, default=None)
    r.add_argument("--sgc-channel-n", type=float, default=None)
    r.add_argument("--sgc-start", default="auto", choices=["auto", "empty", "full"])
    r.add_argument("--sgc-curve", default=None,
                   help="sub-grid-channel hydraulic-geometry curve: a preset name (nd [default], "
                        "bieger-us) or four numbers a,b,c,f for W=a*A^b, D=c*A^f (A in km2, W/D m)")
    r.add_argument("--tag-suffix", default="",
                   help="append this to the run folder name, to park a variant run (e.g. a "
                        "gpu-subfv terrain-sampling run) in its own column without overwriting")
    s = sub.add_parser("score")
    s.add_argument("root")
    a = ap.parse_args(argv)
    if a.cmd == "build":
        build(a.out)
    elif a.cmd == "run":
        if not a.nlcd and not a.n:
            ap.error("give --n or --nlcd")
        curve = a.sgc_curve
        if curve and "," in curve:
            try:
                curve = tuple(float(x) for x in curve.split(","))
            except ValueError:
                ap.error("--sgc-curve must be a preset name or four numbers a,b,c,f")
        run(a.folder, a.n, a.nlcd, a.lisflood, a.threads, a.hours,
            sgc=a.sgc, sgc_min_area_km2=a.sgc_min_area_km2,
            sgc_channel_n=a.sgc_channel_n, sgc_start=a.sgc_start, sgc_curve=curve,
            tag_suffix=a.tag_suffix)
    else:
        score(a.root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
