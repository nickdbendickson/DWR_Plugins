"""Dataset definitions for North Dakota. Since suite 1.18.8 the list is damsuite/updatesources.py,
the same in the ND Data Updater (which adds its offline pack source) and DamSafety Data."""

from .damsuite import updatesources as _US
from .damsuite.updatesources import (GEOFABRIK_URL, MICROSOFT_URL,  # noqa: F401
                                     NSI_GENERATOR_COMMIT, NSI_GENERATOR_ZIP_SHA256)

SOURCE_INFO = _US.source_info()


def build_sources(config, merger=None):
    """config keys: enabled (dict id->bool), usa_structures_url, nsi_interval_days,
    nsi_mode ("merged", "nsi2026", "nsi2022"), nsi_match_m."""
    return _US.build_sources(config, merger=merger, info=SOURCE_INFO)
