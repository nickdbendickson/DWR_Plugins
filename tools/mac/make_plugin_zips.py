#!/usr/bin/env python3
"""Build the plugin zips the Mac installer installs (tools/mac/plugins/*.zip) from the HEAD
commit of this repo checkout (git archive): committed files only, so uncommitted edits never
reach a Mac; tests/, __pycache__, *.pyc and dotfiles are left out. plugins/SHA256SUMS lists
each zip's sha256 and, on its first line, the commit the zips were built from.

    python3 tools/mac/make_plugin_zips.py          (zips are git-ignored)
    python3 tools/mac/make_plugin_zips.py --ref v1 (build from another commit / tag)"""
import argparse
import hashlib
import io
import os
import subprocess
import tarfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = subprocess.check_output(["git", "-C", HERE, "rev-parse", "--show-toplevel"], text=True).strip()
OUT = os.path.join(HERE, "plugins")
PLUGINS = ["DamSafety_Setup", "DamSafety_Data", "DamSafety_Screening", "DamSafety_Runner",
           "DamSafety_Hazard", "DamSafety_HazardLite", "DamSafety_PMP"]
FIXED = (2026, 1, 1, 0, 0, 0)  # fixed timestamps -> same files give the same zip


def git(*args, **kw):
    return subprocess.check_output(["git", "-C", REPO] + list(args), **kw)


def version(files, p):
    for line in files[p + "/metadata.txt"].decode("utf-8").splitlines():
        if line.startswith("version="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no version in {}/metadata.txt".format(p))


def keep(rel):
    parts = rel.split("/")
    return not ("tests" in parts or "__pycache__" in parts or rel.endswith((".pyc", ".pyo"))
                or any(x.startswith(".") for x in parts))


def committed_files(commit, plugin):
    """{path: bytes} of the plugin's regular files at `commit` (git archive)."""
    tar = tarfile.open(fileobj=io.BytesIO(git("archive", "--format=tar", commit, "--", plugin)))
    files = {}
    for m in tar.getmembers():
        if m.isfile():
            files[m.name] = tar.extractfile(m).read()
        elif m.issym():
            raise SystemExit("{} is a symbolic link; the zips take regular files only".format(m.name))
    return files


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--ref", default="HEAD", help="commit, branch or tag to build from [HEAD]")
    a = ap.parse_args()
    commit = git("rev-parse", "--verify", a.ref + "^{commit}", text=True).strip()
    dirty = git("status", "--porcelain", "--", *PLUGINS, text=True).strip()
    if dirty and a.ref == "HEAD":
        print("NOTE: uncommitted changes under the plugin folders are NOT in the zips "
              "(they are built from {}):\n  {}".format(commit[:7], dirty.replace("\n", "\n  ")))
    os.makedirs(OUT, exist_ok=True)
    sums = []
    for p in PLUGINS:
        files = committed_files(commit, p)
        for old in os.listdir(OUT):
            if old.startswith(p + "_") and old.endswith(".zip"):
                os.remove(os.path.join(OUT, old))  # older build of this plugin in tools/mac/plugins
        dest = os.path.join(OUT, "{}_{}.zip".format(p, version(files, p)))
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for f in sorted(x for x in files if keep(x)):
                zi = zipfile.ZipInfo(f, FIXED)
                zi.external_attr = (0o755 if f.endswith(".sh") else 0o644) << 16
                zi.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(zi, files[f])
        with open(dest, "rb") as fh:
            h = hashlib.sha256(fh.read()).hexdigest()
        sums.append("{}  {}".format(h, os.path.basename(dest)))
        print(os.path.basename(dest))
    desc = git("log", "-1", "--format=%cs", commit, text=True).strip()
    with open(os.path.join(OUT, "SHA256SUMS"), "w") as fh:
        fh.write("# DWR_Plugins commit {} ({})\n".format(commit, desc))
        fh.write("\n".join(sums) + "\n")
    print("built from {} -> {}".format(commit[:7], OUT))


if __name__ == "__main__":
    main()
