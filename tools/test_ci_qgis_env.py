#!/usr/bin/env python3
"""tools/ci_qgis_env.py: the QGIS Python folder is found where conda-forge puts it on Windows,
and the path file it writes makes it importable. No QGIS needed."""
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ci_qgis_env as Q  # noqa: E402


def _touch(*parts):
    path = os.path.join(*parts)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w").close()


class CiQgisEnv(unittest.TestCase):
    def test_the_windows_layout_is_found(self):
        with tempfile.TemporaryDirectory() as env:
            _touch(env, "Library", "python", "qgis", "core", "__init__.py")
            _touch(env, "Library", "python", "plugins", "processing", "__init__.py")
            self.assertEqual(Q.find(env), os.path.join(env, "Library", "python"))

    def test_the_shortest_path_wins_and_nothing_is_none(self):
        with tempfile.TemporaryDirectory() as env:
            self.assertIsNone(Q.find(env))
            _touch(env, "Library", "pkgs", "old", "python", "qgis", "core", "__init__.py")
            _touch(env, "Library", "python", "qgis", "core", "__init__.py")
            self.assertEqual(Q.find(env), os.path.join(env, "Library", "python"))

    def test_a_folder_name_with_brackets_is_not_a_pattern(self):
        with tempfile.TemporaryDirectory() as top:
            env = os.path.join(top, "env [1]")
            _touch(env, "share", "qgis", "python", "qgis", "core", "__init__.py")
            self.assertEqual(Q.find(env), os.path.join(env, "share", "qgis", "python"))

    def test_the_line_names_the_prefix(self):
        py = os.path.join("C:" + os.sep + "env", "Library", "python")
        self.assertEqual(Q.lines(py), ["QGIS_PREFIX_PATH=" + os.path.dirname(py)])

    def test_the_path_file_makes_both_folders_importable_in_a_new_python(self):
        # as on the runner: a package in <env>/Library/python and one in its plugins folder
        # are found by a Python whose site-packages holds the path file, whatever PYTHONPATH is
        with tempfile.TemporaryDirectory() as top:
            py = os.path.join(top, "env", "Library", "python")
            _touch(py, "ciq_core_pkg", "__init__.py")        # stand-ins: a real qgis may be installed
            _touch(py, "plugins", "ciq_plugin_pkg", "__init__.py")
            site_dir = os.path.join(top, "site")
            os.makedirs(site_dir)
            pth = Q.write_pth(py, site_dir)
            self.assertEqual(os.path.basename(pth), "ci_qgis.pth")
            with open(pth, encoding="utf-8") as f:
                self.assertEqual(f.read().splitlines(), [py, os.path.join(py, "plugins")])
            env = dict(os.environ, PYTHONPATH="x:y:" + "z")      # an unusable value, as on Windows
            r = subprocess.run(
                [sys.executable, "-c",
                 "import site, sys; site.addsitedir(sys.argv[1]); import ciq_core_pkg, ciq_plugin_pkg; print('ok')",
                 site_dir], capture_output=True, text=True, env=env)
            self.assertEqual(r.stdout.strip(), "ok", r.stderr[-500:])


if __name__ == "__main__":
    unittest.main()
