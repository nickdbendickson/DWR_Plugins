"""3.12.3: culvert prep - a corrected COPY of a structures layer from the culvert pre-check.

What DWR-2801's manual invfix / invfix2 did (2026-10-25), as a tool (GDAL/OGR + numpy, no QGIS,
no model, only window reads around each culvert end):
  1. culvert_precheck.check() on the run DEM (the run's own rules: reconcile_inverts with the
     #218 channel bed first, then anuga_culverts + fine_ground_inverts);
  2. for each HIGH end (given invert > lowest fine ground within max(apron, 5 m) + 0.3 m) the
     proposed invert is that lowest ground (`suggested_invert_ft`, LiDAR-derived, NOT surveyed);
  3. a proposal is APPLIED to the copy only when it is small (|change| <= limit_ft, 1 ft by
     default) and the end is on native fine ground (the composite's top source, cell <= 1 m) of
     a single-part line. Everything else is FLAGGED: left unchanged in the copy and listed for
     review; FALLBACK ends (no fine ground) and ends on a fill source are listed too;
  4. optional straightening of a winding culvert line to its end-point chord (like
     09-122-22.1) is only ever proposed (FLAGGED), never applied without an accept;
  5. the input is never written: the copy goes to a new path; a change log (.md / .csv / .json)
     records the sha256 of the input and of the copy, every item and its status;
  6. accept flow: fill the `accept` column of the change-log CSV (yes / y / x / 1 / true) or
     list item ids in a text file, and run again from the SAME input with `accept=` that file:
     the accepted items are applied (an accepted item whose proposal changed since is left
     unchanged as `accept_stale`); `apply_auto=False` applies the accepted items only.
The run itself is unchanged: its inverts, the #210 bed floor (NDBREACH_CULVERT_BED_FLOOR) and
the #218 channel-bed / inventory-vs-LiDAR check still act on whatever layer it is given; a
post-check of the copy with the same rules is in the log.
"""
import csv
import datetime
import json
import math
import os
import shutil

from . import culvert_precheck as cp
from . import gdal_io
from . import structures as structmod
from .units import FT

DEFAULT_LIMIT_FT = 1.0          # larger invert changes are flagged, not applied
NATIVE_CELL_MAX_M = 1.0         # an end on a coarser source (e.g. the 5 m fill) is not native
STALE_TOL_FT = 0.005            # an accepted proposal must still be this close

RULE_INVERT = "invert_to_native_ground"
RULE_GEOMETRY = "straighten_line"
RULE_FALLBACK = "fallback_end"
RULE_NON_NATIVE = "non_native_ground"

ST_APPLIED = "applied"                  # auto: small change on native ground
ST_ACCEPTED = "applied_accepted"        # flagged before, accepted by the engineer
ST_FLAGGED = "flagged"                  # left unchanged in the copy; for review
ST_STALE = "accept_stale"               # accepted, but the proposal changed: left unchanged
ST_NOT_ACCEPTED = "not_applied"         # apply_auto=False and not accepted
ST_REVIEW = "review_only"               # nothing proposed (fallback / fill ground)

ACCEPT_WORDS = ("yes", "y", "x", "1", "true", "accept", "accepted", "ok")

LOG_COLUMNS = ("item_id", "accept", "status", "rule", "fid", "culvert_id", "name", "end",
               "field", "old_ft", "proposed_ft", "change_ft", "old_m", "proposed_m",
               "fine_ground_m", "search_m", "fine_source", "fine_cell_m", "on_top_source",
               "precheck_flag", "reason")


def plugin_version():
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "metadata.txt")
    try:
        with open(p, encoding="utf-8") as f:
            for ln in f:
                if ln.startswith("version="):
                    return ln.split("=", 1)[1].strip()
    except OSError:
        pass
    return "?"


def _round(v, nd):
    return None if v is None else round(float(v), nd)


def _composite_coverage(fine_dem):
    """A composite VRT's own sources, top first (as culvert_precheck.find_dam_inputs), when it
    has more than one; else []."""
    if not (fine_dem and fine_dem.lower().endswith(".vrt")):
        return []
    try:
        src = [p for p in cp.vrt_sources(fine_dem) if os.path.exists(p)]
    except Exception:                                            # noqa  (not a VRT we can read)
        return []
    return src if len(src) > 1 else []


