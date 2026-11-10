"""Hazard 2.24.5, audit M02 (interim; no number touched): download._adopt_layer parks every
worker-thread layer in _ORPHANS and NEVER frees one (freeing touches the ended thread's PROJ
state). It now logs one line each time the count reaches a multiple of ORPHANS_LOG_EVERY, and
the ndhazard:batch / creep-watch help says to use WORKERS > 1 for long runs.

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_m02_orphans_log.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], False)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from qgis.core import QgsFeature, QgsGeometry, QgsPointXY, QgsVectorLayer  # noqa: E402

from DamSafety_Hazard import download as DL  # noqa: E402

FAILS = []


def check(name, cond):
    print("{:4} {}".format("ok" if cond else "FAIL", name))
    if not cond:
        FAILS.append(name)


def _mem(i):
    lyr = QgsVectorLayer("Point?crs=EPSG:4326", "l%d" % i, "memory")
    f = QgsFeature()
    f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(i, i)))
    lyr.dataProvider().addFeatures([f])
    return lyr


def test_threshold_log_and_nothing_freed():
    logs = []
    old_log, old_every = DL.log_message, DL.ORPHANS_LOG_EVERY
    start = len(DL._ORPHANS)
    DL.log_message = lambda msg, level=None: logs.append(msg)
    try:
        DL.ORPHANS_LOG_EVERY = 3
        base = len(DL._ORPHANS)
        origs = [_mem(i) for i in range(7)]
        news = [DL._adopt_layer(o) for o in origs]
        check("each layer re-created", all(n is not o for n, o in zip(news, origs)))
        check("every original stays parked (nothing trimmed or freed)",
              all(o in DL._ORPHANS for o in origs) and len(DL._ORPHANS) == base + 7)
        crossings = [n for n in range(base + 1, base + 8) if n % 3 == 0]
        check("one log line per multiple of ORPHANS_LOG_EVERY ({} lines)".format(len(crossings)),
              len(logs) == len(crossings))
        check("the line names parked worker-thread layers and WORKERS > 1",
              all("parked worker-thread layers" in m and "WORKERS > 1" in m for m in logs))
    finally:
        DL.log_message, DL.ORPHANS_LOG_EVERY = old_log, old_every
    check("default threshold is 500", DL.ORPHANS_LOG_EVERY == 500)
    check("_ORPHANS only grew", len(DL._ORPHANS) >= start + 7)


def test_source_never_frees():
    src = open(DL.__file__, encoding="utf-8").read()
    bad = re.findall(r"del _ORPHANS|_ORPHANS\.(clear|pop|remove)\(|_ORPHANS\s*=\s*_ORPHANS\[|"
                     r"_ORPHANS\[[^\]]*\]\s*=", src)
    check("download.py never deletes from _ORPHANS", not bad)
    check("_ORPHANS assigned once (module level)", len(re.findall(r"^_ORPHANS\s*=", src, re.M)) == 1)


def test_batch_help_mentions_workers():
    src = open(os.path.join(ROOT, "DamSafety_Hazard", "processing_provider.py"),
               encoding="utf-8").read()
    check("batch help: long batch -> WORKERS > 1",
          "so use WORKERS > 1 for a long batch" in src)
    check("creep-watch help: long run -> WORKERS > 1",
          "so use WORKERS > 1 for a long run" in src)


if __name__ == "__main__":
    test_threshold_log_and_nothing_freed()
    test_source_never_frees()
    test_batch_help_mentions_workers()
    print("\n{} failed".format(len(FAILS)))
    sys.exit(1 if FAILS else 0)
