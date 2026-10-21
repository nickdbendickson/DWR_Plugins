"""Runner 3.10.56: a run's culverts and bridges (as ANUGA used them) in the HEC-RAS geometry.

`hecras_model.build_model` already turned the run's own structures into SA/2D area connections
inside the 2D area (`hecras_model.structure_connection`: a weir along the road from the terrain,
a culvert group for a culvert, an open gap in the weir for a bridge or notch). This module adds
what a HEC-RAS run needs on top of that, and lets the structures come from ANOTHER run:

* `load_run_structures(path)`: the culverts and bridges of any finished run (dam folder,
  `_breach_model` folder or `run_report.json`), as the run used them (`structures_used`, else
  the settings), in that run's model CRS (`work_crs`). A culvert whose inverts the report leaves
  empty gets the inverts ANUGA itself used, read from the run's `anuga/anuga_job.npz`
  (`culverts_json`), so the HEC-RAS culvert is the ANUGA culvert. Use it for a HEC-RAS package
  built from a LISFLOOD-FP run (no structures) next to an ANUGA run of the same dam
  (build option `structures_from`).
* `connections(...)`: the build loop (crest per structure, connection per structure, names),
  shared by `build_model` and `add_to_package`.
* `connection_breaklines(...)`: the connection lines as breaklines BUILT INTO the computation
  points (`hecras_breaklines.enforce`: a pair of points half a spacing each side of the line,
  so cell faces lie on it; HEC-RAS keeps a given point layout and does not enforce breaklines
  itself), then `hecras_breaklines.fix_faces` (HEC-RAS's 8-face and perimeter limits) on the
  points near the new lines only; the rest of the mesh is not touched.
* `cells_check(...)` / `fit_to_cells(...)`: HEC-RAS refuses a weir (or a weir gap) lower than a
  cell it connects ('... has a weir elevation lower than the cells', Absaraka v2e_gmsh_r) and
  a culvert invert below the cell at its end. The cells either side of each face on each
  connection line get their lowest ground on the written terrain
  (`hecras_breaklines.cell_min_elevations`, which errs high), and the weir points on a face are
  raised to the higher of its two cells, a culvert invert to its cell (+ FIT_MARGIN). Every
  change is listed. The terrain is never changed.
* `add_to_package(package, run, out)`: the same for a package that is already built (copy,
  structures added, nothing else changed).

Mapping limits (also in the README section): HEC-RAS's culvert group is FHWA inlet/outlet
control (chart 1 concrete pipe or chart 8 box, Ke 0.5 / 0.4, exit 1.0), ANUGA's Boyd operator
uses a single loss coefficient (`losses`, recorded); a bridge is an open gap in the road weir
down to its invert, as ANUGA and LISFLOOD-FP cut it into the DEM: no deck, low chord, piers or
pressure flow (ANUGA has none of those either). A structure outside the HEC-RAS 2D area is
left out and listed.
"""
import json
import math
import os
import shutil

import numpy as np

from . import hecras_breaklines as hb
from . import hecras_model as hm
from . import conditioning as cond

ANUGA_JOB = os.path.join("anuga", "anuga_job.npz")
FIT_MARGIN_FT = 0.05        # a raised weir point / invert sits this far above the cell (0.05:
#                              HEC-RAS 6.6 cell minimums came out up to 0.006 ft above the model's)
BREAKLINE_REACH_FT = 400.0  # fix_faces may move points this close to a new line, no others
DAM_CLEAR_CELLS = 2.0       # no breakline on a connection this close (cells) to the dam line


# ---------------------------------------------------------------- the structures of a run --
def _report_path(path):
    p = str(path)
    if os.path.isfile(p):
        return p
    for cand in (os.path.join(p, "_breach_model", "run_report.json"),
                 os.path.join(p, "run_report.json")):
        if os.path.exists(cand):
            return cand
    raise hm.HecRasModelError("no run_report.json in {} (a dam folder, its _breach_model "
                              "folder or the report itself)".format(p))


def anuga_job_culverts(model_dir):
    """The culverts ANUGA ran ({name: spec}) from <model dir>/anuga/anuga_job.npz, or {}."""
    job = os.path.join(model_dir, ANUGA_JOB)
    if not os.path.exists(job):
        return {}
    try:
        with np.load(job, allow_pickle=False) as z:
            if "culverts_json" not in z.files:
                return {}
            specs = json.loads(str(z["culverts_json"]))
    except (OSError, ValueError, KeyError):
        return {}
    return {str(s.get("name") or ""): s for s in specs if isinstance(s, dict)}


