"""Audit 2026-10-19 R-H1: the Linux LISFLOOD-FP build script that the Suite status dock starts
(DamSafety_Setup/scripts) applies the arrival-depth patch, and is the same file as the
Runner's (each plugin carries its own copy; nothing is imported across plugins).

    python -m pytest DamSafety_Setup/tests/test_build_lisflood_copy.py
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), "rb") as fh:
        return fh.read()


def test_setup_copy_matches_the_runner_scripts():
    for name in ("build_lisflood_linux.sh", "patch_lisflood.py"):
        assert _read("DamSafety_Setup", "scripts", name) == \
            _read("DamSafety_Runner", "scripts", name), name + " differs from the Runner's"


def test_setup_linux_script_patches_before_cmake():
    text = _read("DamSafety_Setup", "scripts", "build_lisflood_linux.sh").decode("utf-8")
    assert 0 < text.find('python3 "$HERE/patch_lisflood.py"') < text.find("cmake -S")
    assert "grep -q inittm_depth" in text
