# -*- coding: utf-8 -*-
"""The ND DWR export files as offline-pack data sets. Since suite 1.18.6 this module IS
damsuite/dwrfiles.py (sys.modules alias) - edit DamSafety_Core/damsuite/dwrfiles.py in DWR_Plugins."""
import sys

if __package__:
    from .damsuite import dwrfiles as _M
else:                                # build_nd_pack.py run as a script: the plugin folder
    from damsuite import dwrfiles as _M     # noqa: E402
sys.modules[__name__] = _M
