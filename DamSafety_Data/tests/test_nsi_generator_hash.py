"""nsi_generator archive hash is non-fatal (finding L3).

DamSafety_Data/sources.py pinned the nsi_generator GitHub source to a reviewed commit AND an
ENFORCED SHA-256 of GitHub's codeload zip of that commit. GitHub may recompress those archive
zips at any time: that changes the bytes (not the content), so the pinned hash stops matching and
core.GitHubRepoSource.fetch() raises UpdateError ("not installed") -- i.e. a routine GitHub
recompress silently breaks a safety-relevant source.

The fix (matching the sibling nd_data_updater/sources.py) leaves NSI_GENERATOR_ZIP_SHA256 = None.
core.py still computes and LOGS the SHA-256 on every fetch (visible if it ever changes) but no
longer enforces it, so a recompress is non-fatal. Setting a hash again re-enables byte-for-byte
enforcement.

This test runs without QGIS: core.py is pure Python and sources.py imports only core.py. It
(1) checks the module constant and the built source carry no enforced hash, (2) drives
GitHubRepoSource.fetch() over a fake "recompressed" archive and confirms the source installs
without raising and still logs its SHA-256, and (3) as a regression guard, confirms a control
source WITH a (wrong) hash still refuses -- proving enforcement is what was removed, not the whole
check, and that the test would have caught the pre-fix behaviour.

Run:  python3 DamSafety_Data/tests/test_nsi_generator_hash.py
"""
import importlib
import os
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
REPO = os.path.dirname(PLUGIN)
if REPO not in sys.path:
    sys.path.insert(0, REPO)

sources = importlib.import_module("DamSafety_Data.sources")
core = importlib.import_module("DamSafety_Data.core")


class FakeHttp:
    """Stands in for the QGIS HTTP client: writes a GitHub-style archive zip to the download path.

    The payload bytes are arbitrary, modelling an archive whose compression (and therefore whose
    SHA-256) differs from any pinned value -- exactly what a GitHub recompress produces.
    """

    def __init__(self, payload=b"print('nsi-generator')\n"):
        self.payload = payload

    def download(self, url, zip_path, feedback):
        top = "nsi-generator-%s" % sources.NSI_GENERATOR_COMMIT   # GitHub wraps in one top folder
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(top + "/README.md", self.payload)
            zf.writestr(top + "/main.py", self.payload)


def _nsi_generator_source():
    srcs = sources.build_sources({"enabled": {"nsi_generator": True}})
    gen = [s for s in srcs if getattr(s, "id", None) == "nsi_generator"]
    assert gen, "build_sources did not build the nsi_generator source"
    return gen[0]


def check_constant_is_none():
    assert sources.NSI_GENERATOR_ZIP_SHA256 is None, (
        "NSI_GENERATOR_ZIP_SHA256 must be None (logged, not enforced); got %r"
        % (sources.NSI_GENERATOR_ZIP_SHA256,))


def check_built_source_has_no_hash():
    src = _nsi_generator_source()
    assert src.zip_sha256 is None, (
        "built nsi_generator source still carries an enforced hash: %r" % (src.zip_sha256,))
    assert src.commit == sources.NSI_GENERATOR_COMMIT, "the reviewed-commit pin must stay"


def check_recompress_is_nonfatal():
    """A recompressed archive (bytes differ from any pin) installs without raising, and the
    SHA-256 is still logged so a change is visible."""
    src = _nsi_generator_source()
    logs = []
    fb = core.Feedback(log=logs.append)
    remote = "git:" + sources.NSI_GENERATOR_COMMIT
    with tempfile.TemporaryDirectory() as staging:
        version = src.fetch(FakeHttp(), staging, fb, remote)
        assert os.path.exists(os.path.join(staging, "README.md")), "archive was not unpacked"
        assert os.path.exists(os.path.join(staging, "COMMIT.txt")), "COMMIT.txt not written"
    assert version == remote, "fetch returned %r, expected %r" % (version, remote)
    assert any("SHA-256" in m for m in logs), (
        "fetch must still log the archive SHA-256; logs were: %r" % (logs,))


def check_wrong_hash_still_refuses():
    """Regression guard: an enforced (wrong) hash still refuses the source. This is the pre-fix
    behaviour; it must remain reachable when a hash is set, and its presence proves this test
    would have failed before NSI_GENERATOR_ZIP_SHA256 was set to None."""
    src = core.GitHubRepoSource(
        "nsi_generator", "control", "USACE-NSI", "nsi-generator", "main",
        "nsi_generator_code", commit=sources.NSI_GENERATOR_COMMIT, zip_sha256="0" * 64)
    fb = core.Feedback(log=lambda m: None)
    remote = "git:" + sources.NSI_GENERATOR_COMMIT
    raised = False
    with tempfile.TemporaryDirectory() as staging:
        try:
            src.fetch(FakeHttp(), staging, fb, remote)
        except core.UpdateError:
            raised = True
    assert raised, "an enforced wrong hash should still refuse the source"


TESTS = [
    ("nsi_generator_hash_constant_is_none", check_constant_is_none),
    ("built_source_carries_no_enforced_hash", check_built_source_has_no_hash),
    ("github_recompress_is_nonfatal", check_recompress_is_nonfatal),
    ("enforced_wrong_hash_still_refuses", check_wrong_hash_still_refuses),
]


def main():
    failed = 0
    for name, fn in TESTS:
        try:
            fn()
            print("ok   -", name)
        except Exception as e:  # noqa: BLE001 - this is the test reporter
            failed += 1
            print("FAIL -", name, "::", e)
    print("\n%d passed, %d failed" % (len(TESTS) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
