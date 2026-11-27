#!/usr/bin/env python3
"""Write DamSafety_Core/boann/FILES.json: BOANN_VERSION and {path: sha256} of every file of the one
copy of Boann (hydro/ = the flood engine, grid/ = BoannGrid), .py, .metal, .cpp, .h and README
alike, so CI sees any change to it and an install can be checked against it.

    python3 tools/boann_files.py           # rewrite FILES.json after editing DamSafety_Core/boann/
    python3 tools/boann_files.py --check   # exit 1 when FILES.json is stale (tools/ci_checks.py)

tools/ci_checks.py also holds the rest of "one Boann copy" (DamSafety Core 1.7.0): no plugin keeps
a copy (its core/metalflood or core/metalterrain is only the alias shim), and when any engine
file's hash differs from the base branch's FILES.json, BOANN_VERSION must differ too. Phase 2 of the
Boann extraction pins DamIT's vendored copy to this manifest. Pure stdlib."""
import argparse
import hashlib
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE_REL = "DamSafety_Core/boann"
PACKAGE = os.path.join(ROOT, *PACKAGE_REL.split("/"))
FILES_NAME = "FILES.json"
SKIP_DIRS = ("__pycache__", "prebuilt")          # build output, never committed
SKIP_SUFFIX = (".pyc", ".pyo", ".so", ".dll", ".dylib", ".metallib", ".air")
VERSION_RE = re.compile(r'^BOANN_VERSION\s*=\s*"([^"]+)"', re.M)


def boann_version(folder=PACKAGE):
    with open(os.path.join(folder, "hydro", "version.py"), encoding="utf-8") as f:
        m = VERSION_RE.search(f.read())
    return m.group(1) if m else ""


def files(folder=PACKAGE):
    """{posix path relative to folder: sha256} for every shipped file, sorted."""
    out = {}
    for d, subdirs, names in os.walk(folder):
        subdirs[:] = sorted(s for s in subdirs if s not in SKIP_DIRS and not s.startswith("."))
        for name in sorted(names):
            if name.startswith(".") or name.endswith(SKIP_SUFFIX):
                continue
            p = os.path.join(d, name)
            rel = os.path.relpath(p, folder).replace(os.sep, "/")
            if rel == FILES_NAME:
                continue
            with open(p, "rb") as f:
                out[rel] = hashlib.sha256(f.read()).hexdigest()
    return dict(sorted(out.items()))


def manifest(folder=PACKAGE):
    return {"boann_version": boann_version(folder), "files": files(folder)}


def manifest_text(folder=PACKAGE):
    return json.dumps(manifest(folder), indent=1, sort_keys=True) + "\n"


def check(folder=PACKAGE):
    """Problems (strings); [] when FILES.json matches the files."""
    path = os.path.join(folder, FILES_NAME)
    if not os.path.isfile(path):
        return ["{} missing (run: python3 tools/boann_files.py)".format(
            os.path.relpath(path, ROOT))]
    with open(path, encoding="utf-8") as f:
        have = f.read()
    if have != manifest_text(folder):
        return ["{} is stale: does not match Boann's files (run: python3 "
                "tools/boann_files.py)".format(os.path.relpath(path, ROOT))]
    return []


def version_problems(base_manifest, head_manifest):
    """[] unless an engine file changed (added, removed or edited) against the base while
    BOANN_VERSION stayed the same. Either manifest may be None (no base yet: nothing to compare)."""
    if not base_manifest or not head_manifest:
        return []
    bf, hf = base_manifest.get("files") or {}, head_manifest.get("files") or {}
    changed = sorted(k for k in set(bf) | set(hf) if bf.get(k) != hf.get(k))
    if changed and base_manifest.get("boann_version") == head_manifest.get("boann_version"):
        return ["Boann's files changed against the base ({}{}) but BOANN_VERSION is still {} "
                "(bump DamSafety_Core/boann/hydro/version.py: MAJOR when a result moves, MINOR for "
                "a new solver/option/output, PATCH otherwise; then python3 tools/boann_files.py)"
                .format(", ".join(changed[:5]), " ..." if len(changed) > 5 else "",
                        head_manifest.get("boann_version"))]
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
        print("boann FILES.json: {}".format("stale" if problems else "ok"))
        return 1 if problems else 0
    print("wrote {}".format(os.path.relpath(os.path.join(PACKAGE, FILES_NAME), ROOT))
          if write() else "FILES.json already current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
