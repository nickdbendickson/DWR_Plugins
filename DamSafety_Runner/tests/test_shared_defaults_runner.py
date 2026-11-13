"""Core 1.2.0: the Runner reads the shared defaults file (damsuite.shared_defaults): the PMF Vw
rule, the breach method / Manning's n IDs the Processing tools are set from, and the
"assumptions" entry a run writes into dam.json. Synthetic numbers; no QGIS."""
import json
import os
import tempfile
import types

from nd_breach_runner.core import breach as br
from nd_breach_runner.core import flood as fl
from nd_breach_runner.core import workflow as wf
from nd_breach_runner.damsuite import shared_defaults as SD
from nd_breach_runner.tests.registry import test


class _YourFile(object):
    """NDSUITE_DEFAULTS -> a temp file holding `values` (None = no file) for the block."""

    def __init__(self, values=None):
        self.values = values

    def __enter__(self):
        self.old = os.environ.get(SD.ENV_PATH)
        folder = tempfile.mkdtemp(prefix="sd_runner_")
        self.path = os.path.join(folder, "defaults.json")
        if self.values is not None:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"format": SD.FORMAT, "format_version": 1, "values": self.values}, f)
        os.environ[SD.ENV_PATH] = self.path
        return self

    def __exit__(self, *a):
        if self.old is None:
            os.environ.pop(SD.ENV_PATH, None)
        else:
            os.environ[SD.ENV_PATH] = self.old
        return False


def _dam():
    return br.DamData(dam_id="S5", dam_height_ft=30.0, storage_top_acft=2000.0,
                      top_of_dam_ft=1100.0, surface_area_top_ac=150.0, crest_length_ft=600.0)


def _flood(**kw):
    fi = fl.FloodInputs.triangle(3000.0, 12.0, 48.0, normal_pool_ft=1090.0, crest_length_ft=600.0,
                                 trigger="wse", trigger_value=1094.0, **kw)
    return fl.route_flood(_dam(), br.METHOD_PIPING, br.BreachOverrides(), fi)


@test
def shared_runner_method_ids_are_runner_methods():
    for mode in SD.SCHEMA["failure_mode"]["choices"]:
        for m in SD.SCHEMA["breach_method"]["choices"]:
            assert SD.runner_method({"failure_mode": mode, "breach_method": m}) in br.METHODS


@test
def shared_screening_method_ids_are_screening_methods():
    from nd_dam_screening.core import breach_hydro as bh
    assert set(SD.SCREEN_METHOD.values()) <= set(bh.BREACH_METHODS), SD.SCREEN_METHOD


@test
def shared_pmf_vw_follows_the_file():
    with _YourFile(None):
        assert fl.pmf_vw_source() == br.VW_POOL                  # suite file: ND standards
        p = _flood().with_breach.params
        assert p["vw_source"].startswith("storage at the pool"), p["vw_source"]
    with _YourFile({"pmf_breach_volume": "max_storage"}):
        assert fl.pmf_vw_source() == br.VW_MAX_STORAGE
        p = _flood().with_breach.params
        assert p["vw_acft"] == 2000.0 and p["vw_source"].startswith("maximum storage"), p
        # a run's own choice wins over the file
        p = _flood(vw_source=br.VW_POOL).with_breach.params
        assert p["vw_acft"] < 1900.0, p


def _st(**k):
    st = types.SimpleNamespace(scenario=wf.SCENARIO_SUNNY, breach_method=br.METHOD_PIPING,
                               manning_n=0.06, flood=None)
    st.__dict__.update(k)
    return st


@test
def shared_assumptions_record_what_the_run_used():
    log, warnings = [], []
    with _YourFile(None):
        e = wf._assumptions(_st(), "3.18.0", log.append, warnings)
    assert e["used"] == {"failure_mode": "piping", "breach_method": "froehlich2008",
                         "manning_n": 0.06, "sunny_day_start_pool": "max_pool"}, e["used"]
    assert e["differs"] == {} and not warnings and not log, (e["differs"], log)
    json.dumps(e)
    with _YourFile(None):
        e = wf._assumptions(_st(breach_method=br.METHOD_TR60, manning_n=0.08), "3.18.0",
                            log.append, warnings)
    assert e["used"]["failure_mode"] is None and e["used"]["breach_method"] == "tr60"
    assert set(e["differs"]) == {"breach_method", "manning_n"}, e["differs"]
    assert any("0.08" in line for line in log), log


@test
def shared_assumptions_flood_run_records_the_pmf_rule():
    fi = fl.FloodInputs.triangle(3000.0, 12.0, 48.0, normal_pool_ft=1090.0)
    with _YourFile({"pmf_breach_volume": "max_storage"}):
        e = wf._assumptions(_st(scenario=wf.SCENARIO_FLOOD, flood=fi), "3.18.0", [].append, [])
    assert e["used"]["pmf_breach_volume"] == "max_storage" and "sunny_day_start_pool" not in e["used"]
    assert e["your_file"].endswith("defaults.json") and e["differs"] == {}


@test
def shared_normal_pool_sunny_day_is_a_warning():
    warnings = []
    with _YourFile({"sunny_day_start_pool": "normal_pool"}):
        e = wf._assumptions(_st(), "3.18.0", [].append, warnings)
    assert e["differs"]["sunny_day_start_pool"]["used"] == "max_pool"
    assert warnings and "top of dam" in warnings[0], warnings
