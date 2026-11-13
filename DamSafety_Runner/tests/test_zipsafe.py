"""Core 1.4.0 (security audit pass 3): damsuite.zipsafe - a zip is checked before a byte of it
is written. Entries that leave the destination, symbolic links, too many entries, an unpacked
size over the caller's limit, a "zip bomb" compression ratio and a disk that is too small are
all refused, and nothing is extracted. A download past its limit is stopped."""
import io
import os
import shutil
import stat
import tempfile
import zipfile

from nd_breach_runner.damsuite import zipsafe as ZS
from nd_breach_runner.tests.registry import test


def _zip(path, entries, stored=False):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            z.writestr(name, data)
    return path


def _refused(path, dest, **kw):
    with zipfile.ZipFile(path) as z:
        try:
            ZS.extract(z, dest, **kw)
        except ZS.ZipRefused as e:
            assert isinstance(e, ValueError)
            return str(e)
    raise AssertionError("extracted")


def _empty(dest):
    return not os.path.isdir(dest) or not os.listdir(dest)


@test
def zipsafe_a_normal_archive_is_extracted_and_returns_its_paths():
    d = tempfile.mkdtemp()
    try:
        p = _zip(os.path.join(d, "a.zip"), [("tile/dem.asc", b"1 2 3\n" * 100),
                                            ("tile/dem.prj", b"x"), ("__MACOSX/._junk", b"j")])
        dest = os.path.join(d, "out")
        with zipfile.ZipFile(p) as z:
            assert ZS.check(z, dest) == 600 + 1 + 1
            calls = []
            out = ZS.extract(z, dest, skip=lambda n: n.startswith("__MACOSX"),
                             cancel=lambda: calls.append(1))
        assert [os.path.relpath(o, dest).replace(os.sep, "/") for o in out] == \
            ["tile/dem.asc", "tile/dem.prj"] and len(calls) == 2
        assert not os.path.exists(os.path.join(dest, "__MACOSX"))
        assert open(os.path.join(dest, "tile", "dem.prj"), "rb").read() == b"x"
    finally:
        shutil.rmtree(d)


@test
def zipsafe_entries_that_leave_the_destination_are_refused_and_nothing_is_written():
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "out")
        for name, word in (("../evil.txt", "climbs out"), ("a/../../evil.txt", "climbs out"),
                           ("/abs/evil.txt", "absolute"), ("C:/evil.txt", "absolute"),
                           ("..\\evil.txt", "climbs out")):
            p = _zip(os.path.join(d, "t.zip"), [("ok.txt", b"ok"), (name, b"x")])
            why = _refused(p, dest)
            assert word in why and "evil" in why, (name, why)
            assert _empty(dest), name                      # not even the good entry
        assert not os.path.exists(os.path.join(d, "evil.txt"))
        # a symbolic link entry (the way out of a folder that names cannot show)
        p = os.path.join(d, "link.zip")
        with zipfile.ZipFile(p, "w") as z:
            info = zipfile.ZipInfo("link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, "/etc/passwd")
        assert "symbolic link" in _refused(p, dest) and _empty(dest)
        assert ZS.unsafe_name("") and ZS.unsafe_name("a\x00b") and not ZS.unsafe_name("a/b.tif")
    finally:
        shutil.rmtree(d)


@test
def zipsafe_size_count_ratio_and_disk_limits():
    d = tempfile.mkdtemp()
    try:
        dest = os.path.join(d, "out")
        p = _zip(os.path.join(d, "big.zip"), [("a.bin", b"\0" * 5000), ("b.bin", b"\0" * 5000)])
        why = _refused(p, dest, max_bytes=9999)
        assert "unpacks to" in why and ZS.ENV in why and _empty(dest), why
        with zipfile.ZipFile(p) as z:                      # exactly at the limit is fine
            assert ZS.check(z, dest, max_bytes=10000) == 10000
            # the user's override wins in both directions
            assert ZS.check(z, dest, max_bytes=1, environ={ZS.ENV: "1"}) == 10000
            try:
                ZS.check(z, dest, max_bytes=10 ** 9, environ={ZS.ENV: "0.000001"})
                raise AssertionError("the lower limit was ignored")
            except ZS.ZipRefused:
                pass
        assert ZS.limit(5, {}) == 5 and ZS.limit(5, {ZS.ENV: "junk"}) == 5
        assert ZS.limit(5, {ZS.ENV: "2"}) == 2 * ZS.GB and ZS.limit(5, {ZS.ENV: "-1"}) == 5
        # too many entries / a bomb ratio / a full disk: the limits are module values
        old = (ZS.MAX_MEMBERS, ZS.RATIO_FLOOR_BYTES, ZS.MAX_RATIO, ZS.free_bytes)
        try:
            ZS.MAX_MEMBERS = 1
            assert "2 entries" in _refused(p, dest)
            ZS.MAX_MEMBERS = old[0]
            bomb = _zip(os.path.join(d, "bomb.zip"), [("z.bin", b"\0" * 4000000)])
            ZS.RATIO_FLOOR_BYTES, ZS.MAX_RATIO = 1000000, 100
            why = _refused(bomb, dest)
            assert "no real data set compresses that hard" in why and _empty(dest), why
            # the same bytes stored (ratio 1) pass, and so does a small flat raster
            flat = _zip(os.path.join(d, "flat.zip"), [("z.bin", b"\0" * 4000000)], stored=True)
            with zipfile.ZipFile(flat) as z:
                assert ZS.check(z, dest) == 4000000
            ZS.RATIO_FLOOR_BYTES, ZS.MAX_RATIO = old[1], old[2]
            with zipfile.ZipFile(bomb) as z:
                assert ZS.check(z, dest) == 4000000        # under the 1 GB floor: allowed
            ZS.free_bytes = lambda folder: 1000
            why = _refused(p, dest)
            assert "not enough disk space" in why and _empty(dest), why
            ZS.free_bytes = lambda folder: None            # unknown: not refused for that
            with zipfile.ZipFile(p) as z:
                assert ZS.check(z, dest) == 10000
        finally:
            ZS.MAX_MEMBERS, ZS.RATIO_FLOOR_BYTES, ZS.MAX_RATIO, ZS.free_bytes = old
    finally:
        shutil.rmtree(d)


@test
def zipsafe_copy_capped_stops_a_download_past_its_limit():
    src, out = io.BytesIO(b"x" * 5000), io.BytesIO()
    assert ZS.copy_capped(src.read, out.write, max_bytes=5000, block=1024) == 5000
    assert out.getvalue() == b"x" * 5000
    src, out = io.BytesIO(b"x" * 5001), io.BytesIO()
    try:
        ZS.copy_capped(src.read, out.write, max_bytes=5000, what="tile.zip", block=1024)
        raise AssertionError("not stopped")
    except ZS.TooLarge as e:
        assert isinstance(e, ValueError) and "tile.zip is larger than" in str(e)
    assert len(out.getvalue()) <= 5000                     # never a byte past the limit
