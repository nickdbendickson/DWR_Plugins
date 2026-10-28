#!/usr/bin/env python3
"""tools/ci_checks.py version bookkeeping gates (Docs & QA 2026-10-21): RELEASE vs metadata.txt
vs Hazard constants.PLUGIN_VERSION, versions never lower than the base branch, no changelog entry
of the base dropped. Pure stdlib + git; builds a throw-away repo per test.

    python3 tools/test_ci_checks.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ci_checks as C  # noqa: E402

VERSIONS = '''PLUGINS = {"ndhazard": "DamSafety_Hazard",
           "ndbreach": "DamSafety_Runner"}
RELEASE = {"ndhazard": "%s", "ndbreach": "%s"}
'''
HZ_META = """[general]
name=DamSafety Hazard
version=%s
changelog=
%s
tags=x
"""
RN_META = """[general]
name=DamSafety Runner
version=%s
changelog=%s
tags=x
"""
CHANGELOG = """# Changelog

## 2026-10-21
%s
## 2026-10-20
- **Hazard 2.18.51: hazard tables.** text
"""


def _w(root, rel, text):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)


def _tree(root, hz="2.18.52", rn="3.10.53", hz_entries=("2.18.52", "2.18.51"),
          rn_entries=("3.10.53", "3.10.52"), bullets=("- **Runner 3.10.53: arrival.** text",),
          const=None, readme=None, vendored=None):
    _w(root, "shared/damsuite/versions.py", VERSIONS % (hz, rn))
    for plugin in ("DamSafety_Hazard", "DamSafety_Runner"):
        _w(root, plugin + "/damsuite/versions.py", VERSIONS % (vendored or (hz, rn)))
    _w(root, "DamSafety_Hazard/metadata.txt", HZ_META % (hz, "\n".join(
        "    {} - 2026-10-21 (x)\n      * y".format(v) for v in hz_entries)))
    _w(root, "DamSafety_Runner/metadata.txt", RN_META % (rn, "\n    ".join(
        "{} (2026-10-21; x)\n    * y".format(v) for v in rn_entries)))
    _w(root, "DamSafety_Hazard/constants.py", 'PLUGIN_VERSION = "%s"\n' % (const or hz))
    _w(root, "DamSafety_Hazard/README.txt",
       "ND Dam Hazard Classification Helper - Version %s\n" % (readme or hz))
    _w(root, "docs/CHANGELOG.md", CHANGELOG % "\n".join(bullets))


def _git(root, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root,
                   check=True, capture_output=True)


class Repo(object):
    """A repo whose first commit is 'main' (branch base), then the working tree is changed."""

    def __enter__(self):
        self.root = tempfile.mkdtemp(prefix="ci_checks_")
        _tree(self.root)
        _git(self.root, "init", "-q")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-qm", "main")
        _git(self.root, "branch", "-f", "basemain")
        return self.root

    def __exit__(self, *a):
        shutil.rmtree(self.root, ignore_errors=True)


def _problems(root, ref="basemain"):
    problems = []
    C.check_version_bookkeeping(problems, root)
    C.check_against_base(problems, root, ref)
    return problems


def test_consistent_bump_passes():
    with Repo() as root:
        assert _problems(root) == [], _problems(root)
        _tree(root, hz="2.18.54", hz_entries=("2.18.54", "2.18.52", "2.18.51"),
              bullets=("- **Hazard 2.18.54: inventory.** t", "- **Runner 3.10.53: arrival.** text"))
        assert _problems(root) == [], _problems(root)


def test_release_not_matching_metadata_fails():
    with Repo() as root:
        _tree(root)
        _w(root, "shared/damsuite/versions.py", VERSIONS % ("2.18.53", "3.10.53"))
        p = _problems(root)
        assert any("RELEASE['ndhazard'] = 2.18.53 but DamSafety_Hazard/metadata.txt version="
                   "2.18.52" in x for x in p), p
        assert any("DamSafety_Hazard/damsuite/versions.py RELEASE" in x for x in p), p


def test_hazard_plugin_version_and_readme_must_match():
    with Repo() as root:
        _tree(root, const="2.18.50", readme="2.18.49")
        p = _problems(root)
        assert any("PLUGIN_VERSION 2.18.50 != metadata.txt 2.18.52" in x for x in p), p
        assert any("README.txt says Version 2.18.49" in x for x in p), p


def test_version_lower_than_main_fails():
    with Repo() as root:
        _tree(root, hz="2.18.50", hz_entries=("2.18.50", "2.18.52", "2.18.51"))
        p = _problems(root)
        assert any("DamSafety_Hazard: version 2.18.50 is lower than 2.18.52" in x for x in p), p
        assert any("RELEASE['ndhazard'] 2.18.50 is lower" in x for x in p), p
        # the newest changelog entry above version= is caught too
        _tree(root, hz="2.18.52", hz_entries=("2.18.53", "2.18.52", "2.18.51"))
        assert any("newest entry 2.18.53 is above version=2.18.52" in x for x in _problems(root))


def test_dropped_changelog_entries_fail():
    with Repo() as root:
        # a merge that took the branch's side: main's 2.18.51 and 3.10.52 entries are gone
        _tree(root, hz="2.18.53", rn="3.10.56", hz_entries=("2.18.53", "2.18.52"),
              rn_entries=("3.10.56", "3.10.53"), bullets=("- **Hazard 2.18.53: x.** t",))
        p = _problems(root)
        assert any("DamSafety_Hazard: changelog entries on basemain are missing: 2.18.51" in x
                   for x in p), p
        assert any("DamSafety_Runner: changelog entries on basemain are missing: 3.10.52" in x
                   for x in p), p
        assert any("docs/CHANGELOG.md: 1 entr(ies)" in x and "Runner 3.10.53: arrival." in x
                   for x in p), p


def test_no_base_branch_is_a_notice_not_a_failure():
    with Repo() as root:
        old = os.environ.get("CI_BASE_REF")
        os.environ["CI_BASE_REF"] = "no-such-branch"
        try:
            assert C.base_ref(root) is None
            problems = []
            C.check_against_base(problems, root)
            assert problems == []
        finally:
            if old is None:
                os.environ.pop("CI_BASE_REF", None)
            else:
                os.environ["CI_BASE_REF"] = old


def test_parsers_on_the_real_tree():
    root = C.ROOT
    hz = C._read(os.path.join(root, "DamSafety_Hazard", "metadata.txt"))
    rn = C._read(os.path.join(root, "DamSafety_Runner", "metadata.txt"))
    assert C.changelog_versions(hz)[0] == C.metadata_version(hz)
    assert C.changelog_versions(rn)[0] == C.metadata_version(rn)
    assert len(C.changelog_md_entries(C._read(os.path.join(root, "docs", "CHANGELOG.md")))) > 10
    problems = []
    C.check_version_bookkeeping(problems)
    assert problems == [], problems


QT6_BAD = {
    "pkg/a_image.py": "from qgis.PyQt.QtGui import QImage\nimg = QImage(1, 2, QImage.Format_RGB32)\n",
    "pkg/a_io.py": "buf.open(QIODevice.WriteOnly)\n",
    "pkg/a_paint.py": "p.setRenderHint(QPainter.Antialiasing, True)\n",
    "pkg/a_net.py": ("s = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)\n"
                     "ok = (reply.error() == QNetworkReply.NoError)\n"),
    "pkg/a_role.py": "item.setData(Qt.UserRole, v)\n",
    "pkg/a_field.py": 'f = QgsField("x", 10)\ng = QgsField("y", QVariant.String)\n',
}
QT6_GOOD = {
    "pkg/b_scoped.py": (
        "a = QImage.Format.Format_RGB32\n"
        "b = QIODevice.OpenModeFlag.WriteOnly\n"
        "c = QPainter.RenderHint.Antialiasing\n"
        "d = QNetworkRequest.Attribute.HttpStatusCodeAttribute\n"
        "e = QNetworkReply.NetworkError.NoError\n"
        "g = Qt.ItemDataRole.UserRole\n"
        'h = QgsField("x", FT_STRING)\n'
        'i = QgsField("y", QMetaType.Type.QString)\n'),
    "pkg/b_guarded.py": (
        'fmt = QImage.Format_RGB32 if hasattr(QImage, "Format_RGB32") else None\n'
        "hint = QPainter.Antialiasing  # qt6-ok\n"),
    "pkg/b_prose.py": (
        '"""Docstring naming the Qt5 spellings QImage.Format_RGB32 and Qt.UserRole."""\n'
        "x = 1  # comment: QIODevice.WriteOnly and QNetworkReply.NoError\n"),
    "pkg/compat.py": "s = QVariant.String\nw = QIODevice.WriteOnly\n",
    "pkg/qgis_mock/qgis.py": "UserRole = 256\nVAL = QImage.Format_RGB32\n",
}


def test_qt6_enum_lint_fires_and_allows():
    root = tempfile.mkdtemp(prefix="qt6_lint_")
    try:
        for rel, text in dict(QT6_BAD, **QT6_GOOD).items():
            _w(root, rel, text)
        hits = C.scan_qt6_enums(root)
        hit_files = {h[0].replace(os.sep, "/") for h in hits}
        for rel in QT6_BAD:                       # every bad spelling is caught
            assert rel in hit_files, (rel, sorted(hit_files))
        for rel in QT6_GOOD:                      # scoped / compat / guarded / prose / mock: allowed
            assert rel not in hit_files, (rel, sorted(hit_files))
        labels = " ".join(h[2] for h in hits)     # both field-type forms and both network enums
        assert "bare-int" in labels and "QVariant type" in labels, labels
        assert "QNetworkRequest" in labels and "QNetworkReply" in labels, labels
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_qt6_enum_lint_clean_on_the_real_tree():
    hits = C.scan_qt6_enums(C.ROOT)
    assert hits == [], ["{}:{} {}".format(h[0], h[1], h[2]) for h in hits]


def test_qt6_metadata():
    problems = []                                 # every plugin of the real tree declares QGIS 4
    C.check_qt6_metadata(problems)
    assert problems == [], problems
    root = tempfile.mkdtemp(prefix="qt6_meta_")
    try:
        _w(root, "DamSafety_Hazard/metadata.txt",
           "[general]\nqgisMaximumVersion=4.99\nsupportsQt6=True\nversion=1.0.0\n")
        _w(root, "DamSafety_Runner/metadata.txt",          # a changelog mention does not count
           "[general]\nqgisMaximumVersion=3.99\nchangelog=1.0 - supportsQt6=True\n")
        _w(root, "DamSafety_PMP/metadata.txt", "[general]\nsupportsQt6=False\n")
        _w(root, "DamSafety_Data/metadata.txt",            # 4.0 would still hide it on QGIS 4.2
           "[general]\nqgisMaximumVersion=4.0\nsupportsQt6=True\n")
        C.check_qt6_metadata(problems, root)
        text = "\n".join(problems)
        for want in ("DamSafety_Runner: metadata.txt needs supportsQt6=True",
                     "DamSafety_Runner: metadata.txt qgisMaximumVersion=3.99",
                     "DamSafety_PMP: metadata.txt needs supportsQt6=True",
                     "DamSafety_PMP: metadata.txt qgisMaximumVersion=None",
                     "DamSafety_Data: metadata.txt qgisMaximumVersion=4.0"):
            assert want in text, (want, problems)
        assert len(problems) == 5, problems       # Hazard (both keys right) passes
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_mac_lisflood_patch_gate():
    problems = []                                 # the real tree: the 8.3 diff carries every edit
    C.check_mac_lisflood_patch(problems)
    assert problems == [], problems
    root = tempfile.mkdtemp(prefix="mac_patch_")
    try:
        real = open(os.path.join(C.ROOT, C.RUNNER_PATCHER), encoding="utf-8").read()
        _w(root, C.MAC_PATCH, open(os.path.join(C.ROOT, C.MAC_PATCH), encoding="utf-8").read())
        # a new Runner edit that the Mac diff does not carry
        _w(root, C.RUNNER_PATCHER, real.replace("EDITS = [", 'EDITS = [\n    ("pars.cpp", "a;", '
                                                '"a;\\n\\tnew_keyword_x;", "new_keyword_x"),', 1))
        C.check_mac_lisflood_patch(problems, root)
        assert len(problems) == 1 and "new_keyword_x" in problems[0], problems
        problems[:] = []                          # no Mac package: nothing to check
        os.remove(os.path.join(root, C.MAC_PATCH))
        C.check_mac_lisflood_patch(problems, root)
        assert problems == [], problems
    finally:
        shutil.rmtree(root, ignore_errors=True)

def test_merge_markers():
    problems = []                                 # the real tree is clean
    C.check_merge_markers(problems)
    assert problems == [], problems
    root = tempfile.mkdtemp(prefix="markers_")
    try:
        _w(root, "docs/CHANGELOG.md", "# Changelog\n" + "<" * 7 + " HEAD\n- a\n=======\n- b\n"
                                      + ">" * 7 + " f4fdb0c (x)\n")
        _w(root, "DamSafety_Hazard/README.txt", "Title\n=======\n")   # setext rule: fine
        _w(root, "shared/damsuite/versions.py", 'RELEASE = {"a": "1"}\nRELEASE = {"a": "1"}\n')
        C.check_merge_markers(problems, root)
        text = "\n".join(problems)
        assert len(problems) == 3, problems
        assert "docs/CHANGELOG.md:2" in text and "docs/CHANGELOG.md:6" in text, problems
        assert "2 'RELEASE =' lines" in text, problems
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception:
                fails += 1
                import traceback
                traceback.print_exc()
                print("FAIL", name)
    print("{} failed".format(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
