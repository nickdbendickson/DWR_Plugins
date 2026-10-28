"""3.12.0: NGVD29 -> NAVD88 for a breach run's dam elevations (damsuite.vdatum).

The DEM is NAVD88. Every dam elevation a run takes from dam.json, the inventory or a typed
parameter - top of dam, PMF maximum pool, breach bottom, normal pool, the pool-elevation
failure trigger, the spillway rating's elevations and the outlet inverts - is shifted from
NGVD29 to NAVD88 with VERTCON at the dam point unless its datum is stated as NAVD88 (or
"DEM": read from the DEM). The source datum comes from the run's setting ELEV_DATUM
("auto" | "NAVD88" | "NGVD29"); in "auto" it is dam.json's top_of_dam_datum (or a batch
layer's datum field), and NGVD29 with a warning when nothing states it.

Not shifted: a top of dam read from the DEM (the run's own estimate), the tailwater stage
(TW_STAGE_FT, DEM datum by definition), culvert inverts (checked against the LiDAR bed by
structures.reconcile_inverts) and anything in metres from the DEM.

Local datums (3.12.1, damsuite 1.18.1): apply_local() runs after the run reads the DEM crest (see there).

apply() runs once per run: st.vertical_datum holds the record afterwards and a second call
returns it unchanged (no double conversion)."""
from ..damsuite import vdatum as VD

FT = VD.FT
# the run's elevation fields, for the report
DAM_FIELDS = (("top_of_dam_ft", "Top of dam"), ("pmf_max_wse_ft", "PMF maximum pool"),
              ("breach_bottom_ft", "Breach bottom"))


def stated_from_record(rec):
    """(datum text, note) of dam.json's elevations. A top of dam this plugin wrote before
    3.12.0 without a datum came from the TOP_FT parameter, whose datum was the DEM's (its
    label said so): it is read as "DEM" (NAVD88), with a note."""
    if rec is None:
        return "", ""
    d = str(rec.get("top_of_dam_datum") or "").strip()
    if d:
        return d, ""
    src = (getattr(rec, "sources", None) or {}).get("top_of_dam_ft") or {}
    if rec.get("top_of_dam_ft") is not None and str(src.get("tool") or "") == "ndbreach":
        return "DEM", ("dam.json's top of dam has no datum and was written by an earlier "
                       "Runner run from the TOP_FT parameter (DEM datum): taken as NAVD88.")
    return "", ""


def _conv(v, rec, key):
    """v (ft) on NAVD88 under rec. A key converted already (another copy of the same dam or
    flood inputs) is recognised by its value: the NAVD88 value stays, the source value gets
    the recorded result; any other value is shifted without changing the record."""
    f = (rec.get("fields") or {}).get(key)
    if f is None:
        return VD.convert_ft(v, rec, key)
    if abs(float(v) - f["_navd88"]) < 1e-6:
        return float(v)
    if abs(float(v) - f["source"]) < 5e-4:
        return f["_navd88"]
    return float(v) + (float(rec.get("_shift_m", 0.0)) / FT if rec.get("applied") else 0.0)


