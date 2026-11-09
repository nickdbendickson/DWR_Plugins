"""Where tests register themselves, so test modules other than run_tests.py can add to the
suite: `from nd_breach_runner.tests.registry import test, model_test`.

A test that cannot run in this Python raises Skip (through need_osgeo() for the GDAL-backed
ones); run_tests.py counts it as skipped, not failed, so the suite also gives a clean verdict on
a plain python + numpy + scipy (the fast-checks CI job, audit CI matrix 2026-11-07). Where GDAL
is present the test runs in full: nothing is weakened."""
TESTS = []
MODEL_TESTS = []


class Skip(Exception):
    """Raised inside a test that cannot run here (a missing optional dependency)."""


def need_osgeo():
    """Skip the calling test when GDAL's Python bindings (osgeo) are not importable."""
    try:
        from osgeo import gdal  # noqa: F401
    except ImportError:
        raise Skip("no GDAL (osgeo) in this Python")


def test(fn):
    TESTS.append(fn)
    return fn


def model_test(fn):
    MODEL_TESTS.append(fn)
    return fn


# pytest would collect the imported `test` decorator (and any test_* helper that takes
# arguments) as a test; this suite runs through run_tests.py, so tell pytest to leave them.
test.__test__ = False
model_test.__test__ = False
