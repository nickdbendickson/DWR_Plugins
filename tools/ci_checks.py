#!/usr/bin/env python3
"""Fast, QGIS-free merge gate. Runs the checks that need no QGIS/GDAL install and are fully
deterministic, so they can gate every PR cheaply:

  1. no vendored copy — DamSafety Core 1.0.0: every plugin's damsuite/ is exactly the two-line
                        shim onto DamSafety_Core.damsuite (no other file in the folder), every
                        plugin copy of a shared window file is the three-line shim onto
                        DamSafety_Core.ui, and the Core's damsuite/FILES.json is current
                        (tools/damsuite_files.py); every plugin but the Core names
                        plugin_dependencies=DamSafety Core
  2. version sanity   — each plugin metadata.txt has a well-formed version=
     version book      — DamSafety_Core/damsuite/versions.py RELEASE equals each plugin's
                        metadata.txt version; Hazard's constants.PLUGIN_VERSION and README.txt
                        version line agree with its metadata.txt
     qt6 metadata      — every plugin metadata.txt declares supportsQt6=True and a
                        qgisMaximumVersion of 4.99 or later (QGIS 4 / Qt6)
     vs main           — against the base branch (CI_BASE_REF, default origin/main; skipped with a
                        notice when it is not fetched): no plugin version is lower, and no
                        changelog entry of the base (metadata.txt changelog= versions,
                        docs/CHANGELOG.md bullet titles and date sections) is missing
  3. compile          — py_compile every .py in the suite (catches a syntax error from a bad
                        merge across all plugins in seconds, without importing anything)
  4. window colours   — no window code writes a #rrggbb colour outside tokens.py
                        (docs/gui-grammar.md)
  5. zone colours     — the ACER TM 11 danger zones use REG_05.2026 Fig. 5.1 / 5.2's colours in
                        every style, chart and copy (DamSafety_Hazard/tests/test_zone_colors_reg05.py)
  6. golden baseline  — the built-in flood engine still reproduces the frozen regulatory numbers
                        (needs numpy, not scipy; skipped with a notice if numpy is absent)
  7. mac LISFLOOD     — tools/mac/lisflood/lisflood-8.3-ndbreach.patch (the Mac build's fallback
                        when the Runner's patcher does not apply) still adds every line the
                        Runner's scripts/patch_lisflood.py adds
  8. merge markers   — no line matching ^(<<<<<<<|=======$|>>>>>>>) (git's conflict markers,
                        the bare ======= separator included) in any tracked text file, and one
                        RELEASE = line in the Core's damsuite versions.py (a bad stacked merge)
  9. metadata format — every plugin metadata.txt reads the way QGIS reads it (configparser
                        with % interpolation: a bare % makes the value unreadable, so the Plugin
                        Manager shows an empty changelog); an indented continuation line only
                        under changelog= / description= / about= (one after another key joins
                        that key's value: hasProcessingProvider= became 'yes' + a changelog
                        entry and qgis_process skipped the provider); one PLUGIN_VERSION in
                        Hazard's constants.py

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
CORE = "DamSafety_Core"                       # the one copy of the shared code (Core 1.0.0)
CORE_VERSIONS = os.path.join(CORE, "damsuite", "versions.py")
# plugins whose damsuite/ is the shim onto the Core (Setup's used to be a three-module mini copy)
DAMSUITE_SHIM_PLUGINS = ("DamSafety_Hazard", "DamSafety_Runner", "DamSafety_Screening",
                         "DamSafety_Data", "DamSafety_Setup")
# {shared window file: plugins that carry the three-line shim onto DamSafety_Core/ui/<file>}
_HRS = ("DamSafety_Hazard", "DamSafety_Runner", "DamSafety_Screening")
UI_SHIMS = {
    "tokens.py": _HRS + ("DamSafety_Data", "DamSafety_Setup", "DamSafety_HazardLite", "DamSafety_PMP"),
    "ui_widgets.py": _HRS + ("DamSafety_Setup", "DamSafety_HazardLite", "DamSafety_PMP"),
    "ui_fit.py": _HRS + ("DamSafety_Data", "DamSafety_HazardLite", "DamSafety_PMP"),
    "suite_settings.py": _HRS,
    "suite_settings_tools.py": _HRS,
    "suite_styles.py": _HRS,
    "suite_home.py": _HRS,
    "tool_dialog.py": _HRS,
}
# Setup's plugin-root copies of damsuite modules: the same three-line shim onto the Core's damsuite
DAMSUITE_ROOT_SHIMS = {"suite_config.py": ("DamSafety_Setup",), "suite_fill.py": ("DamSafety_Setup",),
                       "pack_convert.py": ("DamSafety_Setup",)}
UI_SHIM_TEXT = "import sys\nfrom DamSafety_Core.ui import {name} as _m\nsys.modules[__name__] = _m\n"
DS_SHIM_TEXT = "import sys\nfrom DamSafety_Core.damsuite import {name} as _m\nsys.modules[__name__] = _m\n"
# the damsuite/__init__.py shim: a docstring, then exactly these two statements and nothing else
DAMSUITE_SHIM_CODE = ("from DamSafety_Core import alias as _alias", "_alias.install(__name__)")

# plugin-root files copied verbatim into more than one plugin (not shared via damsuite): they must
# stay byte-identical so the two tools apply the same rule. {filename: (plugins that carry it)}.
IDENTICAL_ACROSS_PLUGINS = {
    "crest_rule.py": ("DamSafety_Runner", "DamSafety_Screening"),  # one top-of-dam number for both
    # Runner 3.10.43 / Screening 0.17.35: gdal.Warp without the source overviews (-ovr NONE)
    "core/warp_opts.py": ("DamSafety_Runner", "DamSafety_Screening"),
    # Runner 3.10.89 / Screening 0.17.50: one rule for the elevation unit a DEM records (the
    # Runner's 'Detect from the raster' and what Download DEM tags)
    "core/dem_zunit.py": ("DamSafety_Runner", "DamSafety_Screening"),
    # audit R-H1: the Suite status dock's Linux LISFLOOD-FP build applies the same patch
    "scripts/build_lisflood_linux.sh": ("DamSafety_Runner", "DamSafety_Setup"),
    "scripts/patch_lisflood.py": ("DamSafety_Runner", "DamSafety_Setup"),
    # Data 1.5.0: the Hazard's pack engine, vendored so Data's "Build offline pack…" downloads
    # exactly what the Hazard's build_nd_pack.py does. Suite 1.18.6: build_nd_pack.py and
    # dwr_files.py are shims over damsuite/packengine.py / dwrfiles.py (edit DamSafety_Core/damsuite/)
    "build_nd_pack.py": ("DamSafety_Hazard", "DamSafety_Data"),
    "constants.py": ("DamSafety_Hazard", "DamSafety_Data"),
    "osm_parse.py": ("DamSafety_Hazard", "DamSafety_Data"),
    "dwr_files.py": ("DamSafety_Hazard", "DamSafety_Data"),
    "offline_pack.py": ("DamSafety_Hazard", "DamSafety_Data"),
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


MAC_PATCH = "tools/mac/lisflood/lisflood-8.3-ndbreach.patch"
RUNNER_PATCHER = "DamSafety_Runner/scripts/patch_lisflood.py"


def patch_added_lines(text, prefix="LISFLOOD-FP-8.3/source/"):
    """{source-relative file: set of stripped '+' lines} of a unified diff."""
    added, cur = {}, None
    for line in text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].split("\t")[0].strip()
            path = path[2:] if path.startswith("b/") else path
            cur = path[len(prefix):] if path.startswith(prefix) else path
            added.setdefault(cur, set())
        elif line.startswith("+") and cur is not None:
            added[cur].add(line[1:].strip())
    return added


def check_mac_lisflood_patch(problems, root=None):
    """The Mac build (tools/mac/lisflood/build_lisflood.sh) runs the Runner's own
    scripts/patch_lisflood.py and falls back to the bundled 8.3 diff: that diff must add every
    line the patcher's EDITS add, so a new Runner edit cannot be missing from a Mac build."""
    import importlib.util
    root = root or ROOT
    patch, patcher = os.path.join(root, MAC_PATCH), os.path.join(root, RUNNER_PATCHER)
    if not os.path.isfile(patch):
        return                                   # no Mac package in this tree
    spec = importlib.util.spec_from_file_location("_runner_patch_lisflood", patcher)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)                 # defines EDITS and main(); runs nothing
    added = patch_added_lines(_read(patch))
    for rel, old, new, marker in mod.EDITS:
        olds = old if isinstance(old, tuple) else (old,)
        old_lines = {x.strip() for o in olds for x in o.splitlines()}
        have = added.get(rel, set())
        need = [x.strip() for x in new.splitlines() if x.strip() and x.strip() not in old_lines]
        missing = [x for x in need if x not in have]
        if missing or not any(marker in x for x in have):
            _fail(problems, "{} lacks the Runner patcher's edit of {} ({}): {}".format(
                MAC_PATCH, rel, marker, "; ".join(missing) or "marker not added"))


