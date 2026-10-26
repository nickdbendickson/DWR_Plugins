"""Dam-inventory standardisation: units, status/size filters, shared-volume flags.

Pure Python so it can be tested on the real DWR dBase file outside QGIS.
DWR convention: 0 means "not recorded" for height, storage, area and length.
"""
FT_M, AF_M3, AC_M2 = 0.3048, 1233.4818375, 4046.8564224

# ND DWR "Dams by Hazard Class" export (10-character field names)

# role -> names seen on the DWR shapefile export and on the suite GeoPackage
DWR_ALIASES = {
    "ID": ("structure_id", "structure_", "dam_id"),
    "NAME": ("dam_name", "name"),
    "H": ("max_dam_height", "max_dam_he", "dam_height"),
    "NS": ("normal_vol", "normal_storage"),
    "MS": ("max_pool_vol", "max_pool_v", "max_storage"),
    "SA": ("normal_area", "normal_are", "max_pool_area", "surface_area"),
    "HZ": ("hazard_class", "hazard_cla", "hazard"),
    "STATUS": ("status",),
    "NIDREF": ("national_id", "national_i", "nid_id"),
    "LEN": ("dam_length",),
    # 0.17.47: pool elevations (ft; DWR only - the NID has none), so tool 3 can put each storage
    # at its own elevation (only their difference is used: some records are in a local datum)
    "NE": ("normal_elev", "normal_ele", "normal_elevation"),
    "ME": ("max_pool_elev", "max_pool_e", "max_pool_elevation"),
}
NID_ALIASES = {
    "ID": ("nidid", "nid_id", "federal_id"),
    "NAME": ("dam_name", "name"),
    "H": ("dam_height", "nid_height"),
    "NS": ("normal_storage",),
    "MS": ("max_storage", "nid_storage"),
    "SA": ("surface_area",),
    "HZ": ("hazard_potential", "hazard"),
    "STATUS": ("operational_status", "condition_assessment"),
    "NIDREF": ("nidid", "nid_id"),
    "LEN": ("dam_length",),
}



def feature_in_county(feat, county, point=None):
    """True when the feature's county field matches, or its point is in that county.

    point is an EPSG:4326 point (x = longitude) when the caller already transformed
    the geometry. The 53 county outlines in nd_counties.geojson are used for that
    point. The old county box is the fallback.
    """
    want = (county or "").strip()
    if not want:
        return True
    names = [f.name() for f in feat.fields()]
    low = {n.lower(): n for n in names}
    for key in ("county", "countystate", "county_state"):
        if key in low:
            val = str(feat[low[key]] or "")
            return want.lower() in val.lower()
    if point is None:
        g = feat.geometry()
        if g is None or g.isEmpty():
            return False
        point = g.centroid().asPoint()
    try:
        x, y = float(point.x()), float(point.y())
    except AttributeError:
        x, y = float(point[0]), float(point[1])
    if -130.0 <= x <= -90.0 and 45.0 <= y <= 49.5:
        try:
            from . import nd_counties
            name = nd_counties.normalize(want)
            if name:
                return nd_counties.contains(name, x, y)
        except Exception:
            pass
    try:
        from .dem_download import COUNTIES
    except ImportError:
        from damsuite.dem_download import COUNTIES
    box = None
    for name, coords in COUNTIES.items():
        if name.lower() == want.lower():
            box = coords
            break
    if box is None:
        return False
    west, south, east, north = box
    return west <= x <= east and south <= y <= north


def match_fields(names, aliases):
    """{role: actual field name} for the names this layer has."""
    have = {str(n).lower(): n for n in names}
    return {role: have[a] for role, opts in aliases.items() for a in opts if a in have}

DWR_DEFAULTS = {"ID": "structure_", "NAME": "dam_name", "H": "max_dam_he", "NS": "normal_vol",
                "MS": "max_pool_v", "SA": "normal_are", "HZ": "hazard_cla",
                "STATUS": "status", "NIDREF": "national_i", "LEN": "dam_length"}
# NE / ME (pool elevations) have no default box value: older DWR exports lack them, and a default
# name the layer does not have would fail the read; they are filled from DWR_ALIASES instead
NID_DEFAULTS = {
    "ID": "NIDID", "NAME": "NAME", "H": "DAM_HEIGHT", "NS": "NORMAL_STORAGE",
    "MS": "MAX_STORAGE", "SA": "SURFACE_AREA", "HZ": "HAZARD_POTENTIAL",
    "STATUS": "OPERATIONAL_STATUS", "NIDREF": "NIDID", "LEN": "DAM_LENGTH",
}


def _txt(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _num(v, mult):
    try:
        if v is None or v == "":
            return None
        x = float(v)
    except (TypeError, ValueError):
        return None
    return None if x == 0 else x * mult


def standardize(get, fld, metric=False, tag="DWR", idx=0):
    """get(field_name) -> raw value.  fld maps role -> field name (or None)."""
    h_m, v_m, a_m = (1.0, 1.0, 1.0) if metric else (FT_M, AF_M3, AC_M2)
    g = lambda role: get(fld[role]) if fld.get(role) else None
    return {"id": _txt(g("ID")) or f"{tag}{idx}", "name": _txt(g("NAME")),
            "h": _num(g("H"), h_m), "ns": _num(g("NS"), v_m), "ms": _num(g("MS"), v_m),
            "sa": _num(g("SA"), a_m), "hz": _txt(g("HZ")), "status": _txt(g("STATUS")),
            "nidref": _txt(g("NIDREF")).upper(), "len": _num(g("LEN"), h_m),
            "ne": _num(g("NE"), h_m), "me": _num(g("ME"), h_m)}


def parse_status_list(text):
    return {s.strip().lower() for s in (text or "").split(",") if s.strip()}


def status_ok(status, keep):
    """keep = set of lower-case statuses; empty set keeps everything."""
    return (not keep) or (status or "").strip().lower() in keep


def size_skip(h_m, storage_m3, min_h_ft, min_v_af):
    """Skip only when BOTH height and storage are known and BOTH are below the limits.

    An unknown value never causes a skip: a short levee with no recorded storage may
    still hold a lot of water.  Thresholds of 0 disable the filter."""
    if not (min_h_ft or min_v_af) or h_m is None or storage_m3 is None:
        return False
    return h_m < min_h_ft * FT_M and storage_m3 < min_v_af * AF_M3


def shared_volume_flags(vols_m3, min_af=500.0):
    """True where several records carry the identical large volume - typically levee
    or embankment segments of one reservoir, not separate dams."""
    from collections import Counter
    floor = min_af * AF_M3
    cnt = Counter(round(v) for v in vols_m3 if v and v >= floor)
    return [bool(v and v >= floor and cnt[round(v)] > 1) for v in vols_m3]
