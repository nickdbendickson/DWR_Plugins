"""0.18.5 (fork parity with nd Screening 0.17.0): tool 14 (self-test) has an optional EXE file
parameter, and the executable reaches step 7 (tool 7's EXE). Empty: no EXE in step 7, so tool 7
falls back as before (NDBREACH_LISFLOOD, PATH)."""
import os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_dam_screening.algorithms.run_breach_sim import EXE_FILTER  # noqa: E402
from nd_dam_screening.algorithms.selftest import SelfTest  # noqa: E402
from nd_dam_screening.core import selftest as st  # noqa: E402

assert EXE_FILTER == "All files (*)"
alg = SelfTest()
params = []
alg.addParameter = params.append
alg.initAlgorithm()
exe = [p for p in params if p.name == "EXE"]
assert len(exe) == 1 and exe[0].optional, exe
assert type(exe[0]).__name__ == "QgsProcessingParameterFile", type(exe[0])   # a file box, not a string

work = tempfile.mkdtemp(prefix="selftest_exe_")
lay = {"streams": None, "damlines": None}
mk7 = {s[0]: s[2] for s in st.steps(work, lay, "dem", engine=0, exe="/opt/lisflood/lisflood")}["7"]
assert mk7({})["EXE"] == "/opt/lisflood/lisflood" and mk7({})["ENGINE"] == 0
mk7 = {s[0]: s[2] for s in st.steps(work, lay, "dem")}["7"]
assert "EXE" not in mk7({})

# run_selftest hands exe on to steps() (the real steps need each earlier output, so steps() is
# replaced by a one-step list that records what it was given)
seen = {}
real_steps = st.steps


def one_step(work, layers, dem_folder, engine, cell_m, sim_h, exe=""):
    seen["exe"] = exe
    return [("7", "runbreachsim", lambda prev: {}, lambda res: "")]


st.steps = one_step
try:
    r = st.run_selftest(work, lay, "dem", lambda aid, p: {}, engine=0, exe="/x/lisflood")
finally:
    st.steps = real_steps
assert r["ok"] and seen["exe"] == "/x/lisflood", (r, seen)
print("test_selftest_exe: OK")