def _native(row, cell_max):
    """'' when the end is on native fine ground, else why not."""
    if row.get("on_top_source") == "no":
        return "the end is on {} (not the composite's top source)".format(
            row.get("fine_source") or "a fill source")
    c = row.get("fine_cell_m")
    if c is not None and float(c) > cell_max + 1e-6:
        return "the end is on {:.1f} m ground ({}), coarser than {:.0f} m".format(
            float(c), row.get("fine_source") or "?", cell_max)
    return ""


# ------------------------------------------------------------------------------ accept files
def read_accept(path):
    """{item_id: proposed_ft or None} from a change-log / review CSV (columns item_id, accept and
    proposed_ft) or a text file of item ids (one per line, # comments)."""
    out = {}
    if not path:
        return out
    with open(path, encoding="utf-8-sig", newline="") as f:
        text = f.read()
    first = text.splitlines()[0] if text.strip() else ""
    if "item_id" in first and "," in first:
        for r in csv.DictReader(text.splitlines()):
            if str(r.get("accept") or "").strip().lower() in ACCEPT_WORDS:
                iid = (r.get("item_id") or "").strip()
                if iid:
                    out[iid] = cp._num(r.get("proposed_ft"))
    else:
        for ln in text.splitlines():
            ln = ln.split("#", 1)[0].strip()
            if ln:
                out[ln.split(",")[0].strip()] = None
    return out


# ------------------------------------------------------------------------------ plan
def _line_info(path, layer):
    """{fid: (n_vertices, length_m, chord_m, first, last, parts)} of the structures layer."""
    _g, ogr, _o = cp._gdal()
    from ..damsuite import gdalsafe as GS
    ds = GS.open_vector(path)
    lyr = ds.GetLayerByName(layer)
    out = {}
    for f in lyr:
        g = f.GetGeometryRef()
        if g is None:
            continue
        parts = ([g.GetGeometryRef(k) for k in range(g.GetGeometryCount())]
                 if ogr.GT_Flatten(g.GetGeometryType()) == ogr.wkbMultiLineString else [g])
        p = parts[0]
        pts = [(p.GetX(k), p.GetY(k)) for k in range(p.GetPointCount())]
        if len(pts) < 2:
            continue
        ln = sum(math.dist(pts[k], pts[k + 1]) for k in range(len(pts) - 1))
        out[f.GetFID()] = (len(pts), ln, math.dist(pts[0], pts[-1]), pts[0], pts[-1], len(parts))
    return out


