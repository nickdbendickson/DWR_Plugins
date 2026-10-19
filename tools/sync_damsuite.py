#!/usr/bin/env python3
"""Single source of truth for the vendored `damsuite` package.

Each plugin ships a byte-identical copy of the shared `damsuite` package as a sub-package so it
can be imported from a QGIS plugin zip with no external dependency. The canonical copy lives in
`shared/damsuite/` at the repo root; this tool writes it into each plugin and (re)generates the
`damsuite/FILES.json` integrity manifest that `damsuite/__init__.py:selfcheck()` checks at
runtime. Edit `shared/damsuite/` ONLY, never a vendored copy, then run this tool.

    python3 tools/sync_damsuite.py           # write: copy master into each plugin, refresh FILES.json
    python3 tools/sync_damsuite.py --check    # verify: exit 1 if any vendored copy or FILES.json drifted

Use --check as a CI/merge gate so a hand-edited vendored copy (or a stale FILES.json after a
merge) fails the build instead of silently shipping. Pure stdlib; no QGIS/numpy/scipy needed.

Each plugin vendors the SUBSET of the master it needs (its current file set is preserved — e.g.
the Runner and Screening copies omit `pack_convert.py`). DamSafety_Setup is intentionally NOT a
full copy (it carries a trimmed 3-file mini-`damsuite` with its own `__init__.py`), so it is
excluded here and maintained on its own.
"""
import argparse
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = os.path.join(ROOT, "shared", "damsuite")

# plugins that vendor the FULL shared package (not DamSafety_Setup, which vendors a mini-subset)
FULL_COPY_PLUGINS = ("DamSafety_Hazard", "DamSafety_Runner", "DamSafety_Screening")
# a full-copy plugin carries every master module EXCEPT these (modules it has no use for);
# a new master module with no exclusion propagates to every full copy.
EXCLUDE = {
    "DamSafety_Runner": {"pack_convert.py"},
    "DamSafety_Screening": {"pack_convert.py"},
}
FILES_NAME = "FILES.json"


def _expected(plugin, master_py):
    """The .py set a full-copy plugin should vendor: the master minus its exclusions."""
    return sorted(set(master_py) - EXCLUDE.get(plugin, set()))


def _sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _manifest(folder):
    """{file name: sha256} for every .py directly in `folder`, sorted — the exact shape
    damsuite/__init__.py:selfcheck() compares against (its _hashes())."""
    out = {}
    for name in sorted(os.listdir(folder)):
        p = os.path.join(folder, name)
        if name.endswith(".py") and os.path.isfile(p):
            out[name] = _sha(p)
    return out


def _manifest_text(folder):
    return json.dumps(_manifest(folder), indent=1, sort_keys=True) + "\n"


def _vendored_py(folder):
    return sorted(n for n in os.listdir(folder)
                  if n.endswith(".py") and os.path.isfile(os.path.join(folder, n)))


def check(root=ROOT):
    """Return a list of drift problems against the master; [] means every vendored copy and its
    FILES.json match `shared/damsuite/`."""
    problems = []
    if not os.path.isdir(MASTER):
        return ["master missing: {}".format(os.path.relpath(MASTER, root))]
    master_py = set(_vendored_py(MASTER))
    for plugin in FULL_COPY_PLUGINS:
        folder = os.path.join(root, plugin, "damsuite")
        if not os.path.isdir(folder):
            problems.append("{}: no damsuite/".format(plugin))
            continue
        expected = set(_expected(plugin, master_py))
        have = set(_vendored_py(folder))
        for name in sorted(expected - have):
            problems.append("{}/damsuite/{} is missing (in the master, not vendored)".format(
                plugin, name))
        for name in sorted(have - expected):
            why = "excluded for this plugin" if name in EXCLUDE.get(plugin, set()) \
                else "not in the master"
            problems.append("{}/damsuite/{} should not be vendored ({})".format(plugin, name, why))
        for name in sorted(expected & have):
            if _sha(os.path.join(folder, name)) != _sha(os.path.join(MASTER, name)):
                problems.append("{}/damsuite/{} differs from the master".format(plugin, name))
        mpath = os.path.join(folder, FILES_NAME)
        if not os.path.isfile(mpath):
            problems.append("{}/damsuite/{} missing".format(plugin, FILES_NAME))
        else:
            with open(mpath, encoding="utf-8") as f:
                on_disk = f.read()
            if on_disk != _manifest_text(folder):
                problems.append("{}/damsuite/{} is stale (does not match the files)".format(
                    plugin, FILES_NAME))
    return problems


def sync(root=ROOT):
    """Copy the master into each full-copy plugin (preserving that plugin's file set) and rewrite
    FILES.json. Returns the list of changes made."""
    changes = []
    master_py = set(_vendored_py(MASTER))
    for plugin in FULL_COPY_PLUGINS:
        folder = os.path.join(root, plugin, "damsuite")
        if not os.path.isdir(folder):
            continue
        for name in _expected(plugin, master_py):
            src = os.path.join(MASTER, name)
            with open(src, "rb") as f:
                want = f.read()
            dst = os.path.join(folder, name)
            have = None
            if os.path.isfile(dst):
                with open(dst, "rb") as f:
                    have = f.read()
            if want != have:
                with open(dst, "wb") as f:
                    f.write(want)
                changes.append("{} {}/damsuite/{}".format(
                    "added" if have is None else "updated", plugin, name))
        mpath = os.path.join(folder, FILES_NAME)
        want_m = _manifest_text(folder)
        have_m = ""
        if os.path.isfile(mpath):
            with open(mpath, encoding="utf-8") as f:
                have_m = f.read()
        if want_m != have_m:
            with open(mpath, "w", encoding="utf-8") as f:
                f.write(want_m)
            changes.append("refreshed {}/damsuite/{}".format(plugin, FILES_NAME))
    return changes


def main(argv):
    ap = argparse.ArgumentParser(description="Sync the vendored damsuite copies from shared/damsuite.")
    ap.add_argument("--check", action="store_true",
                    help="verify only; exit 1 if any vendored copy or FILES.json drifted")
    args = ap.parse_args(argv)
    if args.check:
        problems = check()
        if problems:
            print("damsuite drift:")
            for p in problems:
                print("  - " + p)
            print("run: python3 tools/sync_damsuite.py")
            return 1
        print("damsuite: all vendored copies match shared/damsuite ({} plugins)".format(
            len(FULL_COPY_PLUGINS)))
        return 0
    changes = sync()
    if changes:
        for c in changes:
            print(c)
    else:
        print("damsuite: already in sync; nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
