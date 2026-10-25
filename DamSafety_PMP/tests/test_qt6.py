"""QGIS 4 (Qt6) readiness for DamSafety_PMP (no QGIS needed).

metadata.txt declares supportsQt6=True with qgisMaximumVersion=4.99 (the suite convention), and the
plugin code has no Qt imports and no Qt5-only spellings (unscoped Qt enums, PyQt5 imports, QVariant
field types, exec_()). The suite-wide checks are tools/ci_checks.py "qt6 enums" and "qt6 metadata"
(both in CI); this test keeps the PMP side explicit.

Run:  python3 DamSafety_PMP/tests/test_qt6.py
"""
import configparser
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
QT5_ONLY = re.compile(r"\bPyQt5\b|\bQVariant\.\w+|\bexec_\(|\bQt\.(?:Align|Checked|Unchecked|Horizontal|Vertical)"
                      r"\w*\b|\bQgis\.(?:Info|Warning|Critical|Success)\b")


def main():
    cp = configparser.ConfigParser(interpolation=None)
    cp.read(os.path.join(PLUGIN, "metadata.txt"), encoding="utf-8")
    g = cp["general"]
    assert g.get("supportsQt6", "").strip() == "True", "supportsQt6 is not True"
    assert g.get("qgisMaximumVersion", "").strip() == "4.99", "qgisMaximumVersion is not 4.99"
    bad = []
    for base, dirs, files in os.walk(PLUGIN):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__")]
        for fn in files:
            if fn.endswith(".py"):
                path = os.path.join(base, fn)
                with open(path, encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if QT5_ONLY.search(line.split("#", 1)[0]):
                            bad.append("%s:%d: %s" % (os.path.relpath(path, PLUGIN), i, line.strip()))
    assert not bad, "Qt5-only spellings:\n" + "\n".join(bad)
    print("ok     metadata declares Qt6 / QGIS 4; no Qt5-only spellings")


if __name__ == "__main__":
    main()
    sys.exit(0)
