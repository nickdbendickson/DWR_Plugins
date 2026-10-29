# -*- coding: utf-8 -*-
"""Build the offline data pack for North Dakota (v2.6.1).

Run on any computer with internet access - QGIS is NOT needed, any Python
3.7+ works (QGIS's own Python too):

    python build_nd_pack.py                      -> nd_offline_pack.zip here
    python build_nd_pack.py --out D:/nd_pack.zip
    python build_nd_pack.py --only nsi,usa --refresh    (update two data sets)
    python build_nd_pack.py --list               (what it downloads)

It fetches every data set the plugin downloads - for all of North Dakota
(its bounding box plus a 0.1 degree margin, so the plugin's 2-3 mile
searches still work at the state line) - from the same services, with the
same addresses (your data-source file is used when --sources names it),
and writes one SQLite file plus manifest.json into a zip. Copy the zip to
the offline computer and choose it on the plugin's Data & Output tab
(7. Offline data pack).

An interrupted build is resumed: the data sets already finished are kept
in <name>_work/ next to the zip (delete that folder to start over). Since
2.8.0 a data set that was in progress is continued too: every finished chunk
(a batch of object ids, an NSI / Overpass tile, a page) is stored at once, and
a rerun after Cancel, a crash or a failed data set fetches only what is
missing (--refresh fetches everything again).

Slow or overloaded services: the requests of a data set share one limiter.
When the service answers HTTP 429 / 503 / 504 (or times out) the number of
requests at once is halved (down to 1), every worker waits together (a
Retry-After header is honoured), and the number slowly recovers after
successes. Each wait is written to the log, and the progress message keeps
changing while waiting ("waiting on the service (HTTP 429), 3 of 6 workers
paused").

The plugin can also run this for you: Data & Output tab > Build offline
pack... (on a computer that is online).
"""

import argparse
import contextvars
import email.utils
import hashlib
import json
import math
import os
import random
import re
import socket
import sys
import threading
import time
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor, wait as _cf_wait, FIRST_COMPLETED
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
if __package__:                      # imported by the plugin (QGIS)
    from . import constants as C
    from . import osm_parse as O
    from . import dwr_files as DF
    from .offline_pack import (PackWriter, DB_NAME, MANIFEST_NAME, ND_BBOX,
                               DEFAULT_PAD_DEG, PackError, STATUS_PARTIAL,
                               write_clean_copy, geom_bbox)
else:                                # run as a script: the plugin folder is beside it
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import constants as C            # noqa: E402  (no QGIS imports in these three)
    import osm_parse as O            # noqa: E402
    import dwr_files as DF           # noqa: E402  (2.18.66: --dwr-folder; sqlite3 only)
    from offline_pack import (PackWriter, DB_NAME, MANIFEST_NAME, ND_BBOX,  # noqa: E402
                              DEFAULT_PAD_DEG, PackError, STATUS_PARTIAL,
                              write_clean_copy, geom_bbox)

BUILDER_VERSION = "1.3"
TIMEOUT_S = 180
RETRIES = 5
NSI_TILE_DEG = C.DOWNLOAD_TILE_DEG           # the plugin's tile size (the API is happy)
OVERPASS_TILE_DEG = 2.0
OVERPASS_TIMEOUT_S = 300
IDS_CHUNK = 1000                             # features per ArcGIS request (or less)
# failed requests in a row (no success between, all workers): the service is
# down. A single bad feature fails about one request per halving level with a
# success between, so concurrent descents never come near it.
OUTAGE_FAILS = 24
MAX_MISSING_SHARE = 0.01                     # more ArcGIS features missing: data set fails
# v2.8.0: throttling / visible retries
THROTTLE_CODES = (429, 503, 504)             # "slow down" answers of the services
RETRY_AFTER_MAX_S = 120                      # longest Retry-After that is obeyed
RECOVER_AFTER = 8                            # successes in a row that give back one request
HEARTBEAT_S = 5.0                            # progress is reported at least this often
_SLICE_S = 0.25                              # how often a waiting thread looks at Cancel
# 1.3 (Hazard 2.20.2 / Data 1.5.1): tiled object-id listing ("arcgis_tiled", the Data pack's
# 3DHP hydro). The id query of a whole state times out (HTTP 504) on 3DHP; a 0.3 x 0.2 degree
# box answers in seconds. A tile that fails is quartered (after a short pause) down to
# TILED_SPLIT_DEPTH levels; what still fails is an area another service can fill.
TILED_TILE_DEG = 0.25
TILED_SPLIT_DEPTH = 4                        # 0.25 deg -> 1/16 of it at the deepest
TILED_CHUNK = 250                            # features per request (long flowline geometry)
SPLIT_PAUSE_S = 2.0                          # pause before a failed box is split (x level)
# 1.3: a paged read answered HTTP 400 is tried again with smaller pages down to PAGE_MIN
PAGE_MIN = 100
PAGE_400_PAUSE_S = 10.0

# v2.6.2: set when the build is cancelled - every worker stops at its next
# request or back-off instead of running on (inside QGIS: until QGIS exits)
_CANCEL = threading.Event()


class BuildError(Exception):
    pass


class BuildCancelled(BuildError):
    pass


class _Health(object):
    """Failed requests in a row for one data set; too many = the service is
    down, and halving chunks would only multiply the failing requests."""

    def __init__(self, limit=OUTAGE_FAILS):
        self.lock = threading.Lock()
        self.fails = 0
        self.limit = limit

    def ok(self):
        with self.lock:
            self.fails = 0

    def failed(self):
        with self.lock:
            self.fails += 1
            return self.fails >= self.limit

    def down(self):
        with self.lock:
            return self.fails >= self.limit


def _check_cancel():
    if _CANCEL.is_set():
        raise BuildCancelled("cancelled")


_NET_ERRORS = (HTTPError, URLError, TimeoutError, OSError, ValueError, BuildError)


# ------------------------------------------------------ throttle / reporting --
class ServiceBusy(BuildError):
    """An HTTP 200 answer whose body is an ArcGIS error saying "slow down"."""

    def __init__(self, code, msg):
        BuildError.__init__(self, msg)
        self.code = code


def _backoff_s(kind, attempt):
    """Seconds to wait before try attempt + 2. kind: "busy" (429 / 503 / 504),
    "timeout", "error" (anything else)."""
    if kind == "busy":
        return 30 * (attempt + 1)
    if kind == "timeout":
        return 10 * (attempt + 1)
    return 4 * (attempt + 1)


def _retry_after_s(err):
    """Seconds asked for by a Retry-After header (number or HTTP date), capped
    at RETRY_AFTER_MAX_S; None when there is none / it cannot be read."""
    hdrs = getattr(err, "headers", None)
    if hdrs is None:
        hdrs = getattr(err, "hdrs", None)
    try:
        val = hdrs.get("Retry-After") if hdrs is not None else None
    except Exception:
        val = None
    if val is None:
        return None
    val = str(val).strip()
    try:
        sec = float(val)
    except ValueError:
        try:
            dt = email.utils.parsedate_to_datetime(val)
            if dt.tzinfo is None:
                import datetime
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            sec = dt.timestamp() - time.time()
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    if sec != sec:                                   # NaN
        return None
    return min(max(sec, 1.0), float(RETRY_AFTER_MAX_S))