def apply(dam, st, lon, lat, stated=None, override=None, top_is_dem=False, log=None):
    """Shift the run's dam elevations to NAVD88 in place; returns the record (vdatum.plan +
    "fields"), kept in st.vertical_datum. stated / override default to st.elev_datum_stated /
    st.elev_datum. Never raises, never converts twice: the dam and the flood inputs are marked
    once shifted (copies of the settings share the record and the flood inputs), and a value
    already listed in the record is recognised (see _conv)."""
    log = log or (lambda m: None)
    rec = getattr(st, "vertical_datum", None)
    fresh = rec is None
    if fresh:
        if stated is None:          # a dam's own stated datum (the Cascade's datum field) first
            stated = getattr(dam, "elev_datum_stated", None) or \
                getattr(st, "elev_datum_stated", "") or ""
        if override is None:
            override = getattr(st, "elev_datum", VD.AUTO) or VD.AUTO
        try:
            rec = VD.plan(lon, lat, stated, override)
        except Exception as e:                          # noqa - never stop a run for this
            rec = {"target": VD.TARGET, "source_datum": "", "how": "", "stated": stated,
                   "override": VD.mode(override), "lon": lon, "lat": lat, "shift_m": 0.0,
                   "shift_ft": 0.0, "method": "none (error: {})".format(e), "applied": False,
                   "fields": {}, "warnings": ["The datum check failed ({}): the elevations "
                                              "are used as given.".format(e)]}
        rec["dam_id"] = getattr(dam, "dam_id", None)
    own = getattr(dam, "dam_id", None) == rec.get("dam_id")
    if not getattr(dam, "_vdatum_done", False):
        for name, _label in DAM_FIELDS:
            if name == "top_of_dam_ft" and top_is_dem:
                continue
            v = getattr(dam, name, None)
            if v is not None:
                key = name if own else "{}:{}".format(dam.dam_id, name)
                setattr(dam, name, _conv(v, rec, key))
        try:
            dam._vdatum_done = True
        except Exception:                               # noqa
            pass
    fl = getattr(st, "flood", None)
    if fl is not None and not getattr(fl, "_vdatum_done", False):
        if fl.normal_pool_ft is not None:
            fl.normal_pool_ft = _conv(fl.normal_pool_ft, rec, "normal_pool_ft")
        if fl.trigger == "wse" and fl.trigger_value is not None:
            fl.trigger_value = _conv(fl.trigger_value, rec, "trigger_wse_ft")
        sp = getattr(fl, "spillway", None)
        if sp is not None and len(sp.elev_m):
            _conv(float(sp.elev_m[0]) / FT, rec, "spillway_rating_first_row_ft")
            if rec.get("applied"):
                sp.elev_m = sp.elev_m + float(rec["_shift_m"])
        for c in getattr(fl, "outlets", None) or []:
            c.invert_m = _conv(c.invert_m / FT, rec,
                               "outlet_{}_invert_ft".format(c.name)) * FT
        fl._vdatum_done = True
    if top_is_dem and own:
        rec["top_of_dam_from_dem"] = True
    if fresh:
        if not rec.get("fields"):
            # no dam elevation in this run (top of dam from the DEM, no flood elevations):
            # nothing to shift, so no "NGVD29 assumed" warning
            rec["warnings"] = [w for w in rec.get("warnings") or []
                               if "assumed" not in w and "could not be converted" not in w]
            rec["applied"] = False
            rec["method"] = "none (no dam elevation given: all from the NAVD88 DEM)"
        st.vertical_datum = rec
        for line in VD.summary_lines(rec):
            log("Elevation datum: " + line)
    return rec


def dem_crest_on_line(read, line_xy, cell_m=1.0, half_width_m=3.0):
    """(crest ft, how) from the DEM along the dam line: the median of the DEM cells within
    half_width_m of the line (the embankment crest is most of the line; the median ignores the
    higher abutment ends and a low spillway / notch). (None, why) without a line or data."""
    import numpy as np
    pts = [(float(x), float(y)) for x, y in (line_xy or [])]
    if len(pts) < 2:
        return None, "no dam line"
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    pad = half_width_m + 2.0 * cell_m
    g = read((min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad), cell_m)
    z = g.masked()
    X, Y = np.meshgrid(g.x_centers(), g.y_centers())
    dist = np.full(z.shape, np.inf)
    for (x0, y0), (x1, y1) in zip(pts[:-1], pts[1:]):
        dx, dy = x1 - x0, y1 - y0
        L2 = dx * dx + dy * dy
        t = np.clip(((X - x0) * dx + (Y - y0) * dy) / L2, 0.0, 1.0) if L2 > 0 else 0.0
        dist = np.minimum(dist, np.hypot(X - (x0 + t * dx), Y - (y0 + t * dy)))
    zz = np.asarray(z, dtype=float)
    sel = (dist <= half_width_m) & np.isfinite(zz)
    if hasattr(z, "mask"):
        sel &= ~np.ma.getmaskarray(z)
    if not sel.any():
        return None, "no DEM data along the dam line"
    return float(np.median(zz[sel])) / FT, "DEM along the dam line (median within {:.0f} m)"\
        .format(half_width_m)


def _dem_units_suspect(dem_crest_ft, fields):
    """Why the DEM crest cannot stand in for a local top (None when it can): outside ND's
    ground (damsuite.vdatum ND_MIN_FT - ND_MAX_FT), or the DEM / top ratio is the
    feet-metres factor (3.2808 within 3 %) - a DEM in the wrong units, which the run's
    unit check reports instead of the DEM being taken as the top of dam."""
    d = float(dem_crest_ft)
    if not (VD.ND_MIN_FT <= d <= VD.ND_MAX_FT):
        return ("the DEM crest {:.2f} ft is outside North Dakota's {:.0f} - {:.0f} ft (DEM units or "
                "datum?)".format(d, VD.ND_MIN_FT, VD.ND_MAX_FT))
    top = (fields.get("top_of_dam_ft") or {}).get("source")
    if top:
        for r in (d / float(top), float(top) / d):
            if abs(r - 3.28084) / 3.28084 < 0.03:
                return ("the DEM crest {:.2f} ft is {:.3f} x the top of dam {:.2f} ft - the feet / "
                        "metres factor (DEM units?)".format(d, d / float(top), float(top)))
    return None