def plan(rows, lines, fields, limit_ft=DEFAULT_LIMIT_FT, simplify_ratio=None,
         cell_max=NATIVE_CELL_MAX_M, accept=None, apply_auto=True):
    """Items (dicts, LOG_COLUMNS + internal keys) from the pre-check rows. Pure."""
    accept = accept or {}
    items = []
    seen_geom = set()   # one geometry item per fid
    for r in rows:
        if r["flag"] == cp.FLAG_SKIPPED:
            continue
        fid = r["fid"]
        up = r["end"] == "upstream"
        field = fields.get("S_INV_UP" if up else "S_INV_DN") or ("invert_up" if up else "invert_dn")
        base = {k: r.get(k) for k in ("fid", "name", "end", "fine_ground_m", "search_m",
                                      "fine_source", "fine_cell_m", "on_top_source")}
        base.update(culvert_id=r.get("culvert_id"), precheck_flag=r["flag"], field=field,
                    item_id="{}:{}".format(fid, field), old_ft=r.get("given_invert_ft"),
                    old_m=r.get("given_invert_m"), accept="")
        nat = _native(r, cell_max)
        parts = (lines.get(fid) or (0, 0, 0, None, None, 1))[5]
        if r["flag"] == cp.FLAG_FALLBACK:
            items.append(dict(base, rule=RULE_FALLBACK, status=ST_REVIEW, proposed_ft=None,
                              proposed_m=None, change_ft=None,
                              reason="no fine ground within {:.0f} m: the run uses the 30 m "
                                     "model DEM here; nothing proposed".format(r["search_m"])))
            continue
        if r["flag"] != cp.FLAG_HIGH:
            if nat:
                items.append(dict(base, rule=RULE_NON_NATIVE, status=ST_REVIEW,
                                  proposed_ft=None, proposed_m=None, change_ft=None,
                                  reason=nat + "; nothing proposed"))
            continue
        new_ft, new_m = r["suggested_invert_ft"], r["suggested_invert_m"]
        ch = round(new_ft - r["given_invert_ft"], 2)
        why = []
        if abs(ch) > limit_ft + 1e-9:
            why.append("change {:+.2f} ft is over the {:g} ft limit".format(ch, limit_ft))
        if nat:
            why.append(nat)
        if parts > 1:
            why.append("multi-part line: the end cannot be tied to one field")
        it = dict(base, rule=RULE_INVERT, proposed_ft=new_ft, proposed_m=new_m, change_ft=ch)
        it["reason"] = ("given {:.2f} ft ({:.3f} m) is {:.2f} m above the lowest fine ground "
                        "within {:.0f} m ({:.3f} m)".format(r["given_invert_ft"],
                                                            r["given_invert_m"], r["delta_m"],
                                                            r["search_m"], r["fine_ground_m"]))
        if why:
            it["reason"] += "; FLAGGED: " + "; ".join(why)
        items.append(_decide(it, bool(why), accept, apply_auto))
    if simplify_ratio:
        for fid, (n, ln, chord, _a, _b, parts) in sorted(lines.items()):
            if fid in seen_geom or n <= 2 or parts > 1 or chord <= 0:
                continue
            if not any(r["fid"] == fid and r["flag"] != cp.FLAG_SKIPPED for r in rows):
                continue                       # culverts only, as the run models them
            if ln / chord <= simplify_ratio:
                continue
            seen_geom.add(fid)
            r0 = next(r for r in rows if r["fid"] == fid)
            it = dict(item_id="{}:geometry".format(fid), accept="", rule=RULE_GEOMETRY, fid=fid,
                      culvert_id=r0.get("culvert_id"), name=r0.get("name"), end="",
                      field="geometry", old_ft=round(ln / FT, 1), proposed_ft=round(chord / FT, 1),
                      change_ft=round((chord - ln) / FT, 1), old_m=round(ln, 2),
                      proposed_m=round(chord, 2), precheck_flag="",
                      reason="line {} vertices, {:.1f} m long; the end-point chord is {:.1f} m "
                             "(ratio {:.3f} > {:g}): straighten to 2 vertices (same ends); "
                             "FLAGGED: geometry is never changed without an accept".format(
                                 n, ln, chord, ln / chord, simplify_ratio))
            items.append(_decide(it, True, accept, apply_auto))
    return items


def _decide(it, flagged, accept, apply_auto):
    iid = it["item_id"]
    if iid in accept:
        it["accept"] = "yes"
        want = accept[iid]
        if want is not None and it["proposed_ft"] is not None and \
                abs(float(want) - float(it["proposed_ft"])) > STALE_TOL_FT + 1e-9:
            it["status"] = ST_STALE
            it["reason"] += "; accepted {:.2f} but the proposal is now {:.2f}: left " \
                            "unchanged".format(float(want), float(it["proposed_ft"]))
        else:
            it["status"] = ST_ACCEPTED if flagged else ST_APPLIED
        return it
    if flagged:
        it["status"] = ST_FLAGGED
    else:
        it["status"] = ST_APPLIED if apply_auto else ST_NOT_ACCEPTED
    return it


APPLIED = (ST_APPLIED, ST_ACCEPTED)


# ------------------------------------------------------------------------------ write the copy
def _note_for(items, fine_label, today, ver):
    parts = []
    for it in items:
        if it["field"] == "geometry":
            parts.append("line straightened to its end-point chord ({:.1f} -> {:.1f} m)".format(
                it["old_m"], it["proposed_m"]))
        else:
            parts.append("{} {:.2f} -> {:.2f} ft".format(it["field"], it["old_ft"],
                                                         it["proposed_ft"]))
    acc = any(it["status"] == ST_ACCEPTED for it in items)
    txt = "PREP {} (DamSafety Runner {} culvert prep{}): {}".format(
        today, ver, ", engineer-accepted" if acc else "", ", ".join(parts))
    inv = [it for it in items if it["field"] != "geometry"]
    if inv:
        srcs = sorted({"{}{}".format(it.get("fine_source") or fine_label,
                                     ", {:g} m cells".format(it["fine_cell_m"])
                                     if it.get("fine_cell_m") else "") for it in inv})
        txt += (" = lowest fine-DEM ground within {:g} m of the end ({}); the given value was "
                "> ground + 0.3 m; LiDAR-derived, NOT surveyed".format(
                    max(float(it.get("search_m") or 5.0) for it in inv), "; ".join(srcs)))
    return txt


