"""Tripwire: the NOAA Atlas 14 core in DamSafety_PMP/noaa14/ is shared BYTE-IDENTICAL with DamIT PMP.

noaa14/SHARED.json lists the five shared files with their sha256 and their DamIT path. This test fails when a file
no longer matches, so nobody edits the DWR copy alone. With DAMIT_CHECKOUT=<a DamIT checkout> it also compares
the bytes with DamIT's copies. Pure stdlib: no GDAL, no QGIS.

    python3 -W error DamSafety_PMP/tests/test_atlas14_shared.py
    # after the same change was made in both repositories (paired branches of the same name):
    python3 DamSafety_PMP/tests/test_atlas14_shared.py --write --damit /path/to/damit
"""
import hashlib
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(os.path.dirname(HERE), "noaa14")
MANIFEST = os.path.join(CORE, "SHARED.json")
SHARED = ("atlas14.py", "temporal.py", "storm.py", "gisgrid.py", "gridstats.py")
DWR_ONLY = ("__init__.py", "SHARED.json")
DAMIT_REPO = "nickdbendickson/damit"
WHY = ("The five files in DamSafety_PMP/noaa14/ (atlas14, temporal, storm, gisgrid, gridstats) are the NOAA Atlas 14 "
       "core shared byte-identical with DamIT PMP (damit_pmp/<name>.py in {repo}). A change to one must be made the "
       "same way in both repositories, on paired branches of the same name (code flows DamIT -> DWR only; nothing "
       "from the AWA-derived ND PMP tool goes to DamIT). Then update SHARED.json with: python3 "
       "DamSafety_PMP/tests/test_atlas14_shared.py --write --damit <DamIT checkout>. DamIT's "
       "scripts/check_atlas14_shared.py checks the pair daily.").format(repo=DAMIT_REPO)


def sha256(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def sha256_lf(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest()


def load_manifest():
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)


class SharedCore(unittest.TestCase):
    def test_manifest_lists_the_five_files(self):
        m = load_manifest()
        self.assertEqual(sorted(f["file"] for f in m["files"]), sorted(SHARED), WHY)
        for f in m["files"]:
            self.assertEqual(f["damit_path"], "damit_pmp/" + f["file"], WHY)
        self.assertEqual(m["damit_repository"], DAMIT_REPO)

    def test_every_shared_file_matches_its_sha256(self):
        bad = []
        for f in load_manifest()["files"]:
            p = os.path.join(CORE, f["file"])
            got = sha256(p) if os.path.exists(p) else "missing"
            if got != f["sha256"]:
                crlf = got != "missing" and sha256_lf(p) == f["sha256"]
                bad.append("noaa14/{} (sha256 {}, SHARED.json {}){}".format(
                    f["file"], got[:12], f["sha256"][:12],
                    " - only its line ends differ: CR LF from a Windows checkout (git config core.autocrlf false, "
                    "then check the file out again)" if crlf else ""))
        self.assertFalse(bad, "changed: {}.\n{}".format("; ".join(bad), WHY))

    def test_nothing_else_in_the_shared_folder(self):
        extra = sorted(n for n in os.listdir(CORE) if n not in SHARED + DWR_ONLY and n != "__pycache__")
        self.assertFalse(extra, "noaa14/ holds only the five shared files, __init__.py and SHARED.json; DWR-only code "
                                "goes in the plugin folder. Found: {}".format(", ".join(extra)))

    @unittest.skipUnless(os.environ.get("DAMIT_CHECKOUT"), "set DAMIT_CHECKOUT=<a DamIT checkout> to compare bytes")
    def test_same_bytes_as_damit(self):
        root = os.environ["DAMIT_CHECKOUT"]
        for f in load_manifest()["files"]:
            with open(os.path.join(CORE, f["file"]), "rb") as a, open(os.path.join(root, f["damit_path"]), "rb") as b:
                self.assertEqual(a.read(), b.read(), "noaa14/{} differs from DamIT's {}.\n{}".format(
                    f["file"], f["damit_path"], WHY))


def write_manifest(damit_root):
    """Rewrite SHARED.json from the files here, after checking they are byte-identical with DamIT's."""
    files = []
    for name in SHARED:
        mine, theirs = os.path.join(CORE, name), os.path.join(damit_root, "damit_pmp", name)
        with open(mine, "rb") as a, open(theirs, "rb") as b:
            if a.read() != b.read():
                raise SystemExit("noaa14/{} differs from {}: make the copies identical first.".format(name, theirs))
        files.append({"file": name, "damit_path": "damit_pmp/" + name, "sha256": sha256(mine)})
    doc = {"about": "NOAA Atlas 14 core shared byte-identical with DamIT PMP; checked by "
                    "DamSafety_PMP/tests/test_atlas14_shared.py and DamIT's scripts/check_atlas14_shared.py.",
           "damit_repository": DAMIT_REPO, "rule": WHY, "files": files}
    with open(MANIFEST, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")
    print("wrote", MANIFEST)


if __name__ == "__main__":
    if "--write" in sys.argv:
        i = sys.argv.index("--damit") if "--damit" in sys.argv else -1
        if i < 0 or i + 1 >= len(sys.argv):
            raise SystemExit("--write needs --damit <DamIT checkout> (the copies are compared before SHARED.json is written)")
        write_manifest(sys.argv[i + 1])
        sys.exit(0)
    unittest.main()
