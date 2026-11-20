"""Your shared defaults file in the desktop window (defaults.py, the pywebview bridge) and the assumptions entry the
hand-off writes into dam.json.

    python3 crest-desktop/tests/test_defaults.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

DESKTOP = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DESKTOP))

import crest.__main__ as m  # noqa: E402
from crest import defaults  # noqa: E402
from crest.handoff import write_dam_folder  # noqa: E402

GOOD = json.dumps({"format": "dam-defaults", "format_version": 1, "name": "Your defaults",
                   "values": {"manning_n": 0.05, "sunny_day_start_pool": "normal_pool"}}, indent=1)


def env(**values):
    clean = {k: v for k, v in os.environ.items() if k not in defaults.ENV_PATHS}
    clean.update(values)
    return mock.patch.dict(os.environ, clean, clear=True)


class PathTests(unittest.TestCase):
    def test_default_is_damsafety_in_the_home_folder(self):
        with tempfile.TemporaryDirectory() as home, env(HOME=home, USERPROFILE=home):
            self.assertEqual(defaults.user_path(), Path(home) / "DamSafety" / "defaults.json")

    def test_env_overrides_crest_first(self):
        with env(DAMSAFETY_DEFAULTS="/x/suite.json"):
            self.assertEqual(defaults.user_path(), Path("/x/suite.json"))
        with env(DAMSAFETY_DEFAULTS="/x/suite.json", CREST_DEFAULTS="/y/mine.json"):
            self.assertEqual(defaults.user_path(), Path("/y/mine.json"))


class ReadWriteTests(unittest.TestCase):
    def test_no_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "DamSafety" / "defaults.json"
            self.assertEqual(defaults.read_user(path), {"path": str(path), "text": None})

    def test_write_then_read_round_trip_creates_the_folder(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "DamSafety" / "defaults.json"
            self.assertEqual(defaults.write_user(GOOD, path), {"saved": str(path)})
            got = defaults.read_user(path)
            self.assertEqual(json.loads(got["text"])["values"], {"manning_n": 0.05, "sunny_day_start_pool": "normal_pool"})
            self.assertTrue(path.read_text(encoding="utf-8").endswith("\n"))
            self.assertEqual([p.name for p in path.parent.iterdir()], ["defaults.json"], "no temp file left behind")

    def test_reads_a_file_saved_with_a_byte_order_mark(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "defaults.json"
            path.write_text(GOOD, encoding="utf-8-sig")
            self.assertEqual(defaults.read_user(path)["text"], GOOD)

    def test_refuses_what_is_not_a_defaults_file_and_keeps_the_old_one(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "defaults.json"
            defaults.write_user(GOOD, path)
            before = path.read_text(encoding="utf-8")
            for bad in (
                "{",
                "[]",
                json.dumps({"format": "crest-desk", "format_version": 1, "values": {}}),
                json.dumps({"format": "dam-defaults", "format_version": 2, "values": {}}),
                json.dumps({"format": "dam-defaults", "format_version": 1, "values": []}),
                json.dumps({"format": "dam-defaults", "format_version": 1, "values": {"manning_n": True}}),
                json.dumps({"format": "dam-defaults", "format_version": 1, "values": {"manning_n": {"a": 1}}}),
                json.dumps({"format": "dam-defaults", "format_version": 1, "values": {"x": "y" * 300_000}}),
                None,
                42,
            ):
                self.assertIn("error", defaults.write_user(bad, path), repr(bad)[:60])
                self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_an_oversized_or_unreadable_file_is_an_error_not_a_read(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "defaults.json"
            path.write_bytes(b" " * (defaults.MAX_BYTES + 1))
            self.assertIn("too large", defaults.read_user(path)["error"])
            path.write_bytes(b"\xff\xfe\xff")
            self.assertIn("cannot be read", defaults.read_user(path)["error"])

    def test_the_bridge_reads_and_writes_your_file(self):
        with tempfile.TemporaryDirectory() as d, env(CREST_DEFAULTS=str(Path(d) / "mine.json")):
            api = m.Api()
            self.assertIsNone(api.get_defaults()["text"])
            self.assertEqual(api.save_defaults(GOOD), {"saved": str(Path(d) / "mine.json")})
            self.assertEqual(json.loads(api.get_defaults()["text"])["values"]["manning_n"], 0.05)
            self.assertIn("error", api.save_defaults("not json"))


DAM = {"id": "d1", "nid": "ND00199", "name": "Test", "heightFt": 27.5, "storageAcFt": 1612.59, "lengthFt": 1700,
       "method": "froehlich", "failureMode": "piping"}
ASSUME = {"used": {"sunny_day_start_pool": "normal_pool", "manning_n": 0.06}, "differs": {"sunny_day_start_pool": {"used": "normal_pool", "shared": "max_pool"}},
          "shared_values": {"sunny_day_start_pool": "max_pool", "manning_n": 0.06}, "tool": "Crest", "version": "1+abc", "when": "2026-11-13T18:00:00"}
ANALYSIS = {"datum": {"crestNavdFt": 1071.09, "streambedNavdFt": 1043.59, "poolNavdFt": 1061.09},
            "breach": {"peakCfs": 9000, "widthFt": 80, "series": [], "startPool": "normal", "startPoolNavdFt": 1061.09, "startPoolLabel": "normal pool"},
            "screen": {"level": "Low", "label": "Low", "reasons": []}, "assumptions": ASSUME}


class HandoffAssumptionsTests(unittest.TestCase):
    def read(self, folder: Path) -> dict:
        return json.loads((folder / "dam.json").read_text(encoding="utf-8"))

    def test_the_hand_off_writes_the_assumptions_entry_and_the_starting_pool(self):
        with tempfile.TemporaryDirectory() as d:
            folder, _ = write_dam_folder(Path(d), DAM, ANALYSIS)
            rec = self.read(folder)
            self.assertEqual(rec["assumptions"], ASSUME)
            result = rec["status"][-1]["result"]
            self.assertEqual(result["assumptions"], ASSUME)
            self.assertEqual(result["starting_pool_ft"], 1061.09)
            self.assertEqual(result["starting_pool_rule"], "normal pool")

    def test_crest_replaces_its_own_entry_but_keeps_another_tools(self):
        with tempfile.TemporaryDirectory() as d:
            folder, _ = write_dam_folder(Path(d), DAM, ANALYSIS)
            newer = {**ASSUME, "when": "2026-11-14T08:00:00"}
            write_dam_folder(Path(d), DAM, {**ANALYSIS, "assumptions": newer})
            self.assertEqual(self.read(folder)["assumptions"]["when"], "2026-11-14T08:00:00")
            rec = self.read(folder)
            rec["assumptions"] = {"tool": "damsafety_run", "used": {"manning_n": 0.05}}
            (folder / "dam.json").write_text(json.dumps(rec), encoding="utf-8")
            _, notes = write_dam_folder(Path(d), DAM, ANALYSIS)
            rec = self.read(folder)
            self.assertEqual(rec["assumptions"]["tool"], "damsafety_run")
            self.assertEqual(rec["status"][-1]["result"]["assumptions"], ASSUME)
            self.assertTrue(any("Kept the assumptions entry damsafety_run" in n for n in notes))

    def test_an_older_desk_without_the_entry_writes_none(self):
        with tempfile.TemporaryDirectory() as d:
            old = {k: v for k, v in ANALYSIS.items() if k != "assumptions"}
            old["breach"] = {"peakCfs": 9000, "widthFt": 80, "series": []}
            folder, _ = write_dam_folder(Path(d), DAM, old)
            rec = self.read(folder)
            self.assertNotIn("assumptions", rec)
            self.assertNotIn("assumptions", rec["status"][-1]["result"])
            self.assertNotIn("starting_pool_ft", rec["status"][-1]["result"])


if __name__ == "__main__":
    unittest.main()
