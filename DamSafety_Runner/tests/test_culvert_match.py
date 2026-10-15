"""Culvert matching: a road-crossing guess takes the real size of an NDDOT / NBI inventory
culvert within the match distance (core/culvert_guess.py)."""
import ast
import os
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test as register

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCES = "/workspace/culvert_sources"
FT = 0.3048


def _guess(x, y, name="guess: road 1 crosses channel 1"):
    return {"name": name, "kind": "culvert", "shape": "box", "barrels": 1, "width_m": 1.2,
            "height_m": 0.9, "source": "guess", "surveyed": False,
            "line": [(x - 6.0, y), (x + 6.0, y)], "note": "not surveyed"}


def _nddot(bid, x, y, text, design="Culvert", spans=2):
    from nd_breach_runner.core import culvert_guess as cg
    return cg.inventory_record({"BRIDGE_ID": bid, "DESIGNMAINDESC": design, "CULVERT": text,
                                "TOTAL_SPANS": spans}, (x, y))


@register
def culvert_match_copies_nddot_size_within_distance():
    from nd_breach_runner.core import culvert_guess as cg
    inv = [_nddot("09-118-21.1", 40.0, 0.0, "DOUBLE 16X8X52' PRECAST RCB")]
    rows, notes = cg.match_inventory([_guess(0, 0), _guess(1000, 0)], inv, 75.0)
    m, u = rows
    assert m["source"] == "NDDOT 09-118-21.1" and m["surveyed"] is True
    assert abs(m["match_m"] - 40.0) < 1e-6
    assert abs(m["width_m"] - 16 * FT) < 1e-3 and abs(m["height_m"] - 8 * FT) < 1e-3
    assert m["barrels"] == 2 and m["shape"] == "box"
    assert m["name"].startswith("NDDOT 09-118-21.1")
    assert u["source"] == "guess" and u["surveyed"] is False and "match_m" not in u
    # distance is configurable
    rows, _ = cg.match_inventory([_guess(0, 0)], inv, 30.0)
    assert rows[0]["source"] == "guess"
    # matching twice does not re-match or rename
    again, _ = cg.match_inventory(rows and cg.match_inventory([_guess(0, 0)], inv)[0], inv)
    assert again[0]["name"].count("matched guess") == 1


@register
def culvert_match_bridges_never_size_and_nddot_wins():
    from nd_breach_runner.core import culvert_guess as cg
    br = _nddot("B1", 5.0, 0.0, None, design="Slab", spans=3)
    assert br["kind"] == "bridge"
    nbi = cg.inventory_record({"STRUCTURE_NUMBER_008": "000000009116150",
                               "STRUCTURE_TYPE_043B": "19", "MAIN_UNIT_SPANS_045": 2,
                               "MAX_SPAN_LEN_MT_048": 3.0}, (10.0, 0.0))
    assert nbi["kind"] == "culvert" and nbi["source"] == "NBI" and nbi["barrels"] == 2
    nd = _nddot("09-116-15.0", 60.0, 0.0, "DOUBLE, 10X4X34' RCB")
    rows, _ = cg.match_inventory([_guess(0, 0)], [br, nbi, nd], 75.0)
    assert rows[0]["source"] == "NDDOT 09-116-15.0", rows[0]["source"]
    rows, _ = cg.match_inventory([_guess(0, 0)], [br, nbi], 75.0)
    assert rows[0]["source"] == "NBI 9116150"
    assert abs(rows[0]["width_m"] - 3.0) < 1e-3 and rows[0]["height_m"] == 0.9   # no NBI rise
    rows, _ = cg.match_inventory([_guess(0, 0)], [br], 75.0)
    assert rows[0]["source"] == "guess"
    # 43B: 19 culvert, anything else (02, 4) bridge, leading zeros ignored
    assert cg.nbi_is_culvert("19") and cg.nbi_is_culvert("019") and cg.nbi_is_culvert(19)
    assert not cg.nbi_is_culvert("02") and not cg.nbi_is_culvert("") and not cg.nbi_is_culvert(None)


@register
def culvert_match_one_record_per_guess_and_pipe_arch_shapes():
    from nd_breach_runner.core import culvert_guess as cg
    inv = [_nddot("P", 10.0, 0.0, "SINGLE 48\" X 60' CMP"),
           _nddot("A", 500.0, 0.0, "(2) 117\" X 79\" X 168' CSPA")]
    rows, _ = cg.match_inventory([_guess(0, 0), _guess(20, 0), _guess(510, 0)], inv, 75.0)
    assert rows[0]["source"] == "NDDOT P" and rows[1]["source"] == "guess"   # nearest only
    assert rows[0]["shape"] == "pipe" and rows[0]["width_m"] == rows[0]["height_m"]
    assert rows[2]["shape"] == "box" and "arch" in rows[2]["note"]          # arch -> box
    assert abs(rows[2]["width_m"] - 9.75 * FT) < 1e-3


