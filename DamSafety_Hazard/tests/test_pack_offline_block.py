# -*- coding: utf-8 -*-
"""Hazard 2.18.63 (#171 follow-up): an offline-only run stops on a pack it cannot trust.

pipeline._log_offline_mode with download.offline_config / pack_reader patched to a tiny
temp pack (no service, no download, no QGIS project):

  * a broken manifest.json (the reader cannot open the pack): offline only -> RuntimeError
    naming the required layer; online -> logged, no error (the services are used);
  * a cut-short NSI GeoPackage: offline only -> RuntimeError; online -> a warning;
  * no check result at all (offline_pack.check could not run): offline only -> RuntimeError;
  * a good pack: no error;
  * _offline_pack_sig changes when a layer file changes in place (2.18.63).

Nothing outside the temp folder is read or written; the user's offline pack setting is not
read (offline_config is patched).
    python DamSafety_Hazard/tests/test_pack_offline_block.py
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard import pipeline as PL               # noqa: E402
from DamSafety_Hazard import download as DL               # noqa: E402
from DamSafety_Hazard import offline_pack as HOP          # noqa: E402
from DamSafety_Hazard.damsuite import packcheck as PC    # noqa: E402
import test_pack_format2 as T2                            # noqa: E402

FAILS = []


def expect(cond, what):
    print("{} {}".format("ok  " if cond else "FAIL", what))
    if not cond:
        FAILS.append(what)


class Run(object):
    def __init__(self):
        self.lines = []

    def log(self, text):
        self.lines.append(str(text))


def attempt(path, only):
    """(error or None, log lines) of _log_offline_mode with the pack at `path`."""
    saved = DL.offline_config, DL.pack_reader
    DL.offline_config = lambda: (path, only)

    def reader(log=None):
        try:
            return HOP.open_pack(path)
        except Exception as e:                               # noqa: BLE001
            if log:
                log("cannot read: {}".format(e))
            return None
    DL.pack_reader = reader
    run = Run()
    try:
        PL._log_offline_mode(run)
        return None, run.lines
    except Exception as e:                                   # noqa: BLE001
        return e, run.lines
    finally:
        DL.offline_config, DL.pack_reader = saved
        PC.clear_cache()


def main():
    tmp = tempfile.mkdtemp(prefix="pc_block_")
    try:
        good = T2.build_v2(os.path.join(tmp, "good"))
        err, lines = attempt(good, True)
        expect(err is None, "good pack, offline only: no error ({})".format(err))

        bad = shutil.copytree(good, os.path.join(tmp, "badjson"))
        with open(os.path.join(bad, "manifest.json"), "w") as fh:
            fh.write('{"format": "nd-dam-hazard-offline-pack", "format_version": 2, "data')
        err, lines = attempt(bad, True)
        expect(isinstance(err, RuntimeError) and "NSI_structures" in str(err),
               "broken manifest.json, offline only: RuntimeError ({})".format(err))
        expect(any("FAILED" in ln for ln in lines), "broken manifest.json: FAILED in the log")
        err, lines = attempt(bad, False)
        expect(err is None and any("FAILED" in ln for ln in lines),
               "broken manifest.json, online: logged, no error ({})".format(err))

        cut = shutil.copytree(good, os.path.join(tmp, "cut"))
        p = os.path.join(cut, "structures", "NSI_structures.gpkg")
        with open(p, "rb") as fh:
            raw = fh.read()
        with open(p, "wb") as fh:
            fh.write(raw[:len(raw) // 2])
        err, lines = attempt(cut, True)
        expect(isinstance(err, RuntimeError) and "NSI_structures" in str(err),
               "cut-short NSI GeoPackage, offline only: RuntimeError ({})".format(err))
        err, lines = attempt(cut, False)
        expect(err is None and any("NSI_structures" in ln for ln in lines),
               "cut-short NSI GeoPackage, online: a warning, no error ({})".format(err))

        saved = DL.pack_check
        DL.pack_check = lambda path=None, reader=None: None
        try:
            run = Run()
            try:
                PL._log_pack_check(run, good, None, True)
                e = None
            except RuntimeError as ex:
                e = ex
            expect(e is not None, "no check result, offline only: RuntimeError")
            run = Run()
            PL._log_pack_check(run, good, None, False)
            expect(any("could not be checked" in ln for ln in run.lines),
                   "no check result, online: a warning only")
        finally:
            DL.pack_check = saved

        sig = shutil.copytree(good, os.path.join(tmp, "sig"))
        saved = DL.offline_config
        DL.offline_config = lambda: (sig, True)
        try:
            s1 = PL._offline_pack_sig()
            q = os.path.join(sig, "hydro", "3DHP_flowlines.gpkg")
            st = os.stat(q)
            os.utime(q, ns=(st.st_atime_ns, st.st_mtime_ns + 7 * 10 ** 9))
            s2 = PL._offline_pack_sig()
        finally:
            DL.offline_config = saved
        expect(s1 != s2, "_offline_pack_sig: a layer file changed in place -> new signature")
    finally:
        PC.clear_cache()
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n{} failure(s)".format(len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
