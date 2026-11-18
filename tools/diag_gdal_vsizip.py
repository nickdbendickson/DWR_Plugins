"""TEMPORARY diagnostic for the LTR leg (QGIS 3.34 image, GDAL 3.4): test_atlas14_grid.py and
test_atlas14_qgis.py hang at their first gdal.Open("/vsizip/<zip>/<grid>.asc") (faulthandler dump,
CI 2026-11-18). Each variant below runs in its own process with CPL_DEBUG on and a time limit; a
variant that does not finish prints the process state, its open files, GDAL's last debug lines and
its Python stack. Never fails the step. Remove once the cause is fixed.

    python3 tools/diag_gdal_vsizip.py
"""
import os
import signal
import subprocess
import sys
import tempfile
import time
import zipfile

LIMIT_S = 45
PRJ = ('GEOGCS["GCS_North_American_1983",DATUM["D_North_American_1983",SPHEROID["GRS_1980",6378137.0,'
       '298.257222101]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]')
ASC = ("ncols 4\nnrows 4\nxllcorner -97.5\nyllcorner 46.9\ncellsize 0.0083333\nNODATA_value -9\n"
       "6300 6310 6320 6330\n6340 6342 6350 6360\n6400 6410 6420 -9\n6500 6510 6520 6530\n")

CHILD = r'''
import os, sys, faulthandler
faulthandler.enable()
from osgeo import gdal, osr
gdal.UseExceptions()
v, d = sys.argv[1], sys.argv[2]
z = os.path.join(d, "mw100yr24ha.zip")
if v == "versions":
    print("GDAL", gdal.__version__, "PROJ", osr.GetPROJVersionMajor(), osr.GetPROJVersionMinor(),
          osr.GetPROJVersionMicro(), "drivers", gdal.GetDriverCount())
    try:
        print("PROJ search paths", osr.GetPROJSearchPaths())
    except Exception as e:
        print("PROJ search paths ?", e)
    print("PROJ networking", gdal.GetConfigOption("PROJ_NETWORK"), os.environ.get("PROJ_NETWORK"))
    for k in sorted(os.environ):
        if k.startswith(("GDAL", "CPL", "PROJ", "CURL", "HTTP", "http", "VSI", "OGR")):
            print("env", k, "=", os.environ[k])
elif v == "vsi_readdir":
    print(gdal.ReadDir("/vsizip/" + z))
elif v == "vsi_read":
    f = gdal.VSIFOpenL("/vsizip/" + z + "/mw100yr24ha.asc", "rb")
    gdal.VSIFSeekL(f, 0, 2); n = gdal.VSIFTellL(f); gdal.VSIFSeekL(f, 0, 0)
    print(n, gdal.VSIFReadL(1, n, f)[:40]); gdal.VSIFCloseL(f)
elif v == "osr_esri_prj":
    s = osr.SpatialReference(); s.ImportFromESRI([open(os.path.join(d, "x.prj")).read()])
    print(s.GetName(), s.GetAuthorityCode(None))
elif v == "osr_esri_prj_identify":
    s = osr.SpatialReference(); s.ImportFromESRI([open(os.path.join(d, "x.prj")).read()])
    try:
        print(s.AutoIdentifyEPSG(), s.GetAuthorityCode(None))
    except RuntimeError as e:
        print("not identified:", e)
elif v == "osr_epsg_4269":
    s = osr.SpatialReference(); s.ImportFromEPSG(4269); print(s.GetName())
else:
    path = {"open_disk_asc": os.path.join(d, "plain", "g.asc"),
            "open_disk_asc_prj": os.path.join(d, "withprj", "g.asc"),
            "open_zip_asc_only": "/vsizip/" + os.path.join(d, "only.zip") + "/mw100yr24ha.asc",
            "open_zip_fixture": "/vsizip/" + z + "/mw100yr24ha.asc",
            "open_zip_fixture_aaigrid_only": "/vsizip/" + z + "/mw100yr24ha.asc",
            "open_zip_deflated": "/vsizip/" + os.path.join(d, "deflated.zip") + "/mw100yr24ha.asc"}[v]
    if v.endswith("aaigrid_only"):
        ds = gdal.OpenEx(path, gdal.OF_RASTER, allowed_drivers=["AAIGrid"])
    else:
        ds = gdal.Open(path)
    print(ds.GetDriver().ShortName, ds.RasterXSize, ds.RasterYSize, ds.GetProjection()[:60])
print("DONE", v)
sys.stdout.flush()
'''

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRID_TEST = os.path.join(ROOT, "DamSafety_PMP", "tests", "test_atlas14_grid.py")
# the real failing tests, as the PMP runner starts them (its env), with GDAL's debug lines
SCRIPTS = {"pmp_grid_clip_first": [GRID_TEST, "Clip.test_scale_thousandths_to_inches"],
           "pmp_grid_clip_all_nodata": [GRID_TEST, "Clip.test_all_nodata_is_a_coverage_error"]}
