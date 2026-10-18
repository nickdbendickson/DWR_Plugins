# -*- coding: utf-8 -*-
"""Severity-ranked view of checks.check_all() results (2.18.32).

Since 2.18.33 the panel lives in ui_widgets.py (the widgets the DamSafety plugins share,
an identical copy in each plugin); this module re-exports it so existing imports keep working.
"""
from .ui_widgets import (BANNER_QSS, WARN_PILL_QSS, CheckPanel, Collapsible,  # noqa: F401
                         _plural)