def _shim_statements(text):
    """The code statements of a shim module (docstring left out) as source lines, so a shim is
    judged by what it does rather than by its exact bytes."""
    import ast
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    body = list(tree.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None),
                                                               ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]                                   # the docstring
    return [ast.unparse(n) for n in body]


def damsuite_shim_problems(root=None):
    """Core 1.0.0 'no vendored copy': every plugin's damsuite/ holds exactly __init__.py (plus a
    __pycache__), and that file does nothing but install the alias onto DamSafety_Core."""
    root = root or ROOT
    out = []
    for plugin in DAMSUITE_SHIM_PLUGINS:
        folder = os.path.join(root, plugin, "damsuite")
        if not os.path.isdir(folder):
            if os.path.isdir(os.path.join(root, plugin)):
                out.append("{}/damsuite/ missing (the shim onto DamSafety_Core)".format(plugin))
            continue
        names = sorted(n for n in os.listdir(folder) if n != "__pycache__")
        extra = [n for n in names if n != "__init__.py"]
        if extra:
            out.append("{}/damsuite/ holds {} - a vendored copy; only the __init__.py shim may be "
                       "there (the code lives in DamSafety_Core/damsuite/)".format(
                           plugin, ", ".join(extra[:6]) + (" ..." if len(extra) > 6 else "")))
        init = os.path.join(folder, "__init__.py")
        if not os.path.isfile(init):
            out.append("{}/damsuite/__init__.py missing".format(plugin))
            continue
        stmts = _shim_statements(_read(init))
        if stmts != list(DAMSUITE_SHIM_CODE):
            out.append("{}/damsuite/__init__.py is not the alias shim (expected exactly: {})".format(
                plugin, "; ".join(DAMSUITE_SHIM_CODE)))
    core = os.path.join(root, CORE, "damsuite")
    if not os.path.isdir(core):
        out.append("{}/damsuite/ missing (the one copy of the shared package)".format(CORE))
    return out


