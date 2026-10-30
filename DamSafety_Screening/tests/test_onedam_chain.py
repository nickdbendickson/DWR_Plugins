"""0.18.1: the one-dam (Crest ND00199) chain findings of 2026-10-30.

(a) step 1 takes the DWR / NID layers the way the preflight finds them (suite folder), not only
    from explicit dwr / nid keys; (b) the preflight disk estimate counts only the only_ids dams;
(c) tool 15 writes its chain log line by line, so a killed run leaves one; DAM_ID (as the ND copy)
    sets only_ids; (d) tool 6 keeps the dam when ONLY_IDS is its NID id (ND00199) and tool 1
    named it by its DWR id (DWR-2801).
"""
import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
sys.path.insert(0, HERE)
from qgis.core import QgsProcessingContext, QgsProcessingFeedback  # noqa: E402
from qgis import processing  # noqa: E402
import nd_dam_screening as pkg  # noqa: E402
from nd_dam_screening.core import workflow as wf, preflight as pf  # noqa: E402
from nd_dam_screening.algorithms import run_chain as rc  # noqa: E402
from nd_dam_screening.damsuite import suite_fill  # noqa: E402

W = tempfile.mkdtemp(prefix="onedam_")
SUITE = os.path.join(W, "suite"); os.makedirs(SUITE)
DWR = os.path.join(SUITE, "DWR_dams.gpkg") + "|layername=DWR_dams"
NID = os.path.join(SUITE, "NID_dams.gpkg") + "|layername=NID_dams"
real_lfs = suite_fill.layers_for_settings
suite_fill.layers_for_settings = lambda s: ({"dwr": DWR, "nid": NID}
                                            if (s or {}).get("suite_folder") == SUITE else {})

# ---- (a) suite_folder only: step 1 gets both layers (it used to get neither)
S = {"suite_folder": SUITE, "only_ids": "ND00199"}
p = wf.defaults("1", W, S)
assert p.get("DWR") == DWR and p.get("NID") == NID and p.get("ONLY_IDS") == "ND00199", p
assert pf._slot(dict(wf.DEFAULT_SETTINGS, **S), W, "dwr") == DWR       # the preflight's answer
# an Inputs choice still wins
p = wf.defaults("1", W, dict(S, dwr="/own.gpkg"))
assert p["DWR"] == "/own.gpkg" and p["NID"] == NID

# ---- (b) the disk estimate: the only_ids dams, not the whole layer
assert pf.only_ids_count({"only_ids": "ND00199"}) == 1
assert pf.only_ids_count({"only_ids": " nd00199, DWR-2801 ,ND00199"}) == 2
assert pf.only_ids_count({"only_ids": ""}) == 0 and pf.only_ids_count({}) == 0


class Env(pf.Env):
    def url_ok(self, url):
        return True, "stubbed"

    def disk_free(self, path):
        return 1 * 1024 ** 3                                     # 1 GB: short for 10,088 dams

    def layer_info(self, ref):
        if ref == DWR:
            return {"fields": ["structure_"], "count": 10088, "crs": "EPSG:4326", "geometry": "point"}
        return None


def disk(settings):
    c = {x["title"]: x for x in pf.run_checks(W, dict(wf.DEFAULT_SETTINGS, **settings), steps=["1"], env=Env())}
    return c["Disk space"]


d = disk({"suite_folder": SUITE})
assert "10088 dams" in d["detail"] and d["status"] == pf.WARN, d
d = disk(S)
assert "for 1 dams" in d["detail"] and "10088" not in d["detail"], d

# ---- (c) the chain log while the chain runs; DAM_ID
plug = pkg.classFactory(None); plug.initGui()
algs = {a.name(): a for a in plug.provider.algs}
for a in algs.values():
    a.initAlgorithm()
processing.REGISTRY.update(algs)
LOG = os.path.join(W, "chain_log.txt")
seen = []


def killed(aid, params, context=None, feedback=None, is_child_algorithm=False):
    seen.append((aid, dict(params), open(LOG, encoding="utf-8").read()))
    raise RuntimeError("killed")


rc.processing.run = killed
alg = algs["runchain"].createInstance()
res = alg.run_mock({"WORK": W, "SETTINGS": json.dumps({"suite_folder": SUITE}), "DAM_ID": "ND00199",
                    "PREFLIGHT": False, "DRY_RUN": False, "FAIL_ON_ERROR": False, "LOG": LOG},
                   QgsProcessingContext(), QgsProcessingFeedback())
assert res["OK"] is False and seen, res
aid, params, mid = seen[0]
assert aid.endswith(wf.ALG["1"]) and params["ONLY_IDS"] == "ND00199" and params["DWR"] == DWR, params
assert "DAM_ID filter: ND00199" in mid and "Plan:" in mid, mid     # on disk before step 1 ended
assert "killed" in open(LOG, encoding="utf-8").read()

# ---- (d) tool 6's ONLY_IDS: the NID id still finds the dam tool 1 named DWR-2801
from nd_dam_screening.algorithms import triage as tri  # noqa: E402


class _F(dict):
    def id(self):
        return 7


f = _F(dam_id="DWR-2801", nid_id="ND00199")
names = ["dam_id", "nid_id"]
assert tri._wanted(f, names, {"ND00199"}) and tri._wanted(f, names, {"DWR-2801"})
assert tri._wanted(f, names, {"ND00199"}, "DWR-2801")
assert not tri._wanted(f, names, {"ND00200"}) and tri._wanted(f, names, set())
assert not tri._wanted(_F(dam_id="DWR-2801"), ["dam_id"], {"ND00199"})  # no nid_id column: dam id only

suite_fill.layers_for_settings = real_lfs
print("ok test_onedam_chain")
