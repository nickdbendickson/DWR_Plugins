"""DamSafety Core 1.0.0: a plugin's shared window file (tokens.py, ui_widgets.py, ui_fit.py,
suite_settings.py ...) is a three-line shim that replaces itself in sys.modules with the core's
module while it executes:

    import sys
    from DamSafety_Core.ui import tokens as _m
    sys.modules[__name__] = _m

This checks, in plain Python, that `from .tokens import TOKENS`, `from . import tokens` and
`import Plugin.tokens` all see the core module after that trick (importlib returns
sys.modules[name] after exec, not the module object it created), and that tokens.py (the only
Qt-free shared window file) is the same object from two plugins. Run:

    python3 DamSafety_Core/tests/test_ui_shim.py
"""
import importlib
import os
import shutil
import sys
import tempfile
import textwrap

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SHIM = "import sys\nfrom DamSafety_Core.ui import {name} as _m\nsys.modules[__name__] = _m\n"
_FAILS = []


def check(label, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + label + ("" if ok else " - " + str(detail)))
    if not ok:
        _FAILS.append(label)


def _write(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def main():
    work = tempfile.mkdtemp(prefix="core_ui_shim_")
    try:
        for plugin in ("Fake_A", "Fake_B"):
            _write(work, plugin + "/__init__.py", "")
            _write(work, plugin + "/tokens.py", SHIM.format(name="tokens"))
            _write(work, plugin + "/user.py", textwrap.dedent('''\
                from .tokens import TOKENS
                from . import tokens
                '''))
        sys.path.insert(0, work)
        from DamSafety_Core.ui import tokens as core_tokens
        a = importlib.import_module("Fake_A.user")
        b = importlib.import_module("Fake_B.user")
        check("from .tokens import TOKENS reads the core module", a.TOKENS is core_tokens.TOKENS)
        check("from . import tokens is the core module", a.tokens is core_tokens)
        check("import Plugin.tokens is the core module",
              importlib.import_module("Fake_A.tokens") is core_tokens)
        check("two plugins share one module object", a.tokens is b.tokens)
        check("sys.modules holds the core module under the plugin name",
              sys.modules["Fake_A.tokens"] is core_tokens)
        check("the core module keeps its own name", core_tokens.__name__ == "DamSafety_Core.ui.tokens")
        # the real shims: every plugin's copy is the three-line form onto the core file
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import ci_checks
        probs = ci_checks.ui_shim_problems(ROOT)
        check("every plugin's shared window file is the three-line shim", probs == [], probs)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if _FAILS:
        print("{} check(s) failed".format(len(_FAILS)))
        return 1
    print("OK: ui shim")
    return 0


if __name__ == "__main__":
    sys.exit(main())
