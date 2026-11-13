"""damsuite.shared_defaults + suite_settings_tools.apply_shared_defaults (Core 1.2.0): one
defaults file every tool starts from. No QGIS.

    python3 DamSafety_Core/tests/test_shared_defaults.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from DamSafety_Core.damsuite import shared_defaults as SD  # noqa: E402

TMP = tempfile.mkdtemp(prefix="shared_defaults_")


def _user(name, values=None, raw=None):
    p = os.path.join(TMP, name)
    with open(p, "w", encoding="utf-8") as f:
        f.write(raw if raw is not None else json.dumps({"format": SD.FORMAT, "format_version": 1,
                                                       "values": values or {}}))
    return p


def test_shipped_file_holds_every_key_with_a_valid_value():
    vals, problems = SD.read(SD.shipped_path())
    assert problems == [], problems
    assert set(vals) == set(SD.KEYS), vals
    # the suite's decisions as of Core 1.2.0 (Nick 2026-11-13: Manning's n 0.06; pool convention
    # of ND Runner 3.29.4 / DWR Runner 3.16.4)
    assert vals == {"sunny_day_start_pool": "max_pool",
                    "pmf_breach_volume": "storage_at_start_pool",
                    "failure_mode": "piping", "breach_method": "froehlich2008",
                    "manning_n": 0.06}, vals


def test_your_file_overrides_only_its_keys():
    d = SD.load(user=_user("a.json", {"manning_n": 0.045}))
    assert d.get("manning_n") == 0.045 and d.source["manning_n"] == "yours"
    assert d.get("breach_method") == "froehlich2008" and d.source["breach_method"] == "suite"
    assert d.problems == []


def test_missing_your_file_is_the_suite_values():
    d = SD.load(user=os.path.join(TMP, "none.json"))
    assert d.values == d.suite and all(s == "suite" for s in d.source.values())


def test_bad_values_are_reported_and_left_out():
    d = SD.load(user=_user("b.json", {"manning_n": 9, "failure_mode": "sideways", "colour": "red",
                                      "breach_method": "Froehlich (1995)"}))
    assert d.get("manning_n") == 0.06 and d.get("failure_mode") == "piping"
    assert d.get("breach_method") == "froehlich1995"        # a label is accepted
    text = " ".join(d.problems)
    assert "manning_n" in text and "failure_mode" in text and "colour" in text, d.problems


def test_unreadable_your_file_never_raises():
    d = SD.load(user=_user("c.json", raw="{not json"))
    assert d.values == d.suite and d.problems and "not valid JSON" in d.problems[0]


def test_save_user_writes_only_the_changes():
    p = os.path.join(TMP, "sub", "mine.json")
    vals = dict(SD.load(user=p).suite, failure_mode="overtopping")
    SD.save_user(vals, p)
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    assert data["format"] == SD.FORMAT and data["values"] == {"failure_mode": "overtopping"}, data
    assert SD.load(user=p).get("failure_mode") == "overtopping"
    SD.save_user({}, p)                                         # "Use the suite values"
    assert SD.load(user=p).get("failure_mode") == "piping"
    try:
        SD.save_user({"manning_n": "x"}, p)
    except SD.DefaultsError:
        pass
    else:
        raise AssertionError("a bad value was saved")


def test_env_names_your_file():
    p = _user("env.json", {"manning_n": 0.05})
    old = os.environ.get(SD.ENV_PATH)
    os.environ[SD.ENV_PATH] = p
    try:
        assert SD.user_path() == p
        assert SD.value("manning_n") == 0.05
    finally:
        if old is None:
            os.environ.pop(SD.ENV_PATH, None)
        else:
            os.environ[SD.ENV_PATH] = old


def test_runner_method_round_trip():
    for mode in ("piping", "overtopping"):
        for m in SD.SCHEMA["breach_method"]["choices"]:
            rid = SD.runner_method({"failure_mode": mode, "breach_method": m})
            assert rid, (mode, m)
            back_mode, back_m = SD.from_runner_method(rid)
            assert back_m == m and back_mode in (mode, None), (rid, back_mode, back_m)
    assert SD.from_runner_method("user") == (None, None)


def test_assumptions_lists_where_a_run_differs():
    d = SD.Defaults(dict(manning_n=0.06, breach_method="froehlich2008", failure_mode="piping"))
    e = SD.assumptions({"manning_n": 0.08, "breach_method": "froehlich2008", "failure_mode": None,
                        "not_a_key": 1}, "ndbreach", "3.18.0", defaults=d)
    assert e["used"] == {"manning_n": 0.08, "breach_method": "froehlich2008", "failure_mode": None}
    assert e["differs"] == {"manning_n": {"used": 0.08, "shared": 0.06}}, e["differs"]
    assert e["tool"] == "ndbreach" and e["shared_values"]["manning_n"] == 0.06
    lines = SD.differs_text(e)
    assert len(lines) == 1 and "0.08" in lines[0] and "0.06" in lines[0], lines
    json.dumps(e)                                               # goes into dam.json


# -------------------------------------------- the Processing adapter (no QGIS) --
class _P(object):
    def __init__(self, name, type_, default, options=None, minimum=None, maximum=None):
        self.name, self.type, self.defaultValue = name, type_, default
        self.options, self.minimum, self.maximum = options, minimum, maximum
        self.description, self.flags, self.isDestination, self.help = name, 0, False, ""


class _Alg(object):
    def __init__(self, name, params, ids=None):
        self.name = name
        self._params = {p.name: p for p in params}
        if ids is not None:
            self.SHARED_OPTION_IDS = ids


def _tools():
    from DamSafety_Core.ui import suite_settings_tools as ST
    return ST


def test_adapter_sets_the_bound_parameters():
    ST = _tools()
    d = SD.Defaults(dict(manning_n=0.07, failure_mode="overtopping", breach_method="froehlich1995",
                         sunny_day_start_pool="normal_pool"))
    runner_ids = ["piping", "piping_orifice", "overtopping", "froehlich1995_piping",
                  "froehlich1995_overtopping", "mlm1984", "vtg1990", "tr60", "tr60_curvilinear",
                  "user"]
    alg = _Alg("rundam", [_P("MANNING", "number", 0.06, minimum=0.005, maximum=1.0),
                          _P("METHOD", "enum", 0, options=list(runner_ids)),
                          _P("CELL", "number", 10.0)], {"METHOD": runner_ids})
    applied, bad = ST.apply_shared_defaults(alg, "ndbreach", defaults=d)
    assert dict(applied) == {"MANNING": 0.07, "METHOD": 4}, (applied, bad)
    assert alg._params["CELL"].defaultValue == 10.0                 # not a shared key
    sim = _Alg("runbreachsim", [_P("NDEF", "number", 0.05, minimum=0.01, maximum=0.3),
                                _P("SCEN", "enum", 0, options=["td", "sd"])], {"SCEN": ["td", "sd"]})
    applied, bad = ST.apply_shared_defaults(sim, "ndscreen", defaults=d)
    assert dict(applied) == {"NDEF": 0.07, "SCEN": 1}, (applied, bad)


def test_adapter_leaves_an_enum_without_ids_or_of_another_length_alone():
    ST = _tools()
    d = SD.Defaults(dict(failure_mode="overtopping", breach_method="froehlich2008"))
    alg = _Alg("other", [_P("METHOD", "enum", 0, options=["a", "b"])])          # no ids
    assert ST.apply_shared_defaults(alg, "ndbreach", defaults=d) == ([], [])
    alg = _Alg("other", [_P("METHOD", "enum", 0, options=["a", "b"])], {"METHOD": ["x"]})
    assert ST.apply_shared_defaults(alg, "ndbreach", defaults=d) == ([], [])
    assert alg._params["METHOD"].defaultValue == 0


def test_adapter_refuses_a_number_outside_the_tools_range():
    ST = _tools()
    d = SD.Defaults(dict(manning_n=0.29))
    sim = _Alg("runbreachsim", [_P("NDEF", "number", 0.05, minimum=0.01, maximum=0.2)])
    applied, bad = ST.apply_shared_defaults(sim, "ndscreen", defaults=d)
    assert applied == [] and bad and bad[0][0] == "NDEF", bad
    assert sim._params["NDEF"].defaultValue == 0.05


def test_the_settings_workbook_still_wins():
    """Order: built-in < shared defaults < settings workbook row < the dialog."""
    ST = _tools()
    from DamSafety_Core.damsuite import settings as DS
    alg = _Alg("runbreachsim", [_P("NDEF", "number", 0.05, minimum=0.01, maximum=0.3)])
    ST.apply_shared_defaults(alg, "ndscreen", defaults=SD.Defaults(dict(manning_n=0.06)))
    assert alg._params["NDEF"].defaultValue == 0.06
    wb = DS.Settings({("ndscreen", "runbreachsim", "NDEF"): 0.04})
    ST.apply_file_defaults(alg, "ndscreen", lambda t: ("Run", 0), settings=wb)
    assert alg._params["NDEF"].defaultValue == 0.04


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("ok", t.__name__)
    print("{} tests ok".format(len(tests)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
