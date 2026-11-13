"""Screening 0.19.8 (security audit pass 3): a LiDAR tile download and its zip are bounded.

  1. _safe_extract: an ordinary tile zip unpacks; one that unpacks past the tile limit, holds a
     symbolic link or climbs out of the folder is refused (ValueError) and nothing is written
  2. _capped_write / _ftp_retr: a plain-FTP transfer past the limit is stopped and the partial
     file removed
  3. _https_get: an HTTPS body past the limit is stopped at once, not retried, no file kept;
     an ordinary body still arrives through <name>.part

No network (the transfers are fakes), no large file (the limit is lowered for the test).
Run: python3 tests/test_dem_download_caps.py
"""
import importlib
import io
import os
import shutil
import stat
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(PLUGIN))
PKG = os.path.basename(PLUGIN)
dd = importlib.import_module(PKG + ".core.dem_download")
ZS = importlib.import_module(PKG + ".damsuite.zipsafe")

W = tempfile.mkdtemp(prefix="dem_caps_")
n = 0


def ok(cond, msg):
    global n
    assert cond, msg
    n += 1
    print("ok  " + msg)


def make(name, entries):
    p = os.path.join(W, name)
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        for item, data in entries:
            z.writestr(item, data)
    return p


def refused(path, dest):
    with zipfile.ZipFile(path) as z:
        try:
            dd._safe_extract(z, dest)
        except ValueError as e:
            return str(e)
    return ""


def files(folder):
    return sorted(f for _d, _s, fs in os.walk(folder) for f in fs) if os.path.isdir(folder) else []


try:
    ok(dd._zipsafe() is ZS, "the tile reader uses damsuite.zipsafe (DamSafety Core 1.4.0)")
    ok(dd.MAX_TILE_UNPACKED_BYTES == 8 * 1024 ** 3 and dd.MAX_TILE_DOWNLOAD_BYTES == 4 * 1024 ** 3,
       "tile limits: 4 GB downloaded, 8 GB unpacked")

    # ---- 1. the zip
    tile = make("tile.zip", [("t/dem.img", b"\1" * 5000), ("t/dem.prj", b"p")])
    d1 = os.path.join(W, "d1")
    with zipfile.ZipFile(tile) as z:
        dd._safe_extract(z, d1)
    ok(files(d1) == ["dem.img", "dem.prj"], "an ordinary tile zip unpacks")

    old = dd.MAX_TILE_UNPACKED_BYTES
    dd.MAX_TILE_UNPACKED_BYTES = 1000
    try:
        why = refused(tile, os.path.join(W, "d2"))
    finally:
        dd.MAX_TILE_UNPACKED_BYTES = old
    ok("limit" in why and files(os.path.join(W, "d2")) == [],
       "a zip that unpacks past the tile limit is refused, nothing written")

    climb = make("climb.zip", [("t/dem.img", b"x"), ("../../evil.txt", b"x")])
    why = refused(climb, os.path.join(W, "d3"))
    ok("'..'" in why and files(os.path.join(W, "d3")) == [] and
       not os.path.exists(os.path.join(os.path.dirname(W), "evil.txt")),
       "an entry that climbs out of the folder is refused, nothing written")

    link = os.path.join(W, "link.zip")
    with zipfile.ZipFile(link, "w") as z:
        info = zipfile.ZipInfo("t/out")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "/etc")
    why = refused(link, os.path.join(W, "d4"))
    ok("symbolic link" in why, "a symbolic link in the zip is refused")

    # ---- 2. plain FTP
    buf = io.BytesIO()
    write = dd._capped_write(buf, "tile.zip", limit=2500)
    write(b"a" * 1000)
    write(b"b" * 1500)
    try:
        write(b"c")
        ok(False, "not stopped")
    except dd.TileTooLarge as e:
        ok(isinstance(e, IOError) and "NDSUITE_ZIP_MAX_GB" in str(e) and
           buf.getvalue() == b"a" * 1000 + b"b" * 1500,
           "the write stops once more than the limit has arrived, and says how to raise it")

    class FakeFtp(object):
        def __init__(self, blocks):
            self.blocks = blocks

        def retrbinary(self, cmd, callback):
            for b in self.blocks:
                callback(b)

    old = dd.MAX_TILE_DOWNLOAD_BYTES
    dd.MAX_TILE_DOWNLOAD_BYTES = 3000
    try:
        local = os.path.join(W, "ftp_ok.zip")
        dd._ftp_retr(FakeFtp([b"z" * 1000] * 3), "a/ok.zip", local)
        ok(os.path.getsize(local) == 3000, "an FTP transfer up to the limit is kept")
        local = os.path.join(W, "ftp_big.zip")
        try:
            dd._ftp_retr(FakeFtp([b"z" * 1000] * 50), "a/big.zip", local)
            ok(False, "not stopped")
        except dd.TileTooLarge:
            ok(not os.path.exists(local), "an FTP transfer past the limit is stopped, no file kept")

        # ---- 3. HTTPS
        import time
        import urllib.request
        calls, sleeps = [], []

        class Body(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        real_open, real_sleep = urllib.request.urlopen, time.sleep
        time.sleep = lambda s: sleeps.append(s)
        try:
            urllib.request.urlopen = lambda req, timeout=None: (calls.append(1), Body(b"q" * 2500))[1]
            local = os.path.join(W, "https_ok.zip")
            dd._https_get("https://example.invalid/ok.zip", local)
            ok(os.path.getsize(local) == 2500 and not os.path.exists(local + ".part") and
               len(calls) == 1, "an ordinary HTTPS body arrives through .part")

            del calls[:]
            urllib.request.urlopen = lambda req, timeout=None: (calls.append(1), Body(b"q" * 90000))[1]
            local = os.path.join(W, "https_big.zip")
            try:
                dd._https_get("https://example.invalid/big.zip", local)
                ok(False, "not stopped")
            except dd.TileTooLarge:
                ok(len(calls) == 1 and not sleeps, "an HTTPS body past the limit is not tried again")
                ok(not os.path.exists(local) and not os.path.exists(local + ".part"),
                   "and neither the file nor its .part is kept")
        finally:
            urllib.request.urlopen, time.sleep = real_open, real_sleep
    finally:
        dd.MAX_TILE_DOWNLOAD_BYTES = old
finally:
    shutil.rmtree(W, ignore_errors=True)

print("\nOK ({} checks)".format(n))
