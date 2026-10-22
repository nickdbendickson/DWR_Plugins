"""3.10.68: the Windows HEC-RAS 6.6 launcher of the batch chain (core/hecras_windows.py).
Mocks only: no HEC-RAS, no Wine, no Windows; Ras.exe, RasUnsteady.exe and the COM
controller are fakes that write (or do not write) the plan's results HDF."""
import os
import shutil
import subprocess
import tempfile

from nd_breach_runner.core import chain as CH
from nd_breach_runner.core import hecras_windows as HW
from nd_breach_runner.tests.registry import test

WIN = r"C:\HEC\RAS66"


def _project(root, terrain=True, rasmap_terrain=True):
    with open(os.path.join(root, "Dam.prj"), "w") as f:
        f.write("Proj Title=Dam\nCurrent Plan=p01\nGeom File=g01\nPlan File=p01\n")
    with open(os.path.join(root, "Terrain_5070.prj"), "w") as f:      # a GIS projection
        f.write('PROJCS["NAD83 / Conus Albers"]')
    with open(os.path.join(root, "Dam.p01"), "w") as f:
        f.write("Plan Title=Breach run\nShort Identifier=Breach\n")
    with open(os.path.join(root, "Dam.rasmap"), "w") as f:
        f.write("<RASMapper>\r\n  <Terrains />\r\n</RASMapper>\r\n" if not rasmap_terrain else
                '<RASMapper><Terrains><Layer Type="TerrainLayer" /></Terrains></RASMapper>')
    os.makedirs(os.path.join(root, "Terrain"))
    open(os.path.join(root, "Terrain", "Dam Terrain.tif"), "w").close()
    if terrain:
        open(os.path.join(root, "Terrain", "Dam Terrain.hdf"), "w").close()
    return os.path.join(root, "Dam.prj")


def _d():
    return {"available": True, "platform": "win32", "windows_dir": WIN,
            "ras_exe": WIN + r"\Ras.exe", "rasunsteady": WIN + r"\RasUnsteady.exe",
            "com": False}