def load_run_structures(path):
    """(structures, source text, work CRS text or None, notes) of a finished run.
    Inverts the report leaves empty come from ANUGA's own job file when it has the culvert."""
    rp = _report_path(path)
    with open(rp, encoding="utf-8") as f:
        report = json.load(f)
    structs, src = hm.run_structures(report)
    notes = []
    job = anuga_job_culverts(os.path.dirname(rp))
    filled = 0
    for s in structs:
        spec = job.get(str(s.name or ""))
        if spec and s.kind == "culvert" and len(spec.get("invert_elevations") or []) == 2:
            inv = [float(v) for v in spec["invert_elevations"]]
            if s.invert_up_m is None or s.invert_dn_m is None:
                s.invert_up_m = inv[0] if s.invert_up_m is None else s.invert_up_m
                s.invert_dn_m = inv[1] if s.invert_dn_m is None else s.invert_dn_m
                filled += 1
    if filled:
        notes.append("{} culvert(s) took the inverts ANUGA used (anuga_job.npz)".format(filled))
    if structs:
        src = "{} ({})".format(src, rp)
    return structs, src, report.get("work_crs"), notes


# ---------------------------------------------------------------- connections -------------
def connections(structs, to_model, perim, cell, terrain_tif, srs, units, log=None,
                used=None):
    """The build loop of build_model: each structure as a connection inside the 2D area.
    Returns {'exported': [...], 'skipped': [...]} (the sinfo of build_model, less 'source')."""
    log = log or (lambda m: None)
    used = set(used or (hm.CONN_NAME, hm.AREA_NAME, hm.SA_NAME))
    out = {"exported": [], "skipped": []}
    todo = []
    for s in structs:
        label = s.name or s.kind or "?"
        line_u = to_model(np.asarray(s.line, dtype=float)) if (
            to_model is not None and len(s.line)) else np.zeros((0, 2))
        if to_model is None:
            todo.append((s, label, line_u, None, "the run did not record its model coordinate "
                                                 "system"))
            continue
        if len(line_u) < 2:
            todo.append((s, label, line_u, None, "needs a line with two ends"))
            continue
        pad = units.from_ft(hm.STRUCT_MAX_HALF_FT) + 4 * cell
        sb = (line_u[:, 0].min() - pad, line_u[:, 1].min() - pad,
              line_u[:, 0].max() + pad, line_u[:, 1].max() + pad)
        todo.append((s, label, line_u, hm._tif_window(terrain_tif, sb, srs), None))
    crests = []
    for i, (s, label, line_u, g, whynot) in enumerate(todo):
        if g is not None and cond.points_in_polygon(line_u[:, 0], line_u[:, 1], perim).all():
            cr = hm.structure_crest(line_u, lambda xy, g=g: hm._sample(g[0], g[1], g[2], g[3], xy),
                                    g[3], units.from_ft(0.1))
            if cr is not None:
                crests.append((i, label, cr[0]))
    done = set()
    for i, (s, label, line_u, g, whynot) in enumerate(todo):
        c = None
        if whynot is None:
            ground = (lambda xy, g=g: hm._sample(g[0], g[1], g[2], g[3], xy))
            avoid = [(e["name"], e["line"]) for e in out["exported"]]
            c, whynot = hm.structure_connection(s, line_u, ground, perim, cell, g[3], units, "",
                                                avoid, [(lab, q) for j, lab, q in crests if j != i])
            if whynot and any(j > i for j, _, _ in crests):
                c, whynot = hm.structure_connection(s, line_u, ground, perim, cell, g[3], units,
                                                    "", avoid, [(lab, q) for j, lab, q in crests
                                                                if j in done])
        if whynot and g is not None and s.kind != "culvert" and "too close to" in whynot:
            # 3.10.56: a bridge / notch on the same road crossing as a connection already
            # placed (an NBI bridge on a guessed culvert's crossing): its gap goes into that
            # connection's weir, as ANUGA and LISFLOOD-FP had both at that crossing
            ground = (lambda xy, g=g: hm._sample(g[0], g[1], g[2], g[3], xy))
            host = merge_gap(out["exported"], s, line_u, ground, g[3], units, cell)
            if host is not None:
                log("Structure {} ({}): gap in connection '{}'".format(label, s.kind, host))
                continue
        if whynot:
            out["skipped"].append({"name": label, "kind": s.kind, "why": whynot,
                                   "line": [tuple(p) for p in line_u]})
            log("Structure {} ({}) not exported: {}".format(label, s.kind, whynot))
            continue
        c["name"] = hm.connection_name(label, used)
        out["exported"].append(c)
        done.add(i)
        log("Structure {} ({}): connection '{}' in the 2D area".format(label, s.kind, c["name"]))
    return out


