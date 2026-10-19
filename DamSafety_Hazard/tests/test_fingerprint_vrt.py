# -*- coding: utf-8 -*-
"""Regression: fingerprint.py nested-VRT handling (the version both forks share via the pin slice).

The evaluation key fingerprints every file a run reads, so a changed input forces a re-run. For a
VRT mosaic that means the tiles it points at - including tiles reached through a NESTED VRT, and
tiles whose <SourceFilename> has no relativeToVRT attribute (GDAL treats those as relative). This
locks in the behaviour nd_hazard converged to:

  * nested VRTs are followed (and a cycle does not recurse forever),
  * a SourceFilename with no relativeToVRT counts (GDAL default: relative to the VRT),
  * &amp; / &lt; etc. in a path are unescaped before the tile is resolved,
  * truncating the source list at the tile cap marks the signature incomplete (FP.2),
  * changing a nested tile changes file_sig.

history.py-style QGIS-free test (fingerprint imports only damsuite.chain):

    python3 DamSafety_Hazard/tests/test_fingerprint_vrt.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from DamSafety_Hazard import fingerprint as FP            # noqa: E402

TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def _w(path, data=b"x"):
    with open(path, "wb") as f:
        f.write(data)
    return path


def _vrt(path, sources):
    """Write a minimal VRT. `sources` is a list of (name, rel) where rel is "0", "1" or None
    (None omits the relativeToVRT attribute entirely)."""
    rows = []
    for name, rel in sources:
        attr = "" if rel is None else ' relativeToVRT="{}"'.format(rel)
        rows.append("<SimpleSource><SourceFilename{}>{}</SourceFilename></SimpleSource>".format(
            attr, name))
    with open(path, "w", encoding="utf-8") as f:
        f.write('<VRTDataset rasterXSize="2" rasterYSize="2"><VRTRasterBand>'
                + "".join(rows) + "</VRTRasterBand></VRTDataset>")
    return path


def _names(vrt, **kw):
    paths, incomplete = FP._vrt_source_paths(vrt, **kw)
    return [os.path.basename(p) for p in (paths or [])], incomplete


@test
def fingerprint_follows_nested_vrts_and_default_relative():
    d = tempfile.mkdtemp(prefix="fpvrt_")
    tiles = os.path.join(d, "tiles")
    os.makedirs(tiles)
    _w(os.path.join(tiles, "t1.tif"), b"one")
    _w(os.path.join(tiles, "t2.tif"), b"two")
    _w(os.path.join(tiles, "t3.tif"), b"three")
    # inner.vrt: t1 with relativeToVRT="1"; t2 with NO attribute (must still count, as relative)
    _vrt(os.path.join(d, "inner.vrt"), [("tiles/t1.tif", "1"), ("tiles/t2.tif", None)])
    # outer.vrt references the nested inner.vrt plus a direct tile
    _vrt(os.path.join(d, "outer.vrt"), [("inner.vrt", "1"), ("tiles/t3.tif", "1")])
    names, incomplete = _names(os.path.join(d, "outer.vrt"))
    assert names == ["t1.tif", "t2.tif", "t3.tif"], names    # nested followed; no-attr source kept
    assert incomplete is False


@test
def fingerprint_unescapes_entities_in_source_names():
    d = tempfile.mkdtemp(prefix="fpvrt_")
    _w(os.path.join(d, "a&b.tif"), b"amp")
    _vrt(os.path.join(d, "m.vrt"), [("a&amp;b.tif", "1")])
    names, _ = _names(os.path.join(d, "m.vrt"))
    assert names == ["a&b.tif"], names


@test
def fingerprint_vrt_cycle_terminates():
    d = tempfile.mkdtemp(prefix="fpvrt_")
    _vrt(os.path.join(d, "a.vrt"), [("b.vrt", "1")])         # a -> b -> a
    _vrt(os.path.join(d, "b.vrt"), [("a.vrt", "1")])
    names, incomplete = _names(os.path.join(d, "a.vrt"))     # must return, not recurse forever
    assert names == [] and incomplete is False, (names, incomplete)


@test
def fingerprint_marks_truncated_source_list_incomplete():
    d = tempfile.mkdtemp(prefix="fpvrt_")
    for n in ("t1.tif", "t2.tif", "t3.tif"):
        _w(os.path.join(d, n))
    _vrt(os.path.join(d, "big.vrt"), [("t1.tif", "1"), ("t2.tif", "1"), ("t3.tif", "1")])
    paths, incomplete = FP._vrt_source_paths(os.path.join(d, "big.vrt"), limit=2)
    assert len(paths) == 2 and incomplete is True, (paths, incomplete)
    # _vrt_sources surfaces the truncation as a trailing ["incomplete", True] (what file_sig stores)
    srcs = FP._vrt_sources(os.path.join(d, "big.vrt"), limit=2)
    assert srcs[-1] == ["incomplete", True], srcs
    assert len([s for s in srcs if s[0] != "incomplete"]) == 2, srcs


@test
def fingerprint_file_sig_changes_when_a_nested_tile_changes():
    d = tempfile.mkdtemp(prefix="fpvrt_")
    tiles = os.path.join(d, "tiles")
    os.makedirs(tiles)
    _w(os.path.join(tiles, "t1.tif"), b"before")
    _vrt(os.path.join(d, "inner.vrt"), [("tiles/t1.tif", "1")])
    _vrt(os.path.join(d, "outer.vrt"), [("inner.vrt", "1")])
    before = FP.file_sig(os.path.join(d, "outer.vrt"))
    _w(os.path.join(tiles, "t1.tif"), b"after-a-rewrite")    # same name, new content
    after = FP.file_sig(os.path.join(d, "outer.vrt"))
    assert before != after, "a rewritten nested tile must change the VRT signature"


if __name__ == "__main__":
    failed = 0
    for f in TESTS:
        try:
            f()
            print("ok    ", f.__name__)
        except Exception as e:
            failed += 1
            print("FAIL  ", f.__name__, "->", repr(e))
    print("{} tests, {} failed".format(len(TESTS), failed))
    sys.exit(failed)