def write_copy(src, dst, layer, items, fields, fine_label, info_value=None, today=None):
    """Copy src to dst and apply the APPLIED items there. Returns the number of features
    changed. src is only read."""
    _g, ogr, _o = cp._gdal()
    shutil.copyfile(src, dst)
    today = today or datetime.date.today().isoformat()
    ver = plugin_version()
    by_fid = {}
    for it in items:
        if it["status"] in APPLIED:
            by_fid.setdefault(it["fid"], []).append(it)
    ds = ogr.Open(dst, 1)
    if ds is None:
        raise RuntimeError("cannot open the copy {} for writing".format(dst))
    lyr = ds.GetLayerByName(layer)
    defn = lyr.GetLayerDefn()
    names = {defn.GetFieldDefn(k).GetName().lower(): defn.GetFieldDefn(k).GetName()
             for k in range(defn.GetFieldCount())}
    notes_f = names.get("notes") or names.get("note") or names.get("comments")
    n = 0
    for fid, its in sorted(by_fid.items()):
        f = lyr.GetFeature(int(fid))
        if f is None:
            continue
        for it in its:
            if it["field"] == "geometry":
                g = f.GetGeometryRef()
                pts = [(g.GetX(0), g.GetY(0)), (g.GetX(g.GetPointCount() - 1),
                                                 g.GetY(g.GetPointCount() - 1))]
                ng = ogr.Geometry(ogr.wkbLineString)
                for x, y in pts:
                    ng.AddPoint_2D(x, y)
                f.SetGeometry(ng)
                if "line_len_ft" in names:
                    f.SetField(names["line_len_ft"], float(it["proposed_ft"]))
                if "line_len_src" in names:
                    f.SetField(names["line_len_src"],
                               "straight chord of the placed line ends; {} interior vertices "
                               "dropped (culvert prep)".format(g.GetPointCount() - 2))
            else:
                f.SetField(names.get(it["field"].lower(), it["field"]), float(it["proposed_ft"]))
        if notes_f:
            old = f.GetField(notes_f)
            note = _note_for(its, fine_label, today, ver)
            f.SetField(notes_f, (str(old).rstrip() + "; " + note) if old else note)
        if lyr.SetFeature(f) != 0:
            raise RuntimeError("could not update feature {} in {}".format(fid, dst))
        n += 1
    if info_value is not None:
        info = ds.GetLayerByName("runner_culverts_info")
        if info is not None:
            idef = info.GetLayerDefn()
            inames = [idef.GetFieldDefn(k).GetName() for k in range(idef.GetFieldCount())]
            if "key" in inames and "value" in inames:
                f = ogr.Feature(idef)
                f.SetField("key", "culvert_prep")
                f.SetField("value", info_value)
                info.CreateFeature(f)
    ds = None
    return n