def merge_gap(exported, s, line_u, ground, step, units, cell):
    """A bridge or notch as an open gap in the weir of the nearest connection already placed
    (its crest within a cell of that connection line): span x barrels wide, down to the
    structure's inverts (their mean), else the lowest ground near the ends of its line (the
    Runner's notch rule). Records it in the connection's 'gaps'. Returns the connection name,
    or None when no connection is that close."""
    cr = hm.structure_crest(line_u, ground, step, units.from_ft(0.1))
    if cr is None:
        return None
    C, k, along, zl, _, _ = cr
    best = None
    for c in exported:
        A, B = (np.asarray(p, float) for p in (c["line"][0], c["line"][-1]))
        L = float(np.hypot(*(B - A))) or 1.0
        t = (B - A) / L
        sc = float(np.dot(C - A, t))
        d = abs(float(np.cross(t, C - A)))
        if 0.0 <= sc <= L and d <= cell and (best is None or d < best[0]):
            best = (d, c, sc)
    if best is None:
        return None
    d, c, sc = best
    nb = max(1, int(s.barrels or 1))
    width = nb * units.length(s.width_m)
    total = float(along[-1])
    near = max(units.length(s.width_m), 2.0 * step)
    lo, hi = zl[along <= near], zl[along >= total - near]
    inv = [units.length(v) if v is not None else (float(np.nanmin(z)) if np.isfinite(z).any()
                                                   else float("nan"))
           for v, z in zip((s.invert_up_m, s.invert_dn_m), (lo, hi))]
    if not all(math.isfinite(v) for v in inv):
        return None
    floor = 0.5 * (inv[0] + inv[1])
    w = np.asarray(c["weir"], float)
    a, b = sc - 0.5 * width, sc + 0.5 * width
    za, zb = (float(np.interp(v, w[:, 0], w[:, 1])) for v in (a, b))
    pts = [tuple(p) for p in w if p[0] < a] + [(a, za), (a, min(za, floor))]
    pts += [(p[0], min(p[1], floor)) for p in w if a < p[0] < b]
    pts += [(b, min(zb, floor)), (b, zb)] + [tuple(p) for p in w if p[0] > b]
    outp = []
    for p in pts:
        p = (round(float(p[0]), 2), round(float(p[1]), 2))
        if not outp or p != outp[-1]:
            outp.append(p)
    c["weir"] = outp
    c.setdefault("gaps", []).append({
        "structure": s.name or s.kind, "kind": s.kind, "station": round(sc, 2),
        "width": round(width, 2), "floor": round(floor, 2), "off_line": round(d, 1),
        "inverts_from": " / ".join("run" if v is not None else "terrain"
                                   for v in (s.invert_up_m, s.invert_dn_m))})
    c["notes"].append("{} ({}) shares this crossing: an open gap {:.1f} {u} wide down to "
                      "{:.2f} {u} at station {:.0f} in this weir".format(
                          s.name or s.kind, s.kind, width, floor, sc, u=units.len_name))
    return c["name"]


# ---------------------------------------------------------------- mesh --------------------
def local_spacing(points, line, radius):
    """The median nearest-neighbour spacing of the points within `radius` of a line."""
    from scipy.spatial import cKDTree
    pts = np.asarray(points, float)
    tree = cKDTree(pts)
    hits = tree.query_ball_point(hb.resample(np.asarray(line, float), 0.25 * radius), radius)
    idx = np.unique(np.concatenate([np.asarray(h, int) for h in hits] + [np.zeros(0, int)]))
    if len(idx) < 2:
        return None
    return float(np.median(tree.query(pts[idx], k=2)[0][:, 1]))