class _Res:
    def __init__(self, rc=0, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def _writer(*names):
    def w(folder, name):
        with open(os.path.join(folder, name), "wb") as f:
            f.write(b"hdf")
    return w


def _runner(root, ras_writes=(), unsteady_writes=(), calls=None):
    calls = [] if calls is None else calls

    def run(cmd, cwd=None, timeout=None, **kw):
        calls.append((os.path.basename(cmd[0].replace("\\", "/")), list(cmd[1:]), cwd))
        what = ras_writes if cmd[0].endswith("Ras.exe") else unsteady_writes
        for n in what:
            with open(os.path.join(root, n), "wb") as f:
                f.write(b"hdf")
        return _Res()
    return run, calls


def _tmp():
    return tempfile.mkdtemp(prefix="hecwin_")


@test
def hecras_windows_detect():
    files = {WIN + "\\Ras.exe", os.path.join(WIN, "RasUnsteady.exe")}
    isf = lambda p: p.replace("/", "\\") in {f.replace("/", "\\") for f in files}  # noqa
    d = HW.detect(env={HW.ENV_WINDIR: WIN}, platform="win32", isfile=isf, com=True)
    assert d["available"] and d["ras_exe"].endswith("Ras.exe") and d["rasunsteady"] and d["com"]
    d = HW.detect(env={}, platform="win32", isfile=lambda p: False, com=False)
    assert not d["available"] and HW.ENV_WINDIR in d["reason"]
    d = HW.detect(env={}, platform="linux", isfile=lambda p: True)
    assert not d["available"] and "Windows only" in d["reason"]
    assert HW.methods({}) == ("headless", "com")
    assert HW.methods({HW.ENV_METHODS: "com"}) == ("com",)
    assert HW.methods({HW.ENV_METHODS: "nonsense"}) == ("headless", "com")
    assert HW.timeout_s({HW.ENV_TIMEOUT: "90"}) == 90.0 and HW.timeout_s({}) is None


@test
def hecras_windows_project_and_terrain():
    root = _tmp()
    try:
        prj = _project(root, rasmap_terrain=False)
        assert HW.find_project(root) == prj                     # not the GIS .prj
        assert HW.plan_files(prj) == [os.path.join(root, "Dam.p01")]
        assert HW.plan_title(os.path.join(root, "Dam.p01")) == "Breach run"
        ok, note = HW.terrain_state(root)                        # registered on the way
        assert ok and "registered" in note
        assert "TerrainLayer" in open(os.path.join(root, "Dam.rasmap")).read()
        os.remove(os.path.join(root, "Terrain", "Dam Terrain.hdf"))
        ok, note = HW.terrain_state(root)
        assert not ok and "RAS Mapper" in note
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def hecras_windows_headless_ras_exe():
    root = _tmp()
    try:
        _project(root)
        run, calls = _runner(root, ras_writes=["Dam.p01.hdf"])
        r = HW.run_plan(root, d=_d(), env={}, runner=run, check=lambda p: True)
        assert r["method"] == "headless" and r["plan_hdf"].endswith("Dam.p01.hdf")
        assert [c[0] for c in calls] == ["Ras.exe"]
        assert calls[0][1][0] == "-c" and calls[0][1][1].endswith("Dam.prj") and \
            calls[0][1][2].endswith("Dam.p01")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def hecras_windows_headless_then_rasunsteady():
    root = _tmp()
    try:
        _project(root)
        run, calls = _runner(root, ras_writes=["Dam.b01", "Dam.x01", "Dam.p01.tmp.hdf"])
        r = HW.run_plan(root, d=_d(), env={}, runner=run, check=lambda p: True)
        assert [c[0] for c in calls] == ["Ras.exe", "RasUnsteady.exe"]
        assert calls[1][1] == ["Dam.p01.tmp.hdf", "x01"] and calls[1][2] == root
        assert r["plan_hdf"] == os.path.join(root, "Dam.p01.hdf") and \
            os.path.isfile(r["plan_hdf"]) and not os.path.exists(r["plan_hdf"][:-4] + ".tmp.hdf")
    finally:
        shutil.rmtree(root, ignore_errors=True)


class _Ras:
    def __init__(self, root, write=True):
        self.root, self.write, self.calls = root, write, []

    def __getattr__(self, name):
        def f(*a):
            self.calls.append((name, a))
            if name == "Compute_CurrentPlan" and self.write:
                open(os.path.join(self.root, "Dam.p01.hdf"), "wb").write(b"hdf")
            return (bool(self.write), 1, ("Computations completed",)) \
                if name == "Compute_CurrentPlan" else None
        return f


@test
def hecras_windows_com_fallback():
    root = _tmp()
    try:
        _project(root)
        run, calls = _runner(root)                              # Ras.exe -c writes nothing
        ras = _Ras(root)
        logs = []
        r = HW.run_plan(root, d=_d(), env={}, runner=run, dispatch=lambda pid: ras,
                        check=lambda p: True, log=logs.append)
        assert r["method"] == "com" and r["plan_hdf"].endswith("Dam.p01.hdf")
        assert len(r["attempts"]) == 1 and r["attempts"][0].startswith("headless:")
        names = [c[0] for c in ras.calls]
        assert names == ["Project_Open", "Plan_SetCurrent", "Compute_CurrentPlan",
                         "Project_Close", "QuitRas"], names
        assert ras.calls[1][1] == ("Breach run",) and ras.calls[2][1] == (None, None, True)
        # no pywin32 and no dispatch: the COM step is skipped with the reason
        try:
            HW.run_plan(root, d=dict(_d(), com=False), env={HW.ENV_METHODS: "com"},
                        runner=run, check=lambda p: True)
            raise AssertionError("no error")
        except HW.WindowsRunError as e:
            assert "pywin32" in str(e)
        # both fail: one error naming both attempts, the controller still closed
        os.remove(os.path.join(root, "Dam.p01.hdf"))
        bad = _Ras(root, write=False)
        try:
            HW.run_plan(root, d=_d(), env={}, runner=run, dispatch=lambda pid: bad,
                        check=lambda p: True)
            raise AssertionError("no error")
        except HW.WindowsRunError as e:
            assert len(e.attempts) == 2 and "Computations completed" in str(e)
        assert bad.calls[-1][0] == "QuitRas"
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def hecras_windows_terrain_missing_and_timeout():
    root = _tmp()
    try:
        _project(root, terrain=False)
        run, calls = _runner(root, ras_writes=["Dam.p01.hdf"])
        try:
            HW.run_plan(root, d=_d(), env={}, runner=run, check=lambda p: True)
            raise AssertionError("no error")
        except HW.TerrainMissing as e:
            assert "Terrain" in str(e) and not calls             # nothing launched
        open(os.path.join(root, "Terrain", "Dam Terrain.hdf"), "w").close()

        def slow(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 5)
        try:
            HW.run_plan(root, d=_d(), env={HW.ENV_TIMEOUT: "5", HW.ENV_METHODS: "headless"},
                        runner=slow, check=lambda p: True)
            raise AssertionError("no error")
        except HW.WindowsRunError as e:
            assert "within 5 s" in str(e)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def hecras_windows_chain_launcher_and_fallback():
    """The chain on Windows: 'Run' launches HEC-RAS; a failed launch waits for a manual run."""
    from nd_breach_runner import algorithms as _A  # noqa: F401 - import order
    from nd_breach_runner import algorithms_chain as AC
    mode, note = CH.effective_hec_mode({"HEC_MODE": 0},
                                       {"available": True, "platform": "win32",
                                        "windows_launcher": True})
    assert mode == "run_windows" and not note
    eng = AC.hec_engine(platform="win32", detect_windows=lambda: dict(_d()))
    assert eng["windows_launcher"] and eng["available"]
    eng = AC.hec_engine(platform="win32", detect_windows=lambda: {
        "available": False, "reason": "Ras.exe (HEC-RAS 6.6) not found"})
    assert not eng["windows_launcher"] and "Ras.exe" in eng["reason"]
    root = _tmp()
    try:
        vals = {(CH.PLUGIN, CH.TOOL, "OUT_FOLDER"): root,
                (CH.PLUGIN, CH.TOOL, "HEC_WAIT_MIN"): 0}
        cv, probs = CH.chain_values(vals)
        assert not probs, probs
        dam = CH.Dam("W", os.path.join(root, "W"), vals, cv, mode="run_windows")
        pkg = os.path.join(dam.folder, "hecras_model")
        os.makedirs(pkg)
        _project(pkg)
        dam.outputs["hec_build"] = {"package": pkg, "built_at": 0}
        got = []

        def launcher(folder, log=None):
            got.append(folder)
            p = os.path.join(folder, "Dam.p01.hdf")
            open(p, "wb").write(b"hdf")
            return {"plan_hdf": p, "method": "headless", "attempts": [], "seconds": 1.0,
                    "project": os.path.join(folder, "Dam.prj")}
        ops = AC.QgisOps(None, None, "", algs=None, run=lambda a, p: {},
                         lookup=lambda a: None, win_run=launcher)
        out = ops.run("hec_run", dam)
        assert got == [pkg] and out["plan_hdf"].endswith("Dam.p01.hdf") and \
            out["method"] == "headless"
        os.remove(out["plan_hdf"])

        def broken(folder, log=None):
            raise HW.TerrainMissing("no terrain yet: open RAS Mapper")
        ops = AC.QgisOps(None, None, "", algs=None, run=lambda a, p: {},
                         lookup=lambda a: None, win_run=broken)
        try:
            ops.run("hec_run", dam)
            raise AssertionError("no Waiting")
        except CH.Waiting as e:
            assert "no terrain yet" in str(e) and "waiting for HEC-RAS" in str(e)
        # the plan computed by hand in the meantime: taken without launching again
        p = os.path.join(pkg, "Dam.p01.hdf")
        open(p, "wb").write(b"hdf")
        orig = CH.plan_has_results
        CH.plan_has_results = lambda x: True
        try:
            out = ops.run("hec_run", dam)
        finally:
            CH.plan_has_results = orig
        assert out["plan_hdf"] == p and out["method"] == "manual"
    finally:
        shutil.rmtree(root, ignore_errors=True)