# ------------------------------------------------------------------------------ the tool
def prepare(structures_path, fine_dem, output, dam="", coverage=None, layer=None, fields=None,
            z_choice=gdal_io.Z_AUTO, reconcile=True, model_cell=cp.MODEL_CELL_M,
            reconcile_tol_m=cp.RECONCILE_TOL_M, resample=None, limit_ft=DEFAULT_LIMIT_FT,
            simplify_ratio=None, accept_path=None, apply_auto=True, log_dir=None,
            overwrite=False, cell_max=NATIVE_CELL_MAX_M, today=None, post_check=True):
    """Write the corrected copy `output` and its change log; returns the log dict (also written
    as <log_dir>/culvert_prep_<dam>.json with .md / .csv). Never writes structures_path."""
    src = os.path.abspath(structures_path)
    out = os.path.abspath(output)
    if not os.path.isfile(src):
        raise ValueError("structures layer not found: {}".format(src))
    if os.path.realpath(src) == os.path.realpath(out) or (
            os.path.exists(out) and os.path.samefile(src, out)):
        raise ValueError("the output must not be the input structures file")
    if os.path.exists(out) and not overwrite:
        raise ValueError("{} exists; choose a new name or allow overwriting the earlier copy".format(out))
    if accept_path and os.path.realpath(accept_path) == os.path.realpath(src):
        raise ValueError("the accept file must not be the structures file")
    limit_ft = float(DEFAULT_LIMIT_FT if limit_ft is None else limit_ft)
    if limit_ft < 0:
        raise ValueError("the change limit must be >= 0 ft")
    log_dir = os.path.abspath(log_dir or os.path.dirname(out))
    dam = dam or os.path.splitext(os.path.basename(src))[0]
    stem = "culvert_prep_" + "".join(c if c.isalnum() or c in "-_." else "_" for c in dam)
    cov = list(coverage) if coverage else _composite_coverage(fine_dem)
    sha_in = cp.sha256(src)
    accept = read_accept(accept_path) if accept_path else {}
    rows, summ = cp.check(src, fine_dem, dam=dam, coverage=cov, layer=layer, fields=fields,
                          z_choice=z_choice, reconcile=reconcile, model_cell=model_cell,
                          resample=resample, reconcile_tol_m=reconcile_tol_m)
    lname = summ["layer"]
    from ..damsuite import gdalsafe as GS
    vds = GS.open_vector(src)
    d = vds.GetLayerByName(lname).GetLayerDefn()
    fmap = cp.field_map([d.GetFieldDefn(k).GetName() for k in range(d.GetFieldCount())], fields)
    d = vds = None
    lines = _line_info(src, lname)
    items = plan(rows, lines, fmap, limit_ft=limit_ft, simplify_ratio=simplify_ratio,
                 cell_max=cell_max, accept=accept, apply_auto=apply_auto)
    known = {it["item_id"] for it in items}
    unknown = sorted(k for k in accept if k not in known)
    fine_label = (summ.get("top_source") or os.path.basename(fine_dem))
    today = today or datetime.date.today().isoformat()
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    tmp = os.path.splitext(out)[0] + ".prep_tmp" + (os.path.splitext(out)[1] or ".gpkg")
    info = json.dumps({"date": today, "runner": plugin_version(), "input_sha256": sha_in,
                       "applied": sum(it["status"] in APPLIED for it in items),
                       "flagged": sum(it["status"] in (ST_FLAGGED, ST_STALE, ST_NOT_ACCEPTED)
                                      for it in items),
                       "log": stem + ".json"})
    try:
        n_feat = write_copy(src, tmp, lname, items, fmap, fine_label, info_value=info,
                            today=today)
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    if cp.sha256(src) != sha_in:
        raise RuntimeError("the input structures layer changed while preparing the copy")
    sha_out = cp.sha256(out)
    post = None
    if post_check:
        _r2, s2 = cp.check(out, fine_dem, dam=dam, coverage=cov, layer=lname, fields=fields,
                           z_choice=z_choice, reconcile=reconcile, model_cell=model_cell,
                           resample=resample, reconcile_tol_m=reconcile_tol_m)
        post = {k: s2.get(k) for k in ("culverts", "ends_high", "culverts_high", "ends_fallback",
                                       "culverts_fallback", "ends_not_on_top_source")}
    count = {}
    for it in items:
        count[it["status"]] = count.get(it["status"], 0) + 1
    log = {
        "tool": "DamSafety Runner culvert prep", "runner_version": plugin_version(),
        "date": today, "dam": dam,
        "input": src, "input_sha256": sha_in, "output": out, "output_sha256": sha_out,
        "layer": lname, "fine_dem": os.path.abspath(fine_dem), "coverage": cov,
        "top_source": summ.get("top_source", ""),
        "settings": {"limit_ft": limit_ft, "simplify_ratio": simplify_ratio,
                     "native_cell_max_m": cell_max, "apply_auto": bool(apply_auto),
                     "reconcile": bool(reconcile), "reconcile_tol_ft": round(reconcile_tol_m / FT, 3),
                     "cap_tol_m": structmod.INVERT_CAP_TOL_M,
                     "search_min_m": structmod.INVERT_SEARCH_MIN_M,
                     "bed_floor_env": _bed_floor()},
        "accept_file": os.path.abspath(accept_path) if accept_path else None,
        "accept_file_sha256": cp.sha256(accept_path) if accept_path else None,
        "accepted_ids_not_found": unknown,
        "features_changed": n_feat, "counts": count,
        "precheck": {k: summ.get(k) for k in ("culverts", "ends_high", "culverts_high",
                                               "ends_fallback", "culverts_fallback",
                                               "ends_not_on_top_source")},
        "post_check": post,
        "items": [{k: it.get(k) for k in LOG_COLUMNS} for it in items],
    }
    log["files"] = write_log(log, log_dir, stem)
    return log


