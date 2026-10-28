"""Fill empty plugin slots from a suite data folder.

A layer the engineer already chose is left alone. Matching is by layer name
and by the fields that layer actually has. Unknown structures and dugouts are
not the dam inventory.
"""
import os
import sqlite3

# slot, layer names (best first), fields that must all be present, fields of which one
# must be present, words in the layer name that reject the slot.
# The first names are the individual GeoPackages. A combined file is only a fallback.
RULES = (
    ("dwr", ("dwr_dams", "dams"), ("dam_name",), ("structure_", "structure_id"),
     ("unknown", "dugout", "line")),
    ("dam_lines", ("dwr_dam_lines", "dams_linear_features", "dam_lines"), ("structure_id",), (),
     ("unknown", "dugout")),
    ("structures", ("nsi_structures", "nsi_nd", "nsi"), ("fd_id",), ("occtype", "bldgtype"), ()),
    ("nid", ("nid_dams", "dams_nid", "nid"), (), ("nidid", "nid_id"), ("dwr", "line")),
    ("roads", ("roads_ng911", "nd_roads_ng911", "roads"), (), (), ("county", "rail", "crossing")),
    ("rail", ("rail_lines", "narn_rail_lines", "railroads", "rail"), (), (), ("crossing",)),
    ("streams", ("nhd_flowlines", "flowlines", "nhd_flow"), (), (), ()),
    ("waterbodies", ("nhd_waterbodies", "waterbodies", "nwi"), (), (), ("flow",)),
    ("lifelines", ("hospitals", "lifelines", "care_facilities"), (), (), ()),
    ("dugouts", ("dwr_dugouts",), ("longitude",), (), ("polygon",)),
    # 0.17.54: DWR's own export names DWR_Dugouts.gpkg / DWR_Dugouts_Polygons.gpkg ("dugouts" +
    # "polygons"); points and polygons are told apart by the geometry as well (see scan)
    ("dugout_polygons", ("dwr_dugout_polygons", "dwr_dugouts_polygons", "dugouts_polygons",
                         "dugout_polygons", "dugout_poly"), ("structure_id",), (), ()),
    ("unknown", ("dwr_unknown",), ("dam_name",), (), ()),
    # individual files processing reads in addition to the one layer each slot holds
    ("address_points", ("address_points",), (), (), ()),
    ("structures_usa", ("structures_usa", "usa_structures"), (), (), ()),
    ("county_roads", ("county_roads",), (), (), ()),
    ("rail_crossings", ("rail_crossings", "grade_crossings"), (), (), ()),
    ("bridges", ("bridges_nbi", "bridges"), (), (), ()),
    ("skilled_nursing", ("skilled_nursing",), (), (), ()),
    ("basic_care", ("basic_care",), (), (), ()),
    ("assisted_living", ("assisted_living",), (), (), ()),
)

# slots that are not the one layer a tool asks for first. Processing opens these too.
MORE = {
    "roads": ("county_roads",),
    "lifelines": ("skilled_nursing", "basic_care", "assisted_living"),
    "structures": ("address_points", "structures_usa"),
    "bridges": ("bridges",),
}


def more_uris(found, slot):
    """'a.gpkg|layername=a;;b.gpkg|layername=b' for the extra individual files of a slot."""
    found = found or {}
    return ";;".join(found[k] for k in MORE.get(slot, ()) if found.get(k))


def find_pack(folder):
    """The offline pack in this folder, or ''.

    A sqlite or zip pack wins. A folder named by LAYERS.txt (one GeoPackage per
    layer) is also a pack. Drop-in folders and the old zip still work. scan()
    reads the GeoPackages either way.
    """
    names = ("nd_offline_pack.sqlite", "nd_offline_pack.zip")
    layers_txt = ""
    if not folder or not os.path.isdir(folder):
        return ""
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in ("__MACOSX", ".git")]
        for name in names:
            if name in files:
                return os.path.join(root, name)
        if "LAYERS.txt" in files and not layers_txt:
            layers_txt = root
        if root.replace(folder, "").count(os.sep) >= 3:
            dirs.clear()
    return layers_txt


def _fields(con, table):
    try:
        return [r[1].lower() for r in con.execute('PRAGMA table_info("{}")'.format(table))]
    except sqlite3.Error:
        return []