def connection_breaklines(points, perimeter, conns, cell, units, dam_line=None, clip=None):
    """Breaklines on the connection lines built into the points. Spacing along each line: the
    local spacing of the points there (between a quarter cell and a cell). Only points within
    BREAKLINE_REACH_FT of a new line may move or go. A connection within DAM_CLEAR_CELLS cells
    of the dam line gets no breakline (the dam row and breach cells stay as checked).
    Returns (points, info)."""
    pts = np.asarray(points, float).reshape(-1, 2)
    reach = units.from_ft(BREAKLINE_REACH_FT)
    lines, skipped = [], []
    for c in conns:
        ln = np.asarray(c["line"], float)
        if dam_line is not None:
            d = hb.poly_dist(*hb.resample(ln, 0.1 * cell).T, np.asarray(dam_line, float)).min()
            if d < DAM_CLEAR_CELLS * cell:
                skipped.append({"name": c["name"], "why": "{:.0f} {} from the dam line".format(
                    d, units.len_name)})
                continue
        sp = local_spacing(pts, ln, 3.0 * cell) or cell
        s = float(min(max(sp, 0.25 * cell), cell))
        c["breakline_spacing"] = round(s, 2)
        lines.append({"kind": "structure", "name": c["name"], "line": ln, "near": s, "far": s})
    info = {"points_before": int(len(pts)), "lines": len(lines), "skipped": skipped}
    if not lines:
        info.update(points=int(len(pts)), points_removed=0, points_added=0)
        return pts, info
    new, st = hb.enforce(pts, perimeter, {"lines": lines})
    prot = st.pop("_protected")
    far = np.ones(len(new), bool)
    for ln in lines:
        far &= hb.poly_dist(new[:, 0], new[:, 1], ln["line"]) > reach
    ptol = (units.from_ft(hb.PERIM_SHORT_MIN), units.from_ft(hb.PERIM_VERTEX_MIN))
    new, _, vfix = hb.fix_faces(new, perimeter, prot | far, clip=clip, perimeter_tol=ptol)
    info.update(st, points=int(len(new)), check=vfix["check"],
                fix={k: v for k, v in vfix.items() if k != "check"},
                on_faces={ln["name"]: round(hb.share_on_faces(new, ln["line"]), 3)
                          for ln in lines})
    if vfix["left"] or vfix["check"]["facepoints_over"] or vfix["perimeter_left"]:
        raise hm.HecRasModelError(
            "the structure breaklines leave {} cell(s) with more than {} faces or {} perimeter "
            "problem(s); build with structure_breaklines=False".format(
                vfix["left"], hb.MAX_FACES, vfix["perimeter_left"]))
    return new, info


# ---------------------------------------------------------------- cells along a line -------
def face_segments(points, line, step):
    """Stretches of a straight connection line by the pair of cells either side: [{'s0', 's1',
    'left', 'right'}] (left looking from the line's first point to its last)."""
    from scipy.spatial import cKDTree
    ln = np.asarray(line, float).reshape(-1, 2)
    A, B = ln[0], ln[-1]
    L = float(np.hypot(*(B - A)))
    t = (B - A) / (L or 1.0)
    n = np.array([-t[1], t[0]])
    s = np.arange(0.0, L + 1e-9, step)
    xy = A + s[:, None] * t
    tree = cKDTree(np.asarray(points, float))
    eps = 1e-3 * step
    lc, rc = tree.query(xy + eps * n)[1], tree.query(xy - eps * n)[1]
    out, i0 = [], 0
    for k in range(1, len(s) + 1):
        if k == len(s) or lc[k] != lc[i0] or rc[k] != rc[i0]:
            a = s[i0] if i0 == 0 else 0.5 * (s[i0 - 1] + s[i0])
            b = s[-1] if k == len(s) else 0.5 * (s[k - 1] + s[k])
            out.append({"s0": float(a), "s1": float(b), "left": int(lc[i0]), "right": int(rc[i0])})
            i0 = k
    return out


def _weir_min(weir, a, b):
    w = np.asarray(weir, float)
    inner = w[(w[:, 0] > a) & (w[:, 0] < b), 1]
    ends = np.interp([a, b], w[:, 0], w[:, 1])
    return float(min(ends.min(), inner.min() if len(inner) else np.inf))


