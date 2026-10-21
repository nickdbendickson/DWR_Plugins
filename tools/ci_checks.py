#!/usr/bin/env python3
"""Fast, QGIS-free merge gate. Runs the checks that need no QGIS/GDAL install and are fully
deterministic, so they can gate every PR cheaply:

  1. damsuite drift   — every vendored copy matches shared/damsuite and its FILES.json; and the
                        committed shared pin (tools/damsuite_pin.json) still matches the master
  2. version sanity   — each plugin metadata.txt has a well-formed version=, and the shared
                        damsuite SUITE_VERSION is identical across every copy that ships it
     version book      — shared/damsuite/versions.py RELEASE (and every vendored copy) equals
                        each plugin's metadata.txt version; Hazard's constants.PLUGIN_VERSION and
                        README.txt version line agree with its metadata.txt
     vs main           — against the base branch (CI_BASE_REF, default origin/main; skipped with a
                        notice when it is not fetched): no plugin version is lower, and no
                        changelog entry of the base (metadata.txt changelog= versions,
                        docs/CHANGELOG.md bullet titles and date sections) is missing
  3. compile          — py_compile every .py in the suite (catches a syntax error from a bad
                        merge across all plugins in seconds, without importing anything)
  4. shared/ui        — every plugin copy of the shared window files matches shared/ui/, and no
                        window code writes a #rrggbb colour outside tokens.py (docs/gui-grammar.md)
  5. zone colours     — the ACER TM 11 danger zones use REG_05.2026 Fig. 5.1 / 5.2's colours in
                        every style, chart and copy (DamSafety_Hazard/tests/test_zone_colors_reg05.py)
  6. golden baseline  — the built-in flood engine still reproduces the frozen regulatory numbers
                        (needs numpy, not scipy; skipped with a notice if numpy is absent)

Exit non-zero on any failure. The heavier per-plugin suites (real QGIS / scipy / real engines)
run in a separate CI job that provisions QGIS; this job is the always-on hard gate.

    python3 tools/ci_checks.py
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGINS = ("DamSafety_Hazard", "DamSafety_Runner", "DamSafety_Screening",
           "DamSafety_Data", "DamSafety_Setup", "DamSafety_PMP", "DamSafety_HazardLite")

# plugin-root files copied verbatim into more than one plugin (not shared via damsuite): they must
# stay byte-identical so the two tools apply the same rule. {filename: (plugins that carry it)}.
IDENTICAL_ACROSS_PLUGINS = {
    "crest_rule.py": ("DamSafety_Runner", "DamSafety_Screening"),  # one top-of-dam number for both
    # Runner 3.10.43 / Screening 0.17.35: gdal.Warp without the source overviews (-ovr NONE)
    "core/warp_opts.py": ("DamSafety_Runner", "DamSafety_Screening"),
    # audit R-H1: the Suite status dock's Linux LISFLOOD-FP build applies the same patch
    "scripts/build_lisflood_linux.sh": ("DamSafety_Runner", "DamSafety_Setup"),
    "scripts/patch_lisflood.py": ("DamSafety_Runner", "DamSafety_Setup"),
}


def _fail(problems, msg):
    problems.append(msg)


def check_cross_plugin_identical(problems):
    import hashlib
    for name, plugins in IDENTICAL_ACROSS_PLUGINS.items():
        shas = {}
        for p in plugins:
            try:
                with open(os.path.join(ROOT, p, name), "rb") as f:
                    shas[p] = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                _fail(problems, "{}/{} missing".format(p, name))
        if len(set(shas.values())) > 1:
            _fail(problems, "{} differs across {} (must stay byte-identical)".format(
                name, " / ".join(plugins)))


def check_damsuite_drift(problems):
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import sync_damsuite
    for p in sync_damsuite.check(ROOT):
        _fail(problems, "damsuite drift: " + p)


def check_ui_drift(problems):
    """Every plugin copy of the shared window files (shared/ui/) matches its canonical file."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import sync_ui
    for p in sync_ui.check(ROOT):
        _fail(problems, "shared/ui drift: " + p)


# Window code: the files that build windows. A colour in them comes from tokens.py
# (docs/gui-grammar.md); maps, reports and charts keep their own palettes and are not listed.
WINDOW_FILES = ("dialog.py", "wizard.py", "popup_logic.py", "settings_dialog.py", "status_dock.py",
                "plugin.py", "suite_settings_tab.py", "classify_tab.py", "review_tab.py",
                "facility_review_tab.py", "data_tab.py", "datafolder_tab.py", "dsswise_tab.py",
                "extras_tab.py", "extras290_tab.py", "rasimport_tab.py", "suite_tab.py",
                "ui_check_panel.py", "constants.py")
HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-zA-Z])")


def check_window_colours(problems):
    """No #rrggbb in window code outside tokens.py (the shared/ui files are checked at their
    canonical copy; tokens.py is the one place a window colour is written)."""
    files = [os.path.join(ROOT, "shared", "ui", f) for f in os.listdir(os.path.join(ROOT, "shared", "ui"))
             if f.endswith(".py") and f not in ("tokens.py", "suite_styles.py")]
    for plugin in PLUGINS:
        for f in WINDOW_FILES:
            files.append(os.path.join(ROOT, plugin, f))
    for path in files:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                code = line.split("  #", 1)[0] if not line.lstrip().startswith("#") else ""
                if HEX_RE.search(code):
                    _fail(problems, "{}:{}: colour literal in window code - use tokens.py".format(
                        os.path.relpath(path, ROOT), n))


def check_damsuite_pin(problems):
    """The committed shared pin (tools/damsuite_pin.json) still matches the master, so the master
    cannot be edited without regenerating the pin both forks vendor from."""
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import sync_damsuite
    for p in sync_damsuite.check_pin(ROOT):
        _fail(problems, "damsuite pin: " + p)


def check_versions(problems):
    ver_re = re.compile(r"^\d+\.\d+\.\d+")
    for plugin in PLUGINS:
        meta = os.path.join(ROOT, plugin, "metadata.txt")
        if not os.path.isfile(meta):
            _fail(problems, "{}: metadata.txt missing".format(plugin))
            continue
        with open(meta, encoding="utf-8") as f:
            m = re.search(r"^version=(.+)$", f.read(), re.M)
        if not m or not ver_re.match(m.group(1).strip()):
            _fail(problems, "{}: metadata.txt version not x.y.z".format(plugin))
    # the vendored damsuite SUITE_VERSION must be identical everywhere it ships
    seen = {}
    for plugin in PLUGINS:
        init = os.path.join(ROOT, plugin, "damsuite", "__init__.py")
        if not os.path.isfile(init):
            continue
        with open(init, encoding="utf-8") as f:
            m = re.search(r'SUITE_VERSION\s*=\s*["\']([^"\']+)["\']', f.read())
        if m:
            seen.setdefault(m.group(1), []).append(plugin)
    if len(seen) > 1:
        _fail(problems, "damsuite SUITE_VERSION disagrees across copies: " + repr(seen))


# ------------------------------------------------------------ version bookkeeping ----------
def _ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", (v or "").split("-")[0])[:4])


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def metadata_version(text):
    m = re.search(r"^version=\s*(\S+)", text or "", re.M)
    return m.group(1).strip() if m else None


def release_dict(text):
    """RELEASE = {...} of a damsuite versions.py text ({} when absent)."""
    m = re.search(r"^RELEASE\s*=\s*(\{[^}]*\})", text or "", re.M)
    if not m:
        return {}
    import ast
    try:
        return dict(ast.literal_eval(m.group(1)))
    except Exception:
        return {}


def plugin_folders(text):
    """PLUGINS = {key: folder} of a damsuite versions.py text."""
    m = re.search(r"^PLUGINS\s*=\s*(\{[^}]*\})", text or "", re.M)
    if not m:
        return {}
    import ast
    try:
        return dict(ast.literal_eval(m.group(1)))
    except Exception:
        return {}


def changelog_versions(text):
    """The versions of a metadata.txt changelog= section ("changelog=3.10.53 (...", "    2.18.54
    - ...", "    3.10.52 (..."), in order."""
    out, inside = [], False
    for line in (text or "").splitlines():
        if line.startswith("changelog="):
            inside = True
            line = line[len("changelog="):]
            if not line.strip():
                continue
            line = "    " + line
        elif inside and line and not line[0].isspace():
            break
        if inside:
            m = re.match(r"^\s{2,}(\d+\.\d+\.\d+)\s*[-(]", line)
            if m:
                out.append(m.group(1))
    return out


