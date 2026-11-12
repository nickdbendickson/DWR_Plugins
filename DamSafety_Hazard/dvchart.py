# -*- coding: utf-8 -*-
"""Depth-velocity chart of the wet HABITABLE structures a Hazard classify run inundates.

Port of DamIT Hazard's habitable-only ND depth-velocity chart (damit PR #65), with
Nick's DWR_Plugins rule (2026-11-12, t367u):

  ONLY ND 911 address points count as habitable on the chart until an engineer
  reviews them in Hazard. NSI-only points and building outlines without a 911
  address are not plotted as habitable until reviewed. Reviewed-habitable
  non-911 structures are plotted with a distinct marker. Unreviewed non-911
  wet structures are a count in a note, not points.

This is a CHART filter only. classify_nd / TM 11 still use the automatic
Habitable flag (NSI residential, FEMA outlines, etc.). An engineer's review
already changes that flag when it is saved in Structure_Review_Decisions.csv
and applied by structures._apply_review; this module does not write or change
that file.

One point per plotted structure at its peak depth vs peak velocity over its
wet cells (option C: the two peaks are independent), on REG_05.2026 Fig. 5.1
(houses) with a 0-4 ft/s, 0-3.5 ft zoom panel. Mobile homes go on Fig. 5.2 as
a third panel only when there are any. Points in high or judgment are labelled
with the structure ID. Title counts: high / MEDIUM (judgment) / low. Zones
come from damsuite.tm11_curves.zone (the same function classification uses).
A chart is written for every classify run (Runner results, Screening results,
or imported rasters such as DSS-WISE), labelled by source.

Pure Python + the standard library; matplotlib (Agg) is imported only when the
PNG is drawn. Missing or broken matplotlib skips the PNG with a note (the CSV
is still written). A chart problem never fails a classify run.
"""
import csv
import os
import re

try:
    from .damsuite import tm11_curves as TM11
except ImportError:                          # tests / a script outside the plugin package
    try:
        from damsuite import tm11_curves as TM11
    except ImportError:
        from DamSafety_Core.damsuite import tm11_curves as TM11

HIGH, JUDGMENT, LOW = TM11.HIGH, TM11.JUDGMENT, TM11.LOW
ZONE_TEXT = {HIGH: "high", JUDGMENT: "MEDIUM", LOW: "low"}
ZONE_RANK = {HIGH: 3, JUDGMENT: 2, LOW: 1}
# official REG_05.2026 Fig. 5.1 / 5.2 fills (same as helpers / styles / chart_svg)
COLORS = {"high": "#EF9897", "judgment": "#F9F4A3", "low": "#C4E0AD"}
ZOOM_V_FTS, ZOOM_D_FT = 4.0, 3.5
LABEL_MAX = 80
TRACE_FT = 0.1                               # structures.CONSEQUENCE_DEPTH_FT
CHART_STEM = "ND_DV_Habitable"
SAMPLING_PEAK = "peak depth and peak velocity over the structure's wet cells"
KIND_HOUSE, KIND_MOBILE = "house", "mobile"
TYPE_MOBILE = "Mobile home"
ND_FAMILY = "NDGISHUB"
ROLE_911 = "911"
ROLE_REVIEWED = "reviewed_habitable"
CSV_COLUMNS = ("id", "label", "acer_type", "chart", "zone", "depth_ft", "velocity_fps",
               "trace_depth", "has_911", "reviewed", "role", "source", "sampling")

# path / filename hints used when the scenario has no source tag
_SCREEN_NAME = re.compile(r"^screening[\s_]", re.I)
_DSSWISE_HINT = re.compile(r"dss[\s_\-]*wise|maxdepth|max_?depth_|hcom|arrivaltime_", re.I)
_RUNNER_NAME = re.compile(r"^depth[\s_\-]?\(?max\)?|\AHECRAS Depth", re.I)
_SCREEN_UNITS = "working grids in the parent folder are metres"


class ChartSkipped(Exception):
    pass


def _tm11():
    return TM11


# ----------------------------------------------------------------- 911 / review --
def _text(v):
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    return str(v).strip()