def ui_shim_problems(root=None):
    """Every plugin copy of a shared window file (and Setup's root copies of damsuite modules)
    is the three-line sys.modules shim onto the Core's module."""
    root = root or ROOT
    out = []
    for table, shim, sub in ((UI_SHIMS, UI_SHIM_TEXT, "ui"),
                             (DAMSUITE_ROOT_SHIMS, DS_SHIM_TEXT, "damsuite")):
        for name in sorted(table):
            for plugin in table[name]:
                if not os.path.isdir(os.path.join(root, plugin)):
                    continue
                path = os.path.join(root, plugin, name)
                if not os.path.isfile(path):
                    out.append("{}/{} missing (the shim onto DamSafety_Core/{})".format(
                        plugin, name, sub))
                elif _read(path) != shim.format(name=name[:-3]):
                    out.append("{}/{} is not the three-line shim onto the Core's module (edit "
                               "DamSafety_Core/{}/{} instead)".format(plugin, name, sub, name))
    return out


def check_no_vendored_copy(problems, root=None):
    """Core 1.0.0: one copy of the shared code. Shims only in the plugins; the Core's
    FILES.json current; every plugin names the Core as a dependency."""
    root = root or ROOT
    for p in damsuite_shim_problems(root):
        _fail(problems, "vendored copy: " + p)
    for p in ui_shim_problems(root):
        _fail(problems, "vendored copy: " + p)
    core_pkg = os.path.join(root, CORE, "damsuite")
    if os.path.isdir(core_pkg):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import damsuite_files
        for p in damsuite_files.check(core_pkg):
            _fail(problems, "damsuite FILES.json: " + p)
    for plugin in PLUGINS:
        meta = os.path.join(root, plugin, "metadata.txt")
        if not os.path.isfile(meta):
            continue
        m = re.search(r"^plugin_dependencies\s*=\s*(.*)$", _read(meta), re.M)
        if not m or "DamSafety Core" not in [x.strip() for x in m.group(1).split(",")]:
            _fail(problems, "{}: metadata.txt needs plugin_dependencies=DamSafety Core (its "
                            "damsuite is the Core's)".format(plugin))


