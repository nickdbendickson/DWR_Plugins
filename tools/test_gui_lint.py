#!/usr/bin/env python3
"""tools/gui_lint.py's source rule, without QGIS: QMessageBox.warning / .critical inside a run
handler is a check shown as a pop-up, which docs/gui-grammar.md forbids ("Checks: CheckPanel,
never a message box"). gui_lint.py boots QGIS when imported, so the rule's two functions are
lifted out of it with ast and run on small sources and on the repo's window files.

    python3 tools/test_gui_lint.py
"""
import ast
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _rule_module():
    src = open(os.path.join(HERE, "gui_lint.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    keep = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in
            ("message_boxes_in_run_handlers", "rule_no_message_box_checks", "fail"))
            or (isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                and n.targets[0].id in ("RUN_HANDLER_RE", "MESSAGE_BOX_KINDS",
                                        "LINT_WINDOW_FILES", "PLUGINS", "FAILS"))]
    mod = types.ModuleType("gui_lint_rule")
    mod.re, mod.os, mod.REPO = __import__("re"), os, REPO
    exec(compile(ast.Module(body=keep, type_ignores=[]), "gui_lint.py", "exec"), mod.__dict__)
    return mod


def main():
    M = _rule_module()
    bad = '''
class W(object):
    def _on_run_all(self):
        if not self.ok():
            QMessageBox.warning(self, "T", "Not ready")
            return
    def _run_chain(self, preflight=False):
        def done(outcome):
            QMessageBox.critical(self, "T", str(outcome))
        self._start(done)
    def _launch_classification(self):
        QMessageBox.question(self, "T", "Run?")          # a confirmation: allowed
        QMessageBox.information(self, "T", "done")       # not a check: allowed
    def _open_tool(self):
        QMessageBox.warning(self, "T", "not a run handler: allowed")
'''
    got = M.message_boxes_in_run_handlers(bad)
    assert [(f, k) for f, k, _l in got] == [("_on_run_all", "warning"), ("_run_chain", "critical")], got
    assert got[0][2] == 5 and got[1][2] == 9, got
    assert M.message_boxes_in_run_handlers("x = 1\n") == []
    # the repo's window files pass the rule
    M.FAILS[:] = []
    M.rule_no_message_box_checks(REPO)
    assert not M.FAILS, "\n".join(M.FAILS)
    print("ok test_gui_lint: the message-box rule flags run handlers, and the repo passes it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
