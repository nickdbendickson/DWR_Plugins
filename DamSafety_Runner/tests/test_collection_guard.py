"""run_tests must not exit 0 when it collected nothing (repo audit 2026-11-23).

The guard is the launcher, not a test that an empty collection would skip. These
checks call the same functions main() calls. They do not run the suite.
"""
from nd_breach_runner.tests.registry import test
from nd_breach_runner.tests.run_tests import (
    empty_collection_status, module_count_lines, silent_test_modules,
)


def _fn(mod):
    def f():
        pass
    f.__module__ = mod
    return f


@test
def collection_guard_empty_todo_is_fatal():
    code, msg = empty_collection_status([])
    assert code == 1 and msg.startswith("FATAL: no tests collected"), (code, msg)
    assert empty_collection_status([object()]) == (None, None)


@test
def collection_guard_uses_the_package_prefix():
    on = ["test_runlock", "test_ghost"]
    registered = ["nd_breach_runner.tests.test_runlock"]
    assert silent_test_modules(on, registered) == ["test_ghost"]
    # `tests.` alone, the first draft, flags the healthy module too
    assert silent_test_modules(on, registered, prefix="tests.") == ["test_ghost", "test_runlock"]


@test
def collection_guard_a_model_only_module_is_not_silent():
    assert silent_test_modules(
        ["test_models"], ["nd_breach_runner.tests.test_models"]) == []


@test
def collection_guard_count_table_shows_a_zero_and_unrun_model_tests():
    lines = module_count_lines(
        [_fn("nd_breach_runner.tests.test_a"), _fn("__main__")],
        [_fn("nd_breach_runner.tests.test_metal")],
        ["test_ghost"])
    text = "\n".join(lines)
    assert "test_a" in text and "run_tests" in text, text
    assert "test_ghost" in text and " 0" in text, text
    assert "1 model tests not run (pass --models)" in text, text
