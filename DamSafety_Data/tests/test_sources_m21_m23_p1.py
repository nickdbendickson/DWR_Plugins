"""Regression tests for DamSafety_Data/core.py (findings M21, M22, M23, P1).

Runs without QGIS/GDAL: core.py imports only the standard library at module load time
(the `from . import nsi_merge` calls live inside NsiApiSource, which these tests never build),
so the module is loaded straight from its file path.

What each test pins:
  M21 - address_points (ArcGIS) and overture (Overture) now carry a min_check_interval_hours so
        an unchanged service is skipped instead of re-downloaded on every run; ArcGIS also
        reports a cheap remote_version (last-edit date / feature count) that fetch() reuses, so a
        later check detects "current" without paging the whole service.
  M22 - both sources expose layer_globs matching their output filenames, so "Add North Dakota
        Layers to Project" can find them.
  M23 - the update lock is pid-aware: a lock left by a dead process is cleared (not kept for six
        hours), a lock held by a live process is refused.
  P1  - ArcGIS paging stops instead of looping/growing unbounded when the FeatureServer ignores
        resultOffset while setting exceededTransferLimit.

Run:  python3 DamSafety_Data/tests/test_sources_m21_m23_p1.py
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)


def _load_core():
    spec = importlib.util.spec_from_file_location(
        "damsafety_data_core_under_test", os.path.join(PLUGIN, "core.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


core = _load_core()

_TESTS = []


def test(fn):
    _TESTS.append(fn)
    return fn


def _feedback():
    lines = []
    return core.Feedback(log=lines.append), lines


# --------------------------------------------------------------------------- fake HTTP clients


class _PagingHttp:
    """A well-behaved FeatureServer: honours resultOffset, returns `total` features in pages."""

    def __init__(self, total=5, page_size=3, last_edit=100):
        self.total = total
        self.page_size = page_size
        self.last_edit = last_edit
        self.page_calls = 0

    def get_json(self, url, params=None):
        params = params or {}
        if params.get("f") == "json":
            if params.get("returnCountOnly"):
                return {"count": self.total}
            return {"maxRecordCount": self.page_size,
                    "editingInfo": {"lastEditDate": self.last_edit}}
        self.page_calls += 1
        off = int(params.get("resultOffset", 0))
        feats = [{"id": i, "type": "Feature", "properties": {"fid": i}}
                 for i in range(off, min(off + self.page_size, self.total))]
        exceeded = (off + self.page_size) < self.total
        return {"features": feats, "exceededTransferLimit": exceeded}


class _IgnoreOffsetHttp:
    """A broken FeatureServer: ignores resultOffset and always flags exceededTransferLimit, so a
    naive pager loops forever and the NDJSON grows without bound."""

    def __init__(self, n=3):
        self.n = n
        self.page_calls = 0

    def get_json(self, url, params=None):
        params = params or {}
        if params.get("f") == "json":
            if params.get("returnCountOnly"):
                return {"count": self.n}
            return {"maxRecordCount": 2000, "editingInfo": {"lastEditDate": 1}}
        self.page_calls += 1
        return {"features": [{"id": i, "type": "Feature", "properties": {}}
                             for i in range(self.n)],
                "exceededTransferLimit": True}


class _OfflineHttp:
    def get_json(self, url, params=None):
        raise core.NetworkError("the service is unreachable")


def _arcgis(**kw):
    return core.ArcGISFeatureSource(
        "address_points", "ND Address Points",
        "https://example.test/FeatureServer/0", "address_points",
        layer_name="address_points", **kw)


# --------------------------------------------------------------------------- M21 / M22


@test
def test_sources_have_check_interval_and_layer_globs():
    ov = core.OvertureSource("overture", "Overture", "overture_buildings")
    assert ov.min_check_interval_hours > 0, "overture must have a check interval (M21)"
    assert ov.layer_globs, "overture must set layer_globs (M22)"
    assert "overture_buildings.gpkg" in ov.layer_globs
    assert "overture_buildings.geojson" in ov.layer_globs

    ap = _arcgis()
    assert ap.min_check_interval_hours > 0, "address_points must have a check interval (M21)"
    assert ap.layer_globs, "address_points must set layer_globs (M22)"
    assert ap.layer_globs == ("address_points.gpkg", "address_points.ndjson")


@test
def test_arcgis_remote_version_is_a_cheap_stable_signal():
    fb, _ = _feedback()
    http = _PagingHttp(total=5, last_edit=100)
    v1 = _arcgis().remote_version(http, fb)
    assert v1 == "arcgis:edit:100|count:5", v1
    # no features were paged just to get the version
    assert http.page_calls == 0
    # an unchanged service yields the same signal; a changed one differs
    assert _arcgis().remote_version(_PagingHttp(total=5, last_edit=100), fb) == v1
    assert _arcgis().remote_version(_PagingHttp(total=6, last_edit=100), fb) != v1
    assert _arcgis().remote_version(_PagingHttp(total=5, last_edit=200), fb) != v1


@test
def test_arcgis_remote_version_propagates_network_error():
    fb, _ = _feedback()
    try:
        _arcgis().remote_version(_OfflineHttp(), fb)
    except core.NetworkError:
        pass
    else:
        raise AssertionError("a network failure must propagate so the source is reported offline")


@test
def test_arcgis_fetch_returns_the_remote_version_so_a_later_check_is_current():
    fb, lines = _feedback()
    with tempfile.TemporaryDirectory() as d:
        staging = os.path.join(d, "staging")
        http = _PagingHttp(total=5, last_edit=100)
        src = _arcgis()
        remote = src.remote_version(http, fb)
        version = src.fetch(http, staging, fb, remote)
        assert version == remote, (version, remote)
        # all five features were paged and written (as GeoPackage when ogr2ogr is present,
        # else kept as NDJSON)
        assert any("wrote 5 features" in ln for ln in lines), lines
        assert (os.path.exists(os.path.join(staging, "address_points.gpkg"))
                or os.path.exists(os.path.join(staging, "address_points.ndjson")))


@test
def test_updater_skips_within_interval_and_reports_current_after():
    with tempfile.TemporaryDirectory() as d:
        clock = [1_000_000.0]
        src = _arcgis(check_interval_hours=24)
        http = _PagingHttp(total=5, last_edit=100)
        up = core.Updater(d, [src], http, now=lambda: clock[0])

        r1 = up.check_and_update(core.Feedback())[0]
        assert r1.status == "updated", r1
        first_pages = http.page_calls
        assert first_pages > 0

        # second run, same day: skipped outright, nothing fetched
        r2 = up.check_and_update(core.Feedback())[0]
        assert r2.status == "skipped", r2
        assert http.page_calls == first_pages  # no extra paging

        # a day later, forced past the interval: the cheap signal is unchanged -> current,
        # still without paging the service again
        clock[0] += 24 * 3600 + 1
        r3 = up.check_and_update(core.Feedback())[0]
        assert r3.status == "current", r3
        assert http.page_calls == first_pages


# --------------------------------------------------------------------------- P1 (paging guard)


@test
def test_arcgis_fetch_stops_when_the_server_ignores_resultoffset():
    fb, _ = _feedback()
    with tempfile.TemporaryDirectory() as d:
        staging = os.path.join(d, "staging")
        http = _IgnoreOffsetHttp(n=3)
        src = _arcgis()
        try:
            src.fetch(http, staging, fb, "arcgis:edit:1|count:3")
        except core.UpdateError as e:
            assert "resultOffset" in str(e), str(e)
        else:
            raise AssertionError("a server ignoring resultOffset must not loop forever")
        # it bailed out after only a couple of pages, not thousands
        assert http.page_calls <= 3, http.page_calls
        # and it did not write the repeated features over and over
        with open(os.path.join(staging, "address_points.ndjson"), encoding="utf-8") as f:
            assert sum(1 for _ in f) == 3


@test
def test_normal_paging_still_completes():
    fb, lines = _feedback()
    with tempfile.TemporaryDirectory() as d:
        staging = os.path.join(d, "staging")
        http = _PagingHttp(total=7, page_size=3)
        core.ArcGISFeatureSource("ap", "AP", "https://x.test/FeatureServer/0", "ap",
                                 layer_name="ap").fetch(http, staging, fb, None)
        assert any("wrote 7 features" in ln for ln in lines), lines
        assert (os.path.exists(os.path.join(staging, "ap.gpkg"))
                or os.path.exists(os.path.join(staging, "ap.ndjson")))


# --------------------------------------------------------------------------- M23 (pid-aware lock)


def _dead_pid():
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


@test
def test_pid_alive():
    assert core.pid_alive(os.getpid()) is True
    assert core.pid_alive(_dead_pid()) is False
    assert core.pid_alive(None) is False
    assert core.pid_alive("not a pid") is False


@test
def test_read_lock_reads_pid_and_age():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, core.LOCK_NAME)
        assert core.read_lock(path) == {}
        with open(path, "w") as f:
            f.write("4242")
        info = core.read_lock(path)
        assert info["pid"] == 4242 and info["age"] < 60
        with open(path, "w") as f:
            f.write("")                       # a lock from the old mtime-only code held no pid
        assert core.read_lock(path)["pid"] is None


@test
def test_lock_decision_logic():
    assert core.lock_decision({})[0] == "free"
    assert core.lock_decision({"pid": 4242, "age": 10.0}, alive=lambda p: False)[0] == "clear"
    assert core.lock_decision({"pid": 4242, "age": 10.0}, alive=lambda p: True)[0] == "refuse"
    # a very old lock is cleared even if that pid now belongs to something else
    assert core.lock_decision({"pid": 4242, "age": 7 * 3600.0}, alive=lambda p: True)[0] == "clear"
    # a pid-less lock: refused while fresh, cleared once past the stale window
    assert core.lock_decision({"pid": None, "age": 5.0})[0] == "refuse"
    assert core.lock_decision({"pid": None, "age": 7 * 3600.0})[0] == "clear"
    # M23 fix: a FRESH lock from THIS process is a live concurrent update -> refuse (not stolen);
    # an own-pid lock is cleared only once it is past the stale window.
    assert core.lock_decision({"pid": os.getpid(), "age": 1.0}, alive=lambda p: True)[0] == "refuse"
    assert core.lock_decision({"pid": os.getpid(), "age": 7 * 3600.0}, alive=lambda p: True)[0] == "clear"


@test
def test_updater_clears_a_stale_lock_and_says_so_once():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, core.LOCK_NAME)
        with open(path, "w") as f:
            f.write(str(_dead_pid()))
        said = []
        up = core.Updater(d, [], None)
        results = up.check_and_update(core.Feedback(log=said.append))
        assert results == []
        assert sum(1 for line in said if "Cleared the left-over lock" in line) == 1
        assert not os.path.exists(path)       # the run removed its own lock afterward


@test
def test_updater_refuses_while_a_live_run_holds_the_lock():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, core.LOCK_NAME)
            with open(path, "w") as f:
                f.write(str(child.pid))       # a live, foreign process holds the lock
            up = core.Updater(d, [], None)
            try:
                up.check_and_update(core.Feedback())
            except core.UpdateError as e:
                assert "already running" in str(e)
                assert core.LOCK_NAME in str(e)   # names the file the user can delete
            else:
                raise AssertionError("a live lock must stop the run")
            assert os.path.exists(path)           # the other run's lock is left alone
    finally:
        child.terminate()
        child.wait()


def main():
    failed = 0
    for fn in _TESTS:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {fn.__name__}: {e.__class__.__name__}: {e}")
        else:
            print(f"ok   {fn.__name__}")
    print(f"\n{len(_TESTS) - failed}/{len(_TESTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
