"""On Linux, Crest asks pywebview for Qt when GTK's Python bindings are missing, so pywebview does not print its
harmless "GTK cannot be loaded ... No module named 'gi'" traceback first (install test 2026-11-21)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crest.__main__ import choose_linux_backend  # noqa: E402


def specs(*present):
    return lambda name: object() if name in present else None


class LinuxBackendTests(unittest.TestCase):
    def test_qt_without_gi_asks_for_qt(self):
        env: dict = {}
        self.assertEqual(choose_linux_backend(env, "linux", specs("PyQt6")), "qt")
        self.assertEqual(env, {"PYWEBVIEW_GUI": "qt"})

    def test_gtk_available_is_left_alone(self):
        env: dict = {}
        self.assertIsNone(choose_linux_backend(env, "linux", specs("PyQt6", "gi")))
        self.assertEqual(env, {})

    def test_your_own_choice_is_kept(self):
        env = {"PYWEBVIEW_GUI": "gtk"}
        self.assertIsNone(choose_linux_backend(env, "linux", specs("PyQt6")))
        self.assertEqual(env, {"PYWEBVIEW_GUI": "gtk"})

    def test_not_linux_or_no_qt(self):
        for platform in ("win32", "darwin"):
            env: dict = {}
            self.assertIsNone(choose_linux_backend(env, platform, specs("PyQt6")))
            self.assertEqual(env, {})
        env = {}
        self.assertIsNone(choose_linux_backend(env, "linux", specs()))
        self.assertEqual(env, {})


if __name__ == "__main__":
    unittest.main()
