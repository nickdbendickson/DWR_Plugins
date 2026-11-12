# -*- coding: utf-8 -*-
"""Convert an old single-file offline pack (.sqlite or .zip) into a suite data folder.

An "offline pack" (pack format "nd-dam-hazard-offline-pack", version 1 - see
``offline_pack.py``) stores every feature as one row of a per-data-set table
``ds_<name>`` whose ``feat`` column is the GeoJSON feature *zlib-compressed*
(the writer does ``zlib.compress(json.dumps(feature))``). OGR cannot read those
blobs as geometry, so a plain ``ogr.Open()`` + ``CopyLayer`` on the pack copies
the opaque compressed blobs instead of real features - the written output is
wrong (M4). This converter decodes the blobs exactly as
``offline_pack.PackReader`` does (``json.loads(zlib.decompress(blob))``) and
writes one GeoPackage per data set (or GeoJSON when GDAL is unavailable). A
genuine spatial SQLite that is *not* an offline pack still goes straight through
OGR, as before.
"""
import os
import json
import zlib
import zipfile
import shutil
import sqlite3

# Ported from offline_pack.py: the pack-format marker and the "usable" data-set
# statuses PackReader selects on. Kept local so this module stays pure-stdlib.
_PACK_FORMAT = "nd-dam-hazard-offline-pack"
_USABLE_STATUS = ("complete", "partial")


def _safe_extract(zf, dest):
    """Extract a zip into dest, refusing any entry that leaves dest."""
    root = os.path.realpath(dest)
    for member in zf.infolist():
        target = os.path.realpath(os.path.join(dest, member.filename))
        if target != root and not target.startswith(root + os.sep):
            raise ValueError("archive entry outside the destination: " + member.filename)
    for member in zf.infolist():
        zf.extract(member, dest)


def _pack_table(name):
    """Feature table of data set `name` - offline_pack._table()."""
    safe = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in name)
    return "ds_" + safe


def _has_table(con, name):
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (name,)).fetchone() is not None


def _pack_datasets(con):
    """[(data set name, feature table)] of a format-1 offline pack, or None when
    `con` is not an offline pack. Mirrors offline_pack.PackReader: the usable
    (complete / partial) data sets whose table is present; if the `datasets`
    table is missing, every ``ds_*`` table (minus unfinished ``__new`` stages)."""
    try:
        meta = dict(con.execute("SELECT key, value FROM meta").fetchall())
    except sqlite3.Error:
        return None
    if meta.get("format") != _PACK_FORMAT:
        return None
    pairs = []
    try:
        names = [r[0] for r in con.execute(
            "SELECT name FROM datasets WHERE status IN (?, ?)", _USABLE_STATUS)]
    except sqlite3.Error:
        names = []
    for name in names:
        tbl = _pack_table(name)
        if _has_table(con, tbl):
            pairs.append((name, tbl))
    if not pairs:
        for (tbl,) in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ds\\_%' "
                "ESCAPE '\\' AND name NOT LIKE '%\\_\\_new' ESCAPE '\\' ORDER BY name"):
            pairs.append((tbl, tbl))
    return pairs


def _decode_features(con, tbl):
    """GeoJSON features of one pack table - offline_pack.PackReader._decode()
    (zlib-decompress, then json-decode); unreadable rows are skipped."""
    feats = []
    for (blob,) in con.execute('SELECT feat FROM "{}" ORDER BY fid'.format(tbl)):
        if blob is None:
            continue
        try:
            feats.append(json.loads(zlib.decompress(blob).decode("utf-8")))
        except (zlib.error, ValueError, UnicodeDecodeError, TypeError):
            continue
    return feats