def cells_check(points, perimeter, conns, raster, step=None):
    """For each connection: the faces on its line, the lowest ground of the cells either side,
    the weir against them, and each culvert barrel's inverts against the cells its two ends
    (hm.barrel_ends) fall in. Returns {name: {...}}."""
    from scipy.spatial import cKDTree
    pts = np.asarray(points, float)
    tree = cKDTree(pts)
    step = step or 0.25 * float(raster[3])
    segs = {c["name"]: face_segments(pts, c["line"], step) for c in conns}
    cells = sorted({k for ss in segs.values() for sg in ss for k in (sg["left"], sg["right"])})
    cmin = hb.cell_min_elevations(pts, cells, raster, perimeter=perimeter) if cells else {}
    out = {}
    for c in conns:
        ss = segs[c["name"]]
        A = np.asarray(c["line"][0], float)
        B = np.asarray(c["line"][-1], float)
        t = (B - A) / (np.hypot(*(B - A)) or 1.0)
        nrm = np.array([-t[1], t[0]])
        rows, bad = [], 0
        for sg in ss:
            if sg["left"] == sg["right"]:
                continue
            wm = _weir_min(c["weir"], sg["s0"], sg["s1"])
            hi = max(cmin[sg["left"]], cmin[sg["right"]])
            r = dict(sg, weir_min=round(wm, 3), cell_min_left=round(cmin[sg["left"]], 3),
                     cell_min_right=round(cmin[sg["right"]], 3), excess=round(hi - wm, 3))
            bad += r["excess"] > 0
            rows.append(r)
        res = {"faces": len(rows), "off_face": sum(1 for sg in ss if sg["left"] == sg["right"]),
               "weir_below_cells": int(bad), "faces_rows": rows}
        cv = c.get("culvert")
        if cv:
            # 3.10.56: the cells the barrel's own ends (hm.barrel_ends, written as its GIS
            # centerline) fall in; the headwater end on the right of the line
            bars = []
            for sb, (pu, pd) in zip(cv["stations"], hm.barrel_ends(c)):
                sg = min(ss, key=lambda x: 0.0 if x["s0"] <= sb <= x["s1"] else
                         min(abs(sb - x["s0"]), abs(sb - x["s1"])))
                up, dn = (int(k) for k in tree.query([pu, pd])[1])
                for k in (up, dn):
                    if k not in cmin:
                        cmin.update(hb.cell_min_elevations(pts, [k], raster, perimeter=perimeter))
                side_up = float(np.dot(np.asarray(pu) - A, nrm))
                side_dn = float(np.dot(np.asarray(pd) - A, nrm))
                bars.append({"station": sb, "up_cell": up, "dn_cell": dn,
                             "up_xy": [round(v, 3) for v in pu], "dn_xy": [round(v, 3) for v in pd],
                             "up_on_headwater_side": side_up < 0 < side_dn,
                             "ends_inside": bool(cond.points_in_polygon(
                                 np.array([pu[0], pd[0]]), np.array([pu[1], pd[1]]),
                                 np.asarray(perimeter, float)).all()),
                             "cell_min_up": round(cmin[up], 3), "cell_min_dn": round(cmin[dn], 3),
                             "same_cell": up == dn,
                             "to_face_end": round(min(sb - sg["s0"], sg["s1"] - sb), 2),
                             "inv_up_ok": cv["inv_up"] >= cmin[up] - 1e-9,
                             "inv_dn_ok": cv["inv_dn"] >= cmin[dn] - 1e-9})
            res["barrels"] = bars
        out[c["name"]] = res
    return out


PROBLEMS_HEAD = ("HEC-RAS would refuse these culvert / bridge connections (\"has an error with "
                 "the connection\"), so no package was written: ")


def connection_problems(conns, check, perimeter, other_names=()):
    """3.10.56: what would make HEC-RAS refuse a connection ("has an error with the connection.
    Check that the From: / To: is set correctly", a weir or culvert below its cells), from the
    connections and their cells_check: [str] (empty = none). Names 1-16 characters and unique
    (with the 2D area, storage area and the other connections); both ends of the line inside the
    2D area and faces along it; each barrel's two ends inside the 2D area, in two different
    cells, the headwater end on the headwater side; inverts and weir not below the cells."""
    out = []
    seen = {str(n).strip().lower() for n in other_names}
    per = np.asarray(perimeter, float)
    for c in conns:
        nm = c["name"]
        if not str(nm).strip() or len(str(nm)) > 16 or any(ch in str(nm) for ch in ",=\r\n"):
            out.append("{!r}: the name must be 1 to 16 characters without , or =".format(nm))
        key = str(nm).strip().lower()
        if key in seen:
            out.append("{}: the name is used twice".format(nm))
        seen.add(key)
        ln = np.asarray(c["line"], float)
        if not cond.points_in_polygon(ln[:, 0], ln[:, 1], per).all():
            out.append("{}: its line leaves the 2D area".format(nm))
        ck = check.get(nm) or {}
        if not ck.get("faces"):
            out.append("{}: no cell faces along its line".format(nm))
        if ck.get("weir_below_cells"):
            out.append("{}: the weir is lower than the cells on {} face(s)".format(
                nm, ck["weir_below_cells"]))
        for b in ck.get("barrels", []):
            where = "{} barrel at station {:g}".format(nm, b["station"])
            if not b.get("ends_inside", True):
                out.append(where + ": an end is outside the 2D area")
            if b["same_cell"]:
                out.append(where + ": both ends are in cell {}".format(b["up_cell"]))
            if not b.get("up_on_headwater_side", True):
                out.append(where + ": its ends are not on opposite sides of the line, headwater "
                           "end first")
            if not (b["inv_up_ok"] and b["inv_dn_ok"]):
                out.append(where + ": an invert is lower than its cell")
    return out