def changelog_md_entries(text):
    """{"## section"} and the bold titles of the bullets of docs/CHANGELOG.md."""
    out = set()
    for line in (text or "").splitlines():
        if line.startswith("## "):
            out.add(line.strip())
        m = re.match(r"^- \*\*(.+?)\*\*", line)
        if m:
            out.add(m.group(1).strip())
    return out


def check_version_bookkeeping(problems, root=None):
    """RELEASE == metadata.txt version for each plugin it names (shared master and every vendored
    copy); Hazard constants.PLUGIN_VERSION and README.txt line == its metadata.txt."""
    root = root or ROOT
    shared = os.path.join(root, "shared", "damsuite", "versions.py")
    if not os.path.isfile(shared):
        _fail(problems, "shared/damsuite/versions.py missing")
        return
    master = _read(shared)
    rel, folders = release_dict(master), plugin_folders(master)
    if not rel:
        _fail(problems, "shared/damsuite/versions.py: no RELEASE dict")
    for key, ver in sorted(rel.items()):
        folder = folders.get(key)
        meta = os.path.join(root, folder or "", "metadata.txt")
        if not folder or not os.path.isfile(meta):
            _fail(problems, "RELEASE[{!r}]: no plugin folder / metadata.txt".format(key))
            continue
        mv = metadata_version(_read(meta))
        if mv != ver:
            _fail(problems, "RELEASE[{!r}] = {} but {}/metadata.txt version={} (bump both; "
                            "tools/sync_damsuite.py, --write-pin)".format(key, ver, folder, mv))
    for plugin in sorted(set(folders.values())):
        vend = os.path.join(root, plugin, "damsuite", "versions.py")
        if os.path.isfile(vend):
            vr = release_dict(_read(vend))
            if vr != rel:
                _fail(problems, "{}/damsuite/versions.py RELEASE {} != shared {} (run "
                                "tools/sync_damsuite.py)".format(plugin, vr, rel))
    hz = os.path.join(root, "DamSafety_Hazard")
    if os.path.isdir(hz):
        mv = metadata_version(_read(os.path.join(hz, "metadata.txt")))
        const = os.path.join(hz, "constants.py")
        if os.path.isfile(const):
            m = re.search(r'^PLUGIN_VERSION\s*=\s*["\']([^"\']+)', _read(const), re.M)
            if not m or m.group(1) != mv:
                _fail(problems, "DamSafety_Hazard/constants.py PLUGIN_VERSION {} != metadata.txt "
                                "{}".format(m.group(1) if m else None, mv))
        readme = os.path.join(hz, "README.txt")
        if os.path.isfile(readme):
            m = re.search(r"- Version (\S+)", _read(readme).split("\n", 1)[0])
            if m and m.group(1) != mv:
                _fail(problems, "DamSafety_Hazard/README.txt says Version {} but metadata.txt "
                                "{}".format(m.group(1), mv))
    for plugin in PLUGINS:
        meta = os.path.join(root, plugin, "metadata.txt")
        if os.path.isfile(meta):
            text = _read(meta)
            cv = changelog_versions(text)
            mv = metadata_version(text)
            if cv and mv and _ver_tuple(cv[0]) > _ver_tuple(mv):
                _fail(problems, "{}: changelog's newest entry {} is above version={}".format(
                    plugin, cv[0], mv))


def _git_show(root, ref, rel):
    try:
        r = subprocess.run(["git", "show", "{}:{}".format(ref, rel)], cwd=root,
                           capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    return r.stdout if r.returncode == 0 else None


def base_ref(root=None):
    """The base to compare with: CI_BASE_REF, else origin/main; None when it is not there."""
    root = root or ROOT
    ref = os.environ.get("CI_BASE_REF") or "origin/main"
    try:
        r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref + "^{commit}"],
                           cwd=root, capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    return ref if r.returncode == 0 else None