def _bed_floor():
    try:
        return structmod.bed_floor_setting()
    except ValueError as e:
        return "invalid: {}".format(e)


def _f(v, nd=2):
    if v is None or v == "":
        return "–"
    return "{:.{}f}".format(v, nd) if isinstance(v, float) else str(v)


def write_log(log, out_dir, stem):
    os.makedirs(out_dir, exist_ok=True)
    paths = {k: os.path.join(out_dir, stem + "." + k) for k in ("json", "csv", "md")}
    for p in paths.values():
        if os.path.realpath(p) in (os.path.realpath(log["input"]), os.path.realpath(log["output"])):
            raise ValueError("the change log would overwrite {}".format(p))
    with open(paths["csv"], "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_COLUMNS)
        w.writeheader()
        for it in log["items"]:
            w.writerow({k: "" if it.get(k) is None else it.get(k) for k in LOG_COLUMNS})
    with open(paths["md"], "w", encoding="utf-8") as f:
        f.write(markdown(log, os.path.basename(paths["csv"])))
    log = dict(log, files=paths)
    with open(paths["json"], "w", encoding="utf-8") as f:
        json.dump(log, f, indent=1)
    return paths


def markdown(log, csv_name):
    s = log["settings"]
    c = log["counts"]
    L = ["# Culvert prep: {}".format(log["dam"]), "",
         "Corrected COPY of the structures layer; the input is never written.", "",
         "| | path | sha256 |", "|---|---|---|",
         "| input | `{}` | `{}` |".format(log["input"], log["input_sha256"]),
         "| output (copy) | `{}` | `{}` |".format(log["output"], log["output_sha256"]),
         ]
    if log.get("accept_file"):
        L.append("| accept file | `{}` | `{}` |".format(log["accept_file"],
                                                      log["accept_file_sha256"]))
    L += ["", "- Runner {}, {}; layer `{}`; fine ground `{}`{}".format(
        log["runner_version"], log["date"], log["layer"], log["fine_dem"],
        " (top source {})".format(log["top_source"]) if log["top_source"] else ""),
        "- Rule: a HIGH end (given > lowest fine ground within max(apron, {:.0f} m) + {:.1f} m) "
        "gets that lowest ground. Applied only when |change| <= {:g} ft, on native ground "
        "(top source, cell <= {:g} m), single-part line{}. Everything else is flagged and "
        "left unchanged.".format(s["search_min_m"], s["cap_tol_m"], s["limit_ft"],
                                 s["native_cell_max_m"],
                                 "" if s["apply_auto"] else " - auto-apply is OFF: accepted "
                                 "items only"),
        "- Line straightening: {}.".format("proposed (flagged) above a length / chord ratio of "
                                          "{:g}".format(s["simplify_ratio"])
                                          if s["simplify_ratio"] else "off"),
        "- Pre-check (input): HIGH {} ends / FALLBACK {} ends / not on the top source {} ends."
        .format(log["precheck"]["ends_high"], log["precheck"]["ends_fallback"],
                log["precheck"]["ends_not_on_top_source"])]
    if log.get("post_check"):
        p = log["post_check"]
        L.append("- Post-check (copy, same rules): HIGH {} ends / FALLBACK {} ends.".format(
            p["ends_high"], p["ends_fallback"]))
    L.append("- Items: " + (", ".join("{} {}".format(v, k) for k, v in sorted(c.items()))
                            or "none") + "; features changed in the copy: {}.".format(
        log["features_changed"]))
    if log.get("accepted_ids_not_found"):
        L.append("- Accepted ids not found: " + ", ".join(log["accepted_ids_not_found"]))
    L.append("- #210 bed floor (NDBREACH_CULVERT_BED_FLOOR) at prep time: `{}` (the run applies "
             "its own setting).".format(s["bed_floor_env"]))
    L += ["", "To accept flagged items: put `yes` in the `accept` column of `{}` (or list the "
              "item ids in a text file) and run the prep again from the SAME input with that "
              "file as the accept list.".format(csv_name), ""]
    for title, sts in (("Applied", APPLIED),
                       ("Flagged - left unchanged, for review", (ST_FLAGGED, ST_STALE,
                                                                 ST_NOT_ACCEPTED)),
                       ("Listed for review - nothing proposed", (ST_REVIEW,))):
        its = [i for i in log["items"] if i["status"] in sts]
        L += ["## {} ({})".format(title, len(its)), ""]
        if not its:
            L += ["None.", ""]
            continue
        L += ["| item | culvert | name | old | new | change | fine ground (m) | source | status "
              "| why |", "|---|---|---|---|---|---|---|---|---|---|"]
        for i in its:
            unit = "m" if i["field"] == "geometry" else "ft"
            L.append("| `{}` | {} | {} | {} {} | {} | {} | {} | {} | {} | {} |".format(
                i["item_id"], i["culvert_id"], i["name"],
                _f(i["old_m"] if unit == "m" else i["old_ft"]), unit,
                _f(i["proposed_m"] if unit == "m" else i["proposed_ft"]),
                _f(round((i["proposed_m"] - i["old_m"]), 2) if unit == "m" and i["proposed_m"]
                   is not None else i["change_ft"]),
                _f(i["fine_ground_m"], 3), i["fine_source"] or "–", i["status"],
                str(i["reason"]).replace("|", "/")))
        L.append("")
    L.append("Proposed inverts are the lowest LiDAR ground near each end, not surveyed.")
    return "\n".join(L) + "\n"


# ------------------------------------------------------------------------------ CLI
USAGE = """culvert-prep: a corrected COPY of a structures layer from the culvert pre-check
(HIGH ends -> lowest native fine ground; big / non-native / geometry changes flagged).

  ndbreach_cli.py culvert-prep --structures S.gpkg --fine-dem DEM.vrt --output S_prep.gpkg
                               [--coverage [LABEL=]R ...] [--limit-ft 1] [--simplify-ratio R]
                               [--accept LOG.csv|IDS.txt] [--accepted-only] [--dam ID]
"""


def _parser():
    import argparse
    ap = argparse.ArgumentParser(prog="ndbreach_cli.py culvert-prep", description=USAGE,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--structures", required=True, help="input structures GeoPackage (read only)")
    ap.add_argument("--layer", help="layer (default runner_culverts, else the first line layer)")
    ap.add_argument("--fine-dem", required=True, help="the run's fine DEM (e.g. composite VRT)")
    ap.add_argument("--coverage", nargs="*", default=None, help="sources top first "
                    "([LABEL=]PATH); default: a composite VRT's own sources")
    ap.add_argument("--output", required=True, help="the corrected copy (a new file)")
    ap.add_argument("--log-dir", help="change-log folder (default: the output's folder)")
    ap.add_argument("--dam", default="", help="dam id for the log")
    ap.add_argument("--limit-ft", type=float, default=DEFAULT_LIMIT_FT,
                    help="larger invert changes are flagged (default 1 ft)")
    ap.add_argument("--simplify-ratio", type=float, default=None, help="propose (flagged) "
                    "straightening lines longer than this x their end-point chord (default off)")
    ap.add_argument("--accept", help="accept list: the change-log CSV with its accept column "
                    "filled, or a text file of item ids")
    ap.add_argument("--accepted-only", action="store_true", help="apply the accepted items only")
    ap.add_argument("--dem-z", choices=cp.ZUNITS, default=gdal_io.Z_AUTO)
    ap.add_argument("--no-reconcile", action="store_true")
    ap.add_argument("--overwrite", action="store_true", help="replace an earlier OUTPUT (never "
                    "the input)")
    return ap


def main(argv=None):
    import sys
    a = _parser().parse_args(argv)
    try:
        log = prepare(a.structures, a.fine_dem, a.output, dam=a.dam, coverage=a.coverage,
                      layer=a.layer, z_choice=a.dem_z, reconcile=not a.no_reconcile,
                      limit_ft=a.limit_ft, simplify_ratio=a.simplify_ratio, accept_path=a.accept,
                      apply_auto=not a.accepted_only, log_dir=a.log_dir, overwrite=a.overwrite)
    except (ValueError, RuntimeError, OSError) as e:
        sys.stderr.write("culvert-prep: {}\n".format(e))
        return 2
    c = log["counts"]
    print("{}: applied {}, accepted {}, flagged {}, review-only {} -> {} (sha256 {}); log {}"
          .format(log["dam"], c.get(ST_APPLIED, 0), c.get(ST_ACCEPTED, 0),
                  c.get(ST_FLAGGED, 0) + c.get(ST_STALE, 0) + c.get(ST_NOT_ACCEPTED, 0),
                  c.get(ST_REVIEW, 0), log["output"], log["output_sha256"][:12],
                  log["files"]["md"]))
    return 0
