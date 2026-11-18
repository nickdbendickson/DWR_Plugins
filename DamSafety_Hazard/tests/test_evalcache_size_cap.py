"""DamSafety Hazard evaluation cache (security audit 2026-11-18): outputs/QA/_evaluation_cache.bin
is inflated with a size cap (evalcache.MAX_STATE_BYTES), so a crafted file in an outputs folder
someone hands the user cannot run QGIS out of memory. A state past the cap is a cache miss with a
reason, never an exception; save() does not write a state load() would refuse.

evalcache.py is loaded as a standalone file (importlib.spec_from_file_location, no package), the
way that broke Hazard's test_pack_convert_m4.py once: it must still find damsuite.zipsafe.
QGIS-free; needs numpy.

    python3 DamSafety_Hazard/tests/test_evalcache_size_cap.py
"""
import importlib.util
import io
import json
import os
import pickle
import sys
import tempfile
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "evalcache.py")


def _load():
    spec = importlib.util.spec_from_file_location("evalcache_standalone", SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


EC = _load()
KEY, VER = "k1", "9.9.9"


class _Tmp:
    """A temp outputs folder; evalcache.cache_path (which needs the package) points into it."""

    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.td.name, "QA", EC.FILE_NAME)
        self._orig = EC.cache_path
        EC.cache_path = lambda out_folder: self.path
        return self

    def __exit__(self, *a):
        EC.cache_path = self._orig
        self.td.cleanup()


class _Cap:
    def __init__(self, n):
        self.n = n

    def __enter__(self):
        self._orig = EC.MAX_STATE_BYTES
        EC.MAX_STATE_BYTES = self.n

    def __exit__(self, *a):
        EC.MAX_STATE_BYTES = self._orig


def _write_raw(path, blob, key=KEY, version=VER):
    head = json.dumps({"format": EC.FORMAT, "key": key, "version": version, "when": "t",
                       "saved_at": time.time(), "size": len(blob), "note": ""}).encode("utf-8")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(EC.MAGIC + head + b"\n" + blob)


def _pickled(state):
    buf = io.BytesIO()
    pickle.dump({"format": EC.FORMAT, "state": state}, buf, protocol=4)
    return buf.getvalue()


def test_cap_is_sane():
    assert EC.MAX_STATE_BYTES == 1024 ** 3
    assert issubclass(EC.TooLarge, ValueError)


def test_standalone_load_finds_zipsafe():
    zs = EC._zipsafe()
    assert zs is not None and hasattr(zs, "inflate"), "damsuite.zipsafe not found standalone"


def test_round_trip_unchanged():
    import numpy as np
    state = {"structs": [{"id": i, "depth": float(i) / 3} for i in range(500)],
             "arr": np.arange(1000, dtype="float64"), "names": {"a", "b"}}
    with _Tmp() as t:
        path, size = EC.save("out", KEY, state, "when", VER)
        assert path == t.path and size > 0, size
        got, when, why = EC.load("out", KEY, VER)
        assert why is None, why
        assert when == "when"
        assert got["structs"] == state["structs"] and got["names"] == state["names"]
        assert (got["arr"] == state["arr"]).all()


def test_bomb_is_a_cache_miss_not_a_crash():
    raw = _pickled({"blob": b"\0" * (8 * 1024 ** 2)})        # ~8 MiB that packs to a few KB
    blob = zlib.compress(raw, 9)
    assert len(blob) < 64 * 1024
    for use_zipsafe in (True, False):
        orig = EC._zipsafe
        if not use_zipsafe:
            EC._zipsafe = lambda: None                     # an older Core: the local bounded inflate
        try:
            with _Tmp() as t, _Cap(1024 ** 2):
                _write_raw(t.path, blob)
                state, when, why = EC.load("out", KEY, VER)
                assert state is None and when is None
                assert why and why.startswith("too large"), why
        finally:
            EC._zipsafe = orig


def test_inflate_cap_both_paths():
    blob = zlib.compress(b"x" * 4096, 9)
    assert EC._inflate(blob, 4096) == b"x" * 4096
    try:
        EC._inflate(blob, 4095)
    except EC.TooLarge:
        pass
    else:
        raise AssertionError("past the cap must raise TooLarge")
    orig = EC._zipsafe
    EC._zipsafe = lambda: None
    try:
        assert EC._inflate(blob, 4096) == b"x" * 4096
        try:
            EC._inflate(blob, 4095)
        except EC.TooLarge:
            pass
        else:
            raise AssertionError("local inflate past the cap must raise TooLarge")
    finally:
        EC._zipsafe = orig


def test_under_cap_still_loads():
    with _Tmp() as t, _Cap(1024 ** 2):
        state = {"blob": b"\0" * 1000}
        _write_raw(t.path, zlib.compress(_pickled(state), 1))
        got, _, why = EC.load("out", KEY, VER)
        assert why is None, why
        assert got == state


def test_save_refuses_state_over_cap():
    with _Tmp() as t, _Cap(1024 ** 2):
        path, why = EC.save("out", KEY, {"blob": b"\0" * (2 * 1024 ** 2)}, "when", VER)
        assert path is None and "over" in why, why
        assert not os.path.exists(t.path)


def test_corrupt_blob_is_unreadable_not_a_crash():
    with _Tmp() as t:
        _write_raw(t.path, b"not zlib at all")
        state, _, why = EC.load("out", KEY, VER)
        assert state is None and why.startswith("unreadable"), why


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print("{} tests passed".format(n))
    sys.exit(0)
