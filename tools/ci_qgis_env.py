#!/usr/bin/env python3
"""Make QGIS importable in this Python on a GitHub Actions runner.

    python tools/ci_qgis_env.py >> "$GITHUB_ENV"

On Linux and macOS conda-forge puts `qgis` where Python looks. On Windows it is under
<environment>/Library/python, which Python does not search: every `import qgis` fails.

When `qgis` is not importable this writes a path file (ci_qgis.pth) into this Python's
site-packages that names QGIS's Python folder and its plugins folder (for `processing`), and
prints QGIS_PREFIX_PATH for the environment file. When it is importable it does nothing.
Exit status 1 when QGIS is not in this environment at all.

Why a path file and not PYTHONPATH: each step's shell activates the environment again, and
QGIS's own activation script puts its folders in front of PYTHONPATH joined with ":" (it is a
shell script). Windows Python splits PYTHONPATH on ";", so the first folder of any value set
here ended up glued to that text and was not found (the second run of os-tests.yml)."""
import glob
import importlib.util
import os
import sys
import sysconfig


def find(prefix):
    """The folder that holds the `qgis` package under `prefix`, or None. The shortest path
    wins (a copy inside another package's folder is deeper)."""
    hits = glob.glob(os.path.join(glob.escape(prefix), "**", "qgis", "core", "__init__.py"),
                     recursive=True)
    hits = sorted((os.path.dirname(os.path.dirname(os.path.dirname(h))) for h in hits), key=len)
    return hits[0] if hits else None


PTH_NAME = "ci_qgis.pth"


def write_pth(pydir, site_dir):
    """The path file: one folder per line. Returns its path."""
    path = os.path.join(site_dir, PTH_NAME)
    with open(path, "w", encoding="utf-8") as f:
        f.write(pydir + "\n" + os.path.join(pydir, "plugins") + "\n")
    return path


def lines(pydir):
    """The environment lines for a QGIS Python folder such as <env>/Library/python."""
    return ["QGIS_PREFIX_PATH=" + os.path.dirname(pydir)]


def main():
    if importlib.util.find_spec("qgis") is not None:
        sys.stderr.write("ci_qgis_env: qgis is importable as it is\n")
        return 0
    pydir = find(sys.prefix)
    if not pydir:
        sys.stderr.write("ci_qgis_env: no qgis package under {}\n".format(sys.prefix))
        return 1
    pth = write_pth(pydir, sysconfig.get_paths()["purelib"])
    sys.stderr.write("ci_qgis_env: qgis found in {}; wrote {}\n".format(pydir, pth))
    print("\n".join(lines(pydir)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
