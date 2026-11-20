"""Make pywebview's JS bridge work under the desk's Content-Security-Policy.

The desktop CSP is script-src 'self' with no unsafe-eval (src/lib/crest/csp.ts). pywebview 6
binds window.pywebview.api with `new Function` (webview/js/api.js) and later wraps
Window.evaluate_js in `eval("...")`. Either one logs an Uncaught EvalError at startup (or
when a Save/Open dialog returns) and can leave the native file bridge unbound.

This module rewrites the injected binder to a closure, and runs evaluate_js without eval, so
the CSP does not need to be weakened. Crest.html and the web app are unchanged.
"""

from __future__ import annotations

import re
from typing import Any, Callable

_PATCHED = False

# The assignment pywebview 6.x writes in js/api.js (whitespace may vary).
_NEW_FUNCTION = re.compile(
    r"nestedObject\[functionName\]\s*=\s*new\s+Function\s*\(\s*sanitize_params\(\s*params\s*\)\s*,\s*funcBody\s*\)\s*;",
    re.M,
)

# Closure that does the same bind as new Function(params, funcBody), without eval.
_BINDER = """nestedObject[functionName] = (function (boundName) {
        return function () {
          var __id = (Math.random() + "").substring(2);
          var promise = new Promise(function (resolve, reject) {
            window.pywebview._checkValue(boundName, resolve, reject, __id);
          });
          window.pywebview._jsApiCallback(boundName, Array.prototype.slice.call(arguments), __id);
          return promise;
        };
      })(funcName);"""


def csp_safe_pywebview_js(js: str) -> str:
    """Replace pywebview's new Function API binder with a closure. Unchanged if the pattern is absent."""
    if not js:
        return js
    return _NEW_FUNCTION.sub(_BINDER, js, count=1)


def pywebview_js_uses_eval(js: str) -> bool:
    """True when the injected script would still hit CSP script-src without unsafe-eval."""
    return bool(re.search(r"\bnew\s+Function\s*\(", js or "")) or bool(
        re.search(r"(?<![A-Za-z0-9_])eval\s*\(", js or "")
    )


def install_pywebview_csp_patches(webview_mod=None) -> bool:
    """Patch pywebview in this process. Idempotent. Returns True when the patches were applied."""
    global _PATCHED
    if _PATCHED:
        return True
    if webview_mod is None:
        import webview as webview_mod  # type: ignore

    util = getattr(webview_mod, "util", None)
    if util is None:
        try:
            from webview import util as util  # type: ignore
        except Exception:
            return False

    original_load = getattr(util, "load_js_files", None)
    if not callable(original_load):
        return False

    def load_js_files_safe(window, platform):
        js_code, finish_script = original_load(window, platform)
        return csp_safe_pywebview_js(js_code), finish_script

    util.load_js_files = load_js_files_safe

    Window = getattr(webview_mod, "Window", None)
    if Window is not None and callable(getattr(Window, "evaluate_js", None)):
        _patch_evaluate_js(Window)

    _PATCHED = True
    return True


def _patch_evaluate_js(Window) -> None:
    """Run evaluate_js as a function body instead of eval("..."), so a return value still comes back."""
    original = Window.evaluate_js

    def evaluate_js(self, script: str, callback: Callable[..., Any] | None = None) -> Any:
        # The stock method wraps `script` in eval("...") so a bare expression can be the return
        # value; eval is what the CSP refuses. The script runs as a function body instead.
        # LIMIT: a bare expression ("document.title", or pywebview's own window.dom helpers such as
        # element.text) now returns None; write "return document.title" to get a value. The desk
        # only uses evaluate_js through pywebview's JS-API callbacks, which are statements.
        return _evaluate_without_eval(self, script, callback, original)

    Window.evaluate_js = evaluate_js


def _evaluate_without_eval(window, script, callback, original):
    """Send a no-eval wrapper through the GUI evaluate_js used by run_js (parse_json=False is run_js).

    We still use Window.evaluate_js's original only as a fallback if the GUI object is missing.
    """
    # Recreate the stock wrapper, minus eval, matching webview.window.Window.evaluate_js.
    import uuid

    unique_id = uuid.uuid1().hex
    callbacks = getattr(window, "_callbacks", None)
    if isinstance(callbacks, dict):
        callbacks[unique_id] = callback

    gui = getattr(window, "gui", None)
    if gui is None:
        return original(window, script, callback)

    renderer = getattr(gui, "renderer", "")
    if renderer == "cef":
        return_result = f'window.external.return_result(pywebview.stringify(value), "{unique_id}");'
    elif renderer == "android-webkit":
        return_result = "return pywebview.stringify(value);"
    else:
        return_result = "pywebview.stringify(value);"

    # script is inserted as a function body, never as an eval() string.
    body = (
        "var value;\n"
        "try {\n"
        "  value = (function () {\n"
        f"{script}\n"
        "  })();\n"
        "} catch (e) {\n"
        "  value = {name: e.name, pywebviewJavascriptError420: true};\n"
        "  var keys = Object.getOwnPropertyNames(e);\n"
        "  keys.forEach(function(key) { value[key] = e[key]; });\n"
        "}\n"
        f"{return_result}"
    )
    if callback:
        body = (
            "var value = (function () {\n"
            f"{script}\n"
            "})();\n"
            "if (pywebview._isPromise(value)) {\n"
            f'  value.then(function evaluate_async(result) {{ pywebview._asyncCallback(pywebview.stringify(result), "{unique_id}"); }})\n'
            f'    .catch(function evaluate_async(error) {{ pywebview._asyncCallback(pywebview.stringify(error), "{unique_id}"); }});\n'
            '  "true";\n'
            "} else { " + return_result + " }\n"
        )

    if renderer == "cef":
        result = gui.evaluate_js(body, window.uid, True, unique_id)
    elif renderer == "android-webkit":
        result = gui.evaluate_js("(function() {\n" + body + "\n})()", window.uid, True)
    else:
        result = gui.evaluate_js(body, window.uid, True)

    if isinstance(result, dict) and result.get("pywebviewJavascriptError420"):
        from webview.errors import JavascriptException  # type: ignore

        del result["pywebviewJavascriptError420"]
        raise JavascriptException(result)
    return result
