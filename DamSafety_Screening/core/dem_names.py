"""File names of 'Download DEM' (nddam:downloaddem). No QGIS / GDAL here.

For one dam the file is <DAM_ID>_<cell>m_5070.tif when the caller gives the dam ID (both
wizards do), so the wizards find a fresh download for the selected dam. Without an ID it
stays dam_<lon>_<lat>_<cell>m_5070.tif. The same rule is in the Runner's
wizard_logic.dem_stem (kept equal by DamSafety_Runner/tests/test_dem_download_names.py)."""
import re


def dem_stem(dam_id):
    """'DWR-2801' -> 'DWR-2801'; spaces and characters unsafe in a file name -> '_'."""
    s = re.sub(r"[^\w\-]+", "_", str(dam_id or "").strip()).strip("_")
    return s


def point_stem(point_text):
    raw = (point_text or "").strip()
    return "dam" if not raw else "dam_{}_{}".format(*[p.strip() for p in raw.split(",")[:2]])


def dam_scope_stem(dam_id, point_text):
    return dem_stem(dam_id) or point_stem(point_text)
