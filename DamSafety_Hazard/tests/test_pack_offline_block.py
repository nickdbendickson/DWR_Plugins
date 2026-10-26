# -*- coding: utf-8 -*-
"""Hazard 2.18.64 (#171 follow-up): an offline-only run stops on a pack it cannot trust.

pipeline._log_offline_mode with download.offline_config / pack_reader patched to a tiny
temp pack (no service, no download, no QGIS project):

  * a broken manifest.json (the reader cannot open the pack): offline only -> RuntimeError
    naming the required layer; online -> logged, no error (the services are used);
  * a cut-short NSI GeoPackage: offline only -> RuntimeError; online -> a warning;
  * no check result at all (offline_pack.check could not run): offline only -> RuntimeError;
  * a pack the reader cannot open although its check passes (a format-2 .zip whose NSI
    member has a bad CRC - the listing is intact -, a format-1 .zip without
    nd_offline_pack.sqlite, a format_version the reader refuses): offline only ->
    RuntimeError naming the required layer, and the log does not say "OK"; online -> a
    warning only;
  * no pack at all (an empty folder, a moved or deleted pack - 2.18.64): offline only ->
    RuntimeError saying it is not an offline data pack and cannot be opened (with the
    reader's reason), never "found no problem against manifest.json"; online -> a warning;
  * a good pack: no error;
  * _offline_pack_sig changes when a layer file changes in place (2.18.64).

Nothing outside the temp folder is read or written; the user's offline pack setting is not
read (offline_config is patched).
    python DamSafety_Hazard/tests/test_pack_offline_block.py
"""
import json
import os
import shutil
import sys
import tempfile
import zipfile

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

    def reader(log=None, errors=None):
        try:
            return HOP.open_pack(path)
        except Exception as e:                               # noqa: BLE001
            if log:
                log("cannot read: {}".format(e))
            if errors is not None:
                errors.append(str(e))
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


def zip_pack(folder, zpath, bad_crc=""):
    """`folder` zipped (stored, not compressed); `bad_crc`: a member whose data is changed
    after the CRC is written - the listing (names, sizes) stays intact."""
    os.makedirs(os.path.dirname(zpath), exist_ok=True)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
        for base, _d, files in os.walk(folder):
            for f in sorted(files):
                full = os.path.join(base, f)
                z.write(full, os.path.relpath(full, folder).replace(os.sep, "/"))
    if bad_crc:
        with zipfile.ZipFile(zpath) as z:
            info = z.getinfo(bad_crc)
        with open(zpath, "r+b") as fh:
            fh.seek(info.header_offset + 26)
            n, extra = int.from_bytes(fh.read(2), "little"), int.from_bytes(fh.read(2), "little")
            pos = info.header_offset + 30 + n + extra + info.file_size // 2
            fh.seek(pos)
            b = fh.read(1)
            fh.seek(pos)
            fh.write(bytes([b[0] ^ 0xFF]))
    return zpath


def unopenable(tmp, good):
    """[(what, path)] of packs whose check passes but that the reader cannot open."""
    out = []
    z = zip_pack(good, os.path.join(tmp, "crc", "pack.zip"), "structures/NSI_structures.gpkg")
    out.append(("format-2 zip, NSI member with a bad CRC", z))
    os.makedirs(os.path.join(tmp, "v1zip"))
    z1 = os.path.join(tmp, "v1zip", "pack.zip")
    with zipfile.ZipFile(z1, "w") as z:
        z.writestr("manifest.json", json.dumps({
            "format": "nd-dam-hazard-offline-pack", "format_version": 1,
            "created": "2026-10-14T00:00:00",
            "datasets": {"NSI_structures": {"count": 2, "status": "complete"}}}))
    out.append(("format-1 zip without nd_offline_pack.sqlite", z1))
    fv = shutil.copytree(good, os.path.join(tmp, "newer"))
    T2.edit_manifest(fv, lambda m: m.update(format_version=99))
    out.append(("format_version the reader refuses", fv))
    return out


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

        # 2.18.64: the check passes, the reader cannot open the pack - nothing can be read
        # from it, so offline only stops (it used to log "OK" and run on without NSI)
        for what, pth in unopenable(tmp, good):
            PC.clear_cache()
            res = DL.pack_check(pth)
            expect(res is not None and res.ok and not res.missing_required,
                   "{}: the check alone passes ({})".format(what, res and res.summary()))
            err, lines = attempt(pth, True)
            expect(isinstance(err, RuntimeError) and "NSI_structures" in str(err) and
                   "cannot be opened" in str(err),
                   "{}, offline only: RuntimeError ({})".format(what, err))
            expect(not any("OK - every layer" in ln for ln in lines) and
                   any("cannot be opened" in ln for ln in lines),
                   "{}: the log does not say OK ({})".format(what, lines))
            err, lines = attempt(pth, False)
            expect(err is None and any("WARNING" in ln for ln in lines) and
                   not any("OK - every layer" in ln for ln in lines),
                   "{}, online: a warning, no error ({})".format(what, err))

        # 2.18.64: no pack at all - nothing to check against, so neither the log nor the
        # error may say the check "found no problem against manifest.json"
        os.makedirs(os.path.join(tmp, "empty"))
        for what, pth in (("an empty folder", os.path.join(tmp, "empty")),
                          ("a missing path", os.path.join(tmp, "moved", "pack"))):
            err, lines = attempt(pth, True)
            msg = str(err)
            expect(isinstance(err, RuntimeError) and "NSI_structures" in msg and
                   "not an offline data pack" in msg and "cannot be opened: " in msg and
                   "found no problem" not in msg,
                   "{}, offline only: RuntimeError, not a pack, cannot be opened ({})".format(
                       what, err))
            expect(not any("found no problem" in ln for ln in lines) and
                   any("not an offline data pack" in ln and "cannot be opened" in ln
                       for ln in lines),
                   "{}: the log says it is not a pack ({})".format(what, lines))
            err, lines = attempt(pth, False)
            expect(err is None and any("WARNING" in ln and "not an offline data pack" in ln
                                       for ln in lines) and
                   not any("found no problem" in ln for ln in lines),
                   "{}, online: a warning, no error ({})".format(what, err))
            saved = DL.offline_config
            DL.offline_config = lambda p=pth: (p, True)
            try:
                why = []
                r = DL.pack_reader(errors=why)
            finally:
                DL.offline_config = saved
            expect(r is None and len(why) == 1 and why[0],
                   "{}: download.pack_reader gives the reason ({})".format(what, why))

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