VARIANTS = ["versions", "osr_epsg_4269", "osr_esri_prj", "osr_esri_prj_identify", "vsi_readdir", "vsi_read",
            "open_disk_asc", "open_disk_asc_prj", "open_zip_asc_only", "open_zip_deflated",
            "open_zip_fixture_aaigrid_only", "open_zip_fixture", "pmp_grid_clip_first", "pmp_grid_clip_all_nodata"]


def fixtures(d):
    for sub, prj in (("plain", False), ("withprj", True)):
        os.makedirs(os.path.join(d, sub))
        with open(os.path.join(d, sub, "g.asc"), "w") as f:
            f.write(ASC)
        if prj:
            with open(os.path.join(d, sub, "g.prj"), "w") as f:
                f.write(PRJ)
    with open(os.path.join(d, "x.prj"), "w") as f:
        f.write(PRJ)
    with zipfile.ZipFile(os.path.join(d, "only.zip"), "w") as z:
        z.writestr("mw100yr24ha.asc", ASC)
    for name, comp in (("mw100yr24ha.zip", zipfile.ZIP_STORED), ("deflated.zip", zipfile.ZIP_DEFLATED)):
        with zipfile.ZipFile(os.path.join(d, name), "w", comp) as z:
            z.writestr("mw100yr24ha.asc", ASC)
            z.writestr("mw100yr24ha.prj", PRJ)
            z.writestr("mw100yr24ha.xml", "<metadata><title>test</title><pubdate>20130415</pubdate></metadata>")


def proc_state(pid):
    out = []
    for name in ("status", "wchan", "stack", "syscall"):
        try:
            with open("/proc/{}/{}".format(pid, name)) as f:
                t = f.read()
            if name == "status":
                t = "".join(l for l in t.splitlines(True) if l.startswith(("State", "Threads")))
            out.append("{}: {}".format(name, t.strip()[:800]))
        except Exception as e:
            out.append("{}: ? {}".format(name, e))
    try:
        for tid in sorted(os.listdir("/proc/{}/task".format(pid))):
            with open("/proc/{}/task/{}/wchan".format(pid, tid)) as f:
                w = f.read()
            with open("/proc/{}/task/{}/stat".format(pid, tid)) as f:
                st = f.read().split(") ")[1].split()[0]
            out.append("thread {} state {} wchan {}".format(tid, st, w))
    except Exception as e:
        out.append("threads ? {}".format(e))
    try:
        fds = []
        for fd in sorted(os.listdir("/proc/{}/fd".format(pid)), key=int):
            try:
                fds.append("{}->{}".format(fd, os.readlink("/proc/{}/fd/{}".format(pid, fd))))
            except OSError:
                pass
        out.append("fds: " + " ".join(fds))
    except Exception as e:
        out.append("fds ? {}".format(e))
    return "\n".join(out)


def main():
    d = tempfile.mkdtemp()
    fixtures(d)
    env = dict(os.environ, CPL_DEBUG="ON", CPL_TIMESTAMP="ON", PYTHONFAULTHANDLER="1", PYTHONUNBUFFERED="1")
    proj = os.path.join(sys.prefix, "share", "proj")          # as DamSafety_PMP/tests/run_tests.py does
    if os.path.isdir(proj):
        env.setdefault("PROJ_DATA", proj)
        env.setdefault("PROJ_LIB", proj)
    hung = []
    for v in VARIANTS:
        t0 = time.time()
        cmd = [sys.executable, "-X", "faulthandler"] + (SCRIPTS[v] if v in SCRIPTS else ["-c", CHILD, v, d])
        p = subprocess.Popen(cmd, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            out, err = p.communicate(timeout=LIMIT_S)
            print("== {} rc={} {:.1f} s\n{}".format(v, p.returncode, time.time() - t0, out.strip()[-1500:]))
            if p.returncode:
                print("   stderr tail:\n" + err[-2500:])
        except subprocess.TimeoutExpired:
            hung.append(v)
            state = proc_state(p.pid)
            os.killpg(p.pid, signal.SIGABRT)
            try:
                out, err = p.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                out, err = p.communicate()
            print("== {} HUNG after {} s\n{}\n   stdout: {}\n   stderr tail (GDAL debug + stack):\n{}".format(
                v, LIMIT_S, state, out.strip()[-800:], err[-5000:]))
        sys.stdout.flush()
    print("HUNG:", hung or "none")


if __name__ == "__main__":
    main()
