#!/usr/bin/env python3
"""Build the release zips of the seven DamSafety plugins (what .github/workflows/release.yml
uploads to the GitHub Release and plugins.xml points at), on any OS with git and Python 3.

    python3 tools/build_release.py --out dist                      (HEAD; zips are git-ignored)
    python3 tools/build_release.py --out dist --ref v2026.11.07    (another commit / tag)
    python3 tools/build_release.py --out dist --runner-bin build/bin
        (also puts build/bin/<platform>/lisflood[.exe] into the Runner zip as
         DamSafety_Runner/bin/<platform>/..., which core/lisflood.bundled_exe() finds)

Like tools/mac/make_plugin_zips.py (the Mac installer's zips): committed files only, through
`git archive`, so an uncommitted edit never ships; fixed timestamps, so the same files give the
same zip. Left out: tests/, __pycache__, *.pyc, every dotfile and dotfolder - including
.secrets.baseline / .bandit, which plugins.qgis.org's scanner wants in a zip uploaded THERE but a
GitHub Release does not need (tools/mac/make_plugin_zips.py keeps them for that upload).
<out>/SHA256SUMS lists each zip's sha256 (first line: the commit), and `--runner-bin` adds the
binaries' checksums next to them as <platform>/lisflood lines."""
import argparse
import hashlib
import io
import os
import subprocess
import tarfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
PLUGINS = ["DamSafety_Setup", "DamSafety_Data", "DamSafety_Runner", "DamSafety_Hazard",
           "DamSafety_Screening", "DamSafety_PMP", "DamSafety_HazardLite"]
FIXED = (2026, 1, 1, 0, 0, 0)                      # fixed timestamps -> reproducible zips
BIN_PLATFORMS = ("linux-x64", "windows-x64", "macos-arm64", "macos-x64")


def git(*args, **kw):
    return subprocess.check_output(["git", "-C", REPO] + list(args), **kw)


def version(files, plugin):
    for line in files[plugin + "/metadata.txt"].decode("utf-8").splitlines():
        if line.startswith("version="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no version in {}/metadata.txt".format(plugin))


def keep(rel):
    """True for a file that ships: no tests/, caches, compiled files or dot-anything."""
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


def runner_binaries(folder):
    """{zip path: (bytes, executable?)} for the LISFLOOD-FP builds under folder/<platform>/: the
    executable and whatever ships beside it (macos-arm64 carries libomp.dylib)."""
    out = {}
    if not folder:
        return out
    for plat in BIN_PLATFORMS:
        sub = os.path.join(folder, plat)
        if not os.path.isdir(sub):
            continue
        for name in sorted(os.listdir(sub)):
            path = os.path.join(sub, name)
            if os.path.isfile(path) and not name.startswith("."):
                with open(path, "rb") as fh:
                    out["DamSafety_Runner/bin/{}/{}".format(plat, name)] = (
                        fh.read(), name in ("lisflood", "lisflood.exe"))
    readme = os.path.join(folder, "README.txt")
    if out and os.path.isfile(readme):
        with open(readme, "rb") as fh:
            out["DamSafety_Runner/bin/README.txt"] = (fh.read(), False)
    return out


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_zip(dest, files, extra=None):
    """files: {path: bytes}; extra: {path: (bytes, executable?)} added after them."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in sorted(x for x in files if keep(x)):
            zi = zipfile.ZipInfo(f, FIXED)
            zi.external_attr = (0o755 if f.endswith(".sh") else 0o644) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, files[f])
        for f in sorted(extra or {}):
            data, exe = extra[f]
            zi = zipfile.ZipInfo(f, FIXED)
            zi.external_attr = (0o755 if exe else 0o644) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--ref", default="HEAD", help="commit, branch or tag to build from [HEAD]")
    ap.add_argument("--out", default=os.path.join(REPO, "dist"), help="output folder [dist]")
    ap.add_argument("--runner-bin", default="", metavar="DIR",
                    help="folder with <platform>/lisflood[.exe] to bundle into the Runner zip")
    ap.add_argument("--only", default="", help="comma-separated plugin folders (default: all)")
    a = ap.parse_args(argv)
    commit = git("rev-parse", "--verify", a.ref + "^{commit}", text=True).strip()
    plugins = [p for p in a.only.split(",") if p] or PLUGINS
    bad = [p for p in plugins if p not in PLUGINS]
    if bad:
        raise SystemExit("not a DamSafety plugin folder: " + ", ".join(bad))
    dirty = git("status", "--porcelain", "--", *plugins, text=True).strip()
    if dirty and a.ref == "HEAD":
        print("NOTE: uncommitted changes under the plugin folders are NOT in the zips "
              "(they are built from {}):\n  {}".format(commit[:7], dirty.replace("\n", "\n  ")))
    os.makedirs(a.out, exist_ok=True)
    bins = runner_binaries(a.runner_bin)
    if a.runner_bin and not bins:
        raise SystemExit("no <platform>/lisflood[.exe] under " + a.runner_bin)
    sums, built = [], []
    for p in plugins:
        files = committed_files(commit, p)
        dest = os.path.join(a.out, "{}_{}.zip".format(p, version(files, p)))
        write_zip(dest, files, bins if p == "DamSafety_Runner" else None)
        sums.append("{}  {}".format(sha256_of(dest), os.path.basename(dest)))
        built.append(dest)
        print(os.path.basename(dest), "({} files{})".format(
            sum(1 for x in files if keep(x)),
            ", + {} bundled binar{}".format(len(bins), "y" if len(bins) == 1 else "ies")
            if p == "DamSafety_Runner" and bins else ""))
    for rel in sorted(bins):
        if rel.endswith(("lisflood", "lisflood.exe")):
            sums.append("{}  {}".format(hashlib.sha256(bins[rel][0]).hexdigest(),
                                        rel[len("DamSafety_Runner/bin/"):]))
    date = git("log", "-1", "--format=%cs", commit, text=True).strip()
    with open(os.path.join(a.out, "SHA256SUMS"), "w") as fh:
        fh.write("# dAmIt_Plugins (DamSafety) commit {} ({})\n".format(commit, date))
        fh.write("\n".join(sums) + "\n")
    print("built from {} -> {}".format(commit[:7], a.out))
    return built


if __name__ == "__main__":
    main()
