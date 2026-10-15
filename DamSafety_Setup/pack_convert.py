# -*- coding: utf-8 -*-
"""Convert an old single-file offline pack (.sqlite or .zip) into a suite data folder."""
import os
import zipfile
import shutil


def _safe_extract(zf, dest):
    """Extract a zip into dest, refusing any entry that leaves dest."""
    root = os.path.realpath(dest)
    for member in zf.infolist():
        target = os.path.realpath(os.path.join(dest, member.filename))
        if target != root and not target.startswith(root + os.sep):
            raise ValueError("archive entry outside the destination: " + member.filename)
    for member in zf.infolist():
        zf.extract(member, dest)


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
    written = []
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
    open(os.path.join(dest, "LAYERS.txt"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return "Wrote {} layer(s) to {}. Use this as the suite data folder.".format(len(written), dest)
