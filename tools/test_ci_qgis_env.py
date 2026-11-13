#!/usr/bin/env python3
"""tools/ci_qgis_env.py: the QGIS Python folder is found where conda-forge puts it on Windows,
and the lines it prints are the ones GitHub Actions reads. No QGIS needed."""
import os
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

    def test_the_lines_name_the_folder_its_plugins_and_the_prefix(self):
        py = os.path.join("C:" + os.sep + "env", "Library", "python")
        got = Q.lines(py, "", ";")
        self.assertEqual(got[0], "PYTHONPATH=" + py + ";" + os.path.join(py, "plugins"))
        self.assertEqual(got[1], "QGIS_PREFIX_PATH=" + os.path.dirname(py))
        self.assertTrue(Q.lines(py, "D:\\x", ";")[0].endswith(";D:\\x"))


if __name__ == "__main__":
    unittest.main()
