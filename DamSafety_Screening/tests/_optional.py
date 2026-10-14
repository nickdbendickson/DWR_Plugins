"""Skip a test script when an optional dependency of the plugin is not installed here
(QGIS ships scipy; a bare Python may not). Exit 0 with a SKIP line, so the scripts runner
counts it as skipped rather than failed; under pytest it is a skip."""
import importlib.util
import sys


def need(module, why=""):
    if importlib.util.find_spec(module) is not None:
        return
    msg = "SKIP: {} is not installed{}".format(module, " ({})".format(why) if why else "")
    if "pytest" in sys.modules:
        import pytest
        pytest.skip(msg, allow_module_level=True)
    print(msg)
    sys.exit(0)