def check_against_base(problems, root=None, ref=None):
    """No plugin version lower than on the base branch; no changelog entry of the base dropped
    (metadata.txt changelog= versions, docs/CHANGELOG.md bullet titles and sections)."""
    root = root or ROOT
    ref = ref or base_ref(root)
    if not ref:
        print("  base branch (CI_BASE_REF / origin/main) not available; skipping version and "
              "changelog comparison (git fetch origin main)")
        return
    print("  comparing with " + ref)
    for plugin in PLUGINS:
        rel = plugin + "/metadata.txt"
        old = _git_show(root, ref, rel)
        path = os.path.join(root, rel)
        if old is None or not os.path.isfile(path):
            continue
        new = _read(path)
        ov, nv = metadata_version(old), metadata_version(new)
        if ov and nv and _ver_tuple(nv) < _ver_tuple(ov):
            _fail(problems, "{}: version {} is lower than {} on {} (versions only go up; "
                            "renumber above it)".format(plugin, nv, ov, ref))
        gone = [v for v in changelog_versions(old) if v not in set(changelog_versions(new))]
        if gone:
            _fail(problems, "{}: changelog entries on {} are missing: {}".format(
                plugin, ref, ", ".join(gone[:8]) + (" ..." if len(gone) > 8 else "")))
    old = _git_show(root, ref, "shared/damsuite/versions.py")
    if old is not None:
        orel = release_dict(old)
        nrel = release_dict(_read(os.path.join(root, "shared", "damsuite", "versions.py")))
        for k, v in sorted(orel.items()):
            if k in nrel and _ver_tuple(nrel[k]) < _ver_tuple(v):
                _fail(problems, "shared/damsuite RELEASE[{!r}] {} is lower than {} on {}".format(
                    k, nrel[k], v, ref))
    old = _git_show(root, ref, "docs/CHANGELOG.md")
    path = os.path.join(root, "docs", "CHANGELOG.md")
    if old is not None and os.path.isfile(path):
        gone = sorted(changelog_md_entries(old) - changelog_md_entries(_read(path)))
        if gone:
            _fail(problems, "docs/CHANGELOG.md: {} entr(ies) on {} are missing or retitled: {}"
                  .format(len(gone), ref, "; ".join(g[:70] for g in gone[:5])))


def check_compile(problems):
    import py_compile
    n = 0
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for fn in files:
            if fn.endswith(".py"):
                try:
                    py_compile.compile(os.path.join(base, fn), doraise=True)
                    n += 1
                except py_compile.PyCompileError as e:
                    _fail(problems, "syntax error: " + str(e).splitlines()[-1])
    print("  compiled {} .py files".format(n))


def check_fork_parity(problems):
    """Safety core identical across the DamSafety and nd_hazard forks — only when both are
    checked out (separate repos), otherwise a graceful skip."""
    import check_fork_parity as cfp
    nd = os.environ.get("ND_HAZARD_ROOT", "/home/user/nd_hazard")
    if not os.path.isdir(nd):
        print("  nd_hazard checkout not present; skipping cross-fork parity (set ND_HAZARD_ROOT)")
        return
    for p in cfp.check(nd):
        _fail(problems, "fork parity: " + p)


def check_zone_colours(problems):
    """REG_05.2026 Fig. 5.1 / 5.2 danger-zone colours in every copy (text check, no QGIS)."""
    import importlib.util
    path = os.path.join(ROOT, "DamSafety_Hazard", "tests", "test_zone_colors_reg05.py")
    spec = importlib.util.spec_from_file_location("_zone_colours", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for p in mod.check_text(ROOT):
        _fail(problems, "zone colours: " + p)


def check_golden(problems):
    try:
        import numpy  # noqa: F401
    except Exception:
        print("  numpy not available; skipping golden baseline (run it in the suites job)")
        return
    sys.path.insert(0, ROOT)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from nd_breach_runner.tests.test_golden_baseline import \
            golden_builtin_engine_regulatory_numbers as g
        g()
    except Exception as e:
        _fail(problems, "golden baseline: {}: {}".format(type(e).__name__, e))


def main():
    problems = []
    for name, fn in (("damsuite drift", check_damsuite_drift),
                     ("damsuite pin", check_damsuite_pin), ("versions", check_versions),
                     ("version bookkeeping", check_version_bookkeeping),
                     ("versions and changelogs vs main", check_against_base),
                     ("cross-plugin identical", check_cross_plugin_identical),
                     ("shared/ui drift", check_ui_drift), ("window colours", check_window_colours),
                     ("zone colours (REG_05.2026)", check_zone_colours),
                     ("fork parity", check_fork_parity),
                     ("compile", check_compile), ("golden baseline", check_golden)):
        print("[ci] " + name)
        try:
            fn(problems)
        except Exception as e:      # a check itself blew up
            _fail(problems, "{} check errored: {}: {}".format(name, type(e).__name__, e))
    if problems:
        print("\nFAILED ({} problem(s)):".format(len(problems)))
        for p in problems:
            print("  - " + p)
        return 1
    print("\nAll fast checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
