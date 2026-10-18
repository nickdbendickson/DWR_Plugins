"""0.17.25: one ellipsis character (U+2026) in the visible strings - a source scan (no Qt).
Log lines, comments, docstrings, reports and tests are not scanned.

    python3 DamSafety_Screening/tests/test_no_ascii_ellipsis.py
"""
import ast
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(HERE)

VISIBLE_CALLS = {"QPushButton", "QAction", "QLabel", "QToolButton", "QCheckBox", "QRadioButton",
                 "QGroupBox", "QMessageBox", "setText", "setToolTip", "setWindowTitle", "addAction",
                 "addItem", "addItems", "setPlaceholderText", "setTitle", "setStatusTip",
                 "setLabelText", "setWhatsThis", "information", "warning", "critical", "question",
                 "WrapLabel", "ToolRow", "_num", "addTab", "setTabText", "QProgressDialog"}


def _callee(f):
    return f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")


def _strings(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            yield n


def scan(root, skip_dirs=("tests", "damsuite", "__pycache__", "scripts")):
    bad = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in skip_dirs]
        for f in files:
            if not f.endswith(".py"):
                continue
            p = os.path.join(d, f)
            tree = ast.parse(open(p, encoding="utf-8").read(), p)
            exempt = {id(a) for n in ast.walk(tree) if isinstance(n, ast.Call)
                      and _callee(n.func) == "replace" for a in n.args}   # text.replace("...", "…")
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    nm = _callee(node.func)
                    if nm in VISIBLE_CALLS or nm.startswith("QgsProcessingParameter"):
                        for a in list(node.args) + [k.value for k in node.keywords]:
                            for s in _strings(a):
                                if "..." in s.value and id(s) not in exempt:
                                    bad.append("{}:{}: {}({!r})".format(os.path.relpath(p, root), s.lineno, nm, s.value[:60]))
                # tool tables: tuples whose 2nd item is the label shown on a button / menu
                if isinstance(node, ast.Tuple) and len(node.elts) >= 3 and all(
                        isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.elts[1:3]):
                    if node.elts[1].value.endswith("..."):
                        bad.append("{}:{}: tool label {!r}".format(os.path.relpath(p, root), node.lineno, node.elts[1].value))
    return bad


def test_no_ascii_ellipsis_in_visible_strings():
    """The plugin's visible strings (widget texts, menu entries, tooltips, message boxes,
    Processing labels, tool tables) use one ellipsis character, never '...'."""
    bad = scan(PLUGIN_DIR)
    assert not bad, "\n".join(bad)


def test_identical_suite_home_copies():
    """suite_home.py (the Dam suite home window) is byte-identical in Hazard, Runner and
    Screening; the copies present next to this plugin are compared."""
    repo = os.path.dirname(PLUGIN_DIR)
    texts = {}
    for p in ("DamSafety_Hazard", "DamSafety_Runner", "DamSafety_Screening"):
        f = os.path.join(repo, p, "suite_home.py")
        if os.path.isfile(f):
            texts[p] = open(f, "rb").read()
    assert len(set(texts.values())) <= 1, "suite_home.py copies differ: {}".format(sorted(texts))
    for p, t in texts.items():
        assert b'"Browse..."' not in t and b'+ "..."' not in t, p


if __name__ == "__main__":
    n = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            n += 1
            print("ok", _name)
    print("{}: all {} checks passed".format(os.path.basename(__file__), n))
