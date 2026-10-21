"""gdal.Warp without the source's overviews (Runner 3.10.43 / Screening 0.17.35).

By default gdal.Warp / gdalwarp (-ovr AUTO) reads the source's overview level closest to the
target cell when the target is coarser than the source. A 1 m lidar tile or a VRT of them
(USGS 3DEP COGs carry internal overviews, and a VRT of tiles exposes them as implicit
overviews) warped to a 5 m or 10 m grid is then resampled from the 2 m / 4 m / 8 m overview,
not from the 1 m cells. The overviews were built by someone else with an unknown resampling,
so an 'average' warp is not the average of the lidar cells and a 'bilinear' one samples
pre-smoothed (or pre-decimated) values. no_overviews() makes the warp read the full-resolution
data (gdalwarp -ovr NONE).

Same file in DamSafety_Runner/core and DamSafety_Screening/core (tools/ci_checks.py keeps them
byte-identical). No QGIS imports."""


def _supports_kw(gdal):
    try:
        import inspect
        return "overviewLevel" in inspect.signature(gdal.WarpOptions).parameters
    except (TypeError, ValueError, AttributeError):
        return False


def no_overviews(gdal, kw=None):
    """`kw` (a dict of gdal.WarpOptions / gdal.Warp keywords, changed in place and returned)
    with the source overviews switched off: overviewLevel='NONE' where the bindings have the
    keyword (GDAL 3.8 in QGIS 3.34 here, 3.13 in QGIS 4), else '-ovr NONE' in `options`
    (gdalwarp has had -ovr since GDAL 2.0). A `gdal` without WarpOptions (the Screening
    tests' GDAL double) gets `kw` unchanged."""
    kw = {} if kw is None else kw
    if not hasattr(gdal, "WarpOptions"):
        return kw            # a test double without warp options (real GDAL has had them since 2.1)
    if _supports_kw(gdal):
        kw["overviewLevel"] = "NONE"
    else:
        opts = kw.get("options") or []
        if isinstance(opts, str):
            opts = opts.split()
        kw["options"] = list(opts) + ["-ovr", "NONE"]
    return kw