def _layers(path):
    """[(layer name, geometry, fields)] of a GeoPackage."""
    out = []
    try:
        con = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
    except sqlite3.Error:
        return out
    try:
        rows = con.execute(
            "SELECT table_name, geometry_type_name FROM gpkg_geometry_columns").fetchall()
        for table, geom in rows:
            out.append((table, (geom or "").lower(), _fields(con, table)))
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


def _has_spatial(folder):
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    return any(n.lower().endswith((".gpkg", ".shp", ".geojson", ".json")) for n in names)


def _score(names, path, layer):
    """Higher is a better individual file. 0 means the name did not match."""
    low = layer.lower()
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    score = 0
    for i, name in enumerate(names):
        if low == name or stem == name:
            score = 1000 - i * 10
            break
        if name in low or name in stem:
            score = 100 - i
            break
    if not score:
        return 0
    if stem == low:
        score += 50
    if stem in ("dwr_suite", "suite_layers", "suite"):
        score -= 400
    return score


# offline-pack data set -> Inputs slot. NHD waterbodies are not in the pack.
PACK_SLOTS = {
    "streams": ("NHD_flowlines",),
    "waterbodies": ("NHD_waterbodies",),
    "rail": ("NARN_rail_lines",),
    "roads": ("ND_roads_NG911",),
    "structures": ("NSI_structures",),
    "nid": ("NID_dams",),
    "lifelines": ("lifeline_nd_hospitals", "lifeline_nd_skilled_nursing",
                  "lifeline_nd_basic_care", "lifeline_nd_assisted_living",
                  "lifeline_eia_power_plants"),
    "dwr": ("DWR_dams",),
    "dam_lines": ("DWR_dam_lines",),
    # damsuite 1.17.7 (Screening 0.17.54): the DWR export files the pack builder adds with --dwr-folder
    "unknown": ("DWR_unknown_structures",),
    "dugouts": ("DWR_dugouts",),
    "dugout_polygons": ("DWR_dugout_polygons",),
}
# slots a format-2 pack's dwr/ layer fills when the suite folder has no file for them. Only
# these: the other pack layers reach the tools by their own routes, as before.
DWR_SLOTS = ("dwr", "dam_lines", "unknown", "dugouts", "dugout_polygons")


def pack_datasets(path):
    """{name: {count, status, geometry}} from an unpacked offline pack, or {}."""
    path = str(path or "").split("|")[0]
    if not path.endswith(".sqlite") or not os.path.isfile(path):
        return {}
    try:
        con = sqlite3.connect("file:{}?mode=ro".format(path), uri=True)
        try:
            rows = con.execute("SELECT name, count, status, geometry FROM datasets").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return {}
    return {name: {"count": int(count or 0), "status": status or "", "geometry": geometry or ""}
            for name, count, status, geometry in rows}


def pack_slot(path, slot):
    """The offline-pack data set for an Inputs slot, or None."""
    data = pack_datasets(path)
    names = [n for n in PACK_SLOTS.get(slot, ()) if n in data
             and data[n]["status"] in ("complete", "partial") and data[n]["count"]]
    if not names:
        return None
    return {"count": sum(data[n]["count"] for n in names),
            "geometry": data[names[0]]["geometry"],
            "name": ", ".join(names), "source": "offline pack"}


_SCAN = {}


def _pack_dwr(settings, roots):
    """{slot: uri} of the DWR layers of the active format-2 offline pack (settings["pack"]
    first, else the suite's pack: damsuite.suitedata.active_pack), or {} - none, a format-1
    pack (not a QGIS layer), Data source = Download, or no suitedata beside this module."""
    try:
        from . import suitedata as SD
    except Exception:                                            # noqa  (Setup's copy)
        return {}
    try:
        pack = str((settings or {}).get("pack") or "").strip()
        if not pack:
            if SD.data_source() == SD.SOURCE_DOWNLOAD:
                return {}
            pack = SD.active_pack(roots[0] if roots else None)
        if not pack or not os.path.isdir(pack):
            return {}
        out = {}
        for slot in DWR_SLOTS:
            for name in PACK_SLOTS.get(slot, ()):
                f = SD.pack_file(name, pack)
                if f:
                    out[slot] = "{}|layername={}".format(f, name)
                    break
        return out
    except Exception:                                            # noqa
        return {}


