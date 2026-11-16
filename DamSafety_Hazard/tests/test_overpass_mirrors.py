# -*- coding: utf-8 -*-
"""Hazard: OpenStreetMap Overpass backup servers and the missing OSM download warnings.

On an engineer's Mac every Overpass download (lifelines, crossings, campgrounds, mobile-home
parks, places) failed with "SSL handshake failed" because only overpass-api.de was asked.
download._overpass_get now tries OVERPASS_URL first, then constants.OVERPASS_MIRRORS. Checks
(no network: http_get_json / http_get_bytes are patched):

  * the backup answers when the first server fails (TLS error, HTTP error, partial result),
    and one MSG_WARNING line names the server that failed, why, and the one used;
  * the data-source file's OVERPASS_URL is tried first (read at call time) and is not asked
    twice when it is also a mirror; OVERPASS_MIRRORS can be changed in that file; a file that
    sets its own OVERPASS_URL but not OVERPASS_MIRRORS gets no public backups;
  * a backup that answers is recorded in download.OVERPASS_MIRROR_USED, and the run log /
    data-source list (pipeline) and the data-download log (datafetch) report it;
  * download.py no longer imports OVERPASS_URL (it reads constants.OVERPASS_URL at call time);
  * every server failing: one DownloadError naming each server and its error;
  * CancelledByUser and NotInPack (offline mode) stop at once, no other server is asked;
  * http_get_bytes's error text names the server ("SSL handshake failed (overpass-api.de)");
  * pipeline._collect_facilities warns when the OSM crossings download failed (also when the
    NBI answered), and the places block of the run warns when the OSM places download failed.

    python DamSafety_Hazard/tests/test_overpass_mirrors.py
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
for k in ("NDSUITE_SETTINGS", "NDSUITE_PACK", "NDSUITE_SOURCE",
          "ND_DAM_HAZARD_OFFLINE_PACK", "ND_DAM_HAZARD_OFFLINE_ONLY"):
    os.environ.pop(k, None)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from qgis.core import QgsApplication, QgsCoordinateReferenceSystem  # noqa: E402
_app = QgsApplication.instance() or QgsApplication([], True)
_app.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", "/home/box/miniforge3/envs/qgis334"), True)
_app.initQgis()

from DamSafety_Hazard import constants as C               # noqa: E402
from DamSafety_Hazard import download as DL               # noqa: E402
from DamSafety_Hazard import pipeline as PL               # noqa: E402
from DamSafety_Hazard.compat import MSG_WARNING           # noqa: E402
from DamSafety_Hazard.helpers import CancelledByUser      # noqa: E402

FAILS = []
DEFAULT = "https://overpass-api.de/api/interpreter"
COFFEE = "https://overpass.private.coffee/api/interpreter"
KUMI = "https://overpass.kumi.systems/api/interpreter"
GOOD = {"elements": [{"type": "node", "id": 1, "lat": 47.0, "lon": -100.0}]}


def expect(cond, what):
    print("{} {}".format("ok  " if cond else "FAIL", what))
    if not cond:
        FAILS.append(what)


class Fake(object):
    """http_get_json stand-in: `answers` maps a server address to an exception to raise or
    the data to return; `asked` lists the servers in the order they were asked."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def __call__(self, url, user_agent=None):
        base = url.split("?")[0]
        self.asked.append(base)
        a = self.answers.get(base, DL.DownloadError("no answer"))
        if isinstance(a, BaseException):
            raise a
        return a


def run_case(answers, query="[out:json];node(1);out;"):
    """(result or exception, asked servers, [(text, level)] logged)."""
    fake = Fake(answers)
    logged = []
    DL.http_get_json = fake
    DL.log_message = lambda msg, level=None, *a, **kw: logged.append((msg, level))
    try:
        res = DL._overpass_get(query)
    except BaseException as e:            # noqa - the case's outcome
        res = e
    return res, fake.asked, logged