def fit_to_cells(conns, check, units):
    """Raise each weir point on a face below (or less than FIT_MARGIN_FT above) its cells, and
    each culvert invert below (or less than FIT_MARGIN_FT above) its cell, to the cell's lowest
    ground + FIT_MARGIN_FT. Returns the list of changes."""
    m = units.from_ft(FIT_MARGIN_FT)
    changes = []
    for c in conns:
        ck = check.get(c["name"]) or {}
        w = [list(p) for p in c["weir"]]
        for r in ck.get("faces_rows", []):
            if r["excess"] <= -m + 1e-9:          # 3.10.56: within the margin counts too
                continue
            z = max(r["cell_min_left"], r["cell_min_right"]) + m
            a, b = r["s0"], r["s1"]
            # the weir between a and b no lower than z: points inside raised, a and b added
            za, zb = (float(np.interp(v, [p[0] for p in w], [p[1] for p in w])) for v in (a, b))
            w = [p for p in w if not (a <= p[0] <= b)] + [[a, max(za, z)], [b, max(zb, z)]]
            w.sort(key=lambda p: p[0])
            changes.append({"connection": c["name"], "what": "weir", "from_sta": round(a, 2),
                            "to_sta": round(b, 2), "was_min": r["weir_min"], "now": round(z, 2)})
        if len(w) != len(c["weir"]) or any(tuple(p) != tuple(q) for p, q in zip(w, c["weir"])):
            out = []
            for p in w:
                p = (round(float(p[0]), 2), round(float(p[1]), 2))
                if not out or abs(p[0] - out[-1][0]) > 1e-9 or p[1] != out[-1][1]:
                    out.append(p)
            if len(out) > hm.MAX_WEIR_POINTS:
                st, el = np.array(out).T
                out = hm._profile(st, el, units.from_ft(0.1), hm.MAX_WEIR_POINTS)
            c["weir"] = out
        cv = c.get("culvert")
        for b in ck.get("barrels", []):
            for end, key in (("up", "inv_up"), ("dn", "inv_dn")):
                cm = b["cell_min_" + end]
                if cv[key] < cm + m - 1e-9:
                    new = round(math.ceil((cm + m) * 100.0) / 100.0, 2)
                    changes.append({"connection": c["name"], "what": key, "was": cv[key],
                                    "now": new, "cell": b[end + "_cell"], "cell_min": cm})
                    cv[key] = new
    return changes


def read_raster(path):
    """A terrain GeoTIFF as (array with NaN, x0, ytop, pixel) for cells_check."""
    from osgeo import gdal
    ds = gdal.Open(path)
    gt = ds.GetGeoTransform()
    band = ds.GetRasterBand(1)
    a = band.ReadAsArray().astype(float)
    nd = band.GetNoDataValue()
    if nd is not None:
        a[a == nd] = np.nan
    ds = None
    return a, gt[0], gt[3], abs(gt[1])


def check_summary(check):
    """cells_check without the per-face rows: {name: {faces, off_face, weir_below_cells,
    barrels}} and the totals."""
    out = {n: {k: v for k, v in r.items() if k != "faces_rows"} for n, r in check.items()}
    out["_total"] = {
        "weir_below_cells": sum(r["weir_below_cells"] for r in check.values()),
        "inverts_below_cells": sum((not b["inv_up_ok"]) + (not b["inv_dn_ok"])
                                   for r in check.values() for b in r.get("barrels", [])),
        "barrels_in_one_cell": sum(b["same_cell"] for r in check.values()
                                   for b in r.get("barrels", []))}
    return out


def change_text(x, units):
    u = units.len_name
    if x["what"] == "weir":
        return "{} weir {:.0f}-{:.0f} {u} from {:.2f} to {:.2f} {u}".format(
            x["connection"], x["from_sta"], x["to_sta"], x["was_min"], x["now"], u=u)
    return "{} {} from {:.2f} to {:.2f} {u} (cell {} lowest ground {:.2f})".format(
        x["connection"], "upstream invert" if x["what"] == "inv_up" else "downstream invert",
        x["was"], x["now"], x["cell"], x["cell_min"], u=u)


