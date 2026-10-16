"""Design #68: adding 'LISFLOOD-FP then HEC-RAS 2D' (ENGINE / TIER 2) must not change what
indices 0 and 1 send to the Runner. The golden file was written from main 383ca26's
core/handoff.py before the change."""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
from nd_dam_screening.core import handoff as ho  # noqa: E402

G = json.load(open(os.path.join(HERE, "golden_engine_main_383ca26.json"), encoding="utf-8"))
assert ho.ENGINE_LABELS[:2] == G["ENGINE_LABELS"] and ho.ENGINE_SHORT[:2] == G["ENGINE_SHORT"]
assert len(ho.ENGINE_LABELS) == len(ho.ENGINE_SHORT) == 3 and "HEC-RAS 2D" in ho.ENGINE_LABELS[2]
for k, v in G["effective_engine"].items():
    e, f = k.split(",")
    assert ho.effective_engine(int(e), f == "True") == v, k
for args, want in G["engine_params"]:
    got = list(ho.engine_params(*args))
    assert json.loads(json.dumps(got)) == want, (args, got, want)
for k, v in G["engine_status"].items():
    e, c = k.split(",")
    assert ho.engine_status(int(e), None if c == "None" else c) == v, k
# the new choice
assert ho.effective_engine(2) == 2 and ho.effective_engine(2, True) == 1
p, notes = ho.engine_params(2, False, False, "c.gpkg", {})
assert p == {"TIER": 2, "STRUCTURES": "c.gpkg"} and ho.CULVERTS_IGNORED not in notes, (p, notes)
assert "SA/2D" in ho.engine_status(2, "x")
assert not ho.hecras_configured({}) and ho.hecras_configured(
    {k: "/" for k in ho.HECRAS_ENV}, isdir=lambda p: True)
# the Runner's own TIER list (parsed, no QGIS needed): 0 / 1 unchanged, default 0
src = open(os.path.join(os.path.dirname(PLUGIN), "DamSafety_Runner", "core", "workflow.py"),
           encoding="utf-8").read() if os.path.isdir(os.path.join(os.path.dirname(PLUGIN), "DamSafety_Runner")) else ""
if src:
    assert 'TIERS = (TIER_LISFLOOD, TIER_BOTH, TIER_HECRAS)' in src
print("ok engine golden (indices 0/1 identical to main 383ca26; index 2 = HEC-RAS 2D)")