# Window code: the files that build windows. A colour in them comes from tokens.py
# (docs/gui-grammar.md); maps, reports and charts keep their own palettes and are not listed.
WINDOW_FILES = ("dialog.py", "wizard.py", "popup_logic.py", "settings_dialog.py", "status_dock.py",
                "plugin.py", "suite_settings_tab.py", "classify_tab.py", "review_tab.py",
                "facility_review_tab.py", "data_tab.py", "datafolder_tab.py", "dsswise_tab.py",
                "extras_tab.py", "extras290_tab.py", "rasimport_tab.py", "suite_tab.py",
                "ui_check_panel.py", "constants.py", "streamstats_dialog.py")
HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-zA-Z])")


def check_window_colours(problems):
    """No #rrggbb in window code outside tokens.py (the shared window files are checked in
    DamSafety_Core/ui; tokens.py is the one place a window colour is written)."""
    ui = os.path.join(ROOT, CORE, "ui")
    files = [os.path.join(ui, f) for f in os.listdir(ui)
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


def check_versions(problems):
    ver_re = re.compile(r"^\d+\.\d+\.\d+")
    for plugin in PLUGINS:
        meta = os.path.join(ROOT, plugin, "metadata.txt")
        if not os.path.isfile(meta):
            _fail(problems, "{}: metadata.txt missing".format(plugin))
            continue
        with open(meta, encoding="utf-8") as f:
            text = f.read()
        m = re.search(r"^version=(.+)$", text, re.M)
        if not m or not ver_re.match(m.group(1).strip()):
            _fail(problems, "{}: metadata.txt version not x.y.z".format(plugin))
        # a merge can leave a second version= / changelog= line behind: QGIS and
        # configparser (strict) refuse the file (DuplicateOptionError)
        for key in ("version", "changelog"):
            n = len(re.findall(r"^{}=".format(key), text, re.M))
            if n > 1:
                _fail(problems, "{}: metadata.txt has {} '{}=' lines (one only; older "
                      "entries go indented under changelog=)".format(plugin, n, key))


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
    """RELEASE == metadata.txt version for each plugin it names (the one versions.py, in the
    Core); Hazard constants.PLUGIN_VERSION and README.txt line == its metadata.txt."""
    root = root or ROOT
    shared = os.path.join(root, CORE_VERSIONS)
    if not os.path.isfile(shared):
        _fail(problems, CORE_VERSIONS + " missing")
        return
    master = _read(shared)
    rel, folders = release_dict(master), plugin_folders(master)
    if not rel:
        _fail(problems, CORE_VERSIONS + ": no RELEASE dict")
    for key, ver in sorted(rel.items()):
        folder = folders.get(key)
        meta = os.path.join(root, folder or "", "metadata.txt")
        if not folder or not os.path.isfile(meta):
            _fail(problems, "RELEASE[{!r}]: no plugin folder / metadata.txt".format(key))
            continue
        mv = metadata_version(_read(meta))
        if mv != ver:
            _fail(problems, "RELEASE[{!r}] = {} but {}/metadata.txt version={} (bump both)"
                  .format(key, ver, folder, mv))
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
    # the base may still carry versions.py at its pre-Core path (shared/damsuite/)
    old = _git_show(root, ref, CORE_VERSIONS.replace(os.sep, "/"))
    if old is None:
        old = _git_show(root, ref, "shared/damsuite/versions.py")
    new_path = os.path.join(root, CORE_VERSIONS)
    if old is not None and os.path.isfile(new_path):
        orel = release_dict(old)
        nrel = release_dict(_read(new_path))
        for k, v in sorted(orel.items()):
            if k in nrel and _ver_tuple(nrel[k]) < _ver_tuple(v):
                _fail(problems, "damsuite RELEASE[{!r}] {} is lower than {} on {}".format(
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


# ---- Qt6 enum hygiene -------------------------------------------------------
# PyQt6 / QGIS 4.x removed unscoped enum members from the class namespace, and QgsField no
# longer accepts a bare int / QVariant type. These exact unscoped spellings raise
# AttributeError / TypeError on Qt6 (they bit us in #131 and #134, and the lone911 NAIP clip).
# The scoped form (Class.EnumType.Member) and the compat constants (FT_*, WKB_*, ...) do NOT
# match these patterns, so ported code passes. compat modules, the qgis_mock test doubles and
# this gate's own files are skipped; an intentional Qt5-fallback line can carry "# qt6-ok".
_QT6_FORBIDDEN = [
    ("QImage unscoped format (use QImage.Format.Format_*)",
     re.compile(r"\bQImage\.Format_[A-Za-z0-9_]+")),
    ("QIODevice unscoped open mode (use QIODevice.OpenModeFlag.*)",
     re.compile(r"\bQIODevice\.(?:ReadOnly|WriteOnly|ReadWrite|Append|Truncate|Text|Unbuffered"
                r"|NewOnly|ExistingOnly)\b")),
    ("QPainter unscoped render hint (use QPainter.RenderHint.*)",
     re.compile(r"\bQPainter\.(?:Antialiasing|TextAntialiasing|SmoothPixmapTransform"
                r"|HighQualityAntialiasing|LosslessImageRendering|NonCosmeticDefaultPen"
                r"|VerticalSubpixelPositioning)\b")),
    ("QPainter unscoped composition mode (use QPainter.CompositionMode.*)",
     re.compile(r"\bQPainter\.CompositionMode_[A-Za-z0-9_]+")),
    ("QNetworkRequest unscoped attribute (use QNetworkRequest.Attribute.*)",
     re.compile(r"\bQNetworkRequest\.(?!Attribute\b)[A-Z][A-Za-z0-9_]*Attribute\b")),
    ("QNetworkReply unscoped error (use QNetworkReply.NetworkError.*)",
     re.compile(r"\bQNetworkReply\.(?!NetworkError\b)[A-Za-z]+Error\b")),
    ("Qt unscoped item-data role (use Qt.ItemDataRole.*)",
     re.compile(r"\bQt\.(?:UserRole|DisplayRole|EditRole|DecorationRole|ToolTipRole"
                r"|CheckStateRole|BackgroundRole|ForegroundRole|FontRole|TextAlignmentRole"
                r"|SizeHintRole|WhatsThisRole|StatusTipRole|InitialSortOrderRole)\b")),
    ("QgsField with a bare-int type (use a QMetaType.Type or compat FT_*)",
     re.compile(r"\bQgsField\(\s*[^,()]+,\s*\d+\s*[),]")),
    ("QgsField with a QVariant type (use a QMetaType.Type or compat FT_*)",
     re.compile(r"\bQgsField\([^,()]+,\s*QVariant\.")),
]
_QT6_SKIP_FILES = {"compat.py", "qtcompat.py", "ci_checks.py", "test_ci_checks.py"}


def _mask_strings_comments(src):
    """src with every string/comment token blanked to spaces (line structure preserved) so the
    regexes see only code, not prose in a docstring or comment. On any tokenize error (e.g. a
    Python 2 file) the source is returned unchanged."""
    import io
    import tokenize
    lines = src.split("\n")
    fstring = tuple(getattr(tokenize, n) for n in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
                    if hasattr(tokenize, n))
    blank_types = (tokenize.STRING, tokenize.COMMENT) + fstring
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except Exception:
        return src
    for t in toks:
        if t.type not in blank_types:
            continue
        (sr, sc), (er, ec) = t.start, t.end
        sr, er = sr - 1, er - 1
        if sr == er:
            lines[sr] = lines[sr][:sc] + " " * (ec - sc) + lines[sr][ec:]
        else:
            lines[sr] = lines[sr][:sc] + " " * (len(lines[sr]) - sc)
            for r in range(sr + 1, er):
                lines[r] = " " * len(lines[r])
            lines[er] = " " * ec + lines[er][ec:]
    return "\n".join(lines)


def scan_qt6_enums(root):
    """[(rel_path, line_no, label, text)] of unscoped Qt6-broken enum spellings under root.
    Strings/comments are masked (prose that merely names a Qt5 spelling is not a hit), and a line
    guarded by hasattr(...) or carrying '# qt6-ok' is a deliberate Qt5 fallback and is skipped."""
    hits = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "qgis_mock")]
        for fn in files:
            if not fn.endswith(".py") or fn in _QT6_SKIP_FILES:
                continue
            path = os.path.join(base, fn)
            try:
                with open(path, encoding="utf-8") as f:
                    src = f.read()
            except Exception:
                continue
            raw = src.split("\n")
            code = _mask_strings_comments(src).split("\n")
            for i, line in enumerate(code, 1):
                if "hasattr(" in raw[i - 1] or "# qt6-ok" in raw[i - 1]:
                    continue
                for label, rx in _QT6_FORBIDDEN:
                    if rx.search(line):
                        hits.append((os.path.relpath(path, root), i, label, raw[i - 1].strip()))
    return hits


def check_qt6_enums(problems, root=None):
    for rel, i, label, text in scan_qt6_enums(root or ROOT):
        _fail(problems, "qt6 enums: {}:{}: {} -> {}".format(rel, i, label, text))


def check_qt6_metadata(problems, root=None):
    """Every plugin metadata.txt declares supportsQt6=True and a qgisMaximumVersion of 4.99 or
    later (the suite convention). Without them QGIS 4 treats the plugin as QGIS 3 only (a missing
    maximum is capped at <minimum major>.99), and a maximum such as 4.0 still hides it on QGIS 4.2;
    PMP's own tests/test_qt6.py runs in no CI job, so this is the check that notices."""
    root = root or ROOT
    for plugin in PLUGINS:
        meta = os.path.join(root, plugin, "metadata.txt")
        if not os.path.isfile(meta):
            continue                    # check_versions reports a missing metadata.txt
        text = _read(meta)
        m = re.search(r"^supportsQt6\s*=\s*(.*)$", text, re.M)
        if not m or m.group(1).strip().lower() != "true":
            _fail(problems, "{}: metadata.txt needs supportsQt6=True (QGIS 4 / Qt6)".format(plugin))
        m = re.search(r"^qgisMaximumVersion\s*=\s*(.*)$", text, re.M)
        if not m or _ver_tuple(m.group(1).strip())[:2] < (4, 99):
            _fail(problems, "{}: metadata.txt qgisMaximumVersion={} (needs 4.99 or later)".format(
                plugin, m.group(1).strip() if m else None))


# ------------------------------------------------------------ merge debris ----------------
MARKER_EXTS = (".py", ".txt", ".md", ".json", ".ui", ".qml", ".yml", ".yaml", ".cfg", ".ini",
               ".toml", ".sh", ".csv", ".html", ".qrc", ".spec")


MARKER_RE = re.compile(r"^(<<<<<<<|=======$|>>>>>>>)")


def scan_merge_markers(root=None):
    """(rel, line_no, line) for every line matching MARKER_RE in a tracked text file: a line
    starting with seven '<' or '>', or exactly seven '=' (git's separator; a longer or
    shorter '=' rule such as README.txt's title underline is not one). A
    stacked-branch merge left these in docs/CHANGELOG.md, DamSafety_Hazard/README.txt and
    versions.py on main (2026-10-28); compile only catches the .py ones, and README.txt's first
    line was a marker, so the README version check above silently read nothing."""
    root = root or ROOT
    try:
        r = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True,
                           timeout=60)
        files = r.stdout.splitlines() if r.returncode == 0 else []
    except Exception:
        files = []
    if not files:
        files = [os.path.relpath(os.path.join(d, f), root)
                 for d, _, fs in os.walk(root) if "/.git" not in d for f in fs]
    out = []
    for rel in files:
        if not rel.lower().endswith(MARKER_EXTS):
            continue
        try:
            with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    if MARKER_RE.match(line.rstrip("\r\n")):
                        out.append((rel, i, line.rstrip()[:80]))
        except OSError:
            continue
    return out


