"""Design #68: TIER 2 'LISFLOOD-FP then HEC-RAS 2D' is appended; indices 0 and 1, their
labels (as on main 383ca26) and the default are unchanged, and run_dam never treats
TIER_HECRAS as ANUGA."""
import ast
import os

from nd_breach_runner.core import workflow
from nd_breach_runner.tests.registry import test

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN_TIERS = [("lisflood", "LISFLOOD-FP only"),
              ("lisflood+anuga", "LISFLOOD-FP for the extent, then ANUGA (final results from ANUGA)")]


@test
def hecras_tier_appended_and_defaults_unchanged():
    assert workflow.TIERS[:2] == ("lisflood", "lisflood+anuga")
    assert workflow.TIERS[2] == workflow.TIER_HECRAS == "lisflood+hecras"
    assert workflow.RunSettings().tier == workflow.TIER_LISFLOOD
    src = open(os.path.join(os.path.dirname(HERE), "algorithms.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    node = next(n for n in tree.body if isinstance(n, ast.Assign)
                and getattr(n.targets[0], "id", "") == "TIERS")
    got = []
    for el in node.value.elts:
        a, b = el.elts
        got.append((a.attr, ast.literal_eval(b)))
    names = {"TIER_LISFLOOD": "lisflood", "TIER_BOTH": "lisflood+anuga",
             "TIER_HECRAS": "lisflood+hecras"}
    got = [(names[a], b) for a, b in got]
    assert got[:2] == MAIN_TIERS, got
    assert "HEC-RAS 2D" in got[2][1]
    assert 'QgsProcessingParameterEnum("TIER", "Models",\n' \
           '                                                     [t[1] for t in TIERS], defaultValue=0))' in src
    from nd_breach_runner import wizard_logic as wl
    assert wl.TIER_IDS == workflow.TIERS and wl.TIER_LABELS[workflow.TIER_HECRAS]
    wsrc = open(os.path.join(os.path.dirname(HERE), "core", "workflow.py"), encoding="utf-8").read()
    assert "st.tier == TIER_BOTH" in wsrc and "TIER_HECRAS" not in wsrc.split("def run_dam", 1)[1]


@test
def hecras_tier_stage_passes_its_env_to_the_project_writer():
    """run_stage builds the project with its own env, so NDBREACH_HECRAS_INFLOW_MODE (and the
    base flow / tail settings) reach write_project."""
    from nd_breach_runner.core import hecras_engine as he
    src = open(he.__file__, encoding="utf-8").read().split("def run_stage", 1)[1]
    assert "write_project(dam_folder, proj, log=log, env=env)" in src
    assert '"time_step"' in src and '"inflow_series"' in src
