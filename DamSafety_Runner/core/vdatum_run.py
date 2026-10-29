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

3.12.5 (damsuite 1.18.2, Nick 2026-10-28; revised 2026-10-29: "Forget overrid datum. Convert
local to navd 88."): any elevation between 0 and 300 ft is local, is never VERTCON-shifted and is
converted to NAVD88 by the DEM tie - whatever datum is stated or chosen (no override wording).
apply() hands the run's values to vdatum.plan, which makes the record local when the top of dam
(or, with no top, a pool) is in that band; apply_local() then ties it to the DEM at mid-crest
(workflow.run_dam): top = DEM crest at mid-crest, offset = DEM crest - local crest, every other
local value + offset ("DEM-derived"). No split (Nick 2026-10-29): every elevation of such a
local record, inside ND's range or not, is tied by the same offset; the datum choice is not
applied to any of them.
A 0 - 300 ft value of a dam whose top is not in the band (mixed) is held at apply()
(local_band_leftovers: pools / breach bottom out of the model, the rest as given) and tied by
the same offset when apply_local finds the top local (by any local_check rule, e.g. 350 ft).
With no local crest to tie by - the top of dam is real (NAVD88 / NGVD29) - such values are not
used as given (Nick 2026-10-29, "in that case, use dem elevations"): the streambed / breach
bottom and the outlet inverts become the DEM low point (thalweg) at the downstream toe
(dem_toe_low; "DEM-derived", with a warning naming the value given), the PMF / trigger pool come
from the routing and the normal pool from the spillway rating; what has no DEM / routing source
(a band spillway rating) or fails the 100 ft guard is flagged for review and not used. Without a
DEM crest / low point (or no top of dam) they are flagged for review.
No tie - no DEM crest, a DEM crest outside 500 - 3,600 ft or x3.28 the top (units), no
mid-crest (no dam line / no DEM across it), no local crest - is never guessed: the record gets
review=True / review_reason, a "REVIEW:" warning, and the local values stay UNCONVERTED in
fields (marked "unconverted"); the model takes the top / pools / breach bottom from the DEM /
the spillway rating and uses a trigger pool, rating or outlet as given (all listed).

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
    if VD.is_local_elev(v):                 # 3.12.5: 0 - 300 ft is local - never shifted
        return float(v)
    return float(v) + (float(rec.get("_shift_m", 0.0)) / FT if rec.get("applied") else 0.0)


def run_values(dam, st, top_is_dem=False):
    """{key: ft} of the run's dam elevations, as given (for vdatum.plan's 0 - 300 ft rule)."""
    out = {}
    for name, _label in DAM_FIELDS:
        if name == "top_of_dam_ft" and top_is_dem:
            continue
        v = getattr(dam, name, None)
        if v is not None:
            out[name] = float(v)
    fl = getattr(st, "flood", None)
    if fl is not None:
        if fl.normal_pool_ft is not None:
            out["normal_pool_ft"] = float(fl.normal_pool_ft)
        if fl.trigger == "wse" and fl.trigger_value is not None:
            out["trigger_wse_ft"] = float(fl.trigger_value)
    return out


PENDING_NOTE = ("0-300 ft local value: tied to the DEM crest at mid-crest if the top of dam is "
                "local, else flagged for review")


def local_band_leftovers(dam, st, rec, log=None):
    """3.12.5: values between 0 and 300 ft in a record that is NOT local yet (the top of dam is
    not in the band): never shifted (convert_ft) and held until apply_local decides - the
    pools / breach bottom are taken out of the model (None: the DEM / the spillway rating
    until tied), a trigger pool, rating or outlet stays as given. Marked "pending_tie".
    Returns the keys held."""
    if rec is None or rec.get("local") or rec.get("_band_done"):
        return []
    f = rec.get("fields") or {}
    keys = [k for k, e in f.items() if e.get("local_elev") and not e.get("note")]
    if not keys:
        return []
    rec["_band_done"] = True
    fl = getattr(st, "flood", None)
    for k in keys:
        base = k.split(":")[-1]
        if base in ("pmf_max_wse_ft", "breach_bottom_ft") and hasattr(dam, base):
            setattr(dam, base, None)
        elif base == "normal_pool_ft" and fl is not None:
            fl.normal_pool_ft = None
        f[k].update(navd88=None, _navd88=None, pending_tie=True, note=PENDING_NOTE)
    (log or (lambda m: None))("Elevation datum: 0-300 ft local value(s) held for the DEM tie: "
                              + ", ".join(keys))
    return keys


_OUT_OF_MODEL = ("pmf_max_wse_ft", "breach_bottom_ft", "normal_pool_ft", "top_of_dam_ft")


# 3.12.5 plausibility guard: what the run does without a value tied too far from the crest
_GUARD_USE = {"pmf_max_wse_ft": "the run takes the PMF pool from the DEM / the routing",
              "breach_bottom_ft": "the run takes the breach bottom from the DEM",
              "normal_pool_ft": "the run takes the normal pool from the spillway rating",
              "top_of_dam_ft": "the run takes the top of dam from the DEM",
              "trigger_wse_ft": "the breach starts at the peak pool instead",
              "spillway_rating_first_row_ft": "the spillway rating is left out of the run"}
_GUARD_OUTLET = "the outlet is left out of the run"


def _unconverted_note(key, reason):
    base = key.split(":")[-1]
    use = ("not used: the run takes it from the DEM / the spillway rating"
           if base in _OUT_OF_MODEL else "used as given by the model")
    return "UNCONVERTED - review ({}): {}".format(use, reason)


def _review_leftovers(rec, keys, reason, log):
    """Flag a record whose 0 - 300 ft values cannot be tied (no local crest) for review."""
    f = rec.get("fields") or {}
    for k in keys:
        f[k].pop("pending_tie", None)
        f[k].update(navd88=None, _navd88=None, unconverted=True,
                    note=_unconverted_note(k, reason))
    rec.update(review=True, review_reason=reason, _local_done=True, tie="review")
    msg = ("REVIEW: elevation(s) between 0 and 300 ft are on a local datum but cannot be tied "
           "to the DEM ({}). They were NOT converted: {}. Check the datum before using this "
           "run.".format(reason, ", ".join(keys)))
    rec.setdefault("warnings", []).append(msg)
    log("Elevation datum: " + msg)
    return rec


def finish_band(dam, st, reason="the local-datum tie did not run", log=None):
    """After run_dam's local-datum step: a 0 - 300 ft value still waiting for the tie (or a
    band record never tied) is flagged for review. Returns True when it flagged something."""
    log = log or (lambda m: None)
    rec = getattr(st, "vertical_datum", None)
    if rec is None or rec.get("review"):
        return False
    f = rec.get("fields") or {}
    keys = [k for k, e in f.items() if e.get("pending_tie")]
    if rec.get("local_rule") and not rec.get("_local_done"):
        keys += [k for k, e in f.items() if k not in keys and VD.is_local_elev(e.get("source"))]
    if not keys:
        return False
    _review_leftovers(rec, keys, reason, log)
    return True


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
            rec = VD.plan(lon, lat, stated, override,
                          values=run_values(dam, st, top_is_dem))     # 3.12.5: 0 - 300 ft
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
            if rec.get("applied") and not VD.is_local_elev(float(sp.elev_m[0]) / FT):
                sp.elev_m = sp.elev_m + float(rec["_shift_m"])
        for c in getattr(fl, "outlets", None) or []:
            c.invert_m = _conv(c.invert_m / FT, rec,
                               "outlet_{}_invert_ft".format(c.name)) * FT
        fl._vdatum_done = True
    if top_is_dem and own:
        rec["top_of_dam_from_dem"] = True
    local_band_leftovers(dam, st, rec, log)
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


# 3.12.5 (Nick 2026-10-29, "in that case, use dem elevations"): a 0 - 300 ft value of a dam whose
# top of dam is real (no local crest to tie it by) is replaced by a DEM elevation
TOE_REACH_M = 30.0          # the band past the downstream toe searched for the low point
TOE_SLOPE_H = 3.0           # downstream slope (H:1V) when the dam has none (hecras_model's)
TOE_CREST_WIDTH_FT = 20.0   # crest width when the dam has none (hecras_model's)


def toe_offset_m(dam):
    """Distance (m) from the dam line to the downstream toe, as hecras_model's breach toe:
    ds_slope_h x dam height + half the crest width (3H:1V and 20 ft when not given)."""
    h = float(getattr(dam, "dam_height_ft", None) or 0.0)
    s = float(getattr(dam, "ds_slope_h", None) or TOE_SLOPE_H)
    w = float(getattr(dam, "crest_width_ft", None) or TOE_CREST_WIDTH_FT)
    return (s * max(h, 0.0) + 0.5 * w) * FT


def dem_toe_low(read, dam_xy, line_xy, dam, cell_m=1.0, reach_m=TOE_REACH_M):
    """(ft, how) of the DEM low point (thalweg) at the dam's downstream toe: the lowest DEM cell
    between the toe (toe_offset_m) and reach_m past it - on each side of the dam line (beside
    it, not past its ends), the lower side taken as downstream (the pool side holds water at
    the normal pool); without a line, in a ring around the dam point. (None, why) without
    data. Like the Runner's breach toe (hecras_model: the DEM minimum past the toe)."""
    import numpy as np
    toe = toe_offset_m(dam)
    far = toe + float(reach_m)
    pts = [(float(x), float(y)) for x, y in (line_xy or [])]
    pts = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
    if len(pts) < 2:
        if dam_xy is None:
            return None, "no dam line or dam point"
        pts = [(float(dam_xy[0]), float(dam_xy[1]))]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    pad = far + 2.0 * cell_m
    try:
        g = read((min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad), cell_m)
    except Exception as e:                              # noqa - a DEM read error: no value
        return None, "the DEM could not be read at the toe ({})".format(e)
    z = g.masked()
    zz = np.asarray(np.ma.filled(z, np.nan) if hasattr(z, "mask") else z, dtype=float)
    X, Y = np.meshgrid(g.x_centers(), g.y_centers())
    if len(pts) == 1:
        d = np.hypot(X - pts[0][0], Y - pts[0][1])
        sel = (d >= toe) & (d <= far) & np.isfinite(zz)
        if not sel.any():
            return None, "no DEM data around the dam point at the toe"
        return float(np.min(zz[sel])) / FT, (
            "lowest DEM ground {:.0f} - {:.0f} m from the dam point (no dam line)".format(toe, far))
    dist = np.full(zz.shape, np.inf)
    side = np.zeros(zz.shape)
    inside = np.zeros(zz.shape, dtype=bool)
    for (x0, y0), (x1, y1) in zip(pts[:-1], pts[1:]):
        dx, dy = x1 - x0, y1 - y0
        L2 = dx * dx + dy * dy
        tr = ((X - x0) * dx + (Y - y0) * dy) / L2
        t = np.clip(tr, 0.0, 1.0)
        dd = np.hypot(X - (x0 + t * dx), Y - (y0 + t * dy))
        better = dd < dist
        dist = np.where(better, dd, dist)
        side = np.where(better, np.sign(dx * (Y - y0) - dy * (X - x0)), side)
        inside = np.where(better, (tr >= 0.0) & (tr <= 1.0), inside)
    band = (dist >= toe) & (dist <= far) & inside & np.isfinite(zz)
    lows = []
    for s in (1.0, -1.0):
        sel = band & (side == s)
        if sel.any():
            lows.append(float(np.min(zz[sel])))
    if not lows:
        return None, "no DEM data at the toe of the dam line"
    return min(lows) / FT, ("DEM low point (thalweg) at the downstream toe: lowest ground "
                            "{:.0f} - {:.0f} m from the dam line on its lower side".format(toe, far))


# what replaces a 0 - 300 ft value that has no DEM elevation of its own
_DEM_ROUTING = {"pmf_max_wse_ft": "the PMF pool is taken from the DEM storage / the routing",
                "trigger_wse_ft": "the breach starts at the peak pool of the routing"}


def _dem_leftovers(dam, st, rec, keys, dem_crest_ft, dem_low, log):
    """3.12.5 (Nick 2026-10-29, "in that case, use dem elevations"): the record's top of dam is
    real (NAVD88 / NGVD29), so its 0 - 300 ft values have no local crest to tie them by. They
    are not used as given: the streambed / breach bottom and the outlet inverts become the DEM
    low point at the downstream toe (dem_low), labelled "DEM-derived", with a warning naming
    the value given; the PMF pool and a trigger pool come from the routing, the normal pool
    from the spillway rating. What has no DEM / routing source (the spillway rating, a normal
    pool without a usable rating) or a DEM value failing the 100 ft guard / below-the-top check
    is flagged for review and not used."""
    f = rec.get("fields") or {}
    lim = VD.TIE_MAX_FROM_CREST_FT
    crest = float(dem_crest_ft)
    low = dem_low() if callable(dem_low) else dem_low
    low_ft, low_how = (low if isinstance(low, (tuple, list)) else (low, "DEM low point"))
    low_bad = None
    if low_ft is None:
        low_bad = "no DEM low point at the toe ({})".format(low_how or "no DEM")
    else:
        low_ft = float(low_ft)
        top = getattr(dam, "top_of_dam_ft", None)
        if top is not None and low_ft >= float(top):
            low_bad = ("the DEM low point at the toe {:.2f} ft is not below the top of dam "
                       "{:.2f} ft".format(low_ft, float(top)))
        elif abs(low_ft - crest) > lim:     # 3.12.5 plausibility guard, DEM values too
            low_bad = ("the DEM low point at the toe {:.2f} ft is {:.2f} ft below the DEM crest "
                       "{:.2f} ft (more than {:.0f} ft)".format(low_ft, crest - low_ft, crest, lim))
    fl = getattr(st, "flood", None)
    outlets = {"outlet_{}_invert_ft".format(c.name): c
               for c in (getattr(fl, "outlets", None) or [])} if fl is not None else {}
    band_sp = "spillway_rating_first_row_ft" in keys
    done, flagged, drop = [], [], []

    def dem_set(k, e, v, how):
        e.update(navd88=round(v, 3), _navd88=v, how=how, dem_derived=True,
                 note="DEM-derived: the value given ({:.2f} ft) is in the 0 - 300 ft band and the "
                      "top of dam is not local - replaced by the {}".format(e["source"], low_how))
        done.append((k, e["source"], v, how))

    def flag(k, e, why, use):
        e.update(navd88=None, _navd88=None, unconverted=True,
                 note="UNCONVERTED - review (not used: {}): {}".format(use, why))
        flagged.append((k, e["source"], why, use))

    for k in keys:
        e = f[k]
        e.pop("pending_tie", None)
        base = k.split(":")[-1]
        if base == "breach_bottom_ft" or k in outlets:
            if low_bad:
                flag(k, e, low_bad, _GUARD_USE["breach_bottom_ft"] if base == "breach_bottom_ft"
                     else _GUARD_OUTLET)
                if k in outlets:
                    drop.append(outlets[k])
                continue
            how = "DEM-derived: {}".format(low_how)
            dem_set(k, e, low_ft, how)
            if base == "breach_bottom_ft":
                dam.breach_bottom_ft = low_ft
            else:
                outlets[k].invert_m = low_ft * FT
        elif base in _DEM_ROUTING:
            e.update(navd88=None, _navd88=None, how="routing", replaced=True,
                     note="not used: the value given ({:.2f} ft) is in the 0 - 300 ft band and "
                          "the top of dam is not local - {}".format(e["source"], _DEM_ROUTING[base]))
            done.append((k, e["source"], None, _DEM_ROUTING[base]))
            if base == "pmf_max_wse_ft":
                dam.pmf_max_wse_ft = None
            elif fl is not None:
                fl.trigger, fl.trigger_value = "peak", None     # flood.TRIGGER_PEAK
        elif base == "normal_pool_ft":
            sp = getattr(fl, "spillway", None) if fl is not None else None
            if sp is not None and len(sp.elev_m) and not band_sp:
                v = float(getattr(sp, "crest_m", sp.elev_m[0])) / FT
                e.update(navd88=None, _navd88=None, how="spillway rating", replaced=True,
                         note="not used: the value given ({:.2f} ft) is in the 0 - 300 ft band "
                              "and the top of dam is not local - the run takes the normal pool "
                              "from the spillway rating's crest ({:.2f} ft)".format(e["source"], v))
                done.append((k, e["source"], None, "the spillway rating's crest ({:.2f} ft)"
                             .format(v)))
            else:
                flag(k, e, "no DEM / spillway rating source for the normal pool",
                     "the flood scenario needs a normal pool or a spillway rating")
            if fl is not None:
                fl.normal_pool_ft = None
        elif base == "spillway_rating_first_row_ft":
            flag(k, e, "no DEM source for a spillway rating", _GUARD_USE[base])
            if fl is not None:
                fl.spillway = None
        else:                       # top of dam (not here: it is real), anything else
            flag(k, e, "no DEM source", "not used")
            if hasattr(dam, base):
                setattr(dam, base, None)
    if drop and fl is not None:
        fl.outlets = [c for c in fl.outlets if c not in drop]
    rec.update(_local_done=True, tie="DEM-derived (no local crest)", band_dem=True,
               dem_crest_ft=round(crest, 3), dem_low_ft=None if low_ft is None else round(low_ft, 3),
               dem_low_how=low_how)
    top = (f.get("top_of_dam_ft") or {}).get("source")
    what = "; ".join("{} {:.2f} ft -> {}".format(
        k, src, "{:.2f} ft NAVD88 ({})".format(v, how) if v is not None else how)
        for k, src, v, how in done)
    if done:
        msg = ("The top of dam {} is on a real datum (not local), so the value(s) "
               "between 0 and 300 ft cannot be tied by a local crest and are NOT used as given; "
               "replaced by DEM elevations (Nick 2026-10-29): {}. Check against the as-built "
               "survey.".format("{:.2f} ft".format(top) if top is not None else "",
                                what))
        rec.setdefault("warnings", []).append(msg)
        log("Elevation datum: " + msg)
    if flagged:
        rr = "0 - 300 ft value(s) with no DEM elevation to replace them: {}".format(
            ", ".join(k for k, *_ in flagged))
        rec.update(review=True, review_reason=rr, tie="review")
        msg = ("REVIEW: elevation(s) between 0 and 300 ft with a real top of dam have no DEM "
               "elevation to replace them and are NOT used: {}. Check them before using this "
               "run.".format("; ".join("{} {:.2f} ft ({}; {})".format(k, src, why, use)
                                       for k, src, why, use in flagged)))
        rec.setdefault("warnings", []).append(msg)
        log("Elevation datum: " + msg)
    for line in VD.summary_lines(rec):
        log("Elevation datum: " + line)
    return rec


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


def apply_local(dam, st, dem_crest_ft, crest_from="DEM near the dam point", log=None,
                tie_ok=True, no_tie="", dem_low=None):
    """3.12.1 (damsuite 1.18.1) local datums (damsuite.vdatum.local_check), 3.12.5 the 0 - 300 ft
    rule (see the module text): after the run has read the DEM crest, a dam whose elevations
    are local (stated / chosen "local", detected in auto, or 0 - 300 ft whatever the choice)
    has its top of dam replaced by the DEM crest and every other local elevation moved by the
    same amount (offset = DEM crest - local top), instead of the NGVD29 shift.
    tie_ok False (no mid-crest; no_tie says why) or no DEM crest / a DEM in the wrong units:
    a record with 0 - 300 ft values is flagged for review and left unconverted (never
    guessed); so is one with no local top of dam to tie by. A record whose top of dam is real
    (NAVD88 / NGVD29, not local) with 0 - 300 ft values has no local crest to tie them by: with
    a DEM crest and dem_low ((ft, how) or a callable returning it: dem_toe_low) they are
    replaced by DEM elevations (_dem_leftovers, Nick 2026-10-29), else flagged for review.
    Returns (record, changed). Once."""
    log = log or (lambda m: None)
    rec = getattr(st, "vertical_datum", None)
    if rec is None or rec.get("_local_done") or rec.get("review"):
        return rec, False
    if rec.get("dam_id") is not None and getattr(dam, "dam_id", None) != rec.get("dam_id"):
        return rec, False
    f = rec.setdefault("fields", {})
    if not f:                       # no dam elevation given: all from the DEM already
        return rec, False
    band = bool(rec.get("local_rule"))
    pending = [k for k, e in f.items() if e.get("pending_tie")]
    strict = band or bool(pending)          # 0 - 300 ft values: never guess a tie
    if dem_crest_ft is None:
        no_tie_why = "no DEM crest at the dam"
    else:
        no_tie_why = _dem_units_suspect(dem_crest_ft, f)
    if no_tie_why is None and not tie_ok:
        no_tie_why = no_tie or "no mid-crest DEM crest (no dam line)"
    forced = bool(rec.get("local"))
    why = ""
    if not forced:
        hit = False
        if rec.get("how") != "override" and dem_crest_ft is not None:
            sh = float(rec.get("shift_ft") or 0.0) if rec.get("applied") else 0.0
            if "top_of_dam_ft" in f:
                hit, why = VD.local_check(f["top_of_dam_ft"]["source"], dem_crest_ft, sh)
            else:                   # no top given (DEM top): a pool far above the DEM crest
                for k, label in (("normal_pool_ft", "normal pool"),
                                 ("pmf_max_wse_ft", "PMF maximum pool"),
                                 ("trigger_wse_ft", "failure-trigger pool")):
                    if k in f and not f[k].get("pending_tie"):
                        hit, why = VD.local_check(f[k]["source"], None, 0.0, label)
                        if not hit and f[k]["source"] + sh - dem_crest_ft > VD.DEM_TOL_FT:
                            hit, why = True, ("the {} {:.2f} ft is more than {:.0f} ft above "
                                              "the DEM crest near the dam ({:.2f} ft)".format(
                                                  label, f[k]["source"], VD.DEM_TOL_FT,
                                                  dem_crest_ft))
                        if hit:
                            break
        if not hit:
            if pending:
                top = f.get("top_of_dam_ft")
                r = ("no local crest to tie them by: the top of dam {:.2f} ft is not on a local "
                     "datum".format(top["source"]) if top and dem_crest_ft is not None else
                     "no local crest to tie them by: no top of dam was given" if not top else
                     no_tie_why or "no local crest to tie them by")
                if top and dem_crest_ft is not None and dem_low is not None and \
                        _dem_units_suspect(dem_crest_ft, f) is None:
                    # 3.12.5 (Nick 2026-10-29: "in that case, use dem elevations")
                    return _dem_leftovers(dam, st, rec, pending, dem_crest_ft, dem_low, log), \
                        True
                return _review_leftovers(rec, pending, r, log), True
            return rec, False
    if no_tie_why and not strict:
        if dem_crest_ft is None:
            return rec, False
        bad = _dem_units_suspect(dem_crest_ft, f)
        if bad:                     # a DEM in the wrong units / datum: the run's unit checks
            log("Local datum not applied: " + bad)
            if rec.get("tie") == VD.TIE_PENDING:
                rec["tie"] = "not applied ({})".format(bad)
            return rec, False
        no_tie_why = None           # 1.18.1 record (no 0 - 300 ft value): the crest near the point
    rec["_local_done"] = True
    top = f.get("top_of_dam_ft")
    review = no_tie_why if strict else None
    if review is None and top is None:
        review = "no local crest to tie by: no top of dam was given"
    offset = float(dem_crest_ft) - top["source"] if top and not review else None
    old_m = float(rec.get("_shift_m", 0.0)) if rec.get("applied") else 0.0
    unconv = []
    far = []                        # (key, source, tied, ft from the DEM crest): not used
    lim = VD.TIE_MAX_FROM_CREST_FT

    def place(key, use=None):
        e = f.get(key)
        if e is None:
            return None
        e.pop("pending_tie", None)
        if offset is None:
            e.update(navd88=None, _navd88=None, unconverted=True,
                     note=_unconverted_note(key, review))
            unconv.append(key)
            return None
        new = e["source"] + offset
        d = new - float(dem_crest_ft)
        if abs(d) > lim:            # 3.12.5 plausibility guard (Nick 2026-10-29)
            e.update(navd88=None, _navd88=None, unconverted=True, implausible=True,
                     tied_ft=round(new, 2), off_crest_ft=round(d, 2),
                     note="UNCONVERTED - review (not used: {}): {:.2f} {:+.2f} = {:.2f} ft "
                          "NAVD88 would be {:.2f} ft {} the DEM crest {:.2f} ft (more than "
                          "{:.0f} ft)".format(
                              use or _GUARD_USE.get(key.split(":")[-1], _GUARD_OUTLET),
                              e["source"], offset, new, abs(d),
                              "above" if d > 0 else "below", float(dem_crest_ft), lim))
            e.pop("how", None)
            e.pop("offset_ft", None)
            far.append((key, e["source"], new, d, use or _GUARD_USE.get(key.split(":")[-1],
                                                                         _GUARD_OUTLET)))
            return None
        e.update(navd88=round(new, 3), _navd88=new, how="DEM-derived",
                 offset_ft=round(offset, 3))
        e.pop("note", None)
        return new

    def model_ft(key):
        """The value the model holds now for key (as given, or shifted by apply())."""
        e = f[key]
        shifted = rec.get("applied") and not e.get("local_elev")
        return e["source"] + (old_m / FT if shifted else 0.0)

    if top:
        if offset is not None:
            dam.top_of_dam_ft = float(dem_crest_ft)
            top.update(navd88=round(float(dem_crest_ft), 3), _navd88=float(dem_crest_ft),
                       how=crest_from)
            top.pop("pending_tie", None)
        else:
            dam.top_of_dam_ft = place("top_of_dam_ft")
    for name in ("pmf_max_wse_ft", "breach_bottom_ft"):
        if name in f:
            setattr(dam, name, place(name))
    fl = getattr(st, "flood", None)
    if fl is not None:
        if "normal_pool_ft" in f:
            fl.normal_pool_ft = place("normal_pool_ft")
        if "trigger_wse_ft" in f and fl.trigger == "wse":
            v = place("trigger_wse_ft")
            if v is not None:
                fl.trigger_value = v
            elif f["trigger_wse_ft"].get("implausible"):
                fl.trigger, fl.trigger_value = "peak", None     # flood.TRIGGER_PEAK
        k = "spillway_rating_first_row_ft"
        sp = getattr(fl, "spillway", None)
        if k in f:
            cur = model_ft(k)
            v = place(k)
            if v is not None and sp is not None and len(sp.elev_m):
                sp.elev_m = sp.elev_m + (v - cur) * FT
            elif f[k].get("implausible"):
                fl.spillway = None
        drop = []
        for c in getattr(fl, "outlets", None) or []:
            k = "outlet_{}_invert_ft".format(c.name)
            if k in f:
                cur = model_ft(k)
                v = place(k)
                if v is not None:
                    c.invert_m = c.invert_m + (v - cur) * FT
                elif f[k].get("implausible"):
                    drop.append(c)
        if drop:
            fl.outlets = [c for c in fl.outlets if c not in drop]
    if forced and (band or rec.get("how") == "detected"):   # 3.12.5: the 0 - 300 ft rule
        why = rec.get("local_reason") or why            # (a stated Local too: no empty rule)
    rec.update(source_datum=VD.LOCAL if forced and rec.get("how") != "detected"
               else VD.LOCAL_ASSUMED,
               how=rec.get("how") if forced else "detected", method=VD.METHOD_DEM,
               applied=False, shift_m=0.0, shift_ft=0.0, local=True, local_reason=why,
               dem_crest_ft=None if dem_crest_ft is None else round(float(dem_crest_ft), 3),
               dem_crest_from=crest_from,
               local_offset_ft=None if offset is None else round(offset, 3))
    rec.pop("_shift_m", None)
    pre = rec.pop("_pre_tie_warning", None)     # plan's pre-tie local note: replaced below
    rec["warnings"] = [w for w in rec.get("warnings") or []
                       if "NGVD29 was assumed" not in w and "could not be converted" not in w
                       and "between 0 and 300 ft" not in w and w != pre]
    rec["tie"] = "review" if review else "done"
    label = "a local datum (0 - 300 ft rule: {})".format(why) if band else \
        "a local datum ({})".format(why or ("stated" if rec.get("how") == "stated"
                                            else "set by the engineer"))
    if review:
        rec.update(review=True, review_reason=review)
        msg = ("REVIEW: the dam elevations are on {} but cannot be tied to the DEM: {}. The "
               "local values were NOT converted: {}.".format(label, review, ", ".join(unconv)))
        out = [k for k in unconv if k.split(":")[-1] in _OUT_OF_MODEL]
        kept = [k for k in unconv if k not in out]
        if out:
            msg += " The run takes {} from the DEM / the spillway rating.".format(", ".join(out))
        if kept:
            msg += " Used as given: {}.".format(", ".join(kept))
        msg += " Check the datum / DEM before using this run."
    else:
        msg = ("The dam elevations are on {}: converted to NAVD88 by the DEM tie - the top of "
               "dam is the {} ({:.2f} ft NAVD88), the other local elevations are DEM-derived "
               "(DEM crest - local crest = {:+.2f} ft); never VERTCON-shifted.".format(
                   label, crest_from, float(dem_crest_ft), offset))
        msg += " Check against the as-built survey{}.".format(
            "" if band else "; set the datum to NAVD88 / NGVD29 if wrong")
    rec["warnings"].append(msg)
    if far:                         # 3.12.5 plausibility guard: REVIEW, never used
        what = "; ".join("{} {:.2f} -> {:.2f} ft NAVD88 is {:.2f} ft {} the DEM crest ({})".format(
            k, src, new, abs(d), "above" if d > 0 else "below", use)
            for k, src, new, d, use in far)
        rr = "tied value(s) more than {:.0f} ft from the DEM crest {:.2f} ft: {}".format(
            lim, float(dem_crest_ft), ", ".join(k for k, *_ in far))
        rec.update(review=True, review_reason=rr, tie="review")
        rec["warnings"].append(
            "REVIEW: {} tied value(s) land more than {:.0f} ft from the DEM crest ({:.2f} ft "
            "NAVD88; offset {:+.2f} ft) and are NOT used (left unconverted): {}. Check the datum "
            "of those values before using this run.".format(
                len(far), lim, float(dem_crest_ft), offset, what))
    for line in VD.summary_lines(rec):
        log("Elevation datum: " + line)
    return rec, True


def merge_tie_warnings(warnings, pre_tie, now):
    """3.12.5 (Nick 2026-10-29): the run's warnings after the DEM tie - the pre-tie datum
    warnings the tie replaced (pre_tie, not in the tie record's warnings `now`) and "NGVD29 was
    assumed" are dropped, then the tie's warnings are added once (no near-duplicates). In
    place; returns the list."""
    warnings[:] = [w for w in warnings if "NGVD29 was assumed" not in w
                   and (w not in pre_tie or w in now)]
    for w in now:
        if w not in warnings:
            warnings.append(w)
    return warnings


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
