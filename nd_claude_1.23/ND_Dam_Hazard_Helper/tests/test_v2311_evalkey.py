"""2.31.1: the evaluation cache key follows the auto-discovered point layers and the suite /
offline environment variables (a changed project layer or ND_DAM_SUITE is a new evaluation)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)
if os.path.dirname(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, os.path.dirname(PLUGIN_DIR))
from ND_Dam_Hazard_Helper.tests.registry import test  # noqa: E402


class _Lyr(object):
    def __init__(self, name, n=3):
        self._n, self._c = name, n

    def name(self):
        return self._n

    def source(self):
        return "memory?" + self._n

    def featureCount(self):
        return self._c

    def getFeatures(self):
        return iter(())


@test
def evalkey_follows_discovered_layers_and_env():
    from ND_Dam_Hazard_Helper import pipeline as P
    found = {"NSI": [_Lyr("NSI points")], "NDGISHUB": []}
    orig = P.discover_project_layers
    saved = {k: os.environ.pop(k, None) for k in P.EVAL_ENV_VARS}
    P.discover_project_layers = lambda: found
    try:
        cfg = {"point_sources": "auto", "output_folder": "/tmp/out", "scenarios": []}
        k0 = P.evaluation_key(cfg)
        assert P.evaluation_key(cfg) == k0
        found["NSI"] = [_Lyr("NSI points", n=4)]            # the layer was edited
        k1 = P.evaluation_key(cfg)
        assert k1 != k0
        found["NDGISHUB"] = [_Lyr("GISHUB address points")]  # a layer was added
        k2 = P.evaluation_key(cfg)
        assert k2 != k1
        os.environ["ND_DAM_SUITE"] = "/suite"
        k3 = P.evaluation_key(cfg)
        assert k3 != k2
        del os.environ["ND_DAM_SUITE"]
        os.environ["ND_DAM_OFFLINE"] = "1"
        assert P.evaluation_key(cfg) not in (k2, k3)
        del os.environ["ND_DAM_OFFLINE"]
        assert P.evaluation_key(cfg) == k2
        # an explicit list is not affected by what is in the project
        cfg2 = dict(cfg, point_sources=[("NSI", _Lyr("x"))])
        a = P.evaluation_key(cfg2)
        found["NSI"] = []
        assert P.evaluation_key(cfg2) == a
        assert "lives in jeopardy" in P.GOVERNING_RULE
    finally:
        P.discover_project_layers = orig
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
