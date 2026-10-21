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
