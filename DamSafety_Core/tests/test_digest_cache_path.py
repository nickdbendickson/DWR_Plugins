"""DamSafety Core 1.5.3: the file-digest cache (damsuite.chain) follows the user's cache folder.

It was always ~/.cache/damsuite/file_digests.json, even when XDG_CACHE_HOME pointed the cache elsewhere (Crest
install test 2026-11-21: a sandboxed test wrote into the real home folder). Now: DAMSUITE_DIGEST_CACHE, else
%LOCALAPPDATA% (Windows) or $XDG_CACHE_HOME, else ~/.cache; the same base as suitedata.pack_cache_root(). Plain
Python, no QGIS. Run:

    python3 DamSafety_Core/tests/test_digest_cache_path.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)

from DamSafety_Core.damsuite import chain, suitedata  # noqa: E402

KEYS = ("DAMSUITE_DIGEST_CACHE", "XDG_CACHE_HOME", "LOCALAPPDATA", "NDSUITE_PACK_CACHE")
saved = {k: os.environ.get(k) for k in KEYS}
fails = []


def check(label, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + label + ("" if ok else " - " + str(detail)))
    if not ok:
        fails.append(label)


try:
    for k in KEYS:
        os.environ.pop(k, None)
    home = os.path.join(os.path.expanduser("~"), ".cache", "damsuite", "file_digests.json")
    if os.name != "nt":
        check("nothing set: ~/.cache/damsuite/file_digests.json", chain._disk_path() == home, chain._disk_path())
        os.environ["XDG_CACHE_HOME"] = os.path.join(os.sep, "sandbox", "cache")
        want = os.path.join(os.sep, "sandbox", "cache", "damsuite", "file_digests.json")
        check("XDG_CACHE_HOME is followed", chain._disk_path() == want, chain._disk_path())
        check("the same base as the offline-pack cache",
              os.path.dirname(os.path.dirname(chain._disk_path())) ==
              os.path.dirname(os.path.dirname(suitedata.pack_cache_root())),
              (chain._disk_path(), suitedata.pack_cache_root()))
        os.environ["XDG_CACHE_HOME"] = "  "
        check("a blank XDG_CACHE_HOME is ~/.cache", chain._disk_path() == home, chain._disk_path())
    else:
        os.environ["LOCALAPPDATA"] = r"C:\Users\nick\AppData\Local"
        check("Windows: %LOCALAPPDATA%\\damsuite",
              chain._disk_path() == os.path.join(r"C:\Users\nick\AppData\Local", "damsuite", "file_digests.json"),
              chain._disk_path())
    os.environ["DAMSUITE_DIGEST_CACHE"] = os.path.join(os.sep, "elsewhere", "d.json")
    check("DAMSUITE_DIGEST_CACHE still wins", chain._disk_path() == os.path.join(os.sep, "elsewhere", "d.json"))
    os.environ["DAMSUITE_DIGEST_CACHE"] = ""
    check("DAMSUITE_DIGEST_CACHE empty still turns it off", chain._disk_path() is None)
finally:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

print("OK" if not fails else "FAILED: " + ", ".join(fails))
sys.exit(1 if fails else 0)