def apply_local(dam, st, dem_crest_ft, crest_from="DEM near the dam point", log=None):
    """3.12.1 (damsuite 1.18.1) local datums (damsuite.vdatum.local_check): after the run has read the DEM crest,
    a dam whose elevations are local (stated / chosen "local", or detected in auto) has its
    top of dam replaced by the DEM crest and every other dam elevation moved by the same
    amount (DEM top - (local top - value)), instead of the NGVD29 shift. Without a local top
    the other elevations cannot be placed: the pools / breach bottom are dropped (the run
    takes them from the DEM / the spillway rating) and a rating or outlets are left as given,
    each with a warning. Returns (record, changed). Once per run."""
    log = log or (lambda m: None)
    rec = getattr(st, "vertical_datum", None)
    if rec is None or rec.get("_local_done") or dem_crest_ft is None:
        return rec, False
    if rec.get("dam_id") is not None and getattr(dam, "dam_id", None) != rec.get("dam_id"):
        return rec, False
    f = rec.setdefault("fields", {})
    if not f:                       # no dam elevation given: all from the DEM already
        return rec, False
    forced = bool(rec.get("local"))
    why = ""
    if not forced:
        if rec.get("how") == "override":                # NAVD88 / NGVD29 chosen: no detection
            return rec, False
        sh = float(rec.get("shift_ft") or 0.0) if rec.get("applied") else 0.0
        if "top_of_dam_ft" in f:
            hit, why = VD.local_check(f["top_of_dam_ft"]["source"], dem_crest_ft, sh)
        else:                       # no top given (DEM top): a pool far above the DEM crest
            hit = False
            for k, label in (("normal_pool_ft", "normal pool"),
                             ("pmf_max_wse_ft", "PMF maximum pool"),
                             ("trigger_wse_ft", "failure-trigger pool")):
                if k in f:
                    hit, why = VD.local_check(f[k]["source"], None, 0.0, label)
                    if not hit and f[k]["source"] + sh - dem_crest_ft > VD.DEM_TOL_FT:
                        hit, why = True, ("the {} {:.2f} ft is more than {:.0f} ft above the "
                                          "DEM crest near the dam ({:.2f} ft)".format(
                                              label, f[k]["source"], VD.DEM_TOL_FT,
                                              dem_crest_ft))
                    if hit:
                        break
        if not hit:
            return rec, False
    bad = _dem_units_suspect(dem_crest_ft, f)
    if bad:                         # a DEM in the wrong units / datum: the run's unit checks say so
        log("Local datum not applied: " + bad)
        return rec, False
    rec["_local_done"] = True
    top = f.get("top_of_dam_ft")
    offset = float(dem_crest_ft) - top["source"] if top else None
    old_m = float(rec.get("_shift_m", 0.0)) if rec.get("applied") else 0.0
    dropped, kept = [], []

    def place(key):
        e = f.get(key)
        if e is None:
            return None
        if offset is None:
            e.update(navd88=None, _navd88=None, note="no local top of dam to place it")
            dropped.append(key)
            return None
        new = e["source"] + offset
        e.update(navd88=round(new, 3), _navd88=new)
        return new

    if top:
        dam.top_of_dam_ft = float(dem_crest_ft)
        top.update(navd88=round(float(dem_crest_ft), 3), _navd88=float(dem_crest_ft))
    for name in ("pmf_max_wse_ft", "breach_bottom_ft"):
        if name in f:
            setattr(dam, name, place(name))
    fl = getattr(st, "flood", None)
    if fl is not None:
        if "normal_pool_ft" in f:
            fl.normal_pool_ft = place("normal_pool_ft")
        if "trigger_wse_ft" in f and fl.trigger == "wse":
            v = place("trigger_wse_ft")
            if v is None:
                kept.append("trigger_wse_ft")
                f["trigger_wse_ft"]["note"] = "used as given: no local top of dam to place it"
            else:
                fl.trigger_value = v
        rel = [k for k in f if k == "spillway_rating_first_row_ft" or
               (k.startswith("outlet_") and k.endswith("_invert_ft"))]
        if offset is not None:
            dm = offset * FT - old_m
            sp = getattr(fl, "spillway", None)
            if sp is not None and len(sp.elev_m):
                sp.elev_m = sp.elev_m + dm
            for c in getattr(fl, "outlets", None) or []:
                c.invert_m = c.invert_m + dm
            for k in rel:
                place(k)
        else:
            for k in rel:
                kept.append(k)
                f[k].update(navd88=None, _navd88=None,
                            note="left as given: no local top of dam to place it")
    rec.update(source_datum=VD.LOCAL if forced else VD.LOCAL_ASSUMED,
               how=rec.get("how") if forced else "detected", method=VD.METHOD_DEM,
               applied=False, shift_m=0.0, shift_ft=0.0, local=True, local_reason=why,
               dem_crest_ft=round(float(dem_crest_ft), 3), dem_crest_from=crest_from,
               local_offset_ft=None if offset is None else round(offset, 3))
    rec.pop("_shift_m", None)
    rec["warnings"] = [w for w in rec.get("warnings") or []
                       if "NGVD29 was assumed" not in w and "could not be converted" not in w]
    msg = ("The dam elevations are on a local datum ({}): they were not datum-converted. The "
           "top of dam is the {} ({:.2f} ft NAVD88){}.".format(
               why or ("stated" if rec.get("how") == "stated" else "set by the engineer"),
               crest_from, float(dem_crest_ft),
               "; the other elevations keep their height below the local top ({:+.2f} ft)"
               .format(offset) if offset is not None else
               " (no local top of dam was given)"))
    if dropped:
        msg += " Not placed (no local top): {} - taken from the DEM / the spillway rating."\
            .format(", ".join(dropped))
    if kept:
        msg += " Left as given (no local top): {}.".format(", ".join(kept))
    msg += " Check against the as-built survey; set the datum to NAVD88 / NGVD29 if wrong."
    rec["warnings"].append(msg)
    for line in VD.summary_lines(rec):
        log("Elevation datum: " + line)
    return rec, True