def test_download():
    saved = (DL.http_get_json, DL.http_get_bytes, DL.log_message, DL.offline_config,
             C.OVERPASS_URL, list(C.OVERPASS_MIRRORS))
    saved_over = dict(C.SOURCE_OVERRIDES)
    C.SOURCE_OVERRIDES.clear()               # this machine's data-source file plays no part
    DL.offline_config = lambda: (None, False)
    try:
        expect(not hasattr(DL, "OVERPASS_URL"),
               "download.py: OVERPASS_URL not imported (constants read at call time)")
        expect(C.OVERPASS_URL == DEFAULT and C.OVERPASS_MIRRORS == [COFFEE, KUMI],
               "constants: overpass-api.de first, two backup servers")
        expect("OVERPASS_MIRRORS" in C.SOURCE_KEYS and
               C.current_sources().get("OVERPASS_MIRRORS") == [COFFEE, KUMI],
               "constants: OVERPASS_MIRRORS is a data-source key")
        expect(DL._overpass_hosts() == [DEFAULT, COFFEE, KUMI], "hosts: default order")

        # first server answers: nothing else asked, nothing logged
        res, asked, logged = run_case({DEFAULT: GOOD})
        expect(res == GOOD and asked == [DEFAULT] and not logged,
               "first server answers: only it is asked, no warning")
        expect(not DL.OVERPASS_MIRROR_USED, "first server answers: nothing recorded")

        # TLS error on the first server: the backup answers
        res, asked, logged = run_case({
            DEFAULT: DL.DownloadError("SSL handshake failed (overpass-api.de)"),
            COFFEE: GOOD})
        expect(res == GOOD, "TLS error: the backup's data is returned")
        expect(asked == [DEFAULT, COFFEE], "TLS error: default then backup ({})".format(asked))
        warn = [m for m, lvl in logged if lvl == MSG_WARNING]
        expect(len(logged) == 1 and len(warn) == 1, "TLS error: one warning line ({})".format(
            logged))
        expect(warn and "overpass-api.de" in warn[0] and "SSL handshake failed" in warn[0]
               and "overpass.private.coffee used instead" in warn[0],
               "TLS error: the warning names the failed server, why and the one used ({})"
               .format(warn[0] if warn else ""))
        expect(warn and warn[0].count("overpass-api.de") == 1,
               "TLS error: the server is not named twice for one error")
        expect(DL.OVERPASS_MIRROR_USED == [("SSL handshake failed (overpass-api.de)",
                                            "overpass.private.coffee")],
               "TLS error: recorded in OVERPASS_MIRROR_USED ({})".format(DL.OVERPASS_MIRROR_USED))
        # the same failure on a second query (crossings after lifelines): one report line
        run_case({DEFAULT: DL.DownloadError("SSL handshake failed (overpass-api.de)"),
                  COFFEE: GOOD})
        lines = DL.overpass_mirror_lines()
        expect(len(DL.OVERPASS_MIRROR_USED) == 2 and len(lines) == 1,
               "two queries, same failure: one report line ({})".format(lines))
        expect(lines and lines[0][0].startswith(">>> WARNING: OpenStreetMap:") and
               "SSL handshake failed (overpass-api.de)" in lines[0][0] and
               "overpass.private.coffee" in lines[0][0] and
               "overpass.private.coffee" in lines[0][1] and "Overpass API" in lines[0][1],
               "report: run-log WARNING and data-source line name the failure and the backup "
               "({})".format(lines))
        del DL.OVERPASS_MIRROR_USED[:]

        # HTTP 429 (Qt's text repeats the whole address) and a partial result
        q = "[out:json];node(1);out;"
        full = DEFAULT + "?" + DL.urlencode({"data": q})
        res, asked, logged = run_case({
            DEFAULT: DL.DownloadError("Error transferring {} - server replied: Too Many "
                                      "Requests".format(full)),
            COFFEE: {"elements": [], "remark": "runtime error: Query timed out"},
            KUMI: GOOD}, q)
        expect(res == GOOD and asked == [DEFAULT, COFFEE, KUMI],
               "HTTP 429 + partial result: the third server answers ({})".format(asked))
        warn = [m for m, lvl in logged if lvl == MSG_WARNING]
        expect(warn and "Too Many Requests" in warn[0] and "Query timed out" in warn[0]
               and "overpass.kumi.systems used instead" in warn[0] and "data=" not in warn[0],
               "HTTP 429 + partial result: both reasons logged, without the query ({})".format(
                   warn[0] if warn else ""))

        # every server fails: one error naming each
        res, asked, logged = run_case({
            DEFAULT: DL.DownloadError("SSL handshake failed (overpass-api.de)"),
            COFFEE: DL.DownloadError("Connection refused (overpass.private.coffee)"),
            KUMI: DL.DownloadError("Host overpass.kumi.systems not found")})
        msg = str(res)
        expect(isinstance(res, DL.DownloadError) and not isinstance(res, DL.NotInPack),
               "all fail: DownloadError ({!r})".format(res))
        expect(all(h in msg for h in ("overpass-api.de", "overpass.private.coffee",
                                      "overpass.kumi.systems")) and
               all(t in msg for t in ("SSL handshake failed", "Connection refused",
                                      "not found")),
               "all fail: the error names every server and its error ({})".format(msg))
        expect(asked == [DEFAULT, COFFEE, KUMI], "all fail: each server asked once")
        expect(not logged, "all fail: no 'used instead' line")
        expect(len(DL.OVERPASS_MIRROR_USED) == 1,
               "all fail: nothing more recorded ({})".format(DL.OVERPASS_MIRROR_USED))
        del DL.OVERPASS_MIRROR_USED[:]

        # cancel / offline mode: stop at once
        res, asked, _l = run_case({DEFAULT: CancelledByUser(), COFFEE: GOOD})
        expect(isinstance(res, CancelledByUser) and asked == [DEFAULT],
               "CancelledByUser propagates, no backup asked")
        res, asked, _l = run_case({DEFAULT: DL.NotInPack("offline mode"), COFFEE: GOOD})
        expect(isinstance(res, DL.NotInPack) and asked == [DEFAULT],
               "NotInPack (offline mode) propagates, no backup asked")

        # the user's OVERPASS_URL (data-source file) first, read at call time
        mine = "https://overpass.example.org/api/interpreter"
        C.OVERPASS_URL = mine
        expect(DL._overpass_hosts() == [mine, COFFEE, KUMI],
               "override: the user's server first ({})".format(DL._overpass_hosts()))
        res, asked, logged = run_case({mine: DL.DownloadError("Connection refused"),
                                       COFFEE: GOOD})
        expect(res == GOOD and asked == [mine, COFFEE],
               "override: tried first, then the backup ({})".format(asked))
        expect(logged and "overpass.example.org" in logged[0][0] and
               "Connection refused (overpass.example.org)" in logged[0][0],
               "override: the warning names the user's server")
        C.OVERPASS_URL = KUMI
        expect(DL._overpass_hosts() == [KUMI, COFFEE],
               "override = a mirror: not asked twice ({})".format(DL._overpass_hosts()))
        # a data-source file that sets its own server: no public backups unless it lists some
        C.OVERPASS_URL = mine
        C.SOURCE_OVERRIDES["OVERPASS_URL"] = mine
        expect(DL._overpass_hosts() == [mine],
               "own OVERPASS_URL in the file: no public backups ({})".format(DL._overpass_hosts()))
        res, asked, logged = run_case({mine: DL.DownloadError("Connection refused"),
                                       COFFEE: GOOD})
        expect(isinstance(res, DL.DownloadError) and asked == [mine] and not logged,
               "own OVERPASS_URL fails: no public server asked ({})".format(asked))
        C.SOURCE_OVERRIDES["OVERPASS_MIRRORS"] = [KUMI]
        expect(DL._overpass_hosts() == [mine, COFFEE, KUMI],
               "own OVERPASS_URL + OVERPASS_MIRRORS in the file: backups used ({})".format(
                   DL._overpass_hosts()))
        C.SOURCE_OVERRIDES.clear()
        C.OVERPASS_URL = DEFAULT
        C.OVERPASS_MIRRORS = []
        res, asked, _l = run_case({DEFAULT: DL.DownloadError("SSL handshake failed")})
        expect(asked == [DEFAULT] and "overpass-api.de" in str(res),
               "no mirrors: one server, its name in the error ({})".format(res))

        # the data-source file can change both
        work = tempfile.mkdtemp(prefix="ovp_src_")
        saved_over = dict(C.SOURCE_OVERRIDES)
        try:
            path = os.path.join(work, "src.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"OVERPASS_URL": mine, "OVERPASS_MIRRORS": [KUMI]}, fh)
            C.OVERPASS_MIRRORS = [COFFEE, KUMI]
            C.apply_source_overrides(path)
            expect(DL._overpass_hosts() == [mine, KUMI],
                   "data-source file: OVERPASS_URL and OVERPASS_MIRRORS applied ({})".format(
                       DL._overpass_hosts()))
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"OVERPASS_URL": mine}, fh)
            C.OVERPASS_URL, C.OVERPASS_MIRRORS = DEFAULT, [COFFEE, KUMI]
            C.apply_source_overrides(path)
            expect(DL._overpass_hosts() == [mine],
                   "data-source file: OVERPASS_URL alone -> no public backups ({})".format(
                       DL._overpass_hosts()))
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"OVERPASS_URL": DEFAULT, "OVERPASS_MIRRORS": [COFFEE, KUMI]}, fh)
            C.OVERPASS_URL, C.OVERPASS_MIRRORS = DEFAULT, [COFFEE, KUMI]
            C.apply_source_overrides(path)
            expect(DL._overpass_hosts() == [DEFAULT, COFFEE, KUMI],
                   "data-source file with the built-in addresses: backups used ({})".format(
                       DL._overpass_hosts()))
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"OVERPASS_MIRRORS": "https://not-a-list"}, fh)
            C.OVERPASS_URL, C.OVERPASS_MIRRORS = DEFAULT, [COFFEE, KUMI]
            C.apply_source_overrides(path)
            expect(C.OVERPASS_MIRRORS == [COFFEE, KUMI],
                   "data-source file: a non-list OVERPASS_MIRRORS is ignored")
        finally:
            C.SOURCE_OVERRIDES.clear()
            C.SOURCE_OVERRIDES.update(saved_over)
            shutil.rmtree(work, ignore_errors=True)

        # http_get_bytes path (http_get_json is the real one): the error names the server
        C.OVERPASS_URL, C.OVERPASS_MIRRORS = DEFAULT, [COFFEE, KUMI]
        DL.http_get_json = saved[0]
        asked = []

        def fake_bytes(url, user_agent=None, timeout_s=None):
            asked.append(url.split("?")[0])
            if url.startswith(DEFAULT):
                raise DL.DownloadError(DL._with_host("SSL handshake failed", url))
            return json.dumps(GOOD).encode("utf-8")
        DL.http_get_bytes = fake_bytes
        logged = []
        DL.log_message = lambda msg, level=None, *a, **kw: logged.append((msg, level))
        res = DL._overpass_get("[out:json];node(1);out;")
        expect(res == GOOD and asked == [DEFAULT, COFFEE],
               "http_get_bytes patched: the backup answered ({})".format(asked))
        expect(logged and "SSL handshake failed (overpass-api.de)" in logged[0][0],
               "http_get_bytes patched: warning text ({})".format(logged))
    finally:
        (DL.http_get_json, DL.http_get_bytes, DL.log_message, DL.offline_config,
         C.OVERPASS_URL, C.OVERPASS_MIRRORS) = saved
        C.SOURCE_OVERRIDES.clear()
        C.SOURCE_OVERRIDES.update(saved_over)
        del DL.OVERPASS_MIRROR_USED[:]

    # _with_host (used by http_get_bytes for every download)
    expect(DL._with_host("SSL handshake failed", DEFAULT + "?data=x") ==
           "SSL handshake failed (overpass-api.de)", "_with_host: adds the server name")
    m = "Error transferring https://overpass-api.de/api/interpreter - server replied: Bad"
    expect(DL._with_host(m, DEFAULT) == m, "_with_host: not added when the text has it")
    expect(DL._with_host("network error", "not a url") == "network error",
           "_with_host: no server name, text unchanged")
    with open(os.path.join(ROOT, "DamSafety_Hazard", "download.py"), encoding="utf-8") as fh:
        src = fh.read()
    expect('raise DownloadError(_with_host(req.errorMessage() or "network error", url))' in src,
           "http_get_bytes: its error goes through _with_host")


