"""DamSafety Core security: the offline-pack extractor refuses a decompression bomb and a member
that would be written outside the cache folder (path traversal), including the Windows
drive-relative escape in a later path segment.

    python3 DamSafety_Core/tests/test_offline_pack_safety.py
"""
import io
import os
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from DamSafety_Core.damsuite import offline_pack as op
from DamSafety_Core.damsuite import zipsafe


def _zip(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members:
            z.writestr(name, data)
    buf.seek(0)
    return zipfile.ZipFile(buf)


def test_safe_member():
    assert op._safe_member("packroot/sub/file.gpkg")
    assert not op._safe_member("packroot/sub/D:evil.gpkg")   # Windows drive in a later segment
    assert not op._safe_member("../evil")
    assert not op._safe_member("/etc/passwd")
    assert not op._safe_member("a/../../b")


def test_bomb_refused():
    z = _zip([("manifest.json", b"{}"), ("data.bin", b"x" * 4096)])
    with tempfile.TemporaryDirectory() as d:
        os.environ["NDSUITE_ZIP_MAX_GB"] = "0.000001"   # ~1 KB cap: the 4 KB entry is over it
        try:
            raised = False
            try:
                op._check_pack_size(z, d)
            except op.PackError:
                raised = True
            assert raised, "a pack over the size cap must raise PackError"
        finally:
            os.environ.pop("NDSUITE_ZIP_MAX_GB", None)


def test_zip_slip_refused():
    # zipsafe.check (behind _check_pack_size) rejects a member that climbs out of the destination.
    # (The Windows drive-relative escape "sub/D:evil.gpkg" is a plain filename on posix; it is caught
    # cross-platform by _safe_member above, and by zipsafe's realpath check under ntpath on Windows.)
    for bad in ("../escape.txt", "a/../../b.txt"):
        z = _zip([("manifest.json", b"{}"), (bad, b"x")])
        with tempfile.TemporaryDirectory() as d:
            raised = False
            try:
                zipsafe.check(z, d)
            except zipsafe.ZipRefused:
                raised = True
            assert raised, "member {!r} must be refused".format(bad)


def test_legit_pack_passes():
    z = _zip([("packroot/manifest.json", b"{}"), ("packroot/data.gpkg", b"hello")])
    with tempfile.TemporaryDirectory() as d:
        op._check_pack_size(z, d, skip=lambda n: not op._safe_member(n))  # no raise


def main():
    test_safe_member()
    test_bomb_refused()
    test_zip_slip_refused()
    test_legit_pack_passes()
    print("test_offline_pack_safety: all tests passed")


if __name__ == "__main__":
    main()