def _num(v, default=None):
    if v is None or v == "":
        return default
    try:
        return float(str(v).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def has_911(rec):
    """True when an ND GIS Hub (NENA) 911 address point is joined to this structure.

    In-memory inventory: family NDGISHUB on rec['families'] or rec['sources'].
    Habitable_Structures.csv: ND_Address_Points > 0, or Sources contains NDGISHUB.
    A FEMA parcel address (Address column only) is not a 911 point.
    """
    n = rec.get("ND_Address_Points", rec.get("nd_address_points"))
    if _num(n, 0) > 0:
        return True
    fams = rec.get("families") or ()
    if ND_FAMILY in fams or "ND GIS Hub" in fams:
        return True
    srcs = rec.get("sources")
    if isinstance(srcs, (list, tuple)):
        if any((s.get("family") if isinstance(s, dict) else "") == ND_FAMILY for s in srcs):
            return True
    col = rec.get("Sources")
    if isinstance(col, str) and ND_FAMILY in col.upper():
        return True
    return False


def is_reviewed(rec):
    """True when an engineer has a saved review decision on this structure.

    After structures._apply_review, rec['reviewed'] is a non-empty string
    (reviewer, date, and 'confirmed' or the fields that changed). The raw
    decision may also sit on rec['review'] (a dict) after ReviewStore.attach.
    The CSV 'Reviewed' column is that same string. An empty Reviewed cell is
    not a review: the flag only changes on an explicit review action.
    """
    rv = rec.get("review")
    if isinstance(rv, dict) and any(_text(rv.get(k)) for k in
                                    ("category", "acer_type", "persons", "reviewer",
                                     "date", "note")):
        return True
    for key in ("reviewed", "Reviewed"):
        if _text(rec.get(key)):
            return True
    return False


def is_habitable_yes(rec):
    v = rec.get("habitable", rec.get("Habitable"))
    return _text(v).lower() in ("yes", "1", "true", "y")


def depth_of(rec):
    return _num(rec.get("max_depth", rec.get("Max_Depth_ft", rec.get("depth_ft"))))


def velocity_of(rec):
    v = _num(rec.get("max_vel", rec.get("Max_Vel_fps", rec.get("velocity_fps"))))
    return 0.0 if v is None else v


def acer_type_of(rec):
    return _text(rec.get("acer_type", rec.get("ACER_Type"))) or "Habitable"


def chart_kind(rec):
    """'mobile' for a mobile home, else 'house' (TM 11 Fig. 5.1 / 5.2)."""
    typ = acer_type_of(rec)
    if typ.lower() == TYPE_MOBILE.lower() or "mobile" in typ.lower():
        return KIND_MOBILE
    return KIND_HOUSE


def rec_id(rec):
    v = rec.get("id", rec.get("Struct_ID", rec.get("label")))
    return "" if v is None else str(v)


def is_wet(rec, wet_min=0.0):
    d = depth_of(rec)
    return d is not None and d > wet_min


def role_of(rec, wet_min=0.0):
    """Which chart bucket a structure falls in (see module docstring).

    Returns one of: 'dry', '911', 'reviewed_habitable', 'unreviewed_non911',
    'not_plotted' (a wet 911 / reviewed record that is not Habitable=Yes).
    """
    if rec.get("dwelling_extra"):
        return "not_plotted"
    wet = is_wet(rec, wet_min)
    if not wet:
        return "dry"
    nine = has_911(rec)
    if is_habitable_yes(rec) and nine:
        return ROLE_911
    if is_habitable_yes(rec) and is_reviewed(rec) and not nine:
        return ROLE_REVIEWED
    if not nine:
        return "unreviewed_non911"
    return "not_plotted"


# ----------------------------------------------------------------- source label --
def _units_says_screening(folder):
    path = os.path.join(folder, "UNITS.txt") if folder else ""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, TypeError):
        return False
    return _SCREEN_UNITS.lower() in text.lower() or "screening" in text.lower()


