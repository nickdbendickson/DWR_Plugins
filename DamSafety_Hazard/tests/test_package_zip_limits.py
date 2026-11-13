"""Hazard 2.25.2 (security audit pass 3): a package someone hands the reviewer is checked before
it is unpacked.

  1. reproduce.extract_package: an ordinary package unpacks; one that unpacks past the size
     limit, or that holds a symbolic link, is refused with a ValueError and nothing is written
  2. dsswise.open_package: the same size check; junk entries are not counted; zips nested more
     than MAX_ZIP_DEPTH deep are left as files instead of being unpacked without end

The size limit is lowered for the test with NDSUITE_ZIP_MAX_GB (no large file is written).

    python3 DamSafety_Hazard/tests/test_package_zip_limits.py
"""
import io
import os
import shutil
import stat
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from DamSafety_Hazard import dsswise as D  # noqa: E402
from DamSafety_Hazard import reproduce as R  # noqa: E402
from DamSafety_Hazard.damsuite import zipsafe as ZS  # noqa: E402

fails = []
W = tempfile.mkdtemp(prefix="hz_zip_")
SMALL = "0.00001"                    # about 10 KB


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def make(name, entries):
    p = os.path.join(W, name)
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        for n, data in entries:
            z.writestr(n, data)
    return p


def zip_bytes(entries):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        for n, data in entries:
            z.writestr(n, data)
    return b.getvalue()


def refused(fn, *a):
    try:
        fn(*a)
    except ValueError as e:
        return str(e)
    return ""


class Limit(object):
    def __init__(self, gb):
        self.gb = gb

    def __enter__(self):
        self.old = os.environ.get(ZS.ENV)
        os.environ[ZS.ENV] = self.gb

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop(ZS.ENV, None)
        else:
            os.environ[ZS.ENV] = self.old


try:
    # ---- 1. reproduce.extract_package
    good = make("pkg.zip", [("Dam_A/MANIFEST.csv", b"file,sha256\n"),
                            ("Dam_A/inputs/depth.txt", b"1\n" * 50)])
    w1 = os.path.join(W, "w1")
    os.makedirs(w1)
    top = R.extract_package(good, w1)
    check(os.path.isfile(os.path.join(top, "MANIFEST.csv")) and
          os.path.basename(top) == "Dam_A", "an ordinary package unpacks as before")

    big = make("big.zip", [("Dam_B/MANIFEST.csv", b"x"), ("Dam_B/huge.bin", b"\0" * 200000)])
    w2 = os.path.join(W, "w2")
    os.makedirs(w2)
    with Limit(SMALL):
        why = refused(R.extract_package, big, w2)
    check("was not unpacked" in why and "limit" in why and ZS.ENV in why,
          "a package past the size limit is refused, and the message names the setting")
    check(not os.listdir(os.path.join(w2, "_package")), "nothing of it was written")
    check(os.path.basename(R.extract_package(big, w2)) == "Dam_B",
          "the same package unpacks under the ordinary limit")

    link = os.path.join(W, "link.zip")
    with zipfile.ZipFile(link, "w") as z:
        z.writestr("Dam_C/MANIFEST.csv", b"x")
        info = zipfile.ZipInfo("Dam_C/out")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "/etc")
    w3 = os.path.join(W, "w3")
    os.makedirs(w3)
    why = refused(R.extract_package, link, w3)
    check("symbolic link" in why and not os.listdir(os.path.join(w3, "_package")),
          "a package holding a symbolic link is refused, nothing written")

    # ---- 2. dsswise.open_package
    pkg = make("dss.zip", [("run/MaxDepth_in_Ft.tif", b"d" * 100),
                           ("__MACOSX/._MaxDepth_in_Ft.tif", b"j" * 200000)])
    w4 = os.path.join(W, "w4")
    os.makedirs(w4)
    with Limit(SMALL):
        folder = D.open_package(pkg, w4)
    found = [f for _d, _s, fs in os.walk(folder) for f in fs]
    check(found == ["MaxDepth_in_Ft.tif"],
          "junk entries are neither unpacked nor counted against the limit")

    bigd = make("dssbig.zip", [("run/MaxDepth_in_Ft.tif", b"\0" * 200000)])
    w5 = os.path.join(W, "w5")
    os.makedirs(w5)
    with Limit(SMALL):
        why = refused(D.open_package, bigd, w5)
    check("was not unpacked" in why and "limit" in why,
          "a DSS-WISE package past the size limit is refused")
    check(not [f for _d, _s, fs in os.walk(w5) for f in fs], "nothing of it was written")

    # zip in zip in zip in zip in zip: levels 1-3 are unpacked, the 4th is left as a file
    z5 = zip_bytes([("deepest.txt", b"5")])
    z4 = zip_bytes([("l4.txt", b"4"), ("five.zip", z5)])
    z3 = zip_bytes([("l3.txt", b"3"), ("four.zip", z4)])
    z2 = zip_bytes([("l2.txt", b"2"), ("three.zip", z3)])
    nest = make("nest.zip", [("l1.txt", b"1"), ("two.zip", z2)])
    w6 = os.path.join(W, "w6")
    os.makedirs(w6)
    folder = D.open_package(nest, w6)
    names = sorted(f for _d, _s, fs in os.walk(folder) for f in fs)
    check(D.MAX_ZIP_DEPTH == 3 and "l3.txt" in names and "four.zip" in names,
          "zips are unpacked {} deep".format(D.MAX_ZIP_DEPTH))
    check("l4.txt" not in names and "deepest.txt" not in names,
          "a zip nested deeper is left as a file: {}".format(names))
finally:
    shutil.rmtree(W, ignore_errors=True)

if fails:
    print("\n{} FAILED".format(len(fails)))
    sys.exit(1)
print("\nOK (package zip limits)")