# ---------------------------------------------------------------- an existing package -------
def _read_g01(path):
    with open(path, newline="") as f:
        L = f.read().split("\r\n")
    return L


def _xy_block(L, i, n):
    vals, j = [], i
    while len(vals) < 2 * n:
        s = L[j]
        vals += [float(s[k:k + 16]) for k in range(0, len(s.rstrip()), 16) if s[k:k + 16].strip()]
        j += 1
    return np.array(vals[:2 * n]).reshape(-1, 2), j


def parse_g01(path):
    """The parts of a Runner-written .g01 this module changes: the 2D area's perimeter and
    points (with their line range), the dam connection line, and where the BC line starts."""
    L = _read_g01(path)
    out = {"lines": L}
    area = False
    for i, s in enumerate(L):
        if s.startswith("Storage Area="):
            area = s.split("=", 1)[1].split(",")[0].strip() == hm.AREA_NAME
        elif area and s.startswith("Storage Area Surface Line=") and "perimeter" not in out:
            out["perimeter"], _ = _xy_block(L, i + 1, int(s.split("=")[1]))
        elif area and s.startswith("Storage Area 2D Points="):
            out["points"], j = _xy_block(L, i + 1, int(s.split("=")[1]))
            out["points_lines"] = (i, j)
        elif s.startswith("Connection="):
            name = s.split("=", 1)[1].split(",")[0].strip()
            out.setdefault("connections", []).append(name)
            if name == hm.CONN_NAME and L[i + 2].startswith("Connection Line="):
                out["dam_line"], _ = _xy_block(L, i + 3, int(L[i + 2].split("=")[1]))
        elif s.startswith("BC Line Name=") and "bc_line" not in out:
            out["bc_line"] = i
    for k in ("perimeter", "points", "bc_line"):
        if k not in out:
            raise hm.HecRasModelError("{} has no {} (not a Runner HEC-RAS geometry?)".format(
                path, k))
    return out


def write_g01(g, points, conns, path, now):
    """The .g01 lines with the new points and the structure connections before the BC line."""
    L = list(g["lines"])
    i, j = g["points_lines"]
    bc = g["bc_line"]
    blocks = []
    for c in conns:
        blocks += hm.structure_block(c, now)
    head = L[:i] + ["Storage Area 2D Points= {} ".format(len(points))] + hm.lines_xy(points, 2)
    mid = L[j:bc]
    out = head + mid + blocks + L[bc:]
    with open(path, "w", newline="") as f:
        f.write("\r\n".join(out))
    return path


