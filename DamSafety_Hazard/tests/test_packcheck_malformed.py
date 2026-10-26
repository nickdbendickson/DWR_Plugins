# -*- coding: utf-8 -*-
"""Hazard 2.18.63 / damsuite 1.17.6 (#171 follow-up): damsuite.packcheck never raises.

Tiny synthetic packs in a temp folder (built with test_pack_format2's builders):

  * a malformed or unreadable manifest.json (not JSON, not UTF-8, empty, a list, a number,
    sections of the wrong type, entries of the wrong type, a non-text file name, a file
    that is a folder, a UTF-8 byte-order mark the readers refuse - folder and zip alike)
    gives a FAILED PackCheck (or a named "manifest" issue for one bad entry) - never an
    exception - and a required layer then shows as missing;
  * a pack-level problem (a format-1 pack's broken manifest.json) is named in the summary,
    not as "missing: " with no layer name;
  * a cut-short (half-written) or garbage GeoPackage is "unreadable"; a required one is
    an error; integrity=True runs SQLite's quick_check and passes on a good file;
  * zipped packs: the listing is checked (a listed file not in the zip, an empty one, a
    broken manifest.json in the zip);
  * pack_signature changes when a layer file changes in place;
  * Hazard's offline_pack.check / download.pack_check never raise.

Nothing outside the temp folder is read or written.
    python3 DamSafety_Hazard/tests/test_packcheck_malformed.py
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

from DamSafety_Hazard import offline_pack as HOP          # noqa: E402
from DamSafety_Hazard.damsuite import packcheck as PC    # noqa: E402
import test_pack_format2 as T2                            # noqa: E402

FAILS = []
REQ = ("NSI_structures",)
F = "nd-dam-hazard-offline-pack"


def expect(cond, what):
    print("{} {}".format("ok  " if cond else "FAIL", what))
    if not cond:
        FAILS.append(what)


def write_manifest(folder, raw):
    with open(os.path.join(folder, "manifest.json"), "wb") as fh:
        fh.write(raw if isinstance(raw, bytes) else raw.encode("utf-8"))


def safe(fn, *a, **k):
    try:
        return fn(*a, **k), None
    except Exception as e:                                   # noqa: BLE001
        return None, e


def zip_folder(folder, zpath, skip=(), empty=(), manifest=None):
    with zipfile.ZipFile(zpath, "w") as z:
        for base, _d, files in os.walk(folder):
            for f in files:
                full = os.path.join(base, f)
                rel = os.path.relpath(full, folder).replace(os.sep, "/")
                if rel in skip:
                    continue
                if rel == "manifest.json" and manifest is not None:
                    z.writestr(rel, manifest)
                elif rel in empty:
                    z.writestr(rel, b"")
                else:
                    z.write(full, rel)
    return zpath


def main():
    tmp = tempfile.mkdtemp(prefix="pc_malformed_")
    try:
        good = T2.build_v2(os.path.join(tmp, "good"))
        res = PC.check_pack(good, required=REQ)
        expect(res.ok and not res.failed, "a good pack checks OK ({})".format(res.summary()))
        with open(os.path.join(good, "manifest.json"), "rb") as fh:
            bom = b"\xef\xbb\xbf" + fh.read()      # Windows Notepad / PowerShell 5 "UTF-8"

        # -- manifest.json that cannot be read: FAILED, never raises
        cases = [("not JSON", "{\"format_version\": 2, "), ("empty", ""),
                 ("not UTF-8", b"\xff\xfe\x00{garbage"), ("a list", "[1, 2]"),
                 ("a number", "2"), ("null", "null"),
                 ("datasets a list", json.dumps({"format": F, "format_version": 2, "datasets": [1]})),
                 ("rasters a string", json.dumps({"format": F, "format_version": 2, "datasets": {},
                                                  "rasters": "x"})),
                 ("left_out a list", json.dumps({"format": F, "format_version": 2, "datasets": {},
                                                 "left_out": ["a"]})),
                 ("UTF-8 BOM", bom)]
        for name, raw in cases:
            f = shutil.copytree(good, os.path.join(tmp, "m_" + name.replace(" ", "_")))
            write_manifest(f, raw)
            PC.clear_cache()
            r, err = safe(PC.check_pack, f, required=REQ)
            expect(err is None and r is not None, "manifest {}: no exception ({})".format(
                name, err))
            if r is None:
                continue
            expect(r.failed and not r.ok and ": FAILED - " in r.summary(),
                   "manifest {}: FAILED ({})".format(name, r.summary()))
            expect(list(r.missing_required) == list(REQ),
                   "manifest {}: the required layer shows as missing".format(name))
            expect(PC.manifest_problem(f), "manifest {}: manifest_problem names it".format(name))
            hr, herr = safe(HOP.check, f, required=REQ)
            expect(herr is None and hr is not None and hr.failed,
                   "manifest {}: Hazard offline_pack.check gives the FAILED result".format(name))
            r2, err2 = safe(PC.check_pack, os.path.join(f, "manifest.json"), required=REQ)
            expect(err2 is None and r2 is not None and r2.failed,
                   "manifest {}: the manifest.json path itself: FAILED".format(name))
            sig, serr = safe(PC.pack_signature, f)
            expect(serr is None and isinstance(sig, list),
                   "manifest {}: pack_signature does not raise".format(name))
        expect(PC.manifest_problem(good) == "", "a good manifest: no problem")

        # -- 1.17.6: a UTF-8 byte-order mark - the pack readers do not read it, so the check
        #    must not pass it (it used to: a folder was silently "not an offline data pack")
        fb = os.path.join(tmp, "m_UTF-8_BOM")
        from DamSafety_Hazard.damsuite import gpkgpack as GP
        expect(GP.read_manifest(fb) is None and HOP.read_manifest(fb) is None,
               "BOM: the pack readers do not read the manifest (why the check FAILS)")
        zb = zip_folder(good, os.path.join(tmp, "bom.zip"), manifest=bom)
        PC.clear_cache()
        for what, pth in (("folder", fb), ("zip", zb)):
            r, err = safe(PC.check_pack, pth, required=REQ)
            expect(err is None and r is not None and r.failed and
                   "byte-order mark" in r.summary() and list(r.missing_required) == list(REQ),
                   "BOM {}: FAILED, named, the required layer missing ({})".format(
                       what, r and r.summary()))
        expect(PC.manifest_problem(fb) == PC.manifest_problem(zb) and
               "byte-order mark" in PC.manifest_problem(fb),
               "BOM: folder and zip give the same problem ({!r})".format(PC.manifest_problem(fb)))

        # -- 1.17.6: a pack-level problem (a format-1 pack's broken manifest.json; layer "")
        #    is named in the summary, not as "missing: " with an empty name
        v1 = os.path.join(tmp, "v1_badjson")
        T2.build_v1(v1)
        write_manifest(v1, "{oops")
        PC.clear_cache()
        r, err = safe(PC.check_pack, v1, required=REQ)
        summ = r.summary() if r is not None else ""
        expect(err is None and r is not None and r.format == 1 and not r.ok and
               not r.missing_required and "missing" not in summ and
               "manifest.json is not valid JSON" in summ,
               "format 1, broken manifest.json: named, no empty 'missing:' ({})".format(summ))
        expect(PC.manifest_problem(os.path.join(tmp, "nowhere")) == "",
               "no manifest at all: not a manifest problem (not a pack)")

        # -- manifest.json is a folder
        d = shutil.copytree(good, os.path.join(tmp, "m_dir"))
        os.remove(os.path.join(d, "manifest.json"))
        os.makedirs(os.path.join(d, "manifest.json"))
        PC.clear_cache()
        r, err = safe(PC.check_pack, d, required=REQ)
        expect(err is None and r is not None, "manifest.json a folder: no exception ({})".format(
            err))

        # -- one bad entry: a named "manifest" issue; required -> error
        for name, fn in [("entry a string", lambda m: m["datasets"].update(
                             NSI_structures="structures/NSI_structures.gpkg")),
                         ("file a number", lambda m: m["datasets"]["NSI_structures"].update(
                             file=5)),
                         ("count text", lambda m: m["datasets"]["NSI_structures"].update(
                             count="many")),
                         ("fields a dict", lambda m: m["datasets"]["NSI_structures"].update(
                             fields={"a": 1}))]:
            f = shutil.copytree(good, os.path.join(tmp, "e_" + name.replace(" ", "_")))
            T2.edit_manifest(f, fn)
            PC.clear_cache()
            r, err = safe(PC.check_pack, f, required=REQ)
            expect(err is None and r is not None, "{}: no exception ({})".format(name, err))
            if r is None:
                continue
            if name in ("entry a string", "file a number"):
                expect("manifest" in T2.kinds(r, "NSI_structures") and
                       list(r.missing_required) == ["NSI_structures"],
                       "{}: a manifest issue, the required layer missing ({})".format(
                           name, r.summary()))
            r3, err3 = safe(PC.check_pack, f)
            expect(err3 is None and r3 is not None and not r3.missing_required,
                   "{}: not required -> no required error".format(name))

        # -- a cut-short GeoPackage (half-written copy), a garbage one, an empty one
        for name, cut in [("cut short", "half"), ("garbage", "junk"), ("empty", "empty")]:
            f = shutil.copytree(good, os.path.join(tmp, "g_" + cut))
            p = os.path.join(f, "structures", "NSI_structures.gpkg")
            with open(p, "rb") as fh:
                raw = fh.read()
            with open(p, "wb") as fh:
                fh.write({"half": raw[:len(raw) // 2], "junk": b"not a geopackage" * 64,
                          "empty": b""}[cut])
            PC.clear_cache()
            r, err = safe(PC.check_pack, f, required=REQ)
            expect(err is None and r is not None, "GPKG {}: no exception ({})".format(name, err))
            if r is None:
                continue
            expect("unreadable" in T2.kinds(r, "NSI_structures") or
                   "missing" in T2.kinds(r, "NSI_structures"),
                   "GPKG {}: named unreadable ({})".format(name, r.summary()))
            expect(list(r.missing_required) == ["NSI_structures"],
                   "GPKG {}: required -> error".format(name))
            sp = PC.sqlite_file_problem(p)
            expect(bool(sp), "GPKG {}: sqlite_file_problem: {}".format(name, sp))
            r4, _ = safe(PC.check_pack, f, required=REQ, deep=False)
            expect(r4 is not None and list(r4.missing_required) == ["NSI_structures"],
                   "GPKG {}: the quick check (deep=False) finds it too".format(name))
        PC.clear_cache()
        ri = PC.check_pack(good, required=REQ, integrity=True)
        expect(ri.ok, "integrity=True on a good pack: OK ({})".format(ri.summary()))
        expect(PC.sqlite_file_problem(os.path.join(good, "structures", "NSI_structures.gpkg"),
                                      integrity=True) == "",
               "sqlite_file_problem(integrity=True) on a good file: none")

        # -- zipped packs: the listing
        zgood = zip_folder(good, os.path.join(tmp, "good.zip"))
        PC.clear_cache()
        r, err = safe(PC.check_pack, zgood, required=REQ)
        expect(err is None and r is not None and r.ok and r.format == 2,
               "zip: a good zipped pack OK ({})".format(r and r.summary()))
        zmiss = zip_folder(good, os.path.join(tmp, "miss.zip"),
                           skip=("structures/NSI_structures.gpkg",))
        r, err = safe(PC.check_pack, zmiss, required=REQ)
        expect(err is None and r is not None and list(r.missing_required) == ["NSI_structures"],
               "zip: a listed file not in the zip -> required missing ({})".format(
                   r and r.summary()))
        zempty = zip_folder(good, os.path.join(tmp, "empty.zip"),
                            empty=("hydro/3DHP_flowlines.gpkg",))
        r, err = safe(PC.check_pack, zempty)
        expect(err is None and r is not None and T2.kinds(r, "3DHP_flowlines"),
               "zip: an empty listed file is named ({})".format(r and r.summary()))
        zbad = zip_folder(good, os.path.join(tmp, "bad.zip"), manifest="{oops")
        r, err = safe(PC.check_pack, zbad, required=REQ)
        expect(err is None and r is not None and r.failed and list(r.missing_required) == list(REQ),
               "zip: a broken manifest.json in the zip -> FAILED ({})".format(r and r.summary()))
        with open(os.path.join(tmp, "notzip.zip"), "wb") as fh:
            fh.write(b"PK\x03\x04 truncated")
        r, err = safe(PC.check_pack, os.path.join(tmp, "notzip.zip"), required=REQ)
        expect(err is None and r is not None, "zip: a broken zip file: no exception")

        # -- pack_signature: a layer file changed in place
        sg = shutil.copytree(good, os.path.join(tmp, "sig"))
        s1 = PC.pack_signature(sg)
        rels = [x[0] for x in s1]
        expect("manifest.json" in rels and "structures/NSI_structures.gpkg" in rels,
               "pack_signature lists the manifest and the layer files")
        p = os.path.join(sg, "hydro", "3DHP_flowlines.gpkg")
        st = os.stat(p)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5 * 10 ** 9))
        s2 = PC.pack_signature(sg)
        expect(s1 != s2, "pack_signature: a layer file touched -> a new signature")
        with open(p, "ab") as fh:
            fh.write(b"\0")
        expect(PC.pack_signature(sg) != s2, "pack_signature: a layer file grown -> new")
        expect(PC.pack_signature(sg) == PC.pack_signature(sg), "pack_signature: stable")

        # -- read-only
        expect(sorted(os.listdir(good)) == ["LAYERS.txt", "hydro", "manifest.json",
                                            "structures"], "nothing written into the pack")
    finally:
        PC.clear_cache()
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n{} failure(s)".format(len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
