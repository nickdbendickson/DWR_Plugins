"""Core 1.2.0: Screening starts from the shared defaults file (damsuite.shared_defaults): tool 7's
Manning's n and scenario, tool 5's failure mode and equation, and the window's scenario choice;
a window choice that only follows the shared file is not written into the settings workbook."""
import json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_dam_screening.core import workflow as wf  # noqa: E402
from nd_dam_screening.damsuite import settings as DS  # noqa: E402
from nd_dam_screening.damsuite import shared_defaults as SD  # noqa: E402
from nd_dam_screening import suite_settings_tools as ST  # noqa: E402

folder = tempfile.mkdtemp(prefix="sd_screen_")


def use(values):
    p = os.path.join(folder, "defaults_{}.json".format(len(os.listdir(folder))))
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"format": SD.FORMAT, "format_version": 1, "values": values}, f)
    os.environ[SD.ENV_PATH] = p


# the suite file: td (maximum pool), as before
use({})
assert wf.default_settings()["scenario"] == 0
use({"sunny_day_start_pool": "normal_pool"})
assert wf.default_settings()["scenario"] == 1
assert wf.DEFAULT_SETTINGS["scenario"] == 0                  # the module constant is unchanged
# following the shared file is not a change the window writes into the workbook
sf = wf.window_settings_to(DS.Settings({}), wf.default_settings(), defaults=False)
assert "scenario" not in sf.window(wf.PLUGIN_KEY), sf.window(wf.PLUGIN_KEY)

# the tools (qgis_mock parameter objects)
try:
    from nd_dam_screening.algorithms.run_breach_sim import RunBreachSim
    from nd_dam_screening.algorithms.breach_hydrograph import BreachHydrograph
except Exception as e:                                        # noqa
    print("SKIP: the tool classes need more than the mock ({}: {})".format(type(e).__name__, e))
    sys.exit(0)
use({"manning_n": 0.07, "failure_mode": "overtopping", "breach_method": "mlm1984",
     "sunny_day_start_pool": "normal_pool"})
sim = RunBreachSim()
sim.initAlgorithm()
applied, bad = ST.apply_shared_defaults(sim, "ndscreen")
assert dict(applied) == {"NDEF": 0.07, "SCEN": 1}, (applied, bad)
bh = BreachHydrograph()
bh.initAlgorithm()
applied, bad = ST.apply_shared_defaults(bh, "ndscreen")
assert dict(applied) == {"MODE": 1, "METHOD": BreachHydrograph.METHODS.index("MLM")}, (applied, bad)
# the shipped values give tool 7 Manning's n 0.06 (Nick 2026-11-13; was 0.05)
use({})
sim = RunBreachSim()
sim.initAlgorithm()
assert dict(ST.apply_shared_defaults(sim, "ndscreen")[0])["NDEF"] == 0.06
print("test_shared_defaults_screening: ok")
