"""3.14.1 (DWR-10554, Nick: "do culvert ideas"): OpenStreetMap culverts in the STRUCTURES
layer (ND Dam Hazard's runner_culverts, source "OSM") are read as MAPPED, NOT SURVEYED
structures labelled with their OSM way; culvert prep can build the layer with OSM."""
import os

import numpy as np

from nd_breach_runner.tests.registry import test as register

HERE = os.path.dirname(os.path.abspath(__file__))


@register
def osm_structures_are_labelled_mapped_not_surveyed():
    from nd_breach_runner.core import structures as S
    line = np.array([[-20.0, 0.0], [20.0, 0.0]])
    s = S.label_source(S.Structure("culvert", line, name="OSM way/492688522 culvert"),
                       {"SOURCE": "OSM", "src_id": "way/492688522"})
    assert s.source == "OSM way/492688522" and s.surveyed is False
    row = S.describe([s])[0]
    assert "mapped in OpenStreetMap (OSM way/492688522), size not surveyed" in row, row
    for at in ({"source": "NBI", "src_id": "1"}, {"source": "NDDOT"}, {}):
        t = S.label_source(S.Structure("culvert", line), at)
        assert t.source == "inventory" and t.surveyed is True, at
    g = S.Structure("culvert", line, name="guess: road 1")
    g.surveyed = False
    assert "not surveyed (a guess" in S.describe([g])[0]


@register
def osm_structures_reach_both_readers_and_culvert_prep():
    alg = open(os.path.join(HERE, "..", "algorithms.py")).read()
    assert "structmod.label_source(structs[-1], at)" in alg
    assert '("source", "src_id")' in alg            # the fields are read, not filtered out
    cli = open(os.path.join(HERE, "..", "scripts", "ndbreach_cli.py")).read()
    assert "structmod.label_source(structs[-1], at)" in cli
    import ast
    tree = ast.parse(open(os.path.join(HERE, "..", "algorithms_tools.py")).read())
    keys = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "BUILD_LAYERS"
                                                for t in node.targets):
            keys = [e.elts[0].value for e in node.value.elts]
    assert keys and "OSM" in keys, keys
