#!/usr/bin/env python3
"""Where this Python finds QGIS, as lines for GitHub Actions' environment file.

    python tools/ci_qgis_env.py >> "$GITHUB_ENV"

On Linux and macOS conda-forge puts `qgis` where Python looks. On Windows it is under
<environment>/Library/python, which Python does not search: every `import qgis` fails until
PYTHONPATH names that folder. This prints PYTHONPATH (QGIS's Python folder and its plugins
folder, for `processing`) and QGIS_PREFIX_PATH when `qgis` is not importable as it is, and
nothing when it is. Exit status 1 when QGIS is not in this environment at all."""
import glob
import importlib.util
import os
import sys


def find(prefix):
    """The folder that holds the `qgis` package under `prefix`, or None. The shortest path
    wins (a copy inside another package's folder is deeper)."""
    hits = glob.glob(os.path.join(glob.escape(prefix), "**", "qgis", "core", "__init__.py"),
                     recursive=True)
    hits = sorted((os.path.dirname(os.path.dirname(os.path.dirname(h))) for h in hits), key=len)
    return hits[0] if hits else None


def lines(pydir, old_path="", sep=os.pathsep):
    """The environment lines for a QGIS Python folder such as <env>/Library/python."""
    parts = [pydir, os.path.join(pydir, "plugins")] + ([old_path] if old_path else [])
    return ["PYTHONPATH=" + sep.join(parts), "QGIS_PREFIX_PATH=" + os.path.dirname(pydir)]


def main():
    if importlib.util.find_spec("qgis") is not None:
        sys.stderr.write("ci_qgis_env: qgis is importable as it is\n")
        return 0
    pydir = find(sys.prefix)
    if not pydir:
        sys.stderr.write("ci_qgis_env: no qgis package under {}\n".format(sys.prefix))
        return 1
    sys.stderr.write("ci_qgis_env: qgis found in {}\n".format(pydir))
    print("\n".join(lines(pydir, os.environ.get("PYTHONPATH", ""))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
