#!/usr/bin/env python3
"""The pictures of docs/USER_GUIDE.md (no QGIS needed).

    python3 tools/test_guide_shots.py

A picture the guide shows but that is not in the repository is a broken image for every reader;
a picture in the folder that no page shows, or that tools/guide_shots.py no longer makes, is one
that will never be retaken when its window changes. Each of the three lists (the tool's, the
folder's, the guide's) must be the same."""
import ast
import os
import re
import struct
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
GUIDE = os.path.join(REPO, "docs", "USER_GUIDE.md")
FOLDER = os.path.join(REPO, "docs", "img", "guide")
IMG = re.compile(r"!\[([^\]]*)\]\((img/guide/[^)\s]+)\)")


def tool_shots():
    """SHOTS of tools/guide_shots.py, read from its source (importing it would start QGIS)."""
    with open(os.path.join(HERE, "guide_shots.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "SHOTS":
            return dict(ast.literal_eval(node.value))
    raise AssertionError("tools/guide_shots.py has no SHOTS")


def guide_images():
    with open(GUIDE, encoding="utf-8") as fh:
        return [(alt, path) for alt, path in IMG.findall(fh.read())]


def png_size(path):
    with open(path, "rb") as fh:
        head = fh.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", path + " is not a PNG"
    return struct.unpack(">II", head[16:24])


class GuideShots(unittest.TestCase):
    def test_the_tool_the_folder_and_the_guide_name_the_same_pictures(self):
        made = set(tool_shots())
        on_disk = {n for n in os.listdir(FOLDER) if not n.startswith(".")}
        shown = {os.path.basename(p) for _a, p in guide_images()}
        self.assertEqual(on_disk, made, "docs/img/guide against tools/guide_shots.py SHOTS")
        self.assertEqual(shown, made, "docs/USER_GUIDE.md against tools/guide_shots.py SHOTS")

    def test_every_picture_says_what_it_shows(self):
        texts = tool_shots()
        for alt, path in guide_images():
            self.assertEqual(alt, texts[os.path.basename(path)], path)

    def test_the_pictures_are_whole_windows_and_not_huge(self):
        for name in tool_shots():
            path = os.path.join(FOLDER, name)
            w, h = png_size(path)
            # made on the 1080p virtual display: a window cut off at 800 x 600 is refused by the
            # tool, and is caught here if a picture is replaced by hand
            self.assertGreaterEqual(w, 900, name)
            self.assertLessEqual(w, 1920, name)
            self.assertLessEqual(h, 1080, name)
            self.assertLess(os.path.getsize(path), 400 * 1024, name + ": keep the repository light")

    def test_no_picture_shows_a_scratch_or_home_path(self):
        """The tool points the windows at <temp>/DamSafety/Dams so that a retake differs only
        where a window changed. This pins that line (the pixels cannot be read here)."""
        with open(os.path.join(HERE, "guide_shots.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('os.path.join(tempfile.gettempdir(), "DamSafety", "Dams")', src)
        self.assertIn('os.environ["HOME"] = tempfile.mkdtemp(', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