def _geojson_to_gpkg(ogr, gj, out, name):
    """Translate a GeoJSON file to a GeoPackage layer via OGR. True on success."""
    in_ds = ogr.Open(gj)
    if in_ds is None or in_ds.GetLayerCount() == 0:
        return False
    drv = ogr.GetDriverByName("GPKG")
    if drv is None:
        return False
    if os.path.exists(out):
        drv.DeleteDataSource(out)
    out_ds = drv.CreateDataSource(out)
    if out_ds is None:
        return False
    out_ds.CopyLayer(in_ds.GetLayerByIndex(0), name)
    out_ds = None
    in_ds = None
    return True


def _convert_offline_pack(con, datasets, dest, src):
    """Decode the zlib/GeoJSON blobs of a format-1 pack into one layer per data
    set: a GeoPackage when GDAL is present, else the decoded GeoJSON itself."""
    try:
        from osgeo import ogr
    except Exception:
        ogr = None
    lines = ["# converted from " + os.path.basename(src)]
    written = []
    for name, tbl in datasets:
        fc = {"type": "FeatureCollection", "features": _decode_features(con, tbl)}
        gj = os.path.join(dest, name + ".geojson")
        with open(gj, "w", encoding="utf-8") as fh:
            json.dump(fc, fh)
        out_name = name + ".geojson"
        if ogr is not None and _geojson_to_gpkg(ogr, gj, os.path.join(dest, name + ".gpkg"), name):
            try:
                os.remove(gj)
            except OSError:
                pass
            out_name = name + ".gpkg"
        written.append(out_name)
        lines.append(out_name)
    with open(os.path.join(dest, "LAYERS.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    how = "GeoJSON (GDAL not available - open in QGIS)" if ogr is None else "GeoPackage"
    return "Decoded {} data set(s) to {} as {}. Use this as the suite data folder.".format(
        len(written), dest, how)


def _convert_spatial_sqlite(path, dest, src):
    """A genuine spatial SQLite (not an offline pack): OGR copies each layer."""
    try:
        from osgeo import ogr
    except Exception:
        ogr = None
    if ogr is None:
        shutil.copy(path, os.path.join(dest, "nd_offline_pack.sqlite"))
        return "GDAL is not available here. Copied the sqlite pack. Open it in QGIS and export each layer."
    ds = ogr.Open(path)
    if ds is None:
        return "Could not open " + path
    lines = ["# converted from " + os.path.basename(src)]
    written = []
    for i in range(ds.GetLayerCount()):
        layer = ds.GetLayerByIndex(i)
        name = layer.GetName()
        out = os.path.join(dest, name + ".gpkg")
        drv = ogr.GetDriverByName("GPKG")
        if os.path.exists(out):
            drv.DeleteDataSource(out)
        out_ds = drv.CreateDataSource(out)
        out_ds.CopyLayer(layer, name)
        out_ds = None
        written.append(name)
        lines.append(name + ".gpkg")
    ds = None
    with open(os.path.join(dest, "LAYERS.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return "Wrote {} layer(s) to {}. Use this as the suite data folder.".format(len(written), dest)


def convert_pack(src, dest):
    os.makedirs(dest, exist_ok=True)
    path = src
    if src.lower().endswith(".zip"):
        with zipfile.ZipFile(src) as z:
            _safe_extract(z, dest)
        found = []
        for root, _d, files in os.walk(dest):
            for name in files:
                if name.endswith(".sqlite"):
                    found.append(os.path.join(root, name))
        if not found:
            return "Unzipped, but no .sqlite pack was inside."
        path = found[0]

    # A format-1 offline pack stores features as zlib-compressed GeoJSON blobs
    # that OGR cannot read directly - decode them (M4). Anything else is a
    # genuine spatial SQLite that OGR can copy as-is.
    con = None
    try:
        con = sqlite3.connect(path)
        datasets = _pack_datasets(con)
        if datasets is not None:
            return _convert_offline_pack(con, datasets, dest, src)
    except sqlite3.Error:
        pass
    finally:
        if con is not None:
            con.close()

    return _convert_spatial_sqlite(path, dest, src)
