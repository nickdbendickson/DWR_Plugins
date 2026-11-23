"""core/atomicio.write_text (Runner 3.74.1, ported from DamIT #150): whole file or nothing,
temp unique per writer, no temp left."""
import os
import tempfile
import threading
from unittest import mock

from nd_breach_runner.core import atomicio
from nd_breach_runner.tests.registry import test


@test
def atomicio_writes_and_replaces():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "sub", "a.json")
        atomicio.write_text(p, "one\n")
        atomicio.write_text(p, "two\n")
        with open(p, encoding="utf-8") as fh:
            assert fh.read() == "two\n"
        assert os.listdir(os.path.dirname(p)) == ["a.json"]


@test
def atomicio_failed_write_keeps_old_file_and_no_temp():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "a.json")
        atomicio.write_text(p, "good\n")
        with mock.patch("nd_breach_runner.core.atomicio.os.replace",
                        side_effect=OSError("disk gone")):
            try:
                atomicio.write_text(p, "half")
            except OSError:
                pass
            else:
                assert False, "expected OSError"
        with open(p, encoding="utf-8") as fh:
            assert fh.read() == "good\n"
        assert os.listdir(d) == ["a.json"]


@test
def atomicio_temp_name_unique_per_writer():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "a.json")
        seen = []
        real_open = open

        def spy(path, *a, **k):
            seen.append(path)
            return real_open(path, *a, **k)

        with mock.patch("builtins.open", side_effect=spy):
            atomicio.write_text(p, "x")
        assert seen == ["{}.{}.{}.tmp".format(p, os.getpid(), threading.get_ident())]
