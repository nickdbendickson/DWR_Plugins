"""Tool 7b (0.17.55): get the Runner's DEM for the chosen dams when the engineer has none loaded.

The DEM covers the box around the chosen dams' breach points (EPSG:5070 metres) plus a buffer
(default 10 mi = half the Runner's default largest domain side, 20 mi). Two sources, both the
Download DEM tool's own code (core/dem_download.py, core/lidar_tnm.py); nothing is duplicated:

  * USGS 3DEP 10 m (ImageServer): one tiled GeoTIFF, float32, EPSG:5070, band unit metres,
    with the <stem>_dem.txt sidecar (dem_download.write_local_dem). Reused when it is there.
  * USGS 3DEP 1 m lidar tiles (native TNM 10 km tiles, never the 1.2 km ImageServer pieces):
    the tiles go to the suite's lidar tile folder (shared with Download DEM, resumed, never in
    the data pack) and a VRT over them (band unit 'm', the tiles' UTM CRS) goes to the DEM folder.
    Refused before anything is downloaded when it is over the size cap.

The file names carry a short key of the box, so a second run for the same dams reuses the file
and a different set of dams gets its own. No QGIS here; the tool turns RunnerDemError into a
QgsProcessingException.
"""
import hashlib
import json
import os

SOURCE_GIVEN, SOURCE_10M, SOURCE_1M = 0, 1, 2
SOURCE_LABELS = ["Use the DEM above",
                 "Download USGS 3DEP 10 m covering the chosen dams",
                 "Download USGS 3DEP 1 m lidar tiles (native) covering the chosen dams"]
DEFAULT_BUFFER_MI = 10.0          # half the Runner's default largest domain side (20 mi)
DEFAULT_MAX_GB = 10.0             # = lidar_tnm.DEFAULT_MAX_BYTES
FOLDER_NAME = "_runner_dem"       # under the Dams folder; '_' = never listed as a dam folder
MI = 1609.344
BYTES_PER_PX = 4                  # float32 ImageServer chunks (an upper estimate; GeoTIFF is compressed)

NO_DEM = ("No DEM for the Runner. Pick a DEM or VRT in 'DEM for the runner', or set 'Runner DEM "
          "source' to '{}' (or the 1 m lidar choice) and the tool downloads one covering the chosen "
          "dams. The 'Download DEM (dam, county, or state)' tool can also download one first."
          ).format(SOURCE_LABELS[SOURCE_10M])


class RunnerDemError(Exception):
    """A clear, user-facing reason the DEM could not be had (no traceback needed)."""


class RunnerDemCancelled(RunnerDemError):
    pass


def default_folder(dams_folder):
    return os.path.join(dams_folder or ".", FOLDER_NAME)


