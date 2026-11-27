"""Security audit run-2 (2026-11-24): "Mark the classification reviewed" hashes a package's
MANIFEST.csv without reading an unbounded zip entry into memory. No QGIS.

A submittal package comes from someone else. Its .zip MANIFEST.csv was read whole
(zf.read), so a ~600 KB zip whose manifest inflates to gigabytes ended in a MemoryError. Now it
is hashed in blocks and refused above markreviewed.MAX_MANIFEST_BYTES.

    python3 DamSafety_Hazard/tests/test_markreviewed_manifest_cap.py
"""
import hashlib
import os
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from DamSafety_Hazard import markreviewed as M  # noqa: E402

T = tempfile.mkdtemp(prefix="hz_markreviewed_")
fails = []


def check(cond, msg):
    print(("ok   " if cond else "FAIL ") + msg)
    if not cond:
        fails.append(msg)


def _zip(name, members):
    p = os.path.join(T, name)
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        for arc, data in members:
            zf.writestr(arc, data)
    return p


# 1. a normal package: the same digest as before (sha256 of the manifest's bytes)
body = b"path,sha256\nreport.pdf,abc\n" * 10
good = _zip("good.zip", [("pkg/MANIFEST.csv", body), ("pkg/report.pdf", b"%PDF")])
sha, where = M.manifest_sha256(good)
check(sha == hashlib.sha256(body).hexdigest(), "a normal package's manifest digest is unchanged")
check(where.endswith("!pkg/MANIFEST.csv"), "and its location names the zip entry")

# 2. a manifest bigger than the cap (a small cap here keeps the test light; the logic is the same)
old = M.MAX_MANIFEST_BYTES
M.MAX_MANIFEST_BYTES = 256 * 1024
try:
    big = _zip("big.zip", [("MANIFEST.csv", b"\0" * (3 * 1024 * 1024))])
    check(os.path.getsize(big) < 64 * 1024, "the bomb zip is small on disk")
    try:
        M.manifest_sha256(big)
        check(False, "an oversize manifest is refused")
    except ValueError as e:
        check("refused" in str(e), "an oversize manifest is refused with a ValueError: {}".format(e))

    # 3. a zip whose central directory under-states the size: the bytes read are counted
    # (zipfile then stops at the stated size and its CRC check fails: a ValueError either way,
    # and never more than the cap in memory)
    import shutil
    lie = os.path.join(T, "lie.zip")
    shutil.copy(big, lie)
    real = M.zipfile.ZipFile.getinfo

    def fake_getinfo(self, name):
        i = real(self, name)
        i.file_size = 10
        return i
    M.zipfile.ZipFile.getinfo = fake_getinfo
    try:
        M.manifest_sha256(lie)
        check(False, "an under-stated size is caught while reading")
    except ValueError as e:
        check(True, "an under-stated size is caught while reading: {}".format(e))
    finally:
        M.zipfile.ZipFile.getinfo = real
finally:
    M.MAX_MANIFEST_BYTES = old

# 4. the real cap is 64 MiB
check(M.MAX_MANIFEST_BYTES == 64 * 1024 * 1024, "the cap is 64 MiB")

# 6. a .zip whose directory is broken is a ValueError too
broken = os.path.join(T, "broken.zip")
data = bytearray(open(good, "rb").read())
cd = data.rfind(b"PK\x01\x02")
data[cd:cd + 4] = b"PK\x09\x09"
open(broken, "wb").write(bytes(data))
if zipfile.is_zipfile(broken):
    try:
        M.manifest_sha256(broken)
        check(False, "a broken .zip is refused")
    except ValueError as e:
        check(True, "a broken .zip is a ValueError: {}".format(str(e)[:80]))

# 5. a corrupt entry is a ValueError, not a crash of the tool
bad = _zip("bad.zip", [("MANIFEST.csv", b"x" * 5000)])
raw = bytearray(open(bad, "rb").read())
off = 30 + len(b"MANIFEST.csv")                  # the local header, then the deflated data
for i in range(off + 2, off + 12):
    raw[i] ^= 0xFF
open(bad, "wb").write(bytes(raw))
try:
    M.manifest_sha256(bad)
    check(False, "a corrupt manifest entry is refused")
except ValueError as e:
    check(True, "a corrupt manifest entry is a ValueError: {}".format(str(e)[:80]))

print("\n{} failed".format(len(fails)) if fails else "\nOK: markreviewed manifest cap")
sys.exit(1 if fails else 0)