def add_to_package(package, run, out, log=None, breaklines=True, fit=True, when=None):
    """Copy a built package (folder with one .g01) to `out` and add the culverts and bridges of
    `run` (a finished run's dam folder or report): connections, breaklines in the points, the
    cell check and fit. The terrain and everything else stay as they are. Returns info."""
    import datetime
    log = log or (lambda m: None)
    if os.path.exists(out):
        raise hm.HecRasModelError("{} exists; choose a new folder".format(out))
    shutil.copytree(package, out)
    g01 = [f for f in os.listdir(out) if f.lower().endswith(".g01")]
    if len(g01) != 1:
        raise hm.HecRasModelError("expected one .g01 in {}".format(package))
    gpath = os.path.join(out, g01[0])
    g = parse_g01(gpath)
    if len(g.get("connections", [])) > 1:
        raise hm.HecRasModelError("{} already has structure connections ({}); start from a "
                                  "package without them".format(g01[0], ", ".join(
                                      g["connections"][1:])))
    terrain = os.path.join(out, "Terrain")
    tif = [os.path.join(terrain, f) for f in os.listdir(terrain) if f.lower().endswith(".tif")]
    if len(tif) != 1:
        shutil.rmtree(out, ignore_errors=True)
        raise hm.HecRasModelError("expected one terrain GeoTIFF in {}".format(terrain))
    # v3.10.56: the copied terrain must carry the package's EPSG code and unit
    from . import hecras_terrain_crs as htc
    try:
        tcrs = htc.check_terrain(tif[0], os.path.join(out, "GIS", "Projection.prj"))
    except htc.TerrainCRSError:
        shutil.rmtree(out, ignore_errors=True)
        raise
    with open(os.path.join(out, "GIS", "Projection.prj")) as f:
        srs = hm.srs_from(f.read())
    units, _ = hm.crs_units(srs)
    structs, src, wtext, notes = load_run_structures(run)
    to_model = hm.transformer(hm.srs_from(wtext), srs) if wtext else None
    perim, pts = g["perimeter"], g["points"]
    cell = None
    man = os.path.join(out, "PACKAGE_MANIFEST.json")
    if os.path.exists(man):
        with open(man, encoding="utf-8") as f:
            cell = json.load(f).get("cell")
    cell = float(cell or np.median(hb_spacing(pts)))
    info = {"source": src, "notes": notes, "cell": round(cell, 2), "points_before": len(pts),
            "terrain_crs": tcrs}
    sinfo = connections(structs, to_model, perim, cell, tif[0], srs, units, log,
                        used=set(g.get("connections", [])) | {hm.AREA_NAME, hm.SA_NAME})
    conns = sinfo["exported"]
    bl = {"lines": 0}
    if conns and breaklines:
        pts, bl = connection_breaklines(pts, perim, conns, cell, units, g.get("dam_line"),
                                        clip=hm.perimeter_clip(perim))
    raster = read_raster(tif[0])
    check = cells_check(pts, perim, conns, raster) if conns else {}
    changes = fit_to_cells(conns, check, units) if (conns and fit) else []
    after = cells_check(pts, perim, conns, raster) if changes else check
    probs = connection_problems(conns, after, perim, other_names=g.get("connections", []) +
                                [hm.AREA_NAME, hm.SA_NAME])
    if probs:
        shutil.rmtree(out, ignore_errors=True)
        raise hm.HecRasModelError(PROBLEMS_HEAD + "; ".join(probs))
    now = when or datetime.datetime.now()
    write_g01(g, pts, conns, gpath, now)
    sinfo.update(source=src, breaklines=bl, fit=changes)
    readme = os.path.join(out, "README_HECRAS.md")
    if os.path.exists(readme):
        m = hm.Model() if hasattr(hm, "Model") else type("M", (), {})()
        m.units = units
        with open(readme, "a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(hm.structures_readme(m, sinfo)) + "\n")
    lay = os.path.join(out, "GIS", "HECRAS Layout.gpkg")
    if os.path.exists(lay):
        add_layout_layers(lay, srs, conns, sinfo["skipped"], units)
    htc.check_terrain(tif[0], os.path.join(out, "GIS", "Projection.prj"))   # still fine
    info.update(exported=conns, skipped=sinfo["skipped"], breaklines=bl, check_before=check_summary(check),
                check_after=check_summary(after), check_rows=after, fit=changes, points_after=len(pts), g01=gpath)
    return info


def hb_spacing(pts):
    from scipy.spatial import cKDTree
    return cKDTree(pts).query(pts, k=2)[0][:, 1]


def add_layout_layers(path, srs, conns, skipped, units):
    """'Structure connections' and 'Structures from the run' (as write_layout names them) added
    to an existing layout GeoPackage (replaced if there)."""
    from osgeo import ogr
    ds = ogr.Open(path, 1)
    for name in ("Structure connections", "Structures from the run"):
        for i in range(ds.GetLayerCount()):
            if ds.GetLayerByIndex(i).GetName() == name:
                ds.DeleteLayer(i)
                break

    def line(pts):
        g = ogr.Geometry(ogr.wkbLineString)
        for x, y in pts:
            g.AddPoint_2D(float(x), float(y))
        return g

    def layer(name, rows):
        lyr = ds.CreateLayer(name, srs, ogr.wkbLineString)
        keys = []
        for _, props in rows:
            for k, v in props.items():
                if k not in keys:
                    keys.append(k)
                    lyr.CreateField(ogr.FieldDefn(k, ogr.OFTReal if isinstance(v, float)
                                                  else ogr.OFTString))
        for geom, props in rows:
            f = ogr.Feature(lyr.GetLayerDefn())
            for k, v in props.items():
                f.SetField(k, v)
            f.SetGeometry(geom)
            lyr.CreateFeature(f)
    if conns:
        layer("Structure connections", [(line(c["line"]), {
            "name": c["name"], "kind": c["kind"], "structure": c["source_name"],
            "hecras": "culvert group" if c["culvert"] else "weir with an open gap",
            "units": units.len_name}) for c in conns])
    rows = [(line(c["structure_line"]), {"name": c["source_name"], "kind": c["kind"],
                                         "exported": "yes", "connection": c["name"]})
            for c in conns]
    rows += [(line(k["line"]), {"name": k["name"], "kind": k["kind"], "exported": "no",
                                "connection": k["why"]}) for k in skipped if len(k["line"]) >= 2]
    if rows:
        layer("Structures from the run", rows)
    ds = None