def dams_box_5070(points, buffer_mi=DEFAULT_BUFFER_MI):
    """(xmin, ymin, xmax, ymax) in EPSG:5070 metres: the box around the points (x, y in 5070)
    grown by buffer_mi on every side, snapped outward to whole 10 m."""
    pts = [(float(x), float(y)) for x, y in points]
    if not pts:
        raise RunnerDemError("None of the chosen dams has a breach point (tool 4), so there is no "
                             "area to download a DEM for.")
    b = max(0.0, float(buffer_mi or 0.0)) * MI
    if b <= 0:
        b = 1000.0                                  # never a zero-size box (one dam)
    x0 = min(p[0] for p in pts) - b
    y0 = min(p[1] for p in pts) - b
    x1 = max(p[0] for p in pts) + b
    y1 = max(p[1] for p in pts) + b
    snap = lambda v, up: (int(v // 10) + (1 if up and v % 10 else 0)) * 10.0
    return (snap(x0, False), snap(y0, False), snap(x1, True), snap(y1, True))


def box_key(box):
    """A short stable key of the box (to the metre) for the file names."""
    txt = ",".join("{:.0f}".format(v) for v in box)
    return hashlib.sha1(txt.encode("ascii")).hexdigest()[:8]


def stem(box, n_dams):
    return "7b_dem_{}dams_{}".format(int(n_dams), box_key(box))


def paths(folder, box, n_dams):
    s = stem(box, n_dams)
    return {"10m": os.path.join(folder, s + "_10m_5070.tif"),
            "1m": os.path.join(folder, s + "_1m_lidar.vrt")}


def plan_10m(box):
    """{"requests", "side_km", "est_bytes", "width_km", "height_km"} for a 10 m download."""
    from . import dem_download as dd
    n, side_km = dd.imageserver_chunks(box, 10)
    w, h = box[2] - box[0], box[3] - box[1]
    return {"requests": int(n), "side_km": side_km, "width_km": w / 1000.0, "height_km": h / 1000.0,
            "est_bytes": int(round(w / 10.0) * round(h / 10.0) * BYTES_PER_PX)}


def _fmt(n):
    from . import lidar_tnm as lt
    return lt.fmt_bytes(n)


def _cancelled(feedback):
    return feedback is not None and hasattr(feedback, "isCanceled") and feedback.isCanceled()


def _info(feedback, msg):
    if feedback is not None:
        feedback.pushInfo(msg)


def _warn(feedback, msg):
    if feedback is not None:
        getattr(feedback, "pushWarning", feedback.pushInfo)(msg)


def get_10m(box, folder, n_dams, max_bytes, feedback=None, dry=False):
    """The 10 m GeoTIFF for `box` in `folder` (reused when there). Returns its path."""
    from . import dem_download as dd
    dest = paths(folder, box, n_dams)["10m"]
    side = os.path.splitext(dest)[0] + "_dem.txt"
    if os.path.isfile(dest) and os.path.isfile(side):
        bad = dd.data_problem(dest)
        if bad:
            raise RunnerDemError("Not reusing {}: {}.".format(os.path.basename(dest), bad))
        if not dd.band_unit(dest):                  # our own USGS download: metres
            dd.set_metres(dest)
        _info(feedback, "Runner DEM: reusing {} (already downloaded for these dams)".format(dest))
        return dest
    pl = plan_10m(box)
    _info(feedback, "Runner DEM: USGS 3DEP 10 m for {:.1f} x {:.1f} km (EPSG:5070) - {} ImageServer "
          "request(s) of up to {:.0f} km a side, about {} to download".format(
              pl["width_km"], pl["height_km"], pl["requests"], pl["side_km"], _fmt(pl["est_bytes"])))
    if max_bytes and pl["est_bytes"] > max_bytes:
        raise RunnerDemError(
            "The 10 m DEM for the chosen dams is about {} to download, over the {} limit "
            "('Largest DEM download for one run (GB)'). Send fewer dams at a time (or dams closer "
            "together), lower the buffer, or raise the limit.".format(_fmt(pl["est_bytes"]), _fmt(max_bytes)))
    if dry:
        _info(feedback, "Runner DEM: validate only - not downloaded; it would be {}".format(dest))
        return dest
    os.makedirs(folder, exist_ok=True)
    if os.path.isfile(dest):                        # our own unfinished file (no sidecar)
        os.remove(dest)
    from .tile_workdir import tile_workdir
    try:
        with tile_workdir(folder) as work:
            vrt = dd.download(box, 10, work, stem(box, n_dams) + "_src", feedback, scope=1)
            if _cancelled(feedback):
                raise dd.Cancelled("cancelled")
            _info(feedback, "Runner DEM: writing the local GeoTIFF")
            tif, side = dd.write_local_dem(vrt, dest, 10, dd.product_info("usgs", 10), feedback)
    except dd.Cancelled:
        if os.path.isfile(dest) and not os.path.isfile(side):
            os.remove(dest)
        raise RunnerDemCancelled("Cancelled while downloading the Runner DEM; nothing was run.")
    except RunnerDemError:
        raise
    except Exception as e:                          # noqa - IOError from 3DEP, GDAL errors
        raise RunnerDemError("Downloading the USGS 3DEP 10 m DEM failed: {}. Check the internet "
                             "connection and run again (finished files are reused).".format(e))
    if dd.band_unit(tif) not in ("m", "metre", "metres", "meter", "meters"):
        dd.set_metres(tif)
    _info(feedback, "Runner DEM: {} (EPSG:5070, metres; facts: {})".format(tif, side))
    return tif


def _reusable_vrt(vrt):
    """True when a 1 m VRT built before for this box is complete and its tiles are all here."""
    js = os.path.splitext(vrt)[0] + ".json"         # lidar_tnm.build_vrt's sources file
    if not (os.path.isfile(vrt) and os.path.isfile(js)):
        return False
    try:
        with open(js, encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return False
    if meta.get("missing_tiles"):
        return False
    srcs = [t.get("path") for t in (meta.get("tiles") or []) if isinstance(t, dict)]
    return bool(srcs) and all(p and os.path.isfile(p) for p in srcs)


def get_1m(box, folder, n_dams, max_bytes, feedback=None, dry=False, http=None, tile_folder="",
           suite=None):
    """The VRT over the native 1 m lidar tiles covering `box` (EPSG:5070). Returns its path."""
    from . import dem_download as dd
    from . import lidar_tnm as lt
    vrt = paths(folder, box, n_dams)["1m"]
    if not dry and _reusable_vrt(vrt):
        _info(feedback, "Runner DEM: reusing {} (all its 1 m tiles are here)".format(vrt))
        return vrt
    if suite is None:
        try:
            from ..damsuite import suitedata as SD
            suite = SD.data_folder()
        except Exception:                           # noqa - no suite folder lookup here
            suite = ""
    try:
        tfolder, where, notes = lt.tile_folder(tile_folder or "", suite or "", folder)
    except lt.TileError as e:
        raise RunnerDemError(str(e))
    for n in notes:
        _warn(feedback, n)
    box_ll = lt.box_5070_to_ll(box)
    _info(feedback, "Runner DEM: USGS 3DEP 1 m lidar tiles (native TNM tiles) for {:.1f} x {:.1f} km "
          "around the chosen dams ({:.5f}, {:.5f}, {:.5f}, {:.5f}); tile folder ({}): {}".format(
              (box[2] - box[0]) / 1000.0, (box[3] - box[1]) / 1000.0, *box_ll, where, tfolder))
    http = http or lt.UrlHttp()
    try:
        plan = lt.make_plan(box_ll, tfolder, http, feedback)
    except lt.Cancelled:
        raise RunnerDemCancelled("Cancelled while asking The National Map; nothing was downloaded.")
    except Exception as e:                          # noqa
        raise RunnerDemError("The National Map query failed: {}. Check the internet connection and "
                             "run again.".format(e))
    if not plan["tiles"]:
        raise RunnerDemError("The National Map has no 1 m lidar tiles for the chosen dams' area. "
                             "Use '{}' instead.".format(SOURCE_LABELS[SOURCE_10M]))
    for line in lt.describe_plan(plan):
        _info(feedback, line)
    _info(feedback, "Runner DEM: {} native 1 m tile(s), {} in all, {} still to download".format(
        len(plan["tiles"]), _fmt(plan["total_bytes"]), _fmt(plan["need_bytes"])))
    ok, msg = lt.disk_check(plan, max_bytes)
    msg = msg.replace(" (raise 'Largest download')", "")   # that is the Download DEM tool's box
    if plan["need_bytes"] and not ok:
        raise RunnerDemError(
            "Not downloading the 1 m lidar: {} Send fewer dams at a time (or dams closer together), "
            "lower the buffer, raise 'Largest DEM download for one run (GB)', or use '{}'.".format(
                msg, SOURCE_LABELS[SOURCE_10M]))
    _info(feedback, msg)
    if dry:
        _info(feedback, "Runner DEM: validate only - not downloaded; it would be {}".format(vrt))
        return vrt
    try:
        entries, failed = lt.fetch(plan, http, feedback)
    except lt.Cancelled:
        raise RunnerDemCancelled("Cancelled while downloading the 1 m lidar tiles; finished tiles are "
                                 "kept and the current one resumes on the next run. Nothing was run.")
    except Exception as e:                          # noqa
        raise RunnerDemError("Downloading the 1 m lidar tiles failed: {}. Run again to resume.".format(e))
    for f in failed:
        _warn(feedback, f)
    if not entries:
        raise RunnerDemError("No 1 m lidar tile could be downloaded. Run again to resume, or use "
                             "'{}'.".format(SOURCE_LABELS[SOURCE_10M]))
    ten = paths(folder, box, n_dams)["10m"]
    fallback = ten if os.path.isfile(ten) else ""
    os.makedirs(folder, exist_ok=True)
    try:
        vrt, js = lt.build_vrt(entries, vrt, fallback, box_ll, feedback=feedback, meta={
            "area": "7b chosen dams ({}) + buffer".format(int(n_dams)), "tile_folder": tfolder,
            "missing_tiles": failed, "overlap_notes": plan["notes"]})
        info = dd.product_info("tnm1m", 1, {})
        with open(js, encoding="utf-8") as fh:
            info["crs"] = json.load(fh)["crs"]
        dd.write_sidecar(vrt, info)
    except Exception as e:                          # noqa
        raise RunnerDemError("Building the 1 m VRT failed: {}".format(e))
    if failed:
        _warn(feedback, "{} 1 m tile(s) failed; {}".format(
            len(failed), "the 10 m DEM shows there" if fallback else "the VRT has holes there - run again to resume"))
    _info(feedback, "Runner DEM: {} (native 1 m lidar, metres, {})".format(vrt, info["crs"]))
    return vrt


def get_dem(source, points, buffer_mi, folder, max_gb, feedback=None, dry=False, **kw):
    """The Runner DEM for `source` (SOURCE_10M / SOURCE_1M) around `points` (EPSG:5070)."""
    box = dams_box_5070(points, buffer_mi)
    n = len(points)
    max_bytes = int(float(max_gb) * 1024 ** 3) if max_gb else 0
    _info(feedback, "Runner DEM: area = the {} chosen dam(s)' breach points + {:g} mi = EPSG:5070 "
          "({:.0f}, {:.0f}, {:.0f}, {:.0f}); folder {}".format(n, float(buffer_mi or 0), *box, folder))
    if source == SOURCE_1M:
        return get_1m(box, folder, n, max_bytes, feedback, dry, **kw)
    return get_10m(box, folder, n, max_bytes, feedback, dry)
