"""Every DamSafety_Hazard .py file must parse (guards against SyntaxError shipping).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Hazard/tests/test_py_compile.py
"""
import os
import py_compile
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HAZARD = os.path.dirname(HERE)


def main():
    paths = []
    for root, dirs, files in os.walk(HAZARD):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".py"):
                paths.append(os.path.join(root, f))
    failed = 0
    for p in sorted(paths):
        try:
            py_compile.compile(p, doraise=True)
        except py_compile.PyCompileError as e:
            failed += 1
            print("FAIL ", os.path.relpath(p, HAZARD), ":", e)
    print("{} files, {} failed".format(len(paths), failed))
    return failed


if __name__ == "__main__":
    raise SystemExit(main())