def check_merge_markers(problems, root=None):
    root = root or ROOT
    for rel, i, line in scan_merge_markers(root):
        _fail(problems, "merge-conflict marker: {}:{}: {}".format(rel, i, line))
    path = os.path.join(root, CORE_VERSIONS)
    if os.path.isfile(path):
        n = len(re.findall(r"^RELEASE\s*=", _read(path), re.M))
        if n > 1:
            _fail(problems, "{}: {} 'RELEASE =' lines (one only; a merge left the "
                            "others)".format(CORE_VERSIONS, n))


MULTILINE_KEYS = ("changelog", "description", "about")


def metadata_continuations(text):
    """[(line_no, key, line)] of indented continuation lines under a metadata.txt key other
    than MULTILINE_KEYS (a translated 'about[de]' counts as 'about'). configparser joins such a
    line to the key's value."""
    out, key = [], None
    for i, line in enumerate((text or "").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith(("#", ";")):
            continue
        if line[:1] in (" ", "\t"):
            if key is not None and key.split("[", 1)[0].strip().lower() not in MULTILINE_KEYS:
                out.append((i, key, line.strip()[:80]))
            continue
        if line.startswith("["):
            key = None
            continue
        m = re.match(r"^([^=:]+)[=:]", line)
        key = m.group(1).strip() if m else None
    return out


def metadata_read_errors(text):
    """[message] of what QGIS's own read (qgis.utils / the Plugin Manager:
    configparser.ConfigParser(), strict, BasicInterpolation) cannot get from metadata.txt."""
    import configparser
    cp = configparser.ConfigParser()
    try:
        cp.read_string(text or "")
    except configparser.Error as e:
        return ["not readable: {}".format(str(e).splitlines()[0][:160])]
    out = []
    for sec in cp.sections():
        for opt in cp.options(sec):
            try:
                cp.get(sec, opt)
            except configparser.Error as e:
                out.append("[{}] {}: {} (write a percent sign as %%)".format(
                    sec, opt, type(e).__name__))
    return out


def check_metadata_format(problems, root=None):
    root = root or ROOT
    for plugin in PLUGINS:
        meta = os.path.join(root, plugin, "metadata.txt")
        if not os.path.isfile(meta):
            continue
        text = _read(meta)
        for msg in metadata_read_errors(text):
            _fail(problems, "{}/metadata.txt: {}".format(plugin, msg))
        for i, key, line in metadata_continuations(text):
            _fail(problems, "{}/metadata.txt:{}: indented line after {}= joins its value "
                            "(only changelog / description / about continue): {}".format(
                                plugin, i, key, line))
    const = os.path.join(root, "DamSafety_Hazard", "constants.py")
    if os.path.isfile(const):
        n = len(re.findall(r"^PLUGIN_VERSION\s*=", _read(const), re.M))
        if n > 1:
            _fail(problems, "DamSafety_Hazard/constants.py: {} PLUGIN_VERSION lines (one "
                            "only)".format(n))


def main():
    problems = []
    for name, fn in (("no vendored copy (DamSafety Core)", check_no_vendored_copy),
                     ("versions", check_versions),
                     ("version bookkeeping", check_version_bookkeeping),
                     ("versions and changelogs vs main", check_against_base),
                     ("cross-plugin identical", check_cross_plugin_identical),
                     ("window colours", check_window_colours),
                     ("zone colours (REG_05.2026)", check_zone_colours),
                     ("fork parity", check_fork_parity),
                     ("qt6 enums", check_qt6_enums),
                     ("qt6 metadata", check_qt6_metadata),
                     ("mac LISFLOOD patch vs the Runner's patcher", check_mac_lisflood_patch),
                     ("merge-conflict markers", check_merge_markers),
                     ("metadata format", check_metadata_format),
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