class _Run(object):
    def __init__(self):
        self.cfg, self.out, self.local, self.ctx = {}, "", {}, None
        self.sources, self.messages = [], []

    def log(self, msg):
        self.messages.append(msg)


class _Clip(object):
    crs = QgsCoordinateReferenceSystem("EPSG:26914")
    lat = 47.0


def test_pipeline():
    saved = PL.FAC.read_crossings
    PL.FAC.read_crossings = lambda *a, **kw: []
    osm_warn = "OpenStreetMap crossings download failed"
    try:
        nbi = (object(), 3, "/x/NBI_20261116_abc.geojson")
        xosm = (object(), 2, "/x/OSM_crossings_20261116_abc.geojson")
        failed = (None, 0, None)

        run = _Run()
        PL._collect_facilities(run, _Clip(), None, {"nbi": nbi, "xosm": failed})
        expect(any(osm_warn in m and m.startswith(">>> WARNING:") for m in run.messages),
               "crossings: NBI ok, OSM failed -> OSM warning ({})".format(run.messages))
        expect(run.crossings_evaluated, "crossings: NBI ok -> crossings still evaluated")

        run = _Run()
        PL._collect_facilities(run, _Clip(), None, {"nbi": nbi, "xosm": xosm})
        expect(not any(osm_warn in m for m in run.messages), "crossings: both ok -> no warning")

        run = _Run()
        PL._collect_facilities(run, _Clip(), None, {"nbi": failed, "xosm": failed})
        expect(any(osm_warn in m for m in run.messages) and
               any("bridge / culvert downloads failed" in m for m in run.messages),
               "crossings: both failed -> OSM and general warnings ({})".format(run.messages))

        run = _Run()
        PL._collect_facilities(run, _Clip(), None, {})
        expect(not any("WARNING" in m for m in run.messages),
               "crossings: not downloaded -> no warning")
    finally:
        PL.FAC.read_crossings = saved

    # places: the block sits in the long _collect_inputs; source-level check
    with open(os.path.join(ROOT, "DamSafety_Hazard", "pipeline.py"), encoding="utf-8") as fh:
        src = fh.read()
    a = src.find('    if "places" in got:')
    b = src.find('    if "flow" in got:', a)
    block = src[a:b] if a >= 0 and b > a else ""
    expect("else:" in block and 'run.log(">>> WARNING: OpenStreetMap places download failed'
           in block and "travel-time" in block,
           "places: a failed download is a run-log WARNING")
    # Overpass backups: cleared with STALE_USED and reported next to it (run log + sources)
    a = src.find("def _collect_inputs(")
    b = src.find("\ndef ", a + 1)
    body = src[a:b] if a >= 0 and b > a else ""
    expect("del OVERPASS_MIRROR_USED[:]" in body and
           body.find("del OVERPASS_MIRROR_USED[:]") < body.find('want("life_osm"'),
           "pipeline: OVERPASS_MIRROR_USED cleared before the downloads")
    i = body.find("for line, source in overpass_mirror_lines():")
    expect(i > body.find("for name, day, fname in STALE_USED:") > 0 and
           "run.log(line)" in body[i:i + 120] and "run.sources.append(source)" in body[i:i + 120],
           "pipeline: backups reported to the run log and the data-source list")
    with open(os.path.join(ROOT, "DamSafety_Hazard", "datafetch.py"), encoding="utf-8") as fh:
        dsrc = fh.read()
    expect("del OVERPASS_MIRROR_USED[:]" in dsrc and
           "for line, _source in overpass_mirror_lines():" in dsrc,
           "datafetch: backups cleared and reported in the data-download log")


def main():
    test_download()
    test_pipeline()
    if FAILS:
        print("FAILED: {}".format(len(FAILS)))
        sys.exit(1)
    print("test_overpass_mirrors: all passed")


if __name__ == "__main__":
    main()
