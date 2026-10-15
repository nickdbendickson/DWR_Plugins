# The tests must never read or write the engineer's real suite settings file: a shell that
# exports NDSUITE_SETTINGS (the box's hydro-env.sh does) made the popup tests read its CELL = 40
# and let a test autosave rewrite that file.
import os as _os
_os.environ.pop("NDSUITE_SETTINGS", None)