def _provenance_label(folder):
    """A short source label from results_provenance.json next to the raster, or None."""
    if not folder:
        return None
    path = os.path.join(folder, "results_provenance.json")
    try:
        import json
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    for key in ("model", "source", "tool"):
        v = _text(d.get(key))
        if not v:
            continue
        low = v.lower()
        if "dss" in low and "wise" in low:
            return "DSS-WISE"
        if "screen" in low:
            return "Screening"
        if "runner" in low or "anuga" in low or "lisflood" in low or "breach" in low:
            return "Runner"
        if "hec" in low or "ras" in low:
            return "HEC-RAS"
        return v
    return None


def detect_source(depth_path, override=None, scenario_source=None):
    """(label, kind) of the flood result the depth raster is.

    A given override wins, then the scenario's own source tag (Hazard already
    sets 'DSS-WISE Lite' on an imported DSS-WISE package). Otherwise the
    file name and nearby sidecars: a name starting 'Screening ', a Screening
    UNITS.txt, a DSS-WISE-like name, a Runner 'Depth (Max).tif' (Screening
    also writes that name into the dam folder - UNITS.txt / provenance /
    '_breach_model' tell them apart when present), else
    'Imported raster: <file name>'.
    """
    if override and _text(override):
        return _text(override), "given"
    if scenario_source and _text(scenario_source):
        raw = _text(scenario_source)
        low = raw.lower()
        if "dss" in low and "wise" in low:
            return "DSS-WISE", "dsswise"
        if "screen" in low:
            return "Screening", "screening"
        return raw, "scenario"
    path = str(depth_path or "").split("|")[0]
    name = os.path.basename(path)
    folder = os.path.dirname(path) if path else ""
    low_path = path.replace("\\", "/").lower()
    low_name = name.lower()
    prov = _provenance_label(folder)
    if prov:
        return prov, "provenance"
    if _SCREEN_NAME.search(name) or "/screening" in low_path:
        return "Screening", "screening"
    if _units_says_screening(folder):
        return "Screening", "screening"
    if _DSSWISE_HINT.search(name) or "dsswise" in low_path or "dss-wise" in low_path:
        return "DSS-WISE", "dsswise"
    if "_breach_model" in low_path:
        return "Runner", "runner"
    if low_name in ("depth (max).tif", "depth (max).vrt", "depth (max).tiff") or \
            low_name.startswith("depth (max)."):
        return "Runner", "runner"
    if name:
        return "Imported raster: " + name, "imported"
    return "Imported raster", "imported"


