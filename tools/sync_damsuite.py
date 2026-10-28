#!/usr/bin/env python3
"""Single source of truth for the vendored `damsuite` package.

Each plugin ships a byte-identical copy of the shared `damsuite` package as a sub-package so it
can be imported from a QGIS plugin zip with no external dependency. The canonical copy lives in
`shared/damsuite/` at the repo root; this tool writes it into each plugin and (re)generates the
`damsuite/FILES.json` integrity manifest that `damsuite/__init__.py:selfcheck()` checks at
runtime. Edit `shared/damsuite/` ONLY, never a vendored copy, then run this tool.

    python3 tools/sync_damsuite.py           # write: copy master into each plugin, refresh FILES.json
    python3 tools/sync_damsuite.py --check    # verify: exit 1 if any vendored copy or FILES.json drifted
    python3 tools/sync_damsuite.py --write-pin [--pin damsuite-pin-vN]   # regenerate the shared pin

Use --check as a CI/merge gate so a hand-edited vendored copy (or a stale FILES.json after a
merge) fails the build instead of silently shipping. Pure stdlib; no QGIS/numpy/scipy needed.

THE SHARED PIN. `shared/damsuite/` here is the single source of truth that BOTH forks vendor from
- the DamSafety_* plugins in this repo, and the nd_* plugins in the nd_hazard repo. `--write-pin`
records a content fingerprint of the master (sha256 of every module) in `tools/damsuite_pin.json`
and stamps a tag label (damsuite-pin-vN). That pin file is committed byte-identical into both repos
so each repo's NORMAL CI can verify its own vendored copies against the pin with no combined
checkout and no network (nd_hazard's tools/sync_damsuite.py --check does exactly that). --check in
THIS repo additionally confirms the committed pin still matches the master, so the master can never
be edited without regenerating the pin. Edit shared/damsuite/, run --write-pin (bump --pin on a real
content change), cut the matching git tag, then mirror tools/damsuite_pin.json into nd_hazard.

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
TOOLS = os.path.dirname(os.path.abspath(__file__))
MASTER = os.path.join(ROOT, "shared", "damsuite")

# plugins that vendor the FULL shared package (not DamSafety_Setup, which vendors a mini-subset)
# DamSafety_Data (1.5.0): its offline pack builder writes format 2 with gpkgpack
FULL_COPY_PLUGINS = ("DamSafety_Hazard", "DamSafety_Runner", "DamSafety_Screening",
                     "DamSafety_Data")
# a full-copy plugin carries every master module EXCEPT these (modules it has no use for);
# a new master module with no exclusion propagates to every full copy.
EXCLUDE = {
    "DamSafety_Runner": {"pack_convert.py"},
    "DamSafety_Screening": {"pack_convert.py"},
}
FILES_NAME = "FILES.json"

# --- the shared pin (tools/damsuite_pin.json) -------------------------------------------------
PIN_NAME = "damsuite_pin.json"
PIN_PATH = os.path.join(TOOLS, PIN_NAME)
DEFAULT_PIN_LABEL = "damsuite-pin-v1"
# Files present in a fork's vendored copies but whose CONTENT legitimately differs per fork, so the
# cross-fork pin does NOT require them to match (each fork's own --check still keeps that fork's
# copies equal to its own master/pin). Documented reasons:
#   __init__.py    per-fork module manifest + SUITE_VERSION (the forks run divergent version lines)
#   versions.py    per-fork branding + release history
#   suite_home.py  DamSafety couples it to structure_flags (a DamSafety-only module); nd_hazard
#                  ships the same file minus that feature until the structure_flags port is decided
FORK_SPECIFIC = ("__init__.py", "versions.py", "suite_home.py")


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


def _pin_body(label):
    """The pin dict: sha256 of every canonical master module, the fork-specific list, the tag
    label, and a single fingerprint over the content-checked (non-fork-specific) files."""
    files = _manifest(MASTER)                                   # {name: sha256} for all master .py
    content = {k: v for k, v in files.items() if k not in FORK_SPECIFIC}
    pin_sha = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "pin": label,
        "pin_sha256": pin_sha,
        "source": "nickdbendickson/DWR_Plugins : shared/damsuite",
        "fork_specific": sorted(FORK_SPECIFIC),
        "files": files,
    }


def _pin_text(label):
    return json.dumps(_pin_body(label), indent=1, sort_keys=True) + "\n"


def _read_pin():
    if not os.path.isfile(PIN_PATH):
        return None
    with open(PIN_PATH, encoding="utf-8") as f:
        return json.load(f)


def write_pin(label=None):
    """(Re)write tools/damsuite_pin.json from the master. Keeps the existing pin label unless
    `label` is given (bump it with --pin damsuite-pin-vN on a real content change). Returns the
    list of changes made."""
    if label is None:
        existing = _read_pin()
        label = (existing or {}).get("pin", DEFAULT_PIN_LABEL)
    text = _pin_text(label)
    have = ""
    if os.path.isfile(PIN_PATH):
        with open(PIN_PATH, encoding="utf-8") as f:
            have = f.read()
    if text == have:
        return []
    with open(PIN_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    return ["wrote tools/{} (pin {})".format(PIN_NAME, label)]


def check_pin(root=ROOT):
    """Confirm the committed pin still matches the current master, so the master can never be
    edited without regenerating the pin. Returns a list of problems; [] means in sync."""
    problems = []
    pin = _read_pin()
    if pin is None:
        return ["pin missing: tools/{} (run: python3 tools/sync_damsuite.py --write-pin)".format(
            PIN_NAME)]
    expect = _pin_body(pin.get("pin", DEFAULT_PIN_LABEL))
    have, want = pin.get("files") or {}, expect["files"]
    for name in sorted(set(have) | set(want)):
        if have.get(name) != want.get(name):
            why = ("not in the master" if name not in want else
                   "in the master, not pinned" if name not in have else "hash changed")
            problems.append("pin stale: {} ({})".format(name, why))
    if pin.get("pin_sha256") != expect["pin_sha256"]:
        problems.append("pin_sha256 does not match the master content")
    if sorted(pin.get("fork_specific") or []) != sorted(FORK_SPECIFIC):
        problems.append("pin fork_specific list != {}".format(sorted(FORK_SPECIFIC)))
    if problems:
        problems.append("run: python3 tools/sync_damsuite.py --write-pin")
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
                    help="verify only; exit 1 if any vendored copy, FILES.json or the pin drifted")
    ap.add_argument("--write-pin", action="store_true",
                    help="(re)generate tools/damsuite_pin.json from the master (the shared pin both "
                         "forks vendor from)")
    ap.add_argument("--pin", metavar="LABEL",
                    help="pin label to stamp when writing the pin (e.g. damsuite-pin-v2); defaults "
                         "to the current label")
    args = ap.parse_args(argv)
    if args.write_pin:
        changes = write_pin(args.pin)
        print("\n".join(changes) if changes else "pin: already current")
        return 0
    if args.check:
        problems = check() + check_pin()
        if problems:
            print("damsuite drift:")
            for p in problems:
                print("  - " + p)
            if not any(p.startswith("run:") for p in problems):
                print("run: python3 tools/sync_damsuite.py")
            return 1
        print("damsuite: all vendored copies match shared/damsuite and the pin ({} plugins)".format(
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