@register
def culvert_guesses_near_structures_are_dropped():
    from nd_breach_runner.core import culvert_guess as cg
    from nd_breach_runner.core import structures as S
    s = S.Structure("culvert", np.array([(0.0, -5.0), (0.0, 5.0)]), 1.0, 1.0)
    kept, dropped = cg.drop_near_structures([_guess(30, 0), _guess(300, 0)], [s], 75.0)
    assert len(kept) == 1 and len(dropped) == 1 and kept[0]["line"][0][0] == 294.0
    kept, dropped = cg.drop_near_structures([_guess(30, 0)], [s], 10.0)
    assert len(kept) == 1


@register
def culvert_matched_rows_become_structures_and_csv():
    from nd_breach_runner.core import culvert_guess as cg
    inv = [_nddot("09-116-20.0", 0.0, 0.0, "TRIPLE, 10X4X30' RCB")]
    rows, _ = cg.match_inventory([_guess(0, 0), _guess(900, 0)], inv)
    ss = cg.matched_structures(rows)
    assert len(ss) == 1 and ss[0].barrels == 3 and ss[0].surveyed and \
        ss[0].source == "NDDOT 09-116-20.0"
    assert ss[0].corridor_width(1.0) > 3 * 10 * FT
    p = os.path.join(tempfile.mkdtemp(), "c.csv")
    cg.write_candidates_csv(p, rows)
    lines = open(p).read().splitlines()
    assert lines[0].split(",")[5] == "barrels" and lines[0].split(",")[8] == "match_m"
    assert lines[1].split(",")[6] == "NDDOT 09-116-20.0" and lines[1].split(",")[8] == "0.0"
    assert lines[2].split(",")[7] == "no" and len(lines[2].split(",")) == 14


@register
def culvert_parser_is_the_hazard_parser():
    """nddot_text.py is a verbatim copy of ND Dam Hazard's parser functions."""
    hz = os.path.join(HERE, "..", "..", "DamSafety_Hazard", "runnerculverts.py")
    if not os.path.isfile(hz):
        return
    own = os.path.join(HERE, "..", "core", "nddot_text.py")

    def defs(path):
        t = ast.parse(open(path, encoding="utf-8").read())
        out = {}
        for n in t.body:
            if isinstance(n, ast.FunctionDef):
                out[n.name] = ast.dump(n)
            elif isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
                out[n.targets[0].id] = ast.dump(n)
        return out
    a, b = defs(own), defs(hz)
    for k, v in a.items():
        assert b.get(k) == v, "nddot_text.{} differs from DamSafety_Hazard/runnerculverts.py".format(k)


@register
def culvert_inventory_real_gpkgs():
    """The Absaraka-area NDDOT and NTAD NBI extracts (skipped when absent)."""
    from nd_breach_runner.core import culvert_guess as cg
    nd = os.path.join(SOURCES, "nddot_bridge_inventory.gpkg")
    nb = os.path.join(SOURCES, "ntad_nbi.gpkg")
    if not (os.path.isfile(nd) and os.path.isfile(nb)):
        return
    a = cg.read_inventory_gpkg(nd)
    assert len(a) == 16 and sum(r["kind"] == "culvert" for r in a) == 8
    swan = [r for r in a if r["id"] == "09-118-21.1"][0]
    assert (swan["span_ft"], swan["rise_ft"], swan["barrels"], swan["shape"]) == (16.0, 8.0, 2, "box")
    assert all(r["span_ft"] for r in a if r["kind"] == "culvert")
    b = cg.read_inventory_gpkg(nb)
    assert len(b) == 16 and sum(r["kind"] == "culvert" for r in b) == 7   # 43B = 19
    seen = []
    cg.read_inventory_gpkg(nd, lambda srs: seen.append(srs) or (lambda x, y: (x + 1.0, y)))
    assert seen == [5070]
    # a guess 50 m from NDDOT 09-116-15.0 takes its 2 x 10 x 4 ft RCB
    r = [x for x in a if x["id"] == "09-116-15.0"][0]
    rows, _ = cg.match_inventory([_guess(r["x"] + 50.0, r["y"])], a + b, 75.0)
    assert rows[0]["source"] == "NDDOT 09-116-15.0" and rows[0]["barrels"] == 2
    assert abs(rows[0]["height_m"] - 4 * FT) < 1e-3
