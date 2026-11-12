# -*- coding: utf-8 -*-
"""H01: a cut-short ArcGIS download must not become today's reusable day cache.

_arcgis_collect returns (features, complete); complete is False when the service cut the answer
short (page limit, or a layer that will not page). Before the fix both callers dropped the flag,
so _fetch_arcgis_live wrote the truncated features to the canonical day file _cache_path(...),
and every later run that day reused them with no sign the inventory was short - a dam could come
out LOW on a truncated inventory.

The fix: when the answer is incomplete, _fetch_arcgis_live writes it under a _downloads/_partial/
subfolder (never _todays_file / _stale_cache / _seed_downloads territory) so the next run
re-fetches, and records it in download.INCOMPLETE_USED so the truncation reaches the run log and
the data-source list. A complete answer is unchanged: it lands in the canonical day file and is
reused the same day.

Network is fully stubbed (no service is contacted); nothing outside a temp folder is touched.
    python DamSafety_Hazard/tests/test_incomplete_download_cache.py
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard import download as DL  # noqa: E402

FAILS = []
BBOX = (-104.1, 45.9, -96.5, 49.1)
URL = "https://example.invalid/FeatureServer/0"


def expect(cond, what):
    print("{} {}".format("ok  " if cond else "FAIL", what))
    if not cond:
        FAILS.append(what)


def _pt(i):
    return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-100.0, 47.0]},
            "properties": {"id": i, "NAME": "F%d" % i}}


class _FakeNorm(object):
    def normaliser_for_url(self, url):
        return None


def _install(monkey_feats, monkey_complete, calls):
    """Stub the one network call and the pack/cache lookups so the sequential path runs offline."""
    def fake_collect(url, extent, name, where, progress_cb, paging=None):
        calls.append(name)
        return list(monkey_feats), monkey_complete
    DL._arcgis_collect = fake_collect
    DL.active_tile_cache = lambda: None            # sequential path
    DL.offline_config = lambda: (None, False)      # no offline pack
    DL._hydro3dhp = lambda: _FakeNorm()            # no 3DHP normalisation


def run():
    out = tempfile.mkdtemp(prefix="h01_")
    try:
        folder = DL.download_folder(out)
        canon = DL._cache_path(folder, "TestLayer", BBOX)
        partial = DL._partial_path(canon)

        # ---- incomplete answer: diverted to _partial/, recorded, never reused -------------
        del DL.INCOMPLETE_USED[:]
        calls = []
        _install([_pt(0), _pt(1)], False, calls)
        lyr, n, path = DL.fetch_arcgis_layer(URL, BBOX, out, "TestLayer",
                                             allow_stale=False, pack_fallback=False)
        expect(n == 2, "incomplete: the 2 cut-short features are used this run")
        expect(os.path.basename(os.path.dirname(path)) == "_partial",
               "incomplete: the returned path is under _partial/")
        expect(os.path.isfile(partial), "incomplete: the file is written under _partial/")
        expect(not os.path.isfile(canon),
               "incomplete: the canonical day file is NOT written (so it cannot be reused)")
        expect([t for t in DL.INCOMPLETE_USED if t[0] == "TestLayer" and t[1] == 2],
               "incomplete: INCOMPLETE_USED records (name, kept=2, reason)")

        # a second run the same day must re-fetch (no _todays_file short-circuit)
        lyr2, n2, path2 = DL.fetch_arcgis_layer(URL, BBOX, out, "TestLayer",
                                                allow_stale=False, pack_fallback=False)
        expect(len(calls) == 2, "incomplete: the next run re-fetches (did not reuse the partial)")

        # ---- complete answer: canonical day file, reused the same day ----------------------
        del DL.INCOMPLETE_USED[:]
        calls = []
        _install([_pt(0), _pt(1), _pt(2)], True, calls)
        lyr, n, path = DL.fetch_arcgis_layer(URL, BBOX, out, "TestLayer2",
                                             allow_stale=False, pack_fallback=False)
        canon2 = DL._cache_path(folder, "TestLayer2", BBOX)
        expect(n == 3 and os.path.abspath(path) == os.path.abspath(canon2),
               "complete: the answer lands in the canonical day file")
        expect(os.path.isfile(canon2), "complete: the canonical day file exists")
        expect(not DL.INCOMPLETE_USED, "complete: nothing recorded in INCOMPLETE_USED")
        # a second run the same day reuses it (no re-fetch)
        DL.fetch_arcgis_layer(URL, BBOX, out, "TestLayer2", allow_stale=False, pack_fallback=False)
        expect(len(calls) == 1, "complete: the same-day re-run reuses today's cached download")
    finally:
        shutil.rmtree(out, ignore_errors=True)


def main():
    run()
    print("\n{} check(s), {} failed".format("incomplete-download-cache", len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
