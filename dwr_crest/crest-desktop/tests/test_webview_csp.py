"""The desktop CSP stays without unsafe-eval; pywebview binder is rewritten first."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def _load():
    try:
        from crest.webview_csp import (
            csp_safe_pywebview_js,
            install_pywebview_csp_patches,
            pywebview_js_uses_eval,
        )
        return csp_safe_pywebview_js, pywebview_js_uses_eval, install_pywebview_csp_patches
    except ImportError:
        from damit_desktop.webview_csp import (
            csp_safe_pywebview_js,
            install_pywebview_csp_patches,
            pywebview_js_uses_eval,
        )
        return csp_safe_pywebview_js, pywebview_js_uses_eval, install_pywebview_csp_patches


BINDER = (
    "      var funcBody = \"return 1;\";\n"
    "      nestedObject[functionName] = new Function(\n"
    "        sanitize_params(params),\n"
    "        funcBody\n"
    "      );\n"
    "      window.pywebview._returnValuesCallbacks[funcName] = {};\n"
)


class TestWebviewCsp(unittest.TestCase):
    def setUp(self):
        self.csp_safe_pywebview_js, self.pywebview_js_uses_eval, self.install = _load()

    def test_rewrites_the_pywebview_6_binder(self):
        out = self.csp_safe_pywebview_js(BINDER)
        self.assertNotIn("new Function", out)
        self.assertNotIn("eval(", out)
        self.assertIn("_jsApiCallback", out)
        self.assertIn("_checkValue", out)
        self.assertFalse(self.pywebview_js_uses_eval(out))
        self.assertTrue(self.pywebview_js_uses_eval(BINDER))

    def test_real_pywebview_api_js_if_installed(self):
        try:
            import webview  # noqa: F401
            from webview import util as wvutil
        except ImportError:
            self.skipTest("pywebview is not installed in this environment")
        api = (Path(wvutil.get_js_dir()) / "api.js").read_text(encoding="utf-8")
        self.assertIn("new Function", api)
        out = self.csp_safe_pywebview_js(api)
        self.assertNotIn("new Function", out)
        self.assertIn("window.pywebview", out)
        self.assertIn("_createApi", out)

    def test_unchanged_when_the_pattern_is_absent(self):
        src = "window.pywebview = { api: {} };"
        self.assertEqual(self.csp_safe_pywebview_js(src), src)


if __name__ == "__main__":
    unittest.main()
