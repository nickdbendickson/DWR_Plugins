"""The Screening window does not write its default choices into the shared settings file:
only what the engineer changed goes in (opening the window used to add every window key)."""
import os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_dam_screening.core import workflow as wf  # noqa: E402
from nd_dam_screening.damsuite import settings as DS  # noqa: E402

sf = wf.window_settings_to(DS.Settings({}), dict(wf.DEFAULT_SETTINGS), defaults=False)
assert sf.window(wf.PLUGIN_KEY) == {}, sf.window(wf.PLUGIN_KEY)

# a changed choice is written; a stored one set back to its default is updated, not dropped
k = next(k for k in wf.WINDOW_KEYS if isinstance(wf.DEFAULT_SETTINGS.get(k), (int, float))
         and not isinstance(wf.DEFAULT_SETTINGS.get(k), bool))
changed = dict(wf.DEFAULT_SETTINGS, **{k: wf.DEFAULT_SETTINGS[k] + 1})
sf = wf.window_settings_to(DS.Settings({}), changed, defaults=False)
assert sf.window(wf.PLUGIN_KEY) == {k: wf.DEFAULT_SETTINGS[k] + 1}, sf.window(wf.PLUGIN_KEY)
sf = wf.window_settings_to(sf, dict(wf.DEFAULT_SETTINGS), defaults=False)
assert sf.window(wf.PLUGIN_KEY) == {k: wf.DEFAULT_SETTINGS[k]}
# a run's record and a new file keep every choice
assert wf.window_settings_to(DS.Settings({}), dict(wf.DEFAULT_SETTINGS)).window(wf.PLUGIN_KEY)
print("test_window_defaults_not_saved: ok")
