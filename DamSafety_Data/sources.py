"""Dataset definitions for North Dakota. Since suite 1.18.8 the list is damsuite/updatesources.py,
the same in the ND Data Updater and DamSafety Data; each adds its own offline pack source (here
packsource.py, 1.7.0: the pack Build offline pack made, kept up to date)."""

from .damsuite import updatesources as _US
from .damsuite.updatesources import (GEOFABRIK_URL, MICROSOFT_URL,  # noqa: F401
                                     NSI_GENERATOR_COMMIT, NSI_GENERATOR_ZIP_SHA256)

PACK_INFO = ("Offline pack (keep up to date)",
             "Keeps the pack made by Build offline pack… up to date, in both formats "
             "(ND_Dam_Suite_Pack and nd_offline_pack): layers that are missing or not complete "
             "are built, layers older than the refresh interval are fetched again, the rest is "
             "left alone. Layers from your own DWR / HIFLD files are not fetched over. Starts "
             "nothing on its own: build the pack once in that window first.", True)
SOURCE_INFO = _US.source_info(PACK_INFO)


def pack_source(config, info):
    """The pack source, or None when no pack was built in the Build offline pack folder.

    1.11.2: else the folder of the pack the plugins read (`pack_active`, either format's
    folder), when Build offline pack made it. A pack built from Terminal (python -m
    DamSafety_Data.pack_build, the installer's build-offline-pack.sh) never recorded its folder
    in the window's setting, so "Check for updates now" did not look after it, and the window
    refuses to write into the pack in use: there was no way left to update it in place."""
    import os
    from . import packsource as PS
    cands = []
    out = (config.get("pack_out") or "").strip()
    if out:
        cands.append(out)
    active = (config.get("pack_active") or "").strip()
    if active:
        cands.append(os.path.dirname(os.path.normpath(active)))
    for c in cands:
        try:
            if PS.built_here(c):
                out = c
                break
        except Exception:                                        # noqa: BLE001 - no GDAL etc.
            continue
    else:
        return None
    return PS.DataPackSource(
        "pack", info["pack"][0], out, refresh_days=config.get("pack_refresh_days"),
        description=info["pack"][1], dwr_folder=config.get("pack_dwr_folder") or None,
        hifld_folder=config.get("pack_hifld_folder") or None)


def build_sources(config, merger=None):
    """config keys: enabled (dict id->bool), usa_structures_url, nsi_interval_days,
    nsi_mode ("merged", "nsi2026", "nsi2022"), nsi_match_m, pack_out (the Build offline pack
    folder), pack_active (the pack the plugins read; its folder is used when pack_out holds no
    pack built here), pack_refresh_days, pack_dwr_folder, pack_hifld_folder."""
    return _US.build_sources(config, merger=merger, pack=pack_source, info=SOURCE_INFO)
