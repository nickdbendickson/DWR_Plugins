"""DamSafety Core security: a saved project cannot choose which solver program the suite runs.

suite_config.get()/source() read the project entry first for data keys, but NEVER for the
executable-path keys (lisflood_exe, anuga_python) - those come only from the per-computer setting
or the environment variable, so opening a shared .qgs/.qgz can't make the suite run an
attacker-named binary.

    python3 DamSafety_Core/tests/test_suite_config_exe_keys.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from DamSafety_Core.damsuite import suite_config as sc


class FakeProject:
    """A malicious saved project: every NDDamSuite/<key> read returns the attacker's value."""

    def __init__(self, values):
        self._values = values

    def readEntry(self, scope, key):
        if key in self._values:
            return self._values[key], True
        return "", False


def main():
    evil = FakeProject({
        "lisflood_exe": "/tmp/evil_payload",
        "anuga_python": "/tmp/evil_python",
        "data_folder": "/tmp/project_data",
    })
    for var in ("NDBREACH_LISFLOOD", "NDBREACH_ANUGA_PYTHON", "NDSUITE_DATA_FOLDER"):
        os.environ.pop(var, None)

    # executable keys ignore the project entirely (no computer setting / env here -> "")
    assert sc.get("lisflood_exe", project=evil) == "", sc.get("lisflood_exe", project=evil)
    assert sc.get("anuga_python", project=evil) == "", sc.get("anuga_python", project=evil)
    assert sc.source("lisflood_exe", project=evil) != "project", sc.source("lisflood_exe", project=evil)
    assert sc.source("anuga_python", project=evil) != "project", sc.source("anuga_python", project=evil)

    # the user's own env var (a trusted, per-machine choice) is still honored for an executable key
    os.environ["NDBREACH_LISFLOOD"] = "/opt/lisflood/lisflood"
    assert sc.get("lisflood_exe", project=evil) == "/opt/lisflood/lisflood", sc.get("lisflood_exe", project=evil)
    assert sc.source("lisflood_exe", project=evil) == "env", sc.source("lisflood_exe", project=evil)
    os.environ.pop("NDBREACH_LISFLOOD", None)

    # a non-executable key STILL takes the project override (data locations from a project are fine)
    assert sc.get("data_folder", project=evil) == "/tmp/project_data", sc.get("data_folder", project=evil)
    assert sc.source("data_folder", project=evil) == "project", sc.source("data_folder", project=evil)

    print("test_suite_config_exe_keys: all tests passed")


if __name__ == "__main__":
    main()
