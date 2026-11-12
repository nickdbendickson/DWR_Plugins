#!/usr/bin/env python3
"""Write DamSafety_Core/damsuite/FILES.json: {file name: sha256} of every .py file of the one
shared damsuite package, the manifest `damsuite.selfcheck()` compares the installed files with
at each plugin's start-up (a hand-edited or half-updated install reports "changed: x.py").

    python3 tools/damsuite_files.py           # rewrite FILES.json after editing damsuite/
    python3 tools/damsuite_files.py --check   # exit 1 when FILES.json is stale (tools/ci_checks.py)

DamSafety Core 1.0.0 replaced tools/sync_damsuite.py (which copied the package into every plugin
and wrote each copy's FILES.json) with this: there is one package and one manifest. Pure stdlib.
"""
import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(ROOT, "DamSafety_Core", "damsuite")
FILES_NAME = "FILES.json"


def manifest(folder=PACKAGE):
    """{file name: sha256} for every .py directly in `folder`, sorted - the shape
    damsuite/__init__.py:package_files() builds at runtime."""
    out = {}
    for name in sorted(os.listdir(folder)):
        p = os.path.join(folder, name)
        if name.endswith(".py") and os.path.isfile(p):
            with open(p, "rb") as f:
                out[name] = hashlib.sha256(f.read()).hexdigest()
    return out


def manifest_text(folder=PACKAGE):
    return json.dumps(manifest(folder), indent=1, sort_keys=True) + "\n"


def check(folder=PACKAGE):
    """Problems (strings); [] when FILES.json matches the files."""
    path = os.path.join(folder, FILES_NAME)
    if not os.path.isfile(path):
        return ["{} missing (run: python3 tools/damsuite_files.py)".format(
            os.path.relpath(path, ROOT))]
    with open(path, encoding="utf-8") as f:
        have = f.read()
    if have != manifest_text(folder):
        return ["{} is stale: does not match the .py files (run: python3 "
                "tools/damsuite_files.py)".format(os.path.relpath(path, ROOT))]
    return []


def write(folder=PACKAGE):
    """Rewrite FILES.json; True when it changed."""
    path = os.path.join(folder, FILES_NAME)
    want = manifest_text(folder)
    have = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            have = f.read()
    if want == have:
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(want)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="verify only; exit 1 when stale")
    args = ap.parse_args(argv)
    if args.check:
        problems = check()
        for p in problems:
            print("STALE: " + p)
        print("damsuite FILES.json: {}".format("stale" if problems else "ok"))
        return 1 if problems else 0
    print("wrote {}".format(os.path.relpath(os.path.join(PACKAGE, FILES_NAME), ROOT))
          if write() else "FILES.json already current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
