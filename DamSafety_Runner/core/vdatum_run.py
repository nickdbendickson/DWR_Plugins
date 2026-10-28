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
