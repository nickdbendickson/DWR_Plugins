"""Audit 1.22 item 7: Data follows the suite data folder (into <suite>/structures/) without
touching the pack, the suite's manifest.json, or downloads already in the old folder.

Pure Python, no QGIS needed:  python -m DamSafety_Data.tests.test_suite_folder
(run from the folder that holds DamSafety_Data).
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import traceback

from DamSafety_Data import core, settings as S
from DamSafety_Data.sources import build_sources, SOURCE_INFO

TESTS = []


def register(f):
    TESTS.append(f)
    return f


class FakeQSettings:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def value(self, key, default=None, type=None):          # noqa: A002 - QgsSettings API
        v = self.values.get(key, default)
        return type(v) if (type is not None and v is not None) else v

    def setValue(self, key, value):                          # noqa: N802 - QgsSettings API
        self.values[key] = value


class Env:
    """A temp home: old default folder, a suite folder, no NDSUITE_DATA, no suite config."""
    def __enter__(self):
        self.root = tempfile.mkdtemp(prefix="ds_suite_")
        self.default = os.path.join(self.root, "Documents", "ND Structure Data")
        self.suite = os.path.join(self.root, "suite")
        os.makedirs(self.suite)
        self._saved = (S.DEFAULT_DATA_DIR, S.SUITE_CONFIG, os.environ.pop(S.SUITE_ENV, None))
        S.DEFAULT_DATA_DIR = self.default
        S.SUITE_CONFIG = os.path.join(self.root, "no_config.json")
        return self

    def __exit__(self, *exc):
        S.DEFAULT_DATA_DIR, S.SUITE_CONFIG, env = self._saved
        if env is not None:
            os.environ[S.SUITE_ENV] = env
        for dirpath, dirnames, _ in os.walk(self.root):
            for d in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, d), 0o755)
                except OSError:
                    pass
        shutil.rmtree(self.root, ignore_errors=True)

    def settings(self, **values):
        q = FakeQSettings({S.PREFIX + k: v for k, v in values.items()})
        return S.Settings(q), q


def _download(folder, name="nsi"):
    os.makedirs(os.path.join(folder, name), exist_ok=True)
    with open(os.path.join(folder, name, "nsi.gpkg"), "w") as f:
        f.write("x")


@register
def suite_folder_set_uses_its_structures_subfolder():
    with Env() as e:
        st, q = e.settings()
        q.values[S.SUITE_KEY] = e.suite
        folder, how, notice = st.resolve()
        assert folder == os.path.join(e.suite, "structures") and how == "suite", (folder, how)
        assert notice == ""
        assert st.data_dir == os.path.join(e.suite, "structures")
        assert not os.path.exists(folder)          # resolving never creates anything


@register
def no_suite_folder_keeps_the_old_default():
    with Env() as e:
        st, _ = e.settings()
        assert st.resolve()[:2] == (e.default, "default")


@register
def suite_folder_from_env_and_config_file_like_the_suite():
    with Env() as e:
        with open(S.SUITE_CONFIG, "w") as f:
            json.dump({S.SUITE_KEY: e.suite}, f)
        assert S.suite_folder(FakeQSettings()) == e.suite
        os.environ[S.SUITE_ENV] = os.path.join(e.root, "envsuite")
        try:
            assert S.suite_folder(FakeQSettings({S.SUITE_KEY: e.suite})) == \
                os.path.join(e.root, "envsuite")
        finally:
            del os.environ[S.SUITE_ENV]


@register
def suite_folder_that_is_the_pack_is_guarded():
    with Env() as e:
        pack = os.path.join(e.suite, "ND_Dam_Suite_Pack")
        os.makedirs(pack)
        st, q = e.settings()
        q.values[S.SUITE_KEY] = pack                # what Hazard's datafolder tab can save
        folder, how, notice = st.resolve()
        assert folder == e.default and how == "default", (folder, how)
        assert "offline pack" in notice and "read only" in notice
        assert os.listdir(pack) == []


@register
def renamed_pack_found_by_its_manifest_is_guarded():
    with Env() as e:
        pack = os.path.join(e.root, "my_pack_copy")
        os.makedirs(pack)
        with open(os.path.join(pack, "manifest.json"), "w") as f:
            json.dump({"format": S.PACK_FORMAT, "layout": "gpkg"}, f)
        assert S.in_pack(pack) and S.in_pack(os.path.join(pack, "structures"))
        assert S.resolve_data_dir("", pack)[1] == "default"
        # a suite folder whose manifest.json is the suite's own is not a pack
        with open(os.path.join(e.suite, "manifest.json"), "w") as f:
            json.dump({"datasets": {}, "updated": "2026-10-18T00:00:00"}, f)
        assert not S.in_pack(e.suite)
        assert S.resolve_data_dir("", e.suite)[1] == "suite"


@register
def folder_inside_a_pack_is_guarded_even_when_chosen_in_data_settings():
    with Env() as e:
        inside = os.path.join(e.root, "x", "nd_dam_suite_pack", "deep")
        folder, how, notice = S.resolve_data_dir(inside, "")
        assert folder == e.default and how == "default" and "read only" in notice


@register
def non_writable_suite_folder_is_guarded():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return                                      # root writes anywhere
    with Env() as e:
        os.chmod(e.suite, stat.S_IRUSR | stat.S_IXUSR)
        try:
            folder, how, notice = S.resolve_data_dir("", e.suite)
        finally:
            os.chmod(e.suite, 0o755)
        assert folder == e.default and how == "default", (folder, how)
        assert "Can't write" in notice


@register
def existing_old_downloads_are_kept_with_a_move_notice():
    with Env() as e:
        _download(e.default)
        folder, how, notice = S.resolve_data_dir("", e.suite)
        assert folder == e.default and how == "legacy", (folder, how)
        assert "move" in notice and os.path.join(e.suite, "structures") in notice
        assert os.path.isfile(os.path.join(e.default, "nsi", "nsi.gpkg"))    # nothing moved
        assert not os.path.exists(os.path.join(e.suite, "structures"))
        # an old 1.3.x manifest alone also counts as downloads
        shutil.rmtree(os.path.join(e.default, "nsi"))
        with open(os.path.join(e.default, "manifest.json"), "w") as f:
            json.dump({"nsi": {"version": "a"}}, f)
        assert S.resolve_data_dir("", e.suite)[1] == "legacy"


@register
def once_structures_has_data_the_suite_folder_wins():
    with Env() as e:
        _download(e.default)
        _download(os.path.join(e.suite, "structures"))
        folder, how, notice = S.resolve_data_dir("", e.suite)
        assert how == "suite" and notice == "", (how, notice)


@register
def explicit_data_setting_wins():
    with Env() as e:
        mine = os.path.join(e.root, "mine")
        st, q = e.settings(data_dir=mine)
        q.values[S.SUITE_KEY] = e.suite
        assert st.resolve()[:2] == (mine, "explicit")


@register
def stored_old_default_path_counts_as_not_set():
    with Env() as e:
        st, q = e.settings(data_dir=e.default)      # 1.3.x's dialog saved it on every Save
        q.values[S.SUITE_KEY] = e.suite
        assert st.resolve()[1] == "suite"
        st.data_dir = e.default
        assert q.values[S.PREFIX + "data_dir"] == ""
        st.data_dir = os.path.join(e.root, "picked")
        assert st.resolve()[:2] == (os.path.join(e.root, "picked"), "explicit")


@register
def data_folder_names_match_the_sources():
    cfg = {"enabled": {sid: True for sid in SOURCE_INFO}, "usa_structures_url": "https://x/y.zip"}
    folders = {s.folder for s in build_sources(cfg)}
    assert folders == set(S.DATA_FOLDERS), folders ^ set(S.DATA_FOLDERS)


class _Src:
    def __init__(self, sid):
        self.id = sid
        self.folder = sid


@register
def data_and_suite_manifests_coexist():
    with Env() as e:
        suite_manifest = {"datasets": {"DWR_dams": {"when": "2026-10-18T01:00:00"}},
                          "updated": "2026-10-18T01:00:00"}
        with open(os.path.join(e.suite, "manifest.json"), "w") as f:
            json.dump(suite_manifest, f)
        # even with Data pointed straight at the suite folder, the two files are separate
        for folder in (e.suite, os.path.join(e.suite, "structures")):
            os.makedirs(folder, exist_ok=True)
            up = core.Updater(folder, [_Src("nsi")], http=None)
            assert os.path.basename(up.manifest_path) == "structures_manifest.json"
            assert up.load_manifest() == {}         # the suite's manifest is not taken
            up._save_manifest({"nsi": {"version": "v1"}})
            assert up.load_manifest() == {"nsi": {"version": "v1"}}
        with open(os.path.join(e.suite, "manifest.json")) as f:
            assert json.load(f) == suite_manifest
        # and the suite writing its manifest leaves Data's alone
        with open(os.path.join(e.suite, "manifest.json"), "w") as f:
            json.dump({"datasets": {}, "updated": "later"}, f)
        assert core.Updater(e.suite, [], None).load_manifest() == {"nsi": {"version": "v1"}}


@register
def old_manifest_is_read_once_and_never_rewritten():
    with Env() as e:
        os.makedirs(e.default)
        old = os.path.join(e.default, "manifest.json")
        with open(old, "w") as f:
            json.dump({"nsi": {"version": "v0"}, "osm": {"version": "e1"}}, f)
        up = core.Updater(e.default, [], None)
        m = up.load_manifest()
        assert m == {"nsi": {"version": "v0"}, "osm": {"version": "e1"}}
        m["nsi"] = {"version": "v1"}
        up._save_manifest(m)
        with open(old) as f:
            assert json.load(f)["nsi"] == {"version": "v0"}     # untouched
        assert up.load_manifest()["nsi"] == {"version": "v1"}    # new file wins from now on


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print("ok    ", t.__name__)
        except Exception:                                    # noqa: BLE001
            failed += 1
            print("FAIL  ", t.__name__)
            traceback.print_exc()
    print(f"\n{len(TESTS)} tests, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