def _is_timeout(e):
    if isinstance(e, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(e, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    return "timed out" in str(reason if reason is not None else e).lower()


def _short(e):
    txt = str(getattr(e, "reason", None) or e)
    txt = " ".join(txt.split())
    return txt if len(txt) <= 80 else txt[:77] + "..."


class _Slot(object):
    __slots__ = ("epoch",)

    def __init__(self, epoch):
        self.epoch = epoch


class NetSession(object):
    """The requests of ONE data set: a shared limiter and the reporter of
    their waits (v2.8.0).

    * acquire() / release(): at most `limit` requests run at once. When the
      service pushes back (HTTP 429 / 503 / 504, a timeout) the limit is
      halved (not below 1) - once per "event", however many requests of the
      same round fail - and every worker waits for the SAME pause (the back-off,
      or the Retry-After the service asked for) instead of each backing off on
      its own. RECOVER_AFTER successes in a row give one request back.
    * Every wait is written to `log` once per event; while waiting, beat() (the
      thread that drives the progress bar calls it) reports what is going on
      through `on_beat(text)` at least every HEARTBEAT_S.
    Everything is thread safe. http_json finds the session of the running data
    set in _NET (a context variable set by the builder; a no-op when unset)."""

    def __init__(self, label, workers, log=None, on_beat=None, clock=None):
        self.label = label
        self.max = max(1, int(workers))
        self.limit = self.max
        self.active = 0
        self.blocked = 0                     # threads waiting to run a request
        self.epoch = 0
        self.streak = 0
        self.paused_until = 0.0
        self.reason = ""
        self.pushbacks = 0                   # events so far (for the summary)
        self.cond = threading.Condition()
        self._log = log
        self._log_lock = threading.Lock()
        self._on_beat = on_beat
        self._clock = clock or time.monotonic
        self._owner = threading.get_ident()
        self._last_beat = self._clock()

    # -- reporting ---------------------------------------------------------
    def emit(self, text):
        if self._log is None:
            return
        with self._log_lock:
            try:
                self._log("  {}: {}".format(self.label, text))
            except Exception:
                pass

    def status_text(self):
        now = self._clock()
        with self.cond:
            paused = now < self.paused_until
            if paused or self.blocked:
                return "waiting on the service ({}), {} of {} workers paused".format(
                    self.reason or "slow answers", max(1, self.blocked), self.max)
            if self.limit < self.max:
                return "service is busy - {} of {} requests at a time".format(
                    self.limit, self.max)
            return "waiting for the service to answer ({} request(s) running)".format(
                self.active)

    def touch(self):
        self._last_beat = self._clock()

    def beat(self, main=False):
        """Report the state through on_beat when nothing was reported for
        HEARTBEAT_S. Only the thread that owns the session (the one that
        drives the progress bar) reports, unless main=True. An exception from
        the callback (a cancel) propagates."""
        if self._on_beat is None:
            return
        if not main and threading.get_ident() != self._owner:
            return
        now = self._clock()
        if now - self._last_beat < HEARTBEAT_S:
            return
        self._last_beat = now
        self._on_beat(self.status_text())

    def set_workers(self, n):
        with self.cond:
            n = max(1, int(n))
            self.limit = min(self.limit, n) if self.limit < self.max else n
            self.max = n
            self.cond.notify_all()

    # -- the limiter -------------------------------------------------------
    def acquire(self):
        """Wait until the pause is over and fewer than `limit` requests run;
        cancel-aware. Returns the slot to hand to release()."""
        counted = False
        try:
            while True:
                with self.cond:
                    if self._clock() >= self.paused_until and self.active < self.limit:
                        self.active += 1
                        return _Slot(self.epoch)
                    if not counted:
                        counted = True
                        self.blocked += 1
                    delay = self.paused_until - self._clock()
                    self.cond.wait(_SLICE_S if delay <= 0 else min(_SLICE_S, delay))
                _check_cancel()
                self.beat()
        finally:
            if counted:
                with self.cond:
                    self.blocked -= 1

    def release(self, slot, ok=False, push=None):
        """A request ended. ok: it succeeded. push: the service pushed back -
        {"reason", "wait" (s), "retry_after" (s or None), "try" (the next try
        number, None when there is no retry), "of"}."""
        lines = []
        with self.cond:
            self.active = max(0, self.active - 1)
            if push is not None:
                self.pushbacks += 1
                first = slot is None or slot.epoch == self.epoch
                if first:
                    self.epoch += 1
                    self.streak = 0
                    new = max(1, self.limit // 2)
                    if new < self.limit:
                        self.limit = new
                        lines.append("service is busy - now {} request(s) at a time".format(new))
                ra = push.get("retry_after")
                wait = float(ra) if ra else float(push["wait"])
                until = self._clock() + wait + random.random()
                if until > self.paused_until:
                    self.paused_until = until
                    self.reason = push["reason"]
                if first and push.get("try"):
                    lines.insert(0, "{}, waiting {:.0f} s (try {} of {}){}".format(
                        push["reason"], wait, push["try"], push["of"],
                        " - as the service asked" if ra else ""))
            elif ok:
                self.streak += 1
                if self.limit < self.max and self.streak >= RECOVER_AFTER:
                    self.limit += 1
                    self.streak = 0
                    lines.append("service answers again - {} request(s) at a time".format(
                        self.limit))
            self.cond.notify_all()
        for ln in lines:
            self.emit(ln)

    def backoff(self, reason, wait, attempt, retries):
        """An error that is not a push-back: this worker alone waits (logged),
        cancel-aware."""
        self.emit("{}, waiting {:.0f} s (try {} of {})".format(reason, wait, attempt + 2,
                                                                 retries))
        with self.cond:
            self.blocked += 1
        try:
            self.sleep(wait)
        finally:
            with self.cond:
                self.blocked -= 1

    def sleep(self, seconds):
        end = self._clock() + seconds
        while True:
            _check_cancel()
            rem = end - self._clock()
            if rem <= 0:
                return
            if _CANCEL.wait(min(rem, _SLICE_S)):
                raise BuildCancelled("cancelled")
            self.beat()


_NET = contextvars.ContextVar("nd_pack_net", default=None)


# ------------------------------------------------------------------- HTTP --
def _ua():
    return C.HTTP_USER_AGENT + " offline-pack-builder/" + BUILDER_VERSION


def _sleep_plain(seconds):
    if _CANCEL.wait(seconds):
        raise BuildCancelled("cancelled")


def http_json(url, params=None, post=False, retries=RETRIES, what=""):
    """GET / POST and decode JSON, retried with back-off. An ArcGIS
    {"error": ...} answer is an error too. v2.8.0: inside a data set (a
    NetSession is set) the requests share a limiter, a push-back (429 / 503 /
    504 / timeout) lowers the number of requests at once and pauses all of
    them together (Retry-After honoured), and every wait is logged."""
    data = None
    full = url
    if params:
        enc = urlencode(params)
        if post:
            data = enc.encode("utf-8")
        else:
            full = url + ("&" if "?" in url else "?") + enc
    net = _NET.get()
    last = None
    for attempt in range(retries):
        _check_cancel()
        slot = net.acquire() if net is not None else None
        ok = False
        kind, reason, wait, retry_after, stop = None, "", 0.0, None, False
        more = attempt + 1 < retries
        try:
            try:
                req = Request(full, data=data, headers={
                    "User-Agent": _ua(), "Accept": "application/json, */*",
                    **({"Content-Type": "application/x-www-form-urlencoded"} if data else {})})
                with urlopen(req, timeout=TIMEOUT_S) as r:
                    raw = r.read()
                obj = json.loads(raw.decode("utf-8"))
                if isinstance(obj, dict) and "error" in obj:
                    err = obj["error"]
                    code = err.get("code") if isinstance(err, dict) else None
                    msg = "service error: {}".format(
                        err.get("message") if isinstance(err, dict) else err)
                    try:
                        code = int(code)
                    except (TypeError, ValueError):
                        code = None
                    if code in THROTTLE_CODES:
                        raise ServiceBusy(code, msg)
                    raise BuildError(msg)
                ok = True
                return obj
            except HTTPError as e:
                last = e
                reason = "HTTP {}".format(e.code)
                if e.code in THROTTLE_CODES:
                    kind = "busy"
                    retry_after = _retry_after_s(e)
                else:
                    kind = "error"
                if e.code in (400, 401, 403, 404) and attempt >= 1:
                    stop = True                         # will not get better
            except BuildCancelled:
                raise
            except ServiceBusy as e:
                last = e
                kind, reason = "busy", "HTTP {}".format(e.code)
            except _NET_ERRORS as e:
                last = e
                if _is_timeout(e):
                    kind, reason = "timeout", "timed out"
                else:
                    kind, reason = "error", _short(e)
            # the wait after a failed try (the last try does not wait, but a
            # push-back still slows the others down)
            wait = _backoff_s(kind, attempt if more else 0)
        finally:
            if net is not None:
                push = None
                if kind in ("busy", "timeout"):
                    push = {"reason": reason, "wait": wait, "retry_after": retry_after,
                            "try": attempt + 2 if (more and not stop) else None,
                            "of": retries}
                net.release(slot, ok=ok, push=push)
        if stop:
            break
        if more:                                    # v2.6.2: no sleep after the last try
            if net is None:
                _sleep_plain(wait + random.random())
            elif kind == "error":
                net.backoff(reason, wait, attempt, retries)
            # busy / timeout: the shared pause holds every worker (acquire)
    err = BuildError("{}{} ({})".format(what + ": " if what else "", url.split("?")[0], last))
    err.http_code = getattr(last, "code", None)          # 1.3: HTTP 400 on pages: smaller pages
    raise err


def _pool(ctx, workers, calls, on_result):
    """Run calls [(fn, args)] on a thread pool; on_result(args, result or
    exception) runs on this thread as each finishes. A cancel (an exception
    from the progress callback) drops the calls not started yet at once.
    v2.8.0: this thread also keeps the progress bar alive while every worker
    waits on the service (NetSession.beat), and the workers see the data
    set's NetSession."""
    net = _NET.get()
    if net is not None:
        net.set_workers(workers)
    ex = ThreadPoolExecutor(max_workers=max(1, workers))
    futs, order = {}, {}
    for fn, args in calls:
        fu = ex.submit(contextvars.copy_context().run, fn, *args)
        futs[fu] = args
        order[fu] = len(order)
    pending = set(futs)
    tick = max(0.01, min(1.0, HEARTBEAT_S))
    try:
        while pending:
            done, pending = _cf_wait(pending, timeout=tick, return_when=FIRST_COMPLETED)
            if not done:
                if net is not None:
                    net.beat(main=True)
                continue
            for fu in sorted(done, key=order.get):     # first come, first stored
                try:
                    res = fu.result()
                except BuildCancelled:
                    raise
                except _NET_ERRORS as e:
                    res = e
                on_result(futs[fu], res)
            if net is not None:
                net.touch()
    except BaseException:
        _CANCEL.set()                      # running workers stop at their next request
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except TypeError:                               # Python < 3.9
            for fu in futs:
                fu.cancel()
            ex.shutdown(wait=False)
        raise
    ex.shutdown(wait=True)


# ---------------------------------------------------------------- ArcGIS --
def _envelope(b):
    return {"geometry": "{},{},{},{}".format(*b), "geometryType": "esriGeometryEnvelope",
            "inSR": "4326", "spatialRel": "esriSpatialRelIntersects"}


def _split4(b):
    x0, y0, x1, y1 = b
    xm, ym = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    return [(x0, y0, xm, ym), (xm, y0, x1, ym), (x0, ym, xm, y1), (xm, ym, x1, y1)]


def arcgis_ids(url, bbox, where="1=1", depth=0):
    """Every object id in the box (the box is split when the service will
    not list them all at once)."""
    try:
        d = http_json(url.rstrip("/") + "/query", dict(_envelope(bbox), where=where,
                                                         returnIdsOnly="true", f="json"),
                      post=True, retries=3 if depth else RETRIES, what="object ids")
        ids = d.get("objectIds") or []
        return set(int(i) for i in ids)
    except BuildCancelled:
        raise
    except BuildError:
        if depth >= 3:
            raise
        out = set()
        for q in _split4(bbox):
            out |= arcgis_ids(url, q, where, depth + 1)
        return out


def _arcgis_chunk(url, ids, out_fields, precision, retries=RETRIES):
    params = {"objectIds": ",".join(str(i) for i in ids), "outFields": out_fields,
              "outSR": "4326", "returnGeometry": "true", "f": "geojson"}
    if precision is not None:
        params["geometryPrecision"] = str(precision)
    d = http_json(url.rstrip("/") + "/query", params, post=True, retries=retries,
                  what="features")
    return d.get("features", []) if isinstance(d, dict) else []


def _arcgis_chunk_split(url, ids, out_fields, precision, missing, health=None, depth=0):
    """A chunk; a failing chunk is halved until the single failing features
    are known. v2.6.2: the halves are tried once each (not with the full
    retries), and when the service keeps failing (health) the rest of the
    chunk is given up at once instead of halving into thousands of requests."""
    health = health or _Health()
    if health.down():
        missing.extend(ids)
        return []
    try:
        feats = _arcgis_chunk(url, ids, out_fields, precision,
                              retries=RETRIES if depth == 0 else 1)
        health.ok()
        return feats
    except BuildCancelled:
        raise
    except BuildError:
        if health.failed() or len(ids) == 1:
            missing.extend(ids)
            return []
        h = len(ids) // 2
        return (_arcgis_chunk_split(url, ids[:h], out_fields, precision, missing, health,
                                    depth + 1) +
                _arcgis_chunk_split(url, ids[h:], out_fields, precision, missing, health,
                                    depth + 1))


# ------------------------------------------------------- chunk-level resume --
# A data set is fetched in chunks; every finished chunk is stored at once with
# a key (PackWriter.add(chunk=key)), and a rerun after Cancel / crash / failure
# fetches only the chunks that are not stored. A chunk is keyed by WHAT it
# holds - the object ids of an ArcGIS batch, the box of a tile, the offset of
# a page - so an id list that changed between runs never skips a chunk wrongly.
def _ids_key(ids):
    crc = zlib.crc32(",".join(str(i) for i in ids).encode("ascii")) & 0xffffffff
    return "ids:{}:{}:{}:{:08x}".format(ids[0], ids[-1], len(ids), crc)


def _pack_json(obj):
    return zlib.compress(json.dumps(obj, separators=(",", ":")).encode("utf-8"), 6)


def _unpack_json(blob):
    try:
        return json.loads(zlib.decompress(blob).decode("utf-8"))
    except (TypeError, ValueError, zlib.error):
        return None


def _tile_key(t):
    return "tile:{:.6f},{:.6f},{:.6f},{:.6f}".format(*t)


def _resume_ids(ctx, ids, prefix="ids:", ignore=()):
    """The object ids already stored (chunks kept from an earlier run that are
    still valid for `ids`). A stored chunk holding an id the service no longer
    lists (features deleted meanwhile) is dropped whole - its remaining ids
    are fetched again, so the result is what the service lists now. 1.3:
    prefix = the key start of these chunks; keys starting with one of `ignore`
    belong to another part of the same data set (arcgis_tiled: the fill
    service) and are left alone."""
    w = ctx.writer
    kept = w.chunks()
    if not kept:
        return set()
    listed = set(ids)
    done, stale = set(), []
    for key, blob in kept.items():
        if ignore and key.startswith(tuple(ignore)):
            continue
        cids = _unpack_json(blob) if key.startswith(prefix) and blob else None
        if not isinstance(cids, list) or not all(i in listed for i in cids):
            stale.append(key)
        else:
            done.update(cids)
    if stale:
        w.drop_chunks(stale)
        ctx.log("  the service's feature list changed since the earlier run - {} stored "
                "batch(es) are read again".format(len(stale)))
    if done:
        ctx.log("  resuming: {:,} of {:,} features already stored".format(len(done), len(ids)))
    return done


def _resume_tiles(ctx, tiles):
    """(keys of the tiles already stored, key of every tile). Tiles share
    features along their edges, so when the earlier run used other tiles the
    data set starts over."""
    keys = [_tile_key(t) for t in tiles]
    kept = ctx.writer.chunks()
    if not kept:
        return set(), keys
    if any(k not in set(keys) for k in kept):
        ctx.log("  the earlier partial download used other tiles - starting over")
        ctx.writer.restart_stage()
        return set(), keys
    ctx.log("  resuming: {} of {} tiles already stored".format(len(kept), len(tiles)))
    return set(kept), keys


def _arcgis_chunk_job(url, ids, out_fields, precision, health):
    """One batch of ids on a worker: (features, ids that could not be read)."""
    miss = []
    feats = _arcgis_chunk_split(url, ids, out_fields, precision, miss, health)
    return feats, miss


def _uid_arcgis(oid_field):
    low = (oid_field or "").lower()

    def uid(f):
        if f.get("id") is not None:
            return f["id"]
        props = f.get("properties") or {}
        for k, v in props.items():
            if k.lower() in (low, "objectid", "fid", "oid"):
                return v
        return None
    return uid


def _fetch_id_chunks(ctx, url, ids, done_ids, size, out_fields, precision, uid,
                     prefix="", edit=None, progress=None):
    """The features of `ids` (not in done_ids) in parallel chunks of `size`, each finished
    chunk stored with its key (prefix + ids key). 1.3: edit(features) -> the features to
    store (arcgis_tiled: tag / filter them); progress(done, total) instead of ctx.sub.
    Returns (ids not read, health)."""
    todo = [i for i in ids if i not in done_ids]
    chunks = [todo[i:i + size] for i in range(0, len(todo), size)]
    missing = []
    finished = [len(done_ids)]
    health = _Health()
    show = progress or (lambda k, n: ctx.sub(100.0 * k / max(1, n), "{:,} of {:,} features".format(
        min(n, k), n)))
    if done_ids:
        show(len(done_ids), len(ids))

    def got(args, res):
        ch = args[1]
        if isinstance(res, Exception):
            missing.extend(ch)
        else:
            feats, miss = res
            if edit is not None:
                feats = edit(feats)
            if miss:                # not wholly read: no chunk record - read again next run
                missing.extend(miss)
                ctx.writer.add(feats, uid)
            else:
                ctx.writer.add(feats, uid, chunk=prefix + _ids_key(ch), payload=_pack_json(ch))
        finished[0] += len(ch)
        show(finished[0], len(ids))
    _pool(ctx, ctx.workers, [(_arcgis_chunk_job, (url, ch, out_fields, precision, health))
                             for ch in chunks], got)
    return missing, health


def fetch_arcgis(ctx, url, bbox, out_fields="*", precision=None):
    """Whole-area ArcGIS layer: the object ids first, then the features by
    id in parallel chunks. Services that cannot list ids are paged by
    offset. Returns a note."""
    info = http_json(url.rstrip("/"), {"f": "json"}, what="layer description")
    oid = info.get("objectIdField") or next(
        (f.get("name") for f in info.get("fields") or []
         if f.get("type") == "esriFieldTypeOID"), None)
    mrc = int(info.get("maxRecordCount") or IDS_CHUNK)
    size = max(1, min(IDS_CHUNK, mrc))
    uid = _uid_arcgis(oid)
    try:
        ids = sorted(arcgis_ids(url, bbox))
    except BuildCancelled:
        raise
    except BuildError as e:
        ctx.log("  object ids not listed ({}) - reading pages instead".format(e))
        return _fetch_arcgis_pages(ctx, url, bbox, oid, mrc, out_fields, precision, uid,
                                   edit=(info.get("editingInfo") or {}).get("lastEditDate"),
                                   why=_short(e))
    done_ids = _resume_ids(ctx, ids)
    ctx.log("  {:,} features".format(len(ids)))
    if not ids:
        return "none in the area"
    missing, health = _fetch_id_chunks(ctx, url, ids, done_ids, size, out_fields, precision,
                                       uid)
    n = ctx.writer.count
    note = ""
    if health.down():
        raise BuildError("the service stopped answering ({} requests failed in a row) - "
                         "{:,} of {:,} features read".format(health.limit, n, len(ids)))
    if missing or n < len(ids):
        lost = len(missing) or len(ids) - n
        note = "{:,} of {:,} features ({} could not be read)".format(n, len(ids), lost)
        ctx.log("  WARNING: " + note)
        # v2.6.2: never "complete" with features missing - where they are is
        # unknown, so the plugin uses this data set only when the service
        # cannot be reached, with a warning; many missing: it fails (retried
        # on the next build, the NID mirror is tried)
        if lost > max(1, MAX_MISSING_SHARE * len(ids)):
            raise BuildError(note)
        ctx.status = STATUS_PARTIAL
    return note


# ------------------------------------------------- tiled ArcGIS (1.3) --
TILED_TILE_FAILS = 12                        # failed requests in a row inside one tile
TILED_DOWN_TILES = 6                         # tiles in a row with nothing listed: service down


def _pause(seconds):
    """Cancel-aware wait (through the data set's NetSession when there is one)."""
    if seconds <= 0:
        return
    net = _NET.get()
    if net is not None:
        net.sleep(seconds)
    else:
        _sleep_plain(seconds)


def _list_ids_split(url, box, max_depth, budget, where="1=1", depth=0):
    """1.3: the object ids of `box`, split on failure. Returns (leaves, failed): leaves =
    [(box, [ids])] of the parts the service listed, failed = [box] of the parts it would not
    list (HTTP 504 / time-outs / errors) even when quartered max_depth times. A failed box
    waits SPLIT_PAUSE_S x level before it is split (back-off); budget (a _Health) stops the
    splitting once the tile has failed too often in a row (its remaining parts: failed)."""
    if budget.down():
        return [], [tuple(box)]
    try:
        d = http_json(url.rstrip("/") + "/query", dict(_envelope(box), where=where,
                                                         returnIdsOnly="true", f="json"),
                      post=True, retries=3 if depth == 0 else 2, what="object ids")
    except BuildCancelled:
        raise
    except BuildError as e:
        budget.failed()
        budget.last = _reason(e)
        if depth >= max_depth or budget.down():
            return [], [tuple(box)]
        _pause(SPLIT_PAUSE_S * (depth + 1))
        leaves, failed = [], []
        for q in _split4(box):
            a, b = _list_ids_split(url, q, max_depth, budget, where, depth + 1)
            leaves += a
            failed += b
        return leaves, failed
    budget.ok()
    ids = (d.get("objectIds") if isinstance(d, dict) else None) or []
    return [(tuple(box), sorted(int(i) for i in ids))], []


def _reason(e):
    """1.3: the short cause of a failed request ("HTTP Error 504: Gateway Time-out")."""
    txt = str(e)
    for pat in (r"HTTP Error \d+[^()]*", r"timed out", r"service error: [^()]*"):
        m = re.search(pat, txt)
        if m:
            return m.group(0).strip().rstrip(")")
    return _short(e)


def _list_ids_tiled(ctx, url, tiles, max_depth, what):
    """1.3: every tile listed in parallel (split on failure). Returns (leaves, failed,
    {cause: failed requests})."""
    leaves, failed, causes = [], [], {}
    state = {"bad_row": 0, "done": 0}
    lock = threading.Lock()

    def job(tile):
        with lock:
            down = state["bad_row"] >= TILED_DOWN_TILES
        if down:                            # the service is down: no more requests
            return [], [tuple(tile)], "service not answering"
        budget = _Health(limit=TILED_TILE_FAILS)
        budget.last = ""
        a, b = _list_ids_split(url, tile, max_depth, budget)
        with lock:
            state["bad_row"] = 0 if a else state["bad_row"] + 1
        return a, b, budget.last

    def got(args, res):
        if isinstance(res, Exception):
            failed.append(tuple(args[0]))
            why = _reason(res)
        else:
            leaves.extend(res[0])
            failed.extend(res[1])
            why = res[2] if res[1] else ""
        if why:
            causes[why] = causes.get(why, 0) + 1
        state["done"] += 1
        ctx.sub(20.0 * state["done"] / max(1, len(tiles)), "{}: listing area {} of {}".format(
            what, state["done"], len(tiles)))
    _pool(ctx, ctx.workers, [(job, (t,)) for t in tiles], got)
    return leaves, failed, causes


class _BoxIndex(object):
    """1.3: which of many small boxes holds a point / meets a box (grid of cells)."""

    def __init__(self, boxes, cell=TILED_TILE_DEG):
        self.boxes = [tuple(b) for b in boxes]
        self.cell = float(cell)
        self.grid = {}
        for k, b in enumerate(self.boxes):
            for c in self._cells(b):
                self.grid.setdefault(c, []).append(k)

    def _cells(self, b):
        c = self.cell
        for i in range(int(math.floor(b[0] / c)), int(math.floor(b[2] / c)) + 1):
            for j in range(int(math.floor(b[1] / c)), int(math.floor(b[3] / c)) + 1):
                yield (i, j)

    def __bool__(self):
        return bool(self.boxes)

    def holds(self, x, y):
        c = self.cell
        for k in self.grid.get((int(math.floor(x / c)), int(math.floor(y / c))), ()):
            b = self.boxes[k]
            if b[0] <= x < b[2] and b[1] <= y < b[3]:
                return True
        return False

    def meets(self, bb):
        for cell in self._cells(bb):
            for k in self.grid.get(cell, ()):
                b = self.boxes[k]
                if b[0] <= bb[2] and bb[0] <= b[2] and b[1] <= bb[3] and bb[1] <= b[3]:
                    return True
        return False


def _owned(bb, area, index):
    """1.3: does the region of `index` own a feature with bounding box bb? Its centre lies
    in one of the boxes, or (centre outside the whole area) it touches one."""
    cx, cy = (bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0
    if index.holds(cx, cy):
        return True
    inside = area[0] <= cx <= area[2] and area[1] <= cy <= area[3]
    return not inside and index.meets(bb)


def _tagged_uid(tag, uid):
    """1.3: "<tag>:<id>" - object ids of two services collide; no id: the geometry hash."""
    def f(feat):
        u = uid(feat)
        if u is None:
            g = feat.get("geometry")
            u = "g:" + hashlib.sha1(json.dumps(g, sort_keys=True).encode("utf-8")).hexdigest()
        return "{}:{}".format(tag, u)
    return f


def _layer_oid(info):
    oid = info.get("objectIdField") or next(
        (f.get("name") for f in info.get("fields") or []
         if f.get("type") == "esriFieldTypeOID"), None)
    mrc = int(info.get("maxRecordCount") or IDS_CHUNK)
    return oid, mrc


def _read_part(ctx, url, boxes, spec, tag, prefix, ignore, keep=None, first=0.0):
    """1.3: one service's part of an arcgis_tiled data set - the ids of `boxes` (split on
    failure), then the features in chunks. Returns (failed boxes, leaves [(box, ids)], ids not
    read, error text or "")."""
    try:
        info = http_json(url.rstrip("/"), {"f": "json"}, what="layer description")
    except BuildCancelled:
        raise
    except BuildError as e:
        ctx.log("  {}: the layer description failed ({}) - none of its {} area(s) read".format(
            tag, _short(e), len(boxes)))
        return [tuple(b) for b in boxes], [], [], "layer description: " + _reason(e)
    oid, mrc = _layer_oid(info)
    leaves, failed, causes = _list_ids_tiled(ctx, url, boxes, int(spec.get(
        "split_depth", TILED_SPLIT_DEPTH)), tag)
    ids = sorted(set(i for _b, ii in leaves for i in ii))
    ctx.log("  {}: {:,} features in {} area(s) listed, {} area(s) not listed".format(
        tag, len(ids), len(leaves), len(failed)))
    done = _resume_ids(ctx, ids, prefix=prefix, ignore=ignore)
    field = spec.get("source_field") or "src"

    def edit(feats):
        out = []
        for f in feats:
            if keep is not None:
                g = f.get("geometry") if isinstance(f, dict) else None
                bb = geom_bbox(g) if g else None
                if bb is None or not keep(bb):
                    continue
            props = f.get("properties")
            if not isinstance(props, dict):
                props = f["properties"] = {}
            props[field] = tag
            out.append(f)
        return out

    def show(k, n):
        ctx.sub(first + 20.0 + 40.0 * k / max(1, n), "{}: {:,} of {:,} features".format(
            tag, min(k, n), n))
    size = max(1, min(int(spec.get("chunk") or TILED_CHUNK), mrc))
    # prefix + "ids:..." keys (the fill part's prefix names its areas: other areas - stale)
    missing, _h = _fetch_id_chunks(ctx, url, ids, done, size, spec.get("fields", "*"),
                                   spec.get("precision"), _tagged_uid(tag, _uid_arcgis(oid)),
                                   prefix=prefix if prefix != "ids:" else "", edit=edit,
                                   progress=show)
    why = ", ".join("{} x{}".format(k, v) if v > 1 else k for k, v in sorted(
        causes.items(), key=lambda kv: -kv[1]))
    return failed, leaves, missing, why


def _boxes_key(boxes):
    txt = ";".join("{:.5f},{:.5f},{:.5f},{:.5f}".format(*b) for b in sorted(boxes))
    return "{:08x}".format(zlib.crc32(txt.encode("ascii")) & 0xffffffff)


def _reconcile(ctx, area, nhd_index, tag, fill_tag):
    """1.3: one source per place. Rows of `tag` owned by the fill region and rows of
    fill_tag outside it are removed; the chunk records of removed rows are dropped too, so a
    later run reads them again when its areas differ. Returns rows removed."""
    w = ctx.writer
    table = w._cur["table"]
    drop = []
    for fid, uid, x0, y0, x1, y1 in w.con.execute(
            "SELECT fid, uid, minx, miny, maxx, maxy FROM {}".format(table)):
        mine = _owned((x0, y0, x1, y1), area, nhd_index) if nhd_index else False
        src = str(uid).split(":", 1)[0]
        if (src == tag and mine) or (src == fill_tag and not mine):
            drop.append(fid)
    if not drop:
        return 0
    name = w._cur["name"]
    spans = list(w.con.execute("SELECT ckey, fid_lo, fid_hi FROM chunks WHERE name=?",
                               (name,)))
    hit = set()
    for fid in drop:
        for key, lo, hi in spans:
            if lo is not None and hi is not None and lo < fid <= hi:
                hit.add(key)
    for k in range(0, len(drop), 500):
        part = drop[k:k + 500]
        w.con.execute("DELETE FROM {} WHERE fid IN ({})".format(table, ",".join("?" * len(part))),
                      part)
    for key in hit:
        w.con.execute("DELETE FROM chunks WHERE name=? AND ckey=?", (name, key))
    w._cur["n"] = w.con.execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0]
    w.con.commit()
    return len(drop)


def _count_tag(ctx, tag):
    w = ctx.writer
    return w.con.execute("SELECT COUNT(*) FROM {} WHERE uid LIKE ?".format(w._cur["table"]),
                         (tag + ":%",)).fetchone()[0]


def _round_box(b):
    return [round(float(v), 5) for v in b]


def fetch_arcgis_tiled(ctx, url, bbox, spec):
    """1.3 (Data 1.5.1, 3DHP hydro): an ArcGIS layer read area by area, another service
    filling only the areas it cannot serve.

    The object ids are listed per TILED_TILE_DEG tile (a tile that fails - HTTP 504, time-out
    - is quartered after a pause, down to split_depth levels), then the features are read in
    chunks. The areas whose ids could not be listed, or holding features that could not be
    read, are then read from spec["fill"] (another ArcGIS layer, e.g. NHD, listed and split
    the same way). Every feature carries properties[source_field] = its source tag, and its
    uid is "<tag>:<id>". One source per place: a feature belongs to the fill service when the
    centre of its bounding box lies in a fill area (or lies outside the whole area and the
    feature touches a fill area); features of the other source there are removed, so tile
    edges are not doubled. Fill areas the fill service cannot read either are the data set's
    gaps (status partial). ctx.tiled gets the per-area report (counts per source, the failed
    areas). Returns a note."""
    tag = spec.get("tag") or "main"
    fill = spec.get("fill") or None
    if fill:
        fill = dict(fill, source_field=fill.get("source_field") or spec.get("source_field"))
    ftag = (fill or {}).get("tag") or "fill"
    tiles = _tiles(bbox, float(spec.get("tile_deg") or TILED_TILE_DEG))
    rep = {"tiles": len(tiles), "source": tag, "fill_source": ftag if fill else None}
    ctx.tiled = rep
    ctx.log("  {}: {} area(s) of {} degree".format(tag, len(tiles),
                                                 spec.get("tile_deg") or TILED_TILE_DEG))
    failed, leaves, missing, err = _read_part(ctx, url, tiles, spec, tag, "ids:", ("fill:",))
    fill_boxes = list(failed)
    if missing:
        miss = set(missing)
        fill_boxes += [b for b, ii in leaves if any(i in miss for i in ii)]
    fill_boxes = sorted(set(tuple(b) for b in fill_boxes))
    rep.update(areas_listed=len(leaves), areas_not_listed=len(failed),
               missing_ids=len(missing), error=err or None)
    if failed or missing:
        why = []
        if failed:
            why.append("{} area(s) not listed{}".format(len(failed), " ({})".format(err)
                                                        if err else ""))
        if missing:
            why.append("{:,} feature(s) not read".format(len(missing)))
        rep["why"] = "; ".join(why)
        ctx.log("  {}: {} - {} area(s) to fill{}".format(
            tag, rep["why"], len(fill_boxes), " from " + ftag if fill else ""))
    gaps, nhd_ok = list(fill_boxes), []
    if fill_boxes and fill:
        furl = fill["url"]
        index = _BoxIndex(fill_boxes)
        ctx.log("  {} fill: reading {} area(s) from {}".format(ftag, len(fill_boxes), furl))
        net = NetSession(fill.get("label") or "{} fill".format(ftag), ctx.workers, log=ctx.log,
                         on_beat=getattr(ctx, "beat", None))
        token = _NET.set(net)
        try:
            f_failed, f_leaves, f_missing, f_err = _read_part(
                ctx, furl, fill_boxes, fill, ftag, "fill:{}:".format(_boxes_key(fill_boxes)),
                ("ids:",), keep=lambda bb: _owned(bb, bbox, index), first=40.0)
        finally:
            _NET.reset(token)
        fmiss = set(f_missing)
        nhd_ok = [b for b, ii in f_leaves if not any(i in fmiss for i in ii)]
        gaps = sorted(set(tuple(b) for b in f_failed) |
                      set(b for b, ii in f_leaves if any(i in fmiss for i in ii)))
        rep.update(fill_areas_read=len(nhd_ok), fill_missing_ids=len(f_missing),
                   fill_error=f_err or None)
        ctx.log("  {} fill: {} of {} area(s) read{}".format(
            ftag, len(nhd_ok), len(f_failed) + len(f_leaves),
            "" if not gaps else " - {} area(s) not read by either service".format(len(gaps))))
    removed = _reconcile(ctx, bbox, _BoxIndex(nhd_ok), tag, ftag)
    n_main, n_fill = _count_tag(ctx, tag), _count_tag(ctx, ftag)
    rep.update(count={tag: n_main, ftag: n_fill}, removed_overlap=removed,
               fill_areas=[_round_box(b) for b in fill_boxes],
               gap_areas=[_round_box(b) for b in gaps])
    if not leaves and not nhd_ok:
        raise BuildError("{} and {} both failed for the whole area ({}{})".format(
            tag, ftag if fill else "no fill service", err or "nothing listed",
            "; " + rep.get("fill_error") if rep.get("fill_error") else ""))
    ctx.gaps = [tuple(b) for b in gaps]
    if gaps:
        ctx.status = STATUS_PARTIAL
    if not fill_boxes:
        return "{:,} features from {} ({} areas)".format(n_main, tag, len(tiles))
    return "{:,} features from {}, {:,} from {} in {} of {} area(s){}".format(
        n_main, tag, n_fill, ftag, len(nhd_ok) if fill else 0, len(fill_boxes),
        "; {} area(s) not read".format(len(gaps)) if gaps else "")


def _page_anchor(url, bbox, oid, edit=None):
    """BP.1: what the paged service holds now - {"count", "max_oid", "edit"}
    (the parts it answers), or None when not even the count can be read."""
    try:
        d = http_json(url.rstrip("/") + "/query", dict(_envelope(bbox), where="1=1",
                      returnCountOnly="true", f="json"), post=True, retries=1,
                      what="feature count")
        a = {"count": int(d["count"])}
    except BuildCancelled:
        raise
    except Exception:                                  # noqa: BLE001 - best effort
        return None
    if oid:
        try:
            d = http_json(url.rstrip("/") + "/query", dict(_envelope(bbox), where="1=1",
                          outStatistics=json.dumps([{"statisticType": "max",
                                                     "onStatisticField": oid,
                                                     "outStatisticFieldName": "mx"}]),
                          f="json"), post=True, retries=1, what="last object id")
            attrs = (d.get("features") or [{}])[0].get("attributes") or {}
            mx = next((v for k, v in attrs.items() if k.lower() == "mx"), None)
            if mx is not None:
                a["max_oid"] = mx
        except BuildCancelled:
            raise
        except Exception:                              # noqa: BLE001
            pass
    if edit is not None:
        a["edit"] = edit
    return a


def _page_400(ctx, url, params, size, offset, err, why=""):
    """1.3: a page the service refused with HTTP 400 (FEMA USA_Structures while its object-id
    query answered "Invalid URL"): tried again after a pause with smaller pages (down to
    PAGE_MIN). Returns (answer, page size used); BuildError with a plain reason when even the
    smallest page is refused (the pages read so far stay stored for the next run)."""
    sub = size // 2
    while sub >= PAGE_MIN:
        ctx.log("  page at feature {:,} refused (HTTP 400) - trying {} features per page after "
                "{:.0f} s".format(offset, sub, PAGE_400_PAUSE_S))
        _pause(PAGE_400_PAUSE_S)
        try:
            d = http_json(url.rstrip("/") + "/query", dict(params, resultRecordCount=sub),
                          post=True, retries=2, what="page")
            return d, sub
        except BuildCancelled:
            raise
        except BuildError as e:
            if getattr(e, "http_code", None) != 400:
                raise
        sub //= 2
    raise BuildError(
        "the service refused the page at feature {:,} with HTTP 400 Bad Request, also with "
        "smaller pages (down to {}){}; {:,} features read so far are kept and the next run "
        "continues from there - the service is probably down or changed its query rules, run "
        "the build again later ({})".format(
            offset, PAGE_MIN, " - its object-id list failed too ({})".format(why) if why else "",
            ctx.writer.count, err))


def _fetch_arcgis_pages(ctx, url, bbox, oid, mrc, out_fields, precision, uid, edit=None,
                        why=""):
    offset, page = 0, 0
    size = max(1, min(2000, mrc))
    # BP.1: pages are offsets - kept pages are only valid while the service holds
    # the same features. Its count (+ last object id / edit date) is stored with
    # the pages; a change on resume discards them, an unknown state = PARTIAL.
    akey = "page:{}:anchor".format(size)
    now = _page_anchor(url, bbox, oid, edit)
    resume_note = ""
    # v2.8.0: a page is a chunk too (key = offset and size; payload = how many
    # features it returned and whether more follow)
    kept = ctx.writer.chunks()
    stale = [k for k in kept if not k.startswith("page:{}:".format(size))]
    if stale:
        ctx.writer.drop_chunks(stale)
        kept = ctx.writer.chunks()
    pages = [k for k in kept if k != akey]
    if pages:
        old = _unpack_json(kept[akey]) if kept.get(akey) else None
        if now is None or not isinstance(old, dict):
            resume_note = ("kept pages could not be checked against the service (feature "
                           "count {}) - features may be missing or doubled; rebuild with "
                           "--refresh".format("not answered" if now is None else
                                              "not recorded by the earlier run"))
            ctx.log("  WARNING: " + resume_note)
            ctx.status = STATUS_PARTIAL
        elif any(old.get(k) != now.get(k) for k in now if k in old):
            ctx.log("  the service changed since the pages were stored ({} -> {}) - "
                    "starting the pages over".format(old, now))
            ctx.writer.restart_stage()
            kept, pages = {}, []
    if not pages and now is not None:
        ctx.writer.add([], uid, chunk=akey, payload=_pack_json(now))
    if pages:
        ctx.log("  resuming: {} page(s) already stored".format(len(pages)))
    while True:
        key = "page:{}:{}".format(size, offset)
        meta = _unpack_json(kept[key]) if kept.get(key) else None
        if isinstance(meta, dict) and isinstance(meta.get("n"), int):
            nfe, exceeded = meta["n"], bool(meta.get("exceeded"))
            page += 1
            ctx.sub(min(95.0, page), "{:,} features".format(ctx.writer.count))
            if not nfe or (nfe < size and not exceeded):
                return resume_note
            offset += nfe
            continue
        params = dict(_envelope(bbox), where="1=1", outFields=out_fields, outSR="4326",
                      f="geojson", resultOffset=offset, resultRecordCount=size)
        if oid:
            params["orderByFields"] = "{} ASC".format(oid)
        if precision is not None:
            params["geometryPrecision"] = str(precision)
        got_size = size
        try:
            d = http_json(url.rstrip("/") + "/query", params, post=True, what="page")
        except BuildCancelled:
            raise
        except BuildError as e:
            if getattr(e, "http_code", None) != 400:
                raise
            d, got_size = _page_400(ctx, url, params, size, offset, e, why)
        feats = d.get("features", []) if isinstance(d, dict) else []
        exceeded = bool(d.get("exceededTransferLimit") or
                        (d.get("properties") or {}).get("exceededTransferLimit"))
        if got_size < size and len(feats) >= got_size:
            exceeded = True                 # a smaller page came back full: more follow
        added = ctx.writer.add(feats, uid, chunk=key,
                               payload=_pack_json({"n": len(feats), "exceeded": exceeded}))
        page += 1
        ctx.sub(min(95.0, page), "{:,} features".format(ctx.writer.count))
        if not feats or (len(feats) < size and not exceeded):
            return resume_note
        if added == 0:
            return "the service repeats the same page (no paging) - {:,} kept".format(
                ctx.writer.count)
        offset += len(feats)


# -------------------------------------------------------------------- NSI --
def _tiles(b, deg):
    x0, y0, x1, y1 = b
    nx = max(1, int(math.ceil((x1 - x0) / deg - 1e-9)))
    ny = max(1, int(math.ceil((y1 - y0) / deg - 1e-9)))
    dx, dy = (x1 - x0) / nx, (y1 - y0) / ny
    return [(x0 + i * dx, y0 + j * dy, x0 + (i + 1) * dx, y0 + (j + 1) * dy)
            for j in range(ny) for i in range(nx)]


def _nsi_tile(tile, depth=0):
    x0, y0, x1, y1 = tile
    ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    bbox = ",".join("{:.6f},{:.6f}".format(x, y) for x, y in ring)
    try:
        d = http_json(C.NSI_API_URL, {"bbox": bbox, "fmt": "fc"}, what="NSI tile")
        return d.get("features", []) if isinstance(d, dict) else []
    except BuildCancelled:
        raise
    except BuildError:
        if depth >= 2:
            raise
        out = []
        for q in _split4(tile):
            out += _nsi_tile(q, depth + 1)
        return out


def fetch_nsi(ctx, bbox):
    tiles = _tiles(bbox, NSI_TILE_DEG)
    ctx.log("  {} tiles of {} degree".format(len(tiles), NSI_TILE_DEG))
    have, keys = _resume_tiles(ctx, tiles)
    failed = []
    done = [len(have)]

    def uid(f):
        return (f.get("properties") or {}).get("fd_id")

    def got(args, res):
        if isinstance(res, Exception):
            failed.append((args[0], str(res)))
        else:
            ctx.writer.add(res, uid, chunk=_tile_key(args[0]))
        done[0] += 1
        ctx.sub(100.0 * done[0] / len(tiles), "{} of {} tiles, {:,} structures".format(
            done[0], len(tiles), ctx.writer.count))
    _pool(ctx, ctx.workers, [(_nsi_tile, (t,)) for t, k in zip(tiles, keys)
                             if k not in have], got)
    if failed:
        if len(failed) > max(2, len(tiles) // 20):
            raise BuildError("{} of {} NSI tiles failed, e.g. {}".format(
                len(failed), len(tiles), failed[0][1]))
        note = "{} tile(s) could not be read: {}".format(len(failed), "; ".join(
            "{:.2f},{:.2f}".format(t[0], t[1]) for t, _e in failed))
        ctx.log("  WARNING: " + note)
        # v2.6.2: the plugin treats a study area touching these as not covered
        ctx.gaps = [tuple(t) for t, _e in failed]
        return note
    return ""


# -------------------------------------------------------------- Overpass --
def _overpass(query):
    d = http_json(C.OVERPASS_URL, {"data": query}, post=True, what="Overpass")
    remark = d.get("remark") if isinstance(d, dict) else None
    if remark and any(s in str(remark).lower() for s in
                      ("error", "timed out", "timeout", "out of memory", "aborted")):
        raise BuildError("Overpass: {}".format(remark))
    return d


def _osm_tile(make_query, parse, tile, depth=0):
    try:
        return parse(_overpass(O.with_timeout(make_query(tile), OVERPASS_TIMEOUT_S)))
    except BuildCancelled:
        raise
    except BuildError:
        if depth >= 2:
            raise
        out = []
        for q in _split4(tile):
            out += _osm_tile(make_query, parse, q, depth + 1)
        return out


def fetch_osm(ctx, bbox, make_query, parse):
    tiles = _tiles(bbox, OVERPASS_TILE_DEG)
    have, keys = _resume_tiles(ctx, tiles)
    done = [len(have)]
    errors = []

    def uid(f):
        return (f.get("properties") or {}).get("osm_id")

    def got(args, res):
        if isinstance(res, Exception):
            errors.append(res)
        else:
            ctx.writer.add(res, uid, chunk=_tile_key(args[2]))
        done[0] += 1
        ctx.sub(100.0 * done[0] / len(tiles), "{} of {} areas, {:,} features".format(
            done[0], len(tiles), ctx.writer.count))
    _pool(ctx, min(ctx.workers, C.OVERPASS_WORKERS),
          [(_osm_tile, (make_query, parse, t)) for t, k in zip(tiles, keys)
           if k not in have], got)
    if errors:
        # an area missing would silently drop features: the data set fails
        raise BuildError("{} of {} areas failed: {}".format(len(errors), len(tiles), errors[0]))
    return ""


# ------------------------------------------------------------- data sets --
def datasets(dwr_folder=None):
    """[(name, label, group, kind, spec)] - name = the plugin's download
    name, so the plugin finds each data set under the name it asks for.
    2.18.66: dwr_folder (a folder of the ND DWR GeoPackages): the DWR_* data sets of group
    "dwr" (kind "local_gpkg") are added for the files found there; without it the list is
    the same as before."""
    nhd_fields = "gnis_name,gnis_id,ftype,fcode,reachcode,lengthkm"
    d = [
        ("NSI_structures", "USACE National Structure Inventory", "nsi", "nsi", {}),
        ("NDGISHUB_address_points", "ND GIS Hub site / structure address points", "address",
         "arcgis", {"url": C.ND_ADDRESS_POINTS_URL}),
        ("USA_Structures", "FEMA USA Structures outlines", "usa", "arcgis",
         {"url": C.USA_STRUCTURES_URL}),
        ("NID_dams", "National Inventory of Dams", "nid", "arcgis",
         {"urls": [C.NID_URL, C.NID_MIRROR_URL]}),
        ("NARN_rail_lines", "FRA/BTS rail network", "rail", "arcgis", {"url": C.NARN_URL}),
        ("FRA_grade_crossings", "FRA grade crossings", "rail", "arcgis",
         {"url": C.CROSSINGS_URL}),
        ("ND_roads_NG911", "ND GIS Hub NG9-1-1 road centerlines", "roads", "arcgis",
         {"url": C.ND_ROADS_URL}),
    ]
    hpms = []
    for url in C.HPMS_ND_URLS:
        year = "".join(ch for ch in url.split("HPMS_FULL_ND_")[-1][:4] if ch.isdigit())
        hpms.append(("HPMS_ND_{}".format(year), url))
    d.append(("HPMS_ND", "FHWA HPMS North Dakota (newest year that answers)", "traffic",
              "first_of", {"choices": hpms}))
    if C.NDDOT_AADT_URL:
        d.append(("NDDOT_traffic_counts", "NDDOT traffic counts", "traffic", "arcgis",
                  {"url": "{}/{}".format(C.NDDOT_AADT_URL.rstrip("/"),
                                         C.NDDOT_AADT_LAYERS.get("aadt", 0))}))
    d.append(("ND_county_roads", "ND GIS Hub county roads (AADT)", "traffic", "arcgis",
              {"url": C.ND_COUNTY_ROADS_URL}))
    for key, label, url, _cat in C.lifeline_downloads():
        d.append(("lifeline_" + key, label, "lifelines", "arcgis", {"url": url}))
    d += [
        ("NBI_bridges", "National Bridge Inventory", "bridges", "arcgis", {"url": C.NBI_URL}),
        ("NHD_flowlines", "USGS NHD flowlines (name and type only)", "nhd", "arcgis",
         {"url": C.NHD_FLOWLINE_URL, "fields": nhd_fields, "precision": 6}),
        ("NHD_waterbodies", "USGS NHD waterbodies (large scale)", "waterbodies", "arcgis",
         {"url": C.NHD_WATERBODY_URL,
          "fields": "PERMANENT_IDENTIFIER,GNIS_ID,GNIS_NAME,AREASQKM,ELEVATION,REACHCODE,FTYPE,FCODE",
          "precision": 6}),
        ("OSM_lifelines", "OpenStreetMap lifeline facilities", "osm", "osm",
         {"query": O.overpass_query, "parse": O.overpass_to_features}),
        ("OSM_crossings", "OpenStreetMap culverts / fords / rail bridges", "osm", "osm",
         {"query": O.crossings_query, "parse": O.crossing_features}),
        ("OSM_campgrounds", "OpenStreetMap campgrounds / RV parks", "osm", "osm",
         {"query": O.camps_query, "parse": O.camp_features}),
        ("OSM_trailer_parks", "OpenStreetMap mobile-home parks", "osm", "osm",
         {"query": O.trailer_parks_query, "parse": O.trailer_park_features}),
        ("OSM_places", "OpenStreetMap cities / towns / villages", "osm", "osm",
         {"query": O.places_query, "parse": O.place_features}),
    ]
    if dwr_folder:
        d += dwr_datasets(dwr_folder)
    return d


def dwr_datasets(folder):
    """2.18.66: the DWR_* data sets of the GeoPackages in `folder` (the suite folder's layer
    rules, dwr_files.find_layers). A data set with no file is left out (and named)."""
    try:
        found = DF.find_layers(os.path.abspath(folder))
    except DF.DwrFileError as e:
        raise BuildError(str(e))
    out = []
    for name, slot, label, what in DF.DATASETS:
        if name in found:
            path, layer = found[name]
            out.append((name, label, DF.GROUP, "local_gpkg",
                        {"path": path, "layer": layer, "slot": slot, "what": what}))
    if not out:
        raise BuildError("no ND DWR GeoPackage found in {} (DWR_Dams.gpkg, DWR_Dam_Lines.gpkg, "
                         "DWR_Unknown_Structures.gpkg, DWR_Dugouts.gpkg, "
                         "DWR_Dugouts_Polygons.gpkg)".format(folder))
    return out


def fetch_local_gpkg(ctx, spec):
    """2.18.66: one layer of a local GeoPackage into the pack: every attribute kept, the
    geometry reprojected to lon / lat (dwr_files). Returns the manifest note: the source file,
    its layer, CRS and file date."""
    path, layer = spec["path"], spec["layer"]
    try:
        feats, info = DF.read_layer(path, layer)
    except DF.DwrFileError as e:
        raise BuildError(str(e))
    ctx.sub(50, "{:,} features read".format(len(feats)))
    ctx.writer.add(feats, DF.uid_of)
    # kept as they are, but said: a feature wholly outside North Dakota (+ the pad) is most
    # likely a bad geometry in the source (DWR dugout structure 2163, point and polygon)
    x0, y0, x1, y1 = (ND_BBOX[0] - DEFAULT_PAD_DEG, ND_BBOX[1] - DEFAULT_PAD_DEG,
                      ND_BBOX[2] + DEFAULT_PAD_DEG, ND_BBOX[3] + DEFAULT_PAD_DEG)
    out = 0
    for f in feats:
        b = DF.bbox_of(f.get("geometry"))
        if b and (b[2] < x0 or b[0] > x1 or b[3] < y0 or b[1] > y1):
            out += 1
    extra = []
    if out:
        extra.append("{:,} wholly outside North Dakota (kept)".format(out))
    # 1.3: the file's own date (GeoPackage last change), not the day it was copied here
    day, how = DF.source_date(path, layer)
    ctx.source_date = (day, how)
    return "from {} layer {} (EPSG:{}{}, file dated {} ({}), {:,} rows{})".format(
        os.path.basename(path), layer, info["epsg"],
        ", unit {:g} m".format(info["unit_m"]) if info["unit_m"] != 1.0 else "",
        day, how, info["rows"], "; " + ", ".join(extra) if extra else "")


class _Ctx(object):
    def __init__(self, writer, workers, log, sub, beat=None, refresh=False):
        self.writer, self.workers, self.log, self.sub = writer, workers, log, sub
        self.beat = beat                     # v2.8.0: beat(text) - progress while waiting
        self.refresh = refresh               # v2.8.0: no chunk resume
        self.status = "complete"             # v2.6.2: or partial
        self.gaps = []                       # boxes that could not be read


def _pick(names_or_groups, table):
    want = set(x.strip() for x in (names_or_groups or "").split(",") if x.strip())
    if not want:
        return None
    known = set(n for n, *_r in table) | set(g for _n, _l, g, _k, _s in table)
    bad = want - known
    if bad:
        raise BuildError("unknown data set(s): {} (see --list{})".format(
            ", ".join(sorted(bad)), "; the dwr group needs --dwr-folder PATH" if (
                "dwr" in bad or any(b.startswith("DWR_") for b in bad)) else ""))
    return want



# ND DWR MapService downloads. 2.19.2: the download form of mapservice.dwr.nd.gov
# (index.php?active=Dams > Download Data) posts the layer NAME as qlay, whichextent all /
# partial, srid, downloadformat shp / spat, mapext (the map's EPSG:2266 extent in feet,
# '+'-separated) and map (the server's map file). The old request (layer=<index>,
# format=shapefile, extent=full) is not understood: HTTP 200 with an empty body.
DWR_DOWNLOAD_URL = "https://mapservice.dwr.nd.gov/php/downloadShape.php"
DWR_MAPFILE = "/Volumes/Data/WebServer/newswcprivate/mapfiles/swc.map"
# the web map's full extent (EPSG:2266, ND South State Plane, international feet)
DWR_FULL_EXTENT = (1026879.6641881, 54774.288373765, 2999091.3458676, 1331032.7378794)
DWR_FORMATS = {"shp": ".shp", "spat": ".sqlite"}
DWR_DOWNLOADS = (     # (data set, MapService layer index (for reference), qlay = layer name)
    ("DWR_dam_lines", 253, "Damsline"),
    ("DWR_dams", 254, "Dams"),
    ("DWR_dugout_polygons", 276, "DugoutsPoly"),
    ("DWR_dugouts", 277, "Dugouts"),
)


def dwr_extent(bbox=None):
    """mapext of a DWR download: a lon / lat bbox (west, south, east, north) as the EPSG:2266
    box around it (feet, '+'-separated, the way the web map sends it); None -> the full map."""
    if bbox is None:
        box = DWR_FULL_EXTENT
    else:
        proj, unit = DF._KNOWN[2266]
        w, s, e, n = (float(v) for v in bbox)
        pts = [proj.forward(x, y) for x in (w, e) for y in (s, n)]
        pts += [proj.forward((w + e) / 2.0, y) for y in (s, n)]    # the arc of the parallels
        xs = [p[0] / unit for p in pts]
        ys = [p[1] / unit for p in pts]
        box = (min(xs), min(ys), max(xs), max(ys))
    return "+".join("{:.3f}".format(v) for v in box)


def dwr_request(qlay, bbox=None, fmt="shp"):
    """The form fields of one DWR MapService download (see DWR_DOWNLOAD_URL)."""
    if fmt not in DWR_FORMATS:
        raise BuildError("DWR download format must be one of {}".format(", ".join(DWR_FORMATS)))
    return {"whichextent": "all" if bbox is None else "partial", "srid": "4269",
            "downloadformat": fmt, "qlay": qlay, "mapext": dwr_extent(bbox),
            "map": DWR_MAPFILE}


def download_dwr_shapefiles(dest_folder, log=None, bbox=None, fmt="shp", only=None):
    """Download the four ND DWR layers (dams, dam lines, dugouts, dugout polygons) from the
    DWR MapService and extract each zip into dest_folder/<data set>/.

    bbox (west, south, east, north, lon / lat) downloads only that area (the form's "Data
    within current view"); None -> the full data set. fmt "shp" (shapefile: 10-character
    field names) or "spat" (SpatiaLite: full field names). Coordinates NAD83 lon / lat
    (srid 4269). only: data set names or qlay names to fetch (None -> all four).
    Returns the list of extracted folders; a layer that fails is logged and skipped. An
    answer that is not a zip (the old request got an empty one) is never saved as a .zip."""
    import urllib.request
    import urllib.parse
    log = log or (lambda m: None)
    os.makedirs(dest_folder, exist_ok=True)
    out = []
    for name, _index, qlay in DWR_DOWNLOADS:
        if only and name not in only and qlay not in only:
            continue
        data = urllib.parse.urlencode(dwr_request(qlay, bbox, fmt)).encode()
        req = urllib.request.Request(DWR_DOWNLOAD_URL, data=data,
                                     headers={"User-Agent": _ua()})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                raw = r.read()
        except Exception as e:
            log("  {} failed: {}".format(qlay, e))
            continue
        if not raw.startswith(b"PK"):
            log("  {}: the DWR MapService sent no file ({} bytes)".format(qlay, len(raw)))
            continue
        zpath = os.path.join(dest_folder, "{}.zip".format(name))
        with open(zpath + ".part", "wb") as f:
            f.write(raw)
        os.replace(zpath + ".part", zpath)
        sub = os.path.join(dest_folder, name)
        os.makedirs(sub, exist_ok=True)
        try:
            with zipfile.ZipFile(zpath) as zf:
                zf.extractall(sub)
                names = zf.namelist()
        except zipfile.BadZipFile:
            log("  {} is not a zip ({} bytes)".format(qlay, len(raw)))
            continue
        if not any(n.lower().endswith(DWR_FORMATS[fmt]) for n in names):
            log("  {}: the zip holds no {} file ({})".format(qlay, DWR_FORMATS[fmt],
                                                             ", ".join(names)))
            continue
        out.append(sub)
        log("  {} -> {} ({:,} bytes)".format(qlay, sub, len(raw)))
    return out


def build(out_zip, pad=DEFAULT_PAD_DEG, only=None, skip=None, workers=None, refresh=False,
          sources_file=None, progress=None, log=None, dwr_folder=None):
    """Build (or resume / update) the pack and write out_zip. progress(pct,
    msg) and log(msg) are optional callbacks; an exception raised by
    progress (a cancel) stops the build - the finished data sets are kept
    for the next run. Returns the manifest. 2.18.66: dwr_folder adds the ND DWR
    GeoPackages of that folder (group "dwr"; read again on every run - they are local)."""
    log = log or print
    progress = progress or (lambda p, m: None)
    _CANCEL.clear()
    # v2.6.2: inside QGIS the plugin has applied the data-source file already;
    # applying it again from here cleared the plugin's overrides
    if sources_file and not __package__:
        C.apply_source_overrides(sources_file)
        if C.SOURCE_OVERRIDES:
            log("Data-source file {}: {} address(es) changed".format(
                sources_file, len(C.SOURCE_OVERRIDES)))
    table = datasets(dwr_folder)
    only_set, skip_set = _pick(only, table), _pick(skip, table) or set()
    todo = [t for t in table if (only_set is None or t[0] in only_set or t[2] in only_set)
            and t[0] not in skip_set and t[2] not in skip_set]
    bbox = (ND_BBOX[0] - pad, ND_BBOX[1] - pad, ND_BBOX[2] + pad, ND_BBOX[3] + pad)
    out_zip = os.path.abspath(out_zip)
    if not out_zip.lower().endswith(".zip"):
        out_zip += ".zip"
    work = os.path.splitext(out_zip)[0] + "_work"
    os.makedirs(work, exist_ok=True)
    db = os.path.join(work, DB_NAME)
    log("Offline data pack for North Dakota - area {:.3f}, {:.3f} to {:.3f}, {:.3f} "
        "(lon / lat)".format(*bbox))
    log("Working file: {}".format(db))
    writer = PackWriter(db, bbox, created_by="build_nd_pack.py {} / plugin {}".format(
        BUILDER_VERSION, C.PLUGIN_VERSION))
    workers = max(1, int(workers or C.DOWNLOAD_WORKERS))
    failures = []
    try:
        n = len(todo)
        for i, (name, label, _group, kind, spec) in enumerate(todo):
            lo, hi = 100.0 * i / n, 100.0 * (i + 1) / n

            shown = {"p": 0.0, "m": ""}

            def sub(p, m="", lo=lo, hi=hi, label=label, shown=shown):
                shown["p"], shown["m"] = p, m
                progress(lo + (hi - lo) * max(0.0, min(100.0, p)) / 100.0,
                         "{}: {}".format(label, m) if m else label)

            def beat(text, lo=lo, hi=hi, label=label, shown=shown):
                # v2.8.0: the bar does not move, but the message shows life
                progress(lo + (hi - lo) * max(0.0, min(100.0, shown["p"])) / 100.0,
                         "{}: {}{}".format(label, text, " - " + shown["m"] if shown["m"] else ""))
            ctx = _Ctx(writer, workers, log, sub, beat, refresh)
            names = [c[0] for c in spec["choices"]] if kind == "first_of" else [name]
            # (partial, or complete with gaps: fetched again - it may be complete now;
            # a failed re-fetch keeps what there is)
            if kind != "local_gpkg" and not refresh and any(writer.done(x) for x in names):
                log("[{}/{}] {}: already in the pack (kept - --refresh fetches it "
                    "again)".format(i + 1, n, label))
                continue
            log("[{}/{}] {}".format(i + 1, n, label))
            sub(0, "starting")
            t0 = time.time()
            try:
                got = _run_one(ctx, name, label, kind, spec, bbox)
                log("  done: {:,} features in {:.0f} s".format(got, time.time() - t0))
            except (PackError, BuildCancelled):
                raise
            except _NET_ERRORS as e:
                failures.append((label, str(e)))
                log("  FAILED: {}".format(e))
    finally:
        manifest = writer.manifest()
        writer.close(finalize=False)
    progress(99.0, "Writing the zip\u2026")
    manifest = _write_zip(db, out_zip, manifest, log)
    ok = [k for k, v in manifest["datasets"].items() if v.get("status") == "complete"]
    total = sum(int(v.get("count") or 0) for v in manifest["datasets"].values()
                if v.get("status") == "complete")
    log("")
    log("Pack written: {} ({:.1f} MB) - {} data sets, {:,} features".format(
        out_zip, os.path.getsize(out_zip) / 1048576.0, len(ok), total))
    bad = {k: v for k, v in manifest["datasets"].items()
           if v.get("status") not in ("complete", STATUS_PARTIAL, "not available")}
    for k, v in sorted(bad.items()):
        log("  NOT in the pack: {} - {}".format(k, v.get("note") or v.get("status")))
    for k, v in sorted(manifest["datasets"].items()):
        if v.get("status") == STATUS_PARTIAL or v.get("gaps"):
            log("  INCOMPLETE (used with a warning; run again to retry): {} - {}".format(
                k, v.get("note") or "features missing"))
        elif v.get("status") == "complete" and v.get("note"):
            log("  note: {} - {}".format(k, v["note"]))
    if failures or bad:
        log("Run the same command again later to retry what failed (finished data sets "
            "are kept).")
    progress(100.0, "Offline data pack written")
    manifest["_failures"] = failures
    return manifest


def _run_one(ctx, name, label, kind, spec, bbox):
    w = ctx.writer
    if kind == "first_of":
        errors = []
        for nm, url in spec["choices"]:
            try:
                n = _run_one(ctx, nm, label, "arcgis", {"url": url}, bbox)
            except BuildCancelled:
                raise
            except _NET_ERRORS as e:
                errors.append((nm, str(e)))
                ctx.log("  {} not available ({}) - trying the next".format(nm, e))
                continue
            for old, _e in errors:          # a newer year not published: not a failure
                w.con.execute("UPDATE datasets SET status='not available', note=? "
                              "WHERE name=?", ("not published - {} used".format(nm), old))
            w.con.commit()
            return n
        raise BuildError("; ".join("{}: {}".format(a, b) for a, b in errors))
    if kind == "arcgis" and spec.get("urls"):
        errors = []
        urls = spec["urls"]
        kept = None                          # count of an incomplete result already stored
        for k, url in enumerate(urls):
            try:
                n = _run_one(ctx, name, label, "arcgis", {"url": url}, bbox)
            except BuildCancelled:
                raise
            except _NET_ERRORS as e:
                errors.append(str(e))
                ctx.log("  {} failed ({}) - trying the mirror".format(url.split("/rest/")[0],
                                                                      e))
                continue
            # v2.6.2: features missing - the mirror may have them all (if it
            # fails, the incomplete result stays in the pack)
            if ctx.status != "complete" and k + 1 < len(urls):
                kept = n
                errors.append("incomplete")
                ctx.log("  {} incomplete - trying the mirror".format(url.split("/rest/")[0]))
                ctx.status, ctx.gaps = "complete", []
                continue
            return n
        if kept is not None:
            ctx.log("  the mirror failed too - the incomplete download is kept")
            return kept
        raise BuildError("; ".join(errors))
    if kind == "local_gpkg":
        # 2.18.66: a local file - read whole on every run (no chunks, nothing to resume)
        w.begin(name, label, os.path.abspath(spec["path"]), resume=False,
                signature=_signature(kind, spec, bbox))
        ctx.status, ctx.gaps = "complete", []
        try:
            note = fetch_local_gpkg(ctx, spec)
        except BaseException as e:
            w.fail(name, str(e) if isinstance(e, BuildError) else "interrupted")
            raise
        return w.finish(note, status="complete")
    url = spec.get("url") or (C.NSI_API_URL if kind == "nsi" else C.OVERPASS_URL)
    # v2.8.0: continue the chunks an interrupted run stored (not with --refresh)
    kept = w.begin(name, label, url, resume=not getattr(ctx, "refresh", False),
                   signature=_signature(kind, spec, bbox))
    if kept:
        ctx.log("  continuing an interrupted download: {} finished chunk(s) kept, {:,} "
                "features".format(kept, w.count))
        started = w.stage_started(name)              # BP.2: how old the kept chunks are
        if started:
            try:
                age_h = (time.time() - time.mktime(time.strptime(
                    started, "%Y-%m-%dT%H:%M:%S"))) / 3600.0
                ctx.log("  the kept chunks were started {} ({:.1f} h ago){}".format(
                    started, age_h, " - consider --refresh" if age_h > 24 * 7 else ""))
            except (ValueError, OverflowError):
                ctx.log("  the kept chunks were started {}".format(started))
    ctx.status, ctx.gaps = "complete", []
    ctx.tiled = None                         # 1.3: arcgis_tiled per-area report
    net = NetSession(label, ctx.workers, log=ctx.log, on_beat=getattr(ctx, "beat", None))
    token = _NET.set(net)
    try:
        if kind == "nsi":
            note = fetch_nsi(ctx, bbox)
        elif kind == "arcgis":
            note = fetch_arcgis(ctx, url, bbox, spec.get("fields", "*"),
                                spec.get("precision"))
        elif kind == "osm":
            note = fetch_osm(ctx, bbox, spec["query"], spec["parse"])
        elif kind == "arcgis_tiled":
            note = fetch_arcgis_tiled(ctx, url, bbox, spec)
        else:
            raise BuildError("unknown kind " + kind)
    except BuildCancelled:
        w.fail(name, "interrupted")
        raise
    except _NET_ERRORS as e:
        w.fail(name, str(e))
        raise
    except BaseException:
        w.fail(name, "interrupted")
        raise
    finally:
        _NET.reset(token)
    if net.pushbacks:
        ctx.log("  the service pushed back {} time(s) (HTTP 429 / 503 / 504 or timeouts)".format(
            net.pushbacks))
    return w.finish(note or "", status=ctx.status, gaps=ctx.gaps)


def _signature(kind, spec, bbox):
    """What must be equal for stored chunks of an interrupted run to be
    reused: the settings that change what a chunk holds."""
    if kind == "arcgis":
        return "arcgis|{}|{}".format(spec.get("fields", "*"), spec.get("precision"))
    if kind == "arcgis_tiled":
        f = spec.get("fill") or {}
        return "arcgis_tiled|{}|{}|{}|{}|{}|{}".format(
            spec.get("fields", "*"), spec.get("precision"), spec.get("tile_deg"),
            spec.get("tag"), f.get("url"), f.get("fields", "*"))
    if kind == "nsi":
        return "nsi|{}".format(NSI_TILE_DEG)
    if kind == "osm":
        try:
            q = spec["query"](tuple(bbox))
            return "osm|{}|{:08x}".format(OVERPASS_TILE_DEG,
                                          zlib.crc32(str(q).encode("utf-8")) & 0xffffffff)
        except Exception:
            return "osm|{}".format(OVERPASS_TILE_DEG)
    if kind == "local_gpkg":
        return "local_gpkg|{}|{}".format(spec.get("layer"), DF.file_date(spec["path"]))
    return kind


def _write_zip(db, out_zip, manifest, log):
    # a clean single file (no -wal beside it) goes in the zip
    w = PackWriter(db, tuple(manifest["bbox"]))
    manifest = w.manifest()
    leftovers = w.stage_leftovers() or w.has_chunks()
    w.close(finalize=True)
    manifest = dict(manifest)
    src = db
    if leftovers:
        # v2.8.0: the chunks of interrupted / failed data sets stay in the work file
        # (the next run continues with them) but do not go into the zip
        src = write_clean_copy(db, db + ".zipcopy")
    tmp = out_zip + ".part"
    readme = ("ND Dam Hazard Classification Helper - offline data pack\n\n"
              "Built {} for {} (lon / lat {}).\n\n"
              "On the offline computer: plugin > Data & Output tab > 7. Offline data pack > "
              "Browse..., pick this zip (it is unpacked once, next to the zip or in your "
              "home folder), and tick 'Offline only'.\n\nData sets (name: features, fetched "
              "on):\n{}\n").format(
        manifest.get("created"), manifest.get("area"), manifest.get("bbox"),
        "\n".join("  {}: {} {}{}".format(
            k, v.get("count"), v.get("fetched") or "",
            " - INCOMPLETE ({})".format(v.get("note")) if (
                v.get("status") == STATUS_PARTIAL or v.get("gaps")) else
            "" if v.get("status") == "complete" else
            " - not available ({})".format(v.get("note")) if v.get("status") == "not available"
            else " - NOT INCLUDED ({})".format(v.get("note")))
            for k, v in sorted(manifest["datasets"].items())))
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        z.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
        z.writestr("README.txt", readme)
        z.write(src, DB_NAME)
    if src != db:
        try:
            os.remove(src)
        except OSError:
            pass
    os.replace(tmp, out_zip)
    with open(os.path.join(os.path.dirname(db), MANIFEST_NAME), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


# -------------------------------------------------------------------- CLI --
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Build the ND Dam Hazard Helper offline data pack (North Dakota).")
    ap.add_argument("--out", default="nd_offline_pack.zip", help="zip to write")
    ap.add_argument("--pad", type=float, default=DEFAULT_PAD_DEG,
                    help="margin around North Dakota's bounding box, degrees (default "
                         "%(default)s; 0 = the box itself)")
    ap.add_argument("--only", help="data sets or groups to fetch, comma separated")
    ap.add_argument("--skip", help="data sets or groups to leave out, comma separated")
    ap.add_argument("--refresh", action="store_true",
                    help="fetch again data sets that are already in the pack (and do not "
                         "continue interrupted downloads: everything is read again)")
    ap.add_argument("--workers", type=int, default=None,
                    help="requests at once (default {})".format(C.DOWNLOAD_WORKERS))
    ap.add_argument("--sources", help="the plugin's data-source file (JSON) to use its "
                                      "addresses")
    ap.add_argument("--download-dwr", metavar="FOLDER",
                    help="download the four ND DWR layers (dams, dam lines, "
                         "dugouts, dugout polygons) as shapefile zips from "
                         "mapservice.dwr.nd.gov into FOLDER, then exit")
    ap.add_argument("--dwr-bbox", metavar="W,S,E,N",
                    help="2.19.2: with --download-dwr: only this lon / lat box, written "
                         "with = (--dwr-bbox=-100.85,46.75,-100.70,46.85; default: the "
                         "full data sets)")
    ap.add_argument("--dwr-format", choices=sorted(DWR_FORMATS), default="shp",
                    help="2.19.2: with --download-dwr: shp (shapefile) or spat "
                         "(SpatiaLite, full field names)")
    ap.add_argument("--dwr-folder", metavar="PATH",
                    help="2.18.66: a folder with the ND DWR GeoPackages (DWR_Dams.gpkg, "
                         "DWR_Dam_Lines.gpkg, DWR_Unknown_Structures.gpkg, DWR_Dugouts.gpkg, "
                         "DWR_Dugouts_Polygons.gpkg): adds them as the data sets DWR_dams, "
                         "DWR_dam_lines, DWR_unknown_structures, DWR_dugouts, "
                         "DWR_dugout_polygons (group dwr; all attributes, reprojected to "
                         "lon / lat). --only dwr builds just these.")
    ap.add_argument("--list", action="store_true", help="list the data sets and stop")
    a = ap.parse_args(argv)
    if a.download_dwr:
        bbox = None
        if a.dwr_bbox:
            try:
                bbox = tuple(float(v) for v in a.dwr_bbox.split(","))
                assert len(bbox) == 4 and bbox[0] < bbox[2] and bbox[1] < bbox[3]
            except (ValueError, AssertionError):
                print("ERROR: --dwr-bbox needs west,south,east,north in degrees")
                return 1
        got = download_dwr_shapefiles(a.download_dwr, log=print, bbox=bbox, fmt=a.dwr_format)
        print("{} of {} DWR layer(s) downloaded".format(len(got), len(DWR_DOWNLOADS)))
        return 0 if len(got) == len(DWR_DOWNLOADS) else 1
    if a.list:
        try:
            table = datasets(a.dwr_folder)
        except BuildError as e:
            print("ERROR: {}".format(e))
            return 1
        for name, label, group, kind, spec in table:
            src = spec.get("path") and "{} | {}".format(spec["path"], spec["layer"]) or \
                spec.get("url") or ", ".join(spec.get("urls") or []) or \
                ", ".join(u for _n, u in spec.get("choices") or []) or \
                (C.NSI_API_URL if kind == "nsi" else C.OVERPASS_URL)
            print("{:<32} group {:<10} {}\n{:<32} {}".format(name, group, label, "", src))
        return 0

    last = [0.0]
    tty = sys.stdout.isatty()

    def progress(p, m):
        if tty and time.time() - last[0] > 2.0:
            last[0] = time.time()
            sys.stdout.write("\r  {:5.1f}%  {:<90.90}".format(p, m))
            sys.stdout.flush()

    def log(m):
        if tty:
            sys.stdout.write("\r" + " " * 100 + "\r")
        print(m)
        sys.stdout.flush()
    try:
        man = build(a.out, a.pad, a.only, a.skip, a.workers, a.refresh, a.sources,
                    progress, log, dwr_folder=a.dwr_folder)
    except (KeyboardInterrupt, BuildCancelled):
        print("\nStopped - the finished data sets are kept; run the same command to go on.")
        return 2
    except (BuildError, PackError) as e:
        print("\nERROR: {}".format(e))
        return 1
    bad = [k for k, v in man["datasets"].items()
           if v.get("status") not in ("complete", "not available") or v.get("gaps")]
    return 1 if (man.get("_failures") or bad) else 0


if __name__ == "__main__":
    sys.exit(main())
