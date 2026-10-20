#!/usr/bin/env python3
"""Fast, QGIS-free merge gate. Runs the checks that need no QGIS/GDAL install and are fully
deterministic, so they can gate every PR cheaply:

  1. damsuite drift   — every vendored copy matches shared/damsuite and its FILES.json; and the
                        committed shared pin (tools/damsuite_pin.json) still matches the master
  2. version sanity   — each plugin metadata.txt has a well-formed version=, and the shared
                        damsuite SUITE_VERSION is identical across every copy that ships it
  3. compile          — py_compile every .py in the suite (catches a syntax error from a bad
                        merge across all plugins in seconds, without importing anything)
  4. golden baseline  — the built-in flood engine still reproduces the frozen regulatory numbers
                        (needs numpy, not scipy; skipped with a notice if numpy is absent)

Exit non-zero on any failure. The heavier per-plugin suites (real QGIS / scipy / real engines)
run in a separate CI job that provisions QGIS; this job is the always-on hard gate.

    python3 tools/ci_checks.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS = ("DamSafety_Hazard", "DamSafety_Runner", "DamSafety_Screening",
           "DamSafety_Data", "DamSafety_Setup", "DamSafety_PMP")

# plugin-root files copied verbatim into more than one plugin (not shared via damsuite): they must
# stay byte-identical so the two tools apply the same rule. {filename: (plugins that carry it)}.
IDENTICAL_ACROSS_PLUGINS = {
    "crest_rule.py": ("DamSafety_Runner", "DamSafety_Screening"),  # one top-of-dam number for both
}


def _fail(problems, msg):
    problems.append(msg)


def check_cross_plugin_identical(problems):
    import hashlib
    for name, plugins in IDENTICAL_ACROSS_PLUGINS.items():
        shas = {}
        for p in plugins:
            try:
                with open(os.path.join(ROOT, p, name), "rb") as f:
                    shas[p] = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                _fail(problems, "{}/{} missing".format(p, name))
        if len(set(shas.values())) > 1:
            _fail(problems, "{} differs across {} (must stay byte-identical)".format(
                name, " / ".join(plugins)))


def check_damsuite_drift(problems):
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import sync_damsuite
    for p in sync_damsuite.check(ROOT):
        _fail(problems, "damsuite drift: " + p)


def check_damsuite_pin(problems):
    """The committed shared pin (tools/damsuite_pin.json) still matches the master, so the master
    cannot be edited without regenerating the pin both forks vendor from."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import sync_damsuite
    for p in sync_damsuite.check_pin(ROOT):
        _fail(problems, "damsuite pin: " + p)


def check_versions(problems):
    ver_re = re.compile(r"^\d+\.\d+\.\d+")
    for plugin in PLUGINS:
        meta = os.path.join(ROOT, plugin, "metadata.txt")
        if not os.path.isfile(meta):
            _fail(problems, "{}: metadata.txt missing".format(plugin))
            continue
        with open(meta, encoding="utf-8") as f:
            m = re.search(r"^version=(.+)$", f.read(), re.M)
        if not m or not ver_re.match(m.group(1).strip()):
            _fail(problems, "{}: metadata.txt version not x.y.z".format(plugin))
    # the vendored damsuite SUITE_VERSION must be identical everywhere it ships
    seen = {}
    for plugin in PLUGINS:
        init = os.path.join(ROOT, plugin, "damsuite", "__init__.py")
        if not os.path.isfile(init):
            continue
        with open(init, encoding="utf-8") as f:
            m = re.search(r'SUITE_VERSION\s*=\s*["\']([^"\']+)["\']', f.read())
        if m:
            seen.setdefault(m.group(1), []).append(plugin)
    if len(seen) > 1:
        _fail(problems, "damsuite SUITE_VERSION disagrees across copies: " + repr(seen))


def check_compile(problems):
    import py_compile
    n = 0
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for fn in files:
            if fn.endswith(".py"):
                try:
                    py_compile.compile(os.path.join(base, fn), doraise=True)
                    n += 1
                except py_compile.PyCompileError as e:
                    _fail(problems, "syntax error: " + str(e).splitlines()[-1])
    print("  compiled {} .py files".format(n))


def check_fork_parity(problems):
    """Safety core identical across the DamSafety and nd_hazard forks — only when both are
    checked out (separate repos), otherwise a graceful skip."""
    import check_fork_parity as cfp
    nd = os.environ.get("ND_HAZARD_ROOT", "/home/user/nd_hazard")
    if not os.path.isdir(nd):
        print("  nd_hazard checkout not present; skipping cross-fork parity (set ND_HAZARD_ROOT)")
        return
    for p in cfp.check(nd):
        _fail(problems, "fork parity: " + p)


def check_golden(problems):
    try:
        import numpy  # noqa: F401
    except Exception:
        print("  numpy not available; skipping golden baseline (run it in the suites job)")
        return
    sys.path.insert(0, ROOT)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from nd_breach_runner.tests.test_golden_baseline import \
            golden_builtin_engine_regulatory_numbers as g
        g()
    except Exception as e:
        _fail(problems, "golden baseline: {}: {}".format(type(e).__name__, e))


def main():
    problems = []
    for name, fn in (("damsuite drift", check_damsuite_drift),
                     ("damsuite pin", check_damsuite_pin), ("versions", check_versions),
                     ("cross-plugin identical", check_cross_plugin_identical),
                     ("fork parity", check_fork_parity),
                     ("compile", check_compile), ("golden baseline", check_golden)):
        print("[ci] " + name)
        try:
            fn(problems)
        except Exception as e:      # a check itself blew up
            _fail(problems, "{} check errored: {}: {}".format(name, type(e).__name__, e))
    if problems:
        print("\nFAILED ({} problem(s)):".format(len(problems)))
        for p in problems:
            print("  - " + p)
        return 1
    print("\nAll fast checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
