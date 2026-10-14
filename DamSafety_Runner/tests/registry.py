"""Where tests register themselves, so test modules other than run_tests.py can add to the
suite: `from nd_breach_runner.tests.registry import test, model_test`."""
TESTS = []
MODEL_TESTS = []


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
