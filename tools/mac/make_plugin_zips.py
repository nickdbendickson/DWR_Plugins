#!/usr/bin/env python3
"""Build the plugin zips the Mac installer installs (tools/mac/plugins/*.zip) from this
repo checkout: tracked files only (git ls-files); tests/, __pycache__, *.pyc and dotfiles
are left out. Usage: python3 tools/mac/make_plugin_zips.py   (zips are git-ignored)"""
import hashlib, os, subprocess, zipfile
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = subprocess.check_output(["git", "-C", HERE, "rev-parse", "--show-toplevel"], text=True).strip()
OUT = os.path.join(HERE, "plugins")
PLUGINS = ["DamSafety_Setup", "DamSafety_Data", "DamSafety_Screening", "DamSafety_Runner",
           "DamSafety_Hazard", "DamSafety_HazardLite", "DamSafety_PMP"]
FIXED = (2026, 1, 1, 0, 0, 0)  # fixed timestamps -> same files give the same zip


def version(p):
    for line in open(os.path.join(REPO, p, "metadata.txt"), encoding="utf-8"):
        if line.startswith("version="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no version in {}/metadata.txt".format(p))


def keep(rel):
    parts = rel.split("/")
    return not ("tests" in parts or "__pycache__" in parts or rel.endswith((".pyc", ".pyo"))
                or any(x.startswith(".") for x in parts))


os.makedirs(OUT, exist_ok=True)
commit = subprocess.check_output(["git", "-C", REPO, "rev-parse", "--short", "HEAD"], text=True).strip()
sums = []
for p in PLUGINS:
    files = subprocess.check_output(["git", "-C", REPO, "ls-files", p], text=True).split()
    for old in os.listdir(OUT):
        if old.startswith(p + "_") and old.endswith(".zip"):
            os.remove(os.path.join(OUT, old))  # older build of this plugin in tools/mac/plugins
    dest = os.path.join(OUT, "{}_{}.zip".format(p, version(p)))
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(x for x in files if keep(x)):
            zi = zipfile.ZipInfo(f, FIXED)
            zi.external_attr = (0o755 if f.endswith(".sh") else 0o644) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, open(os.path.join(REPO, f), "rb").read())
    h = hashlib.sha256(open(dest, "rb").read()).hexdigest()
    sums.append("{}  {}".format(h, os.path.basename(dest)))
    print(os.path.basename(dest))
open(os.path.join(OUT, "SHA256SUMS"), "w").write("\n".join(sums) + "\n")
print("built from {} -> {}".format(commit, OUT))