def lonlat_of(st, dam_xy=None, work_srs=None):
    """(lon, lat) of the dam: st.dam_record's point, else dam_xy in `work_srs` through GDAL's
    osr (None when neither is there)."""
    pt = (getattr(st, "dam_record", None) or {}).get("point") or {}
    lon, lat = pt.get("lon"), pt.get("lat")
    if isinstance(lon, (int, float)) and isinstance(lat, (int, float)):
        return float(lon), float(lat)
    if dam_xy is None or not work_srs:
        return None
    try:
        from osgeo import osr
        src = osr.SpatialReference()
        if str(work_srs).upper().startswith("EPSG:"):
            src.ImportFromEPSG(int(str(work_srs).split(":")[1]))
        else:
            src.SetFromUserInput(str(work_srs))
        dst = osr.SpatialReference()
        dst.ImportFromEPSG(4326)
        for s in (src, dst):
            if hasattr(s, "SetAxisMappingStrategy"):
                s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        x, y, _z = osr.CoordinateTransformation(src, dst).TransformPoint(
            float(dam_xy[0]), float(dam_xy[1]))
        return float(x), float(y)
    except Exception:                                   # noqa
        return None


def report_lines(rec):
    """Markdown lines for run_report.md."""
    if not rec:
        return []
    return ["## Elevation datum (NAVD88)", ""] + ["- " + x for x in VD.summary_lines(rec)] + [""]


def damjson_values(rec, values, record=None):
    """The dam.json values of a run after a datum shift: a converted top of dam / normal pool
    is written with top_of_dam_datum "NAVD88 ft" (so no later run shifts it again) only when
    the record has no top of dam, normal pool or datum yet; otherwise they are left out (the
    record keeps its own values in their datum). Returns (values, note)."""
    if not rec or not (rec.get("applied") or rec.get("source_datum") == VD.NAVD88):
        return values, ""
    keys = ("top_of_dam_ft", "normal_pool_ft")
    mine = [k for k in keys if k in values and k in (rec.get("fields") or {})]
    if not mine:
        return values, ""
    has = record is not None and any(record.get(k) not in (None, "")
                                     for k in keys + ("top_of_dam_datum",))
    out = dict(values)
    if has:
        for k in mine:
            out.pop(k, None)
        return out, ("dam.json keeps its own {} (datum {}); this run's NAVD88 values are in "
                     "vertical_datum.".format(" and ".join(mine),
                                              record.get("top_of_dam_datum") or "not stated"))
    if not out.get("top_of_dam_datum"):
        out["top_of_dam_datum"] = VD.NAVD88_TAG
    return out, ""