def layers_for_settings(settings):
    """Layers the chosen files already point at: the suite folder, or the folder
    that holds the DWR file. Cached so the check does not walk the disk each slot.
    damsuite 1.17.7: a DWR slot (DWR_SLOTS) no file fills comes from the offline pack's
    dwr/ layer (a format-2 pack folder); a file of the engineer's own always wins."""
    found = {}
    settings = settings or {}
    roots = []
    for key in ("suite_folder", "dwr", "dam_lines", "package", "structures", "roads", "rail", "streams"):
        raw = str(settings.get(key) or "").split("|")[0].strip()
        if not raw:
            continue
        if os.path.isdir(raw):
            roots.append(os.path.abspath(raw))
        elif os.path.isfile(raw):
            roots.append(os.path.dirname(os.path.abspath(raw)))
    for folder in roots:
        try:
            stamp = os.stat(folder).st_mtime
        except OSError:
            continue
        hit = _SCAN.get(folder)
        if not hit or hit[0] != stamp:
            hit = (stamp, scan(folder)[0])
            _SCAN[folder] = hit
        for key, uri in hit[1].items():
            found.setdefault(key, uri)
    if any(not found.get(s) for s in DWR_SLOTS):
        for key, uri in _pack_dwr(settings, roots).items():
            found.setdefault(key, uri)
    return found


def _pack_roots(folder, pack):
    """The format-2 pack folder found in the suite folder (find_pack: the folder with
    LAYERS.txt), whose layers rank below the engineer's own files; [] for none or a
    format-1 pack."""
    out = []
    if pack and os.path.isdir(pack):
        out.append(os.path.abspath(pack))
    return out


def _in(path, roots):
    p = os.path.abspath(path)
    return any(p == r or p.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


def scan(folder):
    """{slot: uri} for the layers this folder holds, plus 'pack' when the
    offline pack is here. uri is 'path|layername=name'."""
    found = {}
    notes = []
    if not folder or not os.path.isdir(folder):
        return found, ["no suite folder"]
    pack = find_pack(folder)
    if pack:
        found["pack"] = pack
        notes.append("offline pack: {}".format(pack))
    layers = []
    roots = [folder]
    # A folder of individual GeoPackages is the source. Only look one level up when
    # this folder itself has no spatial files (a DWR subfolder, for example).
    if not _has_spatial(folder):
        parent = os.path.dirname(os.path.abspath(folder))
        if parent and os.path.isdir(parent) and _has_spatial(parent):
            roots.append(parent)
    seen = set()
    for base in roots:
        limit = 2
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d not in ("__MACOSX", ".git")]
            for name in files:
                low = name.lower()
                if not low.endswith((".gpkg", ".geojson", ".json", ".shp")):
                    continue
                path = os.path.join(root, name)
                if path in seen:
                    continue
                seen.add(path)
                if low.endswith(".gpkg"):
                    for layer, geom, fields in _layers(path):
                        layers.append((path, layer, geom, fields))
                else:
                    layers.append((path, os.path.splitext(name)[0], "", []))
            if root.replace(base, "").count(os.sep) >= limit:
                dirs.clear()
    notes.append("layers seen: {}".format(len(layers)))
    # damsuite 1.17.7: a layer of the offline pack folder is only a fallback - a file of the
    # engineer's own in the suite folder wins over it whatever the name scores
    packs = _pack_roots(folder, pack)
    used = set()
    for slot, names, need, anyof, reject in RULES:
        best = None
        best_score = (0, 0)
        for path, layer, geom, fields in layers:
            key = (path, layer)
            low = layer.lower()
            if any(word in low for word in reject):
                continue
            if need and fields and not all(f in fields for f in need):
                continue
            if anyof and fields and not any(f in fields for f in anyof):
                continue
            if slot == "dam_lines" and geom and "line" not in geom:
                continue
            if slot == "dugouts" and geom and "point" not in geom:
                continue
            if slot == "dugout_polygons" and (geom and "polygon" not in geom
                                              or not ("dugout" in low and "poly" in low or
                                                      "dugout" in os.path.basename(path).lower()
                                                      and "poly" in os.path.basename(path).lower())):
                continue
            score = _score(names, path, layer)
            if score <= 0 or key in used:
                continue
            rank = (0 if _in(path, packs) else 1, score)
            if best is None or rank > best_score:
                best, best_score = key, rank
        if best:
            used.add(best)
            uri = "{}|layername={}".format(best[0], best[1])
            found[slot] = uri
            notes.append("{}: {}".format(slot, uri))
    return found, notes
