"""The 'folder is non-empty' helpers must close their os.scandir iterator: an unclosed one
raises ResourceWarning (and holds a directory handle open on Windows)."""
import gc, os, sys, tempfile, warnings

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "qgis_mock"))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from nd_dam_screening.core import workflow, selftest  # noqa: E402


def _no_resource_warning(fn, *args):
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        out = fn(*args)
        gc.collect()
    leaks = [x for x in w if issubclass(x.category, ResourceWarning)]
    assert not leaks, [str(x.message) for x in leaks]
    return out


def test_nonempty_dir_closes_scandir():
    d = tempfile.mkdtemp()
    assert _no_resource_warning(workflow._nonempty_dir, d) is False
    for i in range(3):
        open(os.path.join(d, "f%d" % i), "w").close()
    assert _no_resource_warning(workflow._nonempty_dir, d) is True
    assert _no_resource_warning(selftest._nonempty_folder, d) is True
    assert _no_resource_warning(workflow._nonempty_dir, os.path.join(d, "missing")) is False
    assert _no_resource_warning(workflow._nonempty_dir, os.path.join(d, "f0")) is False


if __name__ == "__main__":
    test_nonempty_dir_closes_scandir()
    print("ok")
