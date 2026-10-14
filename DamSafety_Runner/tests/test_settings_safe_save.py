"""damsuite/settings.py saves safely for a file the plugins share: merge with what is on disk,
never overwrite an unreadable file, no rewrite when nothing changed, atomic replace + .bak, a
lock for two writers at once. The three damsuite copies (and suite_settings.py) are identical."""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

from nd_breach_runner.damsuite import settings as DS
from nd_breach_runner.tests.registry import test as register  # "test" would be collected by pytest

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(PLUGIN)


def _tmp():
    return tempfile.mkdtemp(prefix="ds_save_")


def _file(d, values, ext=".csv"):
    p = os.path.join(d, "damsuite_settings" + ext)
    s = DS.Settings(values)
    s.save(p)
    return p


@register
def settings_copies_are_identical():
    for name in ("damsuite/settings.py", "suite_settings.py"):
        h = {hashlib.sha256(open(os.path.join(REPO, p, name), "rb").read()).hexdigest()
             for p in ("DamSafety_Runner", "DamSafety_Screening", "DamSafety_Hazard")}
        assert len(h) == 1, name


@register
def settings_save_merges_another_plugins_rows_written_meanwhile():
    d = _tmp()
    try:
        for ext in (".csv", ".xlsx"):
            p = _file(d, {("ndbreach", "rundam", "CELL"): 40.0}, ext)
            runner = DS.Settings.load(p)
            screening = DS.Settings.load(p)
            screening.set("ndscreen", "t9", "THR", 2.0)
            screening.save()
            runner.set("ndbreach", "rundam", "MANNING", 0.05)       # Runner's own, stale view
            runner.save()
            v = DS.Settings.load(p).values
            assert v[("ndscreen", "t9", "THR")] == 2.0, v         # not clobbered
            assert v[("ndbreach", "rundam", "MANNING")] == 0.05 and v[("ndbreach", "rundam", "CELL")] == 40.0
            # a value cleared by one plugin is removed, others stay
            runner.set("ndbreach", "rundam", "CELL", "")
            runner.save()
            v = DS.Settings.load(p).values
            assert ("ndbreach", "rundam", "CELL") not in v and ("ndscreen", "t9", "THR") in v
    finally:
        shutil.rmtree(d)


@register
def settings_fresh_object_never_clobbers_an_existing_file():
    """The windows start with an empty Settings(); an autosave before (or without) a load used
    to write that empty table over the engineer's file."""
    d = _tmp()
    try:
        p = _file(d, {("ndbreach", "rundam", "CELL"): 40.0, ("ndhazard", "x", "Y"): "a"})
        before = open(p, "rb").read()
        mtime = os.path.getmtime(p)
        fresh = DS.Settings(path=p)
        fresh.save()                                           # autosave on open: nothing changed
        assert open(p, "rb").read() == before and os.path.getmtime(p) == mtime
        assert not os.path.exists(p + ".bak")
        assert fresh.values == DS.Settings.load(p).values      # it now holds the file's rows
        fresh2 = DS.Settings(path=p)
        fresh2.set("ndscreen", "t1", "K", 3.0)
        fresh2.save()
        v = DS.Settings.load(p).values
        assert v[("ndbreach", "rundam", "CELL")] == 40.0 and v[("ndscreen", "t1", "K")] == 3.0
        assert os.path.isfile(p + ".bak")
        assert DS.Settings.load(p + ".bak").values[("ndbreach", "rundam", "CELL")] == 40.0   # the previous file
    finally:
        shutil.rmtree(d)


@register
def settings_unreadable_file_is_not_overwritten_unless_replaced():
    d = _tmp()
    try:
        p = os.path.join(d, "damsuite_settings.xlsx")
        with open(p, "wb") as fh:
            fh.write(b"not a zip")
        s = DS.Settings({("ndbreach", "rundam", "CELL"): 10.0}, path=p)
        try:
            s.save()
        except DS.SettingsError as e:
            assert "cannot be read" in str(e)
        else:
            raise AssertionError("a damaged file was overwritten")
        assert open(p, "rb").read() == b"not a zip"
        s.save(replace=True)                                   # an explicit "save as" over it
        assert DS.Settings.load(p).values[("ndbreach", "rundam", "CELL")] == 10.0
        assert open(p + ".bak", "rb").read() == b"not a zip"   # the old file is kept
    finally:
        shutil.rmtree(d)


@register
def settings_save_is_atomic_and_leaves_no_temp_or_lock():
    d = _tmp()
    try:
        p = _file(d, {("a", "b", "c"): 1.0})
        real = DS.os.replace
        DS.os.replace = lambda a, b: (_ for _ in ()).throw(OSError("disk full"))
        try:
            s = DS.Settings.load(p)
            s.set("a", "b", "c", 2.0)
            try:
                s.save()
            except DS.SettingsError:
                pass
            else:
                raise AssertionError("no error")
        finally:
            DS.os.replace = real
        assert DS.Settings.load(p).values[("a", "b", "c")] == 1.0         # untouched
        left = [f for f in os.listdir(d) if f not in ("damsuite_settings.csv", "damsuite_settings.csv.bak")]
        assert left == [], left
        lock = p + ".lock"
        with open(lock, "w") as fh:                            # a crashed writer's lock
            fh.write("1")
        old = os.path.getmtime(lock) - 120
        os.utime(lock, (old, old))
        s.save()
        assert not os.path.exists(lock) and DS.Settings.load(p).values[("a", "b", "c")] == 2.0
    finally:
        shutil.rmtree(d)


_WRITER = r"""
import sys, importlib.util, os
spec = importlib.util.spec_from_file_location("dspkg", os.path.join(sys.argv[1], "__init__.py"),
                                              submodule_search_locations=[sys.argv[1]])
pkg = importlib.util.module_from_spec(spec); sys.modules["dspkg"] = pkg
import importlib
DS = importlib.import_module("dspkg.settings")
path, plugin, n = sys.argv[2], sys.argv[3], int(sys.argv[4])
s = DS.Settings.load(path)
for i in range(n):
    s.set(plugin, "tool", "K{}".format(i), float(i))
    s.save()
"""


@register
def settings_two_plugins_saving_at_once_lose_nothing():
    d = _tmp()
    try:
        p = _file(d, {("ndbreach", "rundam", "CELL"): 40.0})
        script = os.path.join(d, "writer.py")
        with open(script, "w") as fh:
            fh.write(_WRITER)
        procs = [subprocess.Popen([sys.executable, script, os.path.join(REPO, plugin_dir, "damsuite"),
                                   p, key, "25"])
                 for plugin_dir, key in (("DamSafety_Runner", "ndbreach"), ("DamSafety_Screening", "ndscreen"))]
        assert [q.wait(120) for q in procs] == [0, 0]
        v = DS.Settings.load(p).values
        for key in ("ndbreach", "ndscreen"):
            for i in range(25):
                assert v.get((key, "tool", "K{}".format(i))) == float(i), (key, i)
        assert v[("ndbreach", "rundam", "CELL")] == 40.0
        assert not os.path.exists(p + ".lock")
    finally:
        shutil.rmtree(d)