def run_date_of(path, now=None):
    """YYYY-MM-DD of the raster file (its modified date), else today."""
    import datetime
    try:
        return datetime.datetime.fromtimestamp(os.stat(str(path).split("|")[0]).st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return (now or datetime.datetime.now()).strftime("%Y-%m-%d")


# ----------------------------------------------------------------- select --
def select(structs, source="", sampling=SAMPLING_PEAK, wet_min=0.0):
    """Split structures into chart points and left-out counts.

    Classification fields (habitable, reviewed, category) are never written.
    """
    pts, left = [], {"dry": 0, "unreviewed_non911": 0, "unreviewed_non911_habitable": 0,
                     "not_plotted": 0, "no_depth": 0}
    for rec in structs or []:
        role = role_of(rec, wet_min)
        if role == "dry":
            if depth_of(rec) is None:
                left["no_depth"] += 1
            else:
                left["dry"] += 1
            continue
        if role == "unreviewed_non911":
            left["unreviewed_non911"] += 1
            if is_habitable_yes(rec):
                left["unreviewed_non911_habitable"] += 1
            continue
        if role == "not_plotted":
            left["not_plotted"] += 1
            continue
        d = float(depth_of(rec))
        v = float(velocity_of(rec))
        kind = chart_kind(rec)
        pts.append({
            "id": rec_id(rec), "label": rec_id(rec), "acer_type": acer_type_of(rec),
            "chart": kind, "zone": TM11.zone(kind, d, v), "depth_ft": d, "velocity_fps": v,
            "trace_depth": bool(round(d, 2) <= TRACE_FT),
            "has_911": role == ROLE_911, "reviewed": is_reviewed(rec),
            "role": role, "source": source, "sampling": sampling,
        })
    return {"points": pts, "left_out": left}


def counts(points):
    c = {HIGH: 0, JUDGMENT: 0, LOW: 0, "total": 0, "by_chart": {},
         "911": 0, "reviewed_habitable": 0, "trace": 0}
    for p in points:
        c[p["zone"]] += 1
        c["total"] += 1
        e = c["by_chart"].setdefault(p["chart"], {HIGH: 0, JUDGMENT: 0, LOW: 0})
        e[p["zone"]] += 1
        if p.get("role") == ROLE_REVIEWED or (p.get("role") != ROLE_911 and p.get("reviewed")
                                              and not p.get("has_911")):
            c["reviewed_habitable"] += 1
        else:
            c["911"] += 1
        if p.get("trace_depth"):
            c["trace"] += 1
    return c


def counts_text(c):
    return "{} plotted habitable structure(s): {} high, {} MEDIUM (judgment), {} low".format(
        c["total"], c[HIGH], c[JUDGMENT], c[LOW])


def left_out_note(left):
    n = int(left.get("unreviewed_non911") or 0)
    if not n:
        return ""
    hab = int(left.get("unreviewed_non911_habitable") or 0)
    msg = ("{} unreviewed non-911 wet structure(s) are not plotted (ND 911 address "
           "points are the only habitable points on this chart until an engineer "
           "reviews them in Hazard)".format(n))
    if hab:
        msg += ("; {} of them Hazard already flags as habitable - they appear here "
                "only after that review".format(hab))
    return msg


def title_lines(meta, c, left=None):
    dam = " ".join(x for x in (meta.get("dam_id"), meta.get("dam_name")) if x) or "(dam not named)"
    src = meta.get("source") or "(source not given)"
    first = "{} - {} - run {}".format(dam, src, meta.get("run_date") or "?")
    if meta.get("scenario"):
        first += " ({})".format(meta["scenario"])
    second = counts_text(c)
    third = "Depth-velocity chart, habitable structures only (ND 911 address points until reviewed); "
    third += meta.get("sampling") or SAMPLING_PEAK
    note = left_out_note(left or {})
    if note:
        third += ". " + note
    if meta.get("no_velocity"):
        third += ". No velocity raster; points are shown at 0 ft/s (depth only)"
    return first, second, third


# ----------------------------------------------------------------- polygons / csv --
def zone_polygons(kind):
    """{"low", "judgment", "high"} closed polygons as tm11_curves.zone() reads them."""
    high, judgment = TM11.polygons(kind)
    vmax, dmax = TM11.AXES[kind]
    # low zone: below the low (judgment) boundary, down to depth 0
    c = TM11.CURVES[kind]
    vs = list(c["v"])
    lo = list(c["low"])
    low = [(vs[i], 0.0) for i in range(len(vs))] + [(vs[i], lo[i]) for i in range(len(vs) - 1, -1, -1)]
    return {"low": low, "judgment": [(p[0], p[1]) for p in judgment],
            "high": [(p[0], p[1]) for p in high], "v_max": vmax, "d_max": dmax}


def write_csv(points, path):
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for p in sorted(points, key=lambda q: (-ZONE_RANK[q["zone"]], -q["depth_ft"])):
            w.writerow([p["id"], p["label"], p["acer_type"], p["chart"], ZONE_TEXT[p["zone"]],
                        "{:.3f}".format(p["depth_ft"]), "{:.3f}".format(p["velocity_fps"]),
                        "yes" if p["trace_depth"] else "",
                        "yes" if p["has_911"] else "",
                        "yes" if p["reviewed"] else "", p["role"], p["source"], p["sampling"]])
    return path


# ----------------------------------------------------------------- png --
def _pyplot():
    try:
        import matplotlib
        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt
        from matplotlib.lines import Line2D
        from matplotlib.patches import Patch, Polygon
        return plt, Polygon, Patch, Line2D
    except Exception as e:                                       # noqa: BLE001
        raise ChartSkipped("matplotlib is not available ({}: {})".format(type(e).__name__, e))


def _scatter(ax, pts, **kw):
    if not pts:
        return
    ax.scatter([p["velocity_fps"] for p in pts], [p["depth_ft"] for p in pts], **kw)


def draw(points, meta, path, left=None, dpi=130):
    """Write the PNG. Raises ChartSkipped when matplotlib is missing."""
    plt, Polygon, Patch, Line2D = _pyplot()
    c = counts(points)
    houses = [p for p in points if p["chart"] == KIND_HOUSE]
    mobile = [p for p in points if p["chart"] == KIND_MOBILE]
    panels = [(KIND_HOUSE, None, houses), (KIND_HOUSE, (ZOOM_V_FTS, ZOOM_D_FT), houses)]
    if mobile:
        panels.append((KIND_MOBILE, None, mobile))
    fig, axs = plt.subplots(1, len(panels), figsize=(7.0 * len(panels), 7.2))
    axs = [axs] if len(panels) == 1 else list(axs)
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    try:
        for ax, (kind, zoom, pts) in zip(axs, panels):
            poly = zone_polygons(kind)
            vm, dm = poly["v_max"], poly["d_max"]
            # high first (full axes), then judgment, then low on top - same draw
            # order as Runner/Screening chart_svg (low visible in the corner)
            ax.add_patch(Polygon([(0, 0), (vm, 0), (vm, dm), (0, dm)],
                                 fc=COLORS["high"], ec="none"))
            ax.add_patch(Polygon(poly["judgment"], fc=COLORS["judgment"], ec="#b58400", lw=0.8))
            ax.add_patch(Polygon(poly["low"], fc=COLORS["low"], ec="#4d7a2b", lw=0.8))
            nine = [p for p in pts if p["role"] == ROLE_911 and not p["trace_depth"]]
            rev = [p for p in pts if p["role"] == ROLE_REVIEWED and not p["trace_depth"]]
            trace = [p for p in pts if p["trace_depth"]]
            _scatter(ax, nine, s=30, fc="#1f4e9c", ec="k", zorder=4)
            _scatter(ax, rev, s=34, marker="s", fc="#7b2d8e", ec="k", zorder=5)
            _scatter(ax, trace, s=26, marker="^", fc="white", ec="k", zorder=3)
            xl, yl = (zoom if zoom else (vm, dm))
            ax.set_xlim(0, xl)
            ax.set_ylim(0, yl)
            shown = 0
            for p in sorted(pts, key=lambda q: -q["depth_ft"]):
                if p["zone"] == LOW or shown >= LABEL_MAX:
                    continue
                if p["velocity_fps"] > xl or p["depth_ft"] > yl:
                    continue
                ax.annotate(p["label"], (p["velocity_fps"], p["depth_ft"]), xytext=(4, 4),
                            textcoords="offset points", fontsize=6.5, zorder=6)
                shown += 1
            ax.set_xlabel("Peak velocity (ft/s)")
            ax.set_ylabel("Peak depth (ft)")
            ax.grid(alpha=0.3)
            fig_name = TM11.FIGURES[kind]
            ax.set_title(("ZOOM {:g}-{:g} ft/s, 0-{:g} ft: ".format(0, xl, yl) if zoom else "")
                         + str(fig_name), fontsize=8)
            if not pts:
                ax.text(0.5, 0.5, "no wet habitable structures\non this chart",
                        transform=ax.transAxes, ha="center", va="center", fontsize=11,
                        color="#444", zorder=6, bbox={"fc": "white", "ec": "#888", "alpha": 0.9})
        first, second, third = title_lines(meta, c, left)
        fig.suptitle(first + "\n" + second, fontsize=10.5)
        handles = [Patch(fc=COLORS["high"], label="High danger"),
                   Patch(fc=COLORS["judgment"], label="Judgment (MEDIUM)"),
                   Patch(fc=COLORS["low"], label="Low danger"),
                   Line2D([], [], marker="o", ls="", mfc="#1f4e9c", mec="k",
                          label="ND 911 address point ({})".format(c["911"])),
                   Line2D([], [], marker="s", ls="", mfc="#7b2d8e", mec="k",
                          label="Reviewed habitable, no 911 ({})".format(c["reviewed_habitable"])),
                   Line2D([], [], marker="^", ls="", mfc="white", mec="k",
                          label="Trace depth (< {:.1f} ft)".format(TRACE_FT))]
        fig.legend(handles=handles, loc="lower center", ncol=3, frameon=False, fontsize=8,
                   bbox_to_anchor=(0.5, 0.08))
        import textwrap
        wrapped = "\n".join(textwrap.wrap(third, width=140))
        fig.text(0.5, 0.01, wrapped, ha="center", va="bottom", fontsize=7, color="#333")
        fig.tight_layout(rect=[0, 0.18, 1, 0.93])
        fig.savefig(path, dpi=dpi)
    finally:
        plt.close(fig)
    return path


# ----------------------------------------------------------------- main --
def build(structs, meta, out_dir, stem=CHART_STEM, wet_min=0.0, log=None):
    """Select the plotted structures, write `<stem>.csv` and `<stem>.png` into out_dir.

    Returns {png, csv, counts, text, left_out, note, status} with status "ok" or
    "csv_only". Never raises for a drawing problem. meta: dam_id, dam_name,
    source, run_date, sampling, scenario, no_velocity.
    """
    log = log or (lambda m: None)
    os.makedirs(out_dir, exist_ok=True)
    meta = dict(meta or {})
    meta.setdefault("sampling", SAMPLING_PEAK)
    sel = select(structs, meta.get("source") or "", meta["sampling"], wet_min)
    pts, left = sel["points"], sel["left_out"]
    c = counts(pts)
    res = {"png": None, "csv": os.path.join(out_dir, stem + ".csv"), "counts": c,
           "text": counts_text(c), "left_out": left, "note": left_out_note(left),
           "status": "ok", "source": meta.get("source"),
           "title": " - ".join(title_lines(meta, c, left)[:2]), "points": pts}
    if not pts:
        res["note"] = "; ".join(x for x in (
            res["note"], "no wet habitable structures: the chart shows the empty zones only") if x)
    write_csv(pts, res["csv"])
    try:
        res["png"] = draw(pts, meta, os.path.join(out_dir, stem + ".png"), left=left)
    except ChartSkipped as e:
        res["status"] = "csv_only"
        res["note"] = "; ".join(x for x in (res["note"], "depth-velocity chart PNG skipped: {}; "
                                           "the CSV of the points was still written".format(e)) if x)
        log("depth-velocity chart PNG skipped: {}".format(e))
    except Exception as e:                                       # noqa: BLE001
        res["status"] = "csv_only"
        res["note"] = "; ".join(x for x in (res["note"], "depth-velocity chart PNG failed "
                                           "({}: {}); the CSV of the points was still "
                                           "written".format(type(e).__name__, e)) if x)
        log("depth-velocity chart PNG failed ({}: {})".format(type(e).__name__, e))
    return res


def rows_from_csv(path):
    """Habitable_Structures.csv (or the chart's own CSV) as a list of dicts."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def build_from_csv(csv_path, meta, out_dir, stem=CHART_STEM, wet_min=0.0, log=None):
    """Build the chart from an existing Habitable_Structures.csv (read only)."""
    return build(rows_from_csv(csv_path), meta, out_dir, stem=stem, wet_min=wet_min, log=log)


def write_for_run(structs, out_dir, depth_path="", scenario=None, dam_id="", dam_name="",
                  source_override="", suffix="", log=None, no_velocity=False):
    """Pipeline helper: label the source, write the chart next to the classify outputs."""
    sc_source = ""
    sc_name = ""
    if scenario is not None:
        sc_source = getattr(scenario, "source", "") or ""
        sc_name = getattr(scenario, "name", "") or ""
        depth_path = depth_path or getattr(scenario, "depth", "") or ""
    label, _kind = detect_source(depth_path, override=source_override,
                                 scenario_source=sc_source)
    meta = {"dam_id": dam_id, "dam_name": dam_name, "source": label,
            "run_date": run_date_of(depth_path), "scenario": sc_name if suffix else "",
            "sampling": SAMPLING_PEAK, "no_velocity": bool(no_velocity)}
    stem = CHART_STEM + (suffix or "")
    return build(structs, meta, out_dir, stem=stem, log=log)
