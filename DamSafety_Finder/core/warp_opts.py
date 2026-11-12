"""gdal.Warp without the source's overviews (Runner 3.10.43 / Screening 0.17.35).

By default gdal.Warp / gdalwarp (-ovr AUTO) reads the source's overview level closest to the
target cell when the target is coarser than the source. A 1 m lidar tile or a VRT of them
(USGS 3DEP COGs carry internal overviews, and a VRT of tiles exposes them as implicit
overviews) warped to a 5 m or 10 m grid is then resampled from the 2 m / 4 m / 8 m overview,
not from the 1 m cells. The overviews were built by someone else with an unknown resampling,
so an 'average' warp is not the average of the lidar cells and a 'bilinear' one samples
pre-smoothed (or pre-decimated) values. no_overviews() makes the warp read the full-resolution
data (gdalwarp -ovr NONE).

no_vshift() (Runner 3.10.89 / Screening 0.17.50) keeps the values as stored when a CRS is
compound (gdalwarp -novshift): the tools convert DEM units themselves.

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


def _vshift_known(gdal):
    """True when gdalwarp has a vertical shift to switch off (GDAL 3.4 and later)."""
    try:
        return int(gdal.VersionInfo("VERSION_NUM")) >= 3040000
    except (AttributeError, TypeError, ValueError):
        return False


def no_vshift(gdal, kw=None):
    """`kw` (gdal.WarpOptions / gdal.Warp keywords, changed in place and returned) with
    gdalwarp's vertical shift switched off ('-novshift' in `options`; Runner 3.10.89 /
    Screening 0.17.50). Since GDAL 3.4 a warp whose source or target CRS is compound applies
    the vertical transformation by default, and that converts the values: a DEM in
    'NAD83 / ND North (ftUS) + NAVD88 height (ftUS)' warped to a 2D UTM CRS comes out in metres
    (x 0.3048, the band unit left at 'US survey foot'), a band-'ft' DEM warped into a compound
    metre CRS is converted too, and a raster with no unit warped into a compound feet CRS is
    multiplied by 3.28 (an NLCD class raster as well). The tools apply the DEM's unit
    themselves (z_to_m, a scale VRT), so the warp must keep the values as stored, or the
    unit is applied twice. GDAL before 3.4 (no vertical shift) and a `gdal` without
    WarpOptions (a test double) get `kw` unchanged."""
    kw = {} if kw is None else kw
    if not hasattr(gdal, "WarpOptions") or not _vshift_known(gdal):
        return kw
    opts = kw.get("options") or []
    if isinstance(opts, str):
        opts = opts.split()
    if "-novshift" not in opts:
        kw["options"] = list(opts) + ["-novshift"]
    return kw
