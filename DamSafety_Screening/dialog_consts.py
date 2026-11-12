# -*- coding: utf-8 -*-
"""Constants of the DamSafety Screening popup window shared by dialog.py and its tab mixins
(settings_tab.py, review_tab.py, results_tab.py). 0.18.19: moved here unchanged from dialog.py,
which re-exports them, so `from .dialog import TITLE` still works."""
import os

from .tokens import TOKENS

TITLE = "DamSafety Screening"
SCOPE = "NDDamScreening"          # QgsProject entry scope
ENTRY = "state"
STATE_TEXT = {"done": "\u2713 done", "stale": "\u21bb out of date", "ready": "\u25cf ready",
              "waiting": "\u2026 waiting", "optional": "\u25cb optional"}
DISCLAIMER = ("Screening-level results for prioritising reviews. Every threshold is a screening "
              "value you choose; the official hazard classification is the North Dakota "
              "Department of Water Resources'. Nothing here has been checked against the State's "
              "forms.")
SCREEN_WHAT = ("DWR dam inventory (steps 1-4)", "Ponds and dugouts from a water layer (step 16)",
               "Both (steps 1-4 and 16, merged)")
HELP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "help", "how_it_decides.html")
PRESETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presets")
REVIEW_COLUMNS = ("Dam", "Name", "Why", "From", "Decision")
HINT_STYLE = "color: {};".format(TOKENS["error"])
