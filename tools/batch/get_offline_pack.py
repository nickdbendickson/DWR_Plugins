#!/usr/bin/env python3
"""Get the offline pack without opening QGIS.

The dam plugins read their map layers (structures, roads, rail, bridges, flowlines, dams,
land cover ...) from one folder, the offline pack: <data folder>/ND_Dam_Suite_Pack. Until now
the only way to make it was a window in QGIS. The plugins have carried a pack builder that
needs no QGIS for a long time; this finds it and runs it.

  python3 get_offline_pack.py --check                  # is a pack there, and what does it cover
  python3 get_offline_pack.py                          # the whole State, into ~/ND_Suite_Data
  python3 get_offline_pack.py --dams dams_44.csv --only DWR-3034,DWR-4031   # around those dams
  python3 get_offline_pack.py --bbox=-101.2,46.8,-100.8,47.2                # this lon / lat box
  python3 get_offline_pack.py --zip part1.zip part2.zip part3.zip           # a pack built before
  python3 get_offline_pack.py --dry-run                # print the command, download nothing

The whole State takes hours and a few gigabytes; a few dams take minutes. A build that stops
(Ctrl-C, sleep, a lost connection) is picked up by running the same command again: layers that
finished are kept. A layer whose server does not answer is listed at the end and the pack is
still written without it; run again with --update to fetch only what is missing.

A pack that is already there is never replaced or written into by this script: it says what
is there and stops. --update asks the builder to fill in missing layers of a pack it built.

The run scripts beside this one (run_lisflood_44.py, run_county.py) call it themselves when
they find no pack, for the area of the dams they are about to run.
"""
import argparse
import configparser
import csv
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PACK = "ND_Dam_Suite_Pack"
# North Dakota, lon / lat, as the builder knows it. A request that takes in most of it is
# built as the whole State, so the result is the standard pack and not a near copy of it.
ND_BBOX = (-104.05, 45.93, -96.55, 49.0)
STATE_SHARE = 0.6
# Get data reads the pack for 15 miles around a dam and uses the pack only when it covers all
# of that box, so the pad has to be larger.
PAD_MI = 20.0
QGIS_ENVS = ("ndqgis",)
# What a run without a pack comes to. Tried in QGIS 3.34 with the ND 1.30 plugins on DWR-3034,
# 2026-11-13: Get data wrote 12 layers from the servers, and Screening then stopped at its
# check on "DWR dam lines" and "Water bodies". The dam lines have no server to come from.
NO_PACK = ("Without a pack Get data downloads most layers around each dam from the servers, but "
           "Screening then stops at its check: the DWR dam lines come only from the pack (or "
           "from your own DWR files in the data folder's dwr/), and so do the NSI structure "
           "points and the lifeline facilities.")


def data_folder_default():
    return os.environ.get("NDSUITE_DATA") or "~/ND_Suite_Data"


# ------------------------------------------------------------------ what is there
def chosen_packs():
    """Pack locations chosen outside the data folder, as the plugins look them up: $NDSUITE_PACK,
    "Offline pack" in Suite data and servers (ndsuite/pack_path in a QGIS profile's settings),
    then ~/.ndsuite/config.json. A machine whose pack lives on another drive has it here, and
    must not be given a second one because the data folder holds none."""
    out = []
    if os.environ.get("NDSUITE_PACK"):
        out.append(os.environ["NDSUITE_PACK"].strip())
    for root in profile_roots():
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for prof in names:
            ini = configparser.RawConfigParser(strict=False)
            try:
                ini.read(os.path.join(root, prof, "QGIS", "QGIS3.ini"), encoding="utf-8")
                v = ini.get("ndsuite", "pack_path", fallback="")
            except (configparser.Error, OSError, UnicodeDecodeError):
                continue
            if v.strip().strip('"'):
                out.append(v.strip().strip('"'))
    try:
        with open(os.path.join(os.path.expanduser("~"), ".ndsuite", "config.json"), encoding="utf-8") as fh:
            v = json.load(fh).get("ndsuite/pack_path")
        if v:
            out.append(str(v).strip())
    except (OSError, ValueError, AttributeError):
        pass
    seen, keep = set(), []
    for p in out:
        p = os.path.abspath(os.path.expanduser(p))
        if p not in seen and os.path.exists(p):
            seen.add(p)
            keep.append(p)
    return keep


def find_pack(data_folder):
    """The pack the plugins would read: one chosen elsewhere that exists, else the data
    folder's. A chosen pack that is not a folder with a manifest (a one-file .zip pack) counts
    as found, with no area recorded."""
    for p in chosen_packs():
        st = status_of(p if os.path.isdir(p) else os.path.dirname(p))
        if not st["found"]:
            st.update(found=True, path=p)
        return st
    return pack_status(data_folder)


def pack_status(data_folder):
    """What <data folder>/ND_Dam_Suite_Pack holds (see status_of)."""
    return status_of(os.path.join(data_folder, PACK))


def status_of(path):
    """What the pack folder `path` holds: {found, path, bbox, created, area, layers,
    incomplete: [layer names], left_out: n}. found is False for no folder or no manifest."""
    st = {"found": False, "path": path, "bbox": None, "created": "", "area": "", "layers": 0,
          "incomplete": [], "left_out": 0}
    try:
        with open(os.path.join(path, "manifest.json"), encoding="utf-8") as fh:
            man = json.load(fh)
    except (OSError, ValueError):
        return st
    if not isinstance(man, dict):
        return st
    sets = man.get("datasets") if isinstance(man.get("datasets"), dict) else {}
    box = man.get("bbox")
    try:
        box = tuple(float(v) for v in box) if box and len(box) == 4 else None
    except (TypeError, ValueError):
        box = None
    st.update(found=True, bbox=box, created=str(man.get("created") or ""),
              area=str(man.get("area") or ""), layers=len(sets),
              incomplete=sorted(n for n, d in sets.items()
                                if isinstance(d, dict) and d.get("status") not in (None, "complete")),
              left_out=len(man.get("left_out") or ()))
    return st


def describe(st):
    if not st["found"]:
        return "no offline pack at %s" % st["path"]
    box = ("%.3f, %.3f to %.3f, %.3f" % st["bbox"]) if st["bbox"] else "area not recorded"
    line = "offline pack at %s: %d layers, %s (%s), built %s" % (
        st["path"], st["layers"], st["area"] or "area", box, st["created"][:10] or "?")
    if st["incomplete"]:
        line += "; not complete: " + ", ".join(st["incomplete"])
    return line


def covers(outer, inner):
    return bool(outer and inner and outer[0] <= inner[0] and outer[1] <= inner[1]
                and outer[2] >= inner[2] and outer[3] >= inner[3])


# ------------------------------------------------------------------ the area asked for
def box_around(lon, lat, pad_mi):
    dlat = pad_mi / 69.05
    dlon = pad_mi / (69.17 * max(0.2, math.cos(math.radians(lat))))
    return (lon - dlon, lat - dlat, lon + dlon, lat + dlat)


def dams_bbox(rows, pad_mi=PAD_MI):
    """One lon / lat box that takes in every dam of `rows` (dicts with lat / lon) and pad_mi
    around each, rounded outward to 0.01 degree so the same dams always give the same box (the
    builder refuses to resume a build that was started for another area). None without a dam."""
    boxes = []
    for r in rows:
        try:
            boxes.append(box_around(float(r["lon"]), float(r["lat"]), pad_mi))
        except (KeyError, TypeError, ValueError):
            continue
    if not boxes:
        return None
    w, s = min(b[0] for b in boxes), min(b[1] for b in boxes)
    e, n = max(b[2] for b in boxes), max(b[3] for b in boxes)
    return (math.floor(w * 100) / 100.0, math.floor(s * 100) / 100.0,
            math.ceil(e * 100) / 100.0, math.ceil(n * 100) / 100.0)


def not_covered(st, rows, mi=15.0):
    """Ids of the dams whose surroundings (mi around the dam, what Get data reads) the pack does
    not take in. Every dam when the pack records no area."""
    out = []
    for r in rows:
        try:
            box = box_around(float(r["lon"]), float(r["lat"]), mi)
        except (KeyError, TypeError, ValueError):
            continue
        if not covers(st.get("bbox"), box):
            out.append(r.get("dam_id") or "?")
    return out


def is_statewide(box):
    """True when the box takes in most of North Dakota: built as the whole State then."""
    w, s, e, n = (max(box[0], ND_BBOX[0]), max(box[1], ND_BBOX[1]),
                  min(box[2], ND_BBOX[2]), min(box[3], ND_BBOX[3]))
    if e <= w or n <= s:
        return False
    nd = (ND_BBOX[2] - ND_BBOX[0]) * (ND_BBOX[3] - ND_BBOX[1])
    return (e - w) * (n - s) / nd >= STATE_SHARE


def read_dams(path, only=""):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if only:
        want = {s.strip() for s in only.split(",") if s.strip()}
        missing = want - {r.get("dam_id") for r in rows}
        if missing:
            sys.exit("Not in %s: %s" % (os.path.basename(path), ", ".join(sorted(missing))))
        rows = [r for r in rows if r.get("dam_id") in want]
    return rows


# ------------------------------------------------------------------ the builder and its Python
def profile_roots():
    """Folders that hold QGIS profiles, the chain's own first."""
    out = []
    if os.environ.get("DAMCHAIN_QGIS_PROFILES"):
        out.append(os.path.expanduser(os.environ["DAMCHAIN_QGIS_PROFILES"]))
    out += [os.path.expanduser("~/Library/Application Support/QGIS/QGIS3/profiles"),
            os.path.expanduser("~/.local/share/QGIS/QGIS3/profiles")]
    if os.environ.get("APPDATA"):
        out.append(os.path.join(os.environ["APPDATA"], "QGIS", "QGIS3", "profiles"))
    return out


def plugin_folders():
    """Every folder that may hold the plugins: $PACK_BUILDER, the repository this script is in,
    then each QGIS profile (damsafety and nd first)."""
    out = []
    if os.environ.get("PACK_BUILDER"):
        out.append(os.path.expanduser(os.environ["PACK_BUILDER"]))
    out.append(os.path.dirname(os.path.dirname(HERE)))
    for root in profile_roots():
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        first = [n for n in ("damsafety", "nd", "default") if n in names]
        for prof in first + [n for n in names if n not in first]:
            out.append(os.path.join(root, prof, "python", "plugins"))
    return out


def builder_in(folder):
    """("dwr" | "nd", path) for the pack builder in a plugins folder, or None. dwr: DamSafety
    Data's pack_build.py (with DamSafety Core beside it); nd: the ND suite's build_nd_pack.py."""
    if os.path.isfile(folder) and os.path.basename(folder) == "build_nd_pack.py":
        return "nd", folder
    if (os.path.isfile(os.path.join(folder, "DamSafety_Data", "pack_build.py"))
            and os.path.isfile(os.path.join(folder, "DamSafety_Core", "damsuite", "packengine.py"))):
        return "dwr", folder
    for rel in (("nd_data_updater", "ndpack", "build_nd_pack.py"),
                ("ND_Dam_Hazard_Helper", "build_nd_pack.py")):
        p = os.path.join(folder, *rel)
        if os.path.isfile(p):
            return "nd", p
    return None


def find_builder(folders=None):
    for f in (plugin_folders() if folders is None else folders):
        got = builder_in(f)
        if got:
            return got
    return None


def has_gdal(python):
    try:
        return subprocess.run([python, "-c", "from osgeo import gdal, ogr"], capture_output=True,
                              timeout=120).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def find_python(override=""):
    """(python, has GDAL). The builder reads the DWR layers and the land cover raster through
    GDAL: --python / $PACK_PYTHON, this Python when it has GDAL, else the suite's QGIS conda
    env. Without GDAL the build still runs and leaves those layers out."""
    for cand in (override, os.environ.get("PACK_PYTHON", "")):
        if cand:
            cand = os.path.expanduser(cand)
            if not os.path.isfile(cand):
                sys.exit("No Python at %s" % cand)
            return cand, has_gdal(cand)
    if has_gdal(sys.executable):
        return sys.executable, True
    try:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        from batch_common import env_python
        py = env_python(QGIS_ENVS)
    except Exception:                                                        # noqa
        py = None
    if py and has_gdal(py):
        return py, True
    return sys.executable, False


def build_command(kind, path, python, data_folder, bbox=None, refresh=False):
    """(argv, cwd) that builds the pack into <data folder>/ND_Dam_Suite_Pack. bbox None = the
    whole State. The box is written with repr so the builder reads back the very same numbers:
    it refuses to resume a build whose area differs in any digit."""
    box = ",".join(repr(float(v)) for v in bbox) if bbox else ""
    if kind == "dwr":
        cmd = [python, "-m", "DamSafety_Data.pack_build", "--out", data_folder]
        if box:
            cmd.append("--bbox=" + box)
        cwd = path
    else:
        cmd = [python, path, "--out", os.path.join(data_folder, PACK)]
        if box:
            cmd.append("--box=" + box)
        cwd = os.path.dirname(path)
    if refresh:
        cmd.append("--refresh")
    return cmd, cwd


def run_build(cmd, cwd, say=print):
    """Run the builder with its lines passed on; on a Mac keep the machine awake while it runs."""
    full = (["caffeinate", "-i"] if shutil.which("caffeinate") else []) + cmd
    say("$ " + " ".join(cmd))
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    env.pop("PYTHONPATH", None)
    proc = subprocess.Popen(full, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace")
    try:
        for line in proc.stdout:
            say("  " + line.rstrip())
    except KeyboardInterrupt:
        proc.terminate()
        say("Stopped. Run the same command again to pick the build up where it stopped.")
        return 130
    return proc.wait()


# ------------------------------------------------------------------ a pack built before
def fetch(url, dest, say=print):
    if not url.lower().startswith("https://"):
        sys.exit("Only https links are downloaded: %s" % url)
    say("downloading " + url)
    req = urllib.request.Request(url, headers={"User-Agent": "dam-suite-batch"})
    with urllib.request.urlopen(req, timeout=600) as r, open(dest, "wb") as fh:  # nosec B310 - https only, checked above
        shutil.copyfileobj(r, fh)
    return dest


def install_zips(zips, data_folder, say=print):
    """Unpack the zip files of a pack built before (each holds ND_Dam_Suite_Pack/...) into the
    data folder. Everything is unpacked beside the target first and moved in only when all the
    parts are there and a manifest is among them; an existing pack is never written into."""
    target = os.path.join(data_folder, PACK)
    if os.path.exists(target):
        sys.exit("%s is already there. It is not replaced: move it away first, or name another "
                 "--data-folder." % target)
    os.makedirs(data_folder, exist_ok=True)
    stage = tempfile.mkdtemp(prefix=".pack_unzip_", dir=data_folder)
    try:
        for n, z in enumerate(zips, 1):
            if z.lower().startswith(("http://", "https://")):
                z = fetch(z, os.path.join(stage, "part%d.zip" % n), say)
            z = os.path.expanduser(z)
            if not os.path.isfile(z):
                sys.exit("No file at %s" % z)
            with zipfile.ZipFile(z) as zf:
                bad = zf.testzip()
                if bad:
                    sys.exit("%s is damaged (%s). Download it again." % (z, bad))
                for info in zf.infolist():
                    name = info.filename.replace("\\", "/")
                    parts = name.split("/")
                    if (not name.startswith(PACK + "/") or ".." in parts or os.path.isabs(name)
                            or (info.external_attr >> 16) & 0o170000 == 0o120000):
                        sys.exit("%s holds %s, which is not part of an %s folder. Nothing was "
                                 "unpacked." % (z, info.filename, PACK))
                say("unpacking %s (%d files)" % (os.path.basename(z), len(zf.infolist())))
                zf.extractall(stage)                                         # nosec B202 - every member checked above
        if not os.path.isfile(os.path.join(stage, PACK, "manifest.json")):
            sys.exit("No %s/manifest.json in the zip file(s): that is not a pack. Nothing was "
                     "unpacked." % PACK)
        os.rename(os.path.join(stage, PACK), target)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return target


# ------------------------------------------------------------------ for the run scripts
def ensure_pack(data_folder, rows, say=print, get=True, dry_run=False, pad_mi=PAD_MI, python=""):
    """What a run script does about the pack before it starts its dams. Returns "found",
    "built", "would build", "not built" or "skipped". It never stops the run itself: a data
    folder with the engineer's own DWR files in dwr/ can get through without a pack, and the
    script cannot tell from here. What to expect without one is printed (NO_PACK).

    found      a pack is there (dams it does not cover are named)
    built      there was none; one was built for the dams' area
    not built  there was none and the build did not produce one (the reason is printed)
    skipped    there was none and get is False, or BATCH_GET_PACK=0 is set in the environment
               (for a machine that should never start this download by itself)"""
    if os.environ.get("BATCH_GET_PACK", "1").strip().lower() in ("0", "no", "false", "off"):
        get = False
    st = find_pack(data_folder)
    if st["found"]:
        out = not_covered(st, rows) if st["bbox"] else []
        if out:
            say("NOTE: the %s does not cover %d of %d dam(s) (%s%s). For those the run is a run "
                "without a pack. %s" % (
                    describe(st), len(out), len(rows), ", ".join(out[:6]),
                    ", ..." if len(out) > 6 else "", NO_PACK))
        return "found"
    box = dams_bbox(rows, pad_mi)
    state = box is None or is_statewide(box)
    where = "the whole State" if state else "%.2f, %.2f to %.2f, %.2f" % box
    if not get:
        say("WARNING: %s. %s To build the pack first: python3 %s --data-folder %s" % (
            describe(st), NO_PACK, os.path.join(HERE, "get_offline_pack.py"), data_folder))
        return "skipped"
    got = find_builder()
    if not got:
        say("WARNING: %s, and no pack builder was found to make one (looked in: %s). %s"
            % (describe(st), "; ".join(plugin_folders()), NO_PACK))
        return "not built"
    py, gdal = find_python(python)
    cmd, cwd = build_command(got[0], got[1], py, data_folder, None if state else box)
    say("No offline pack at %s. %s one for %s first (%s). --no-get-pack skips this." % (
        st["path"], "The run would build" if dry_run else "Building", where,
        "hours, a few gigabytes" if state else "minutes for a small area"))
    if not gdal:
        say("NOTE: %s has no GDAL, so the DWR layers and the land cover are left out of the pack. "
            "Name the QGIS Python with $PACK_PYTHON to get them." % py)
    if dry_run:
        say("  " + " ".join(cmd))
        return "would build"
    rc = run_build(cmd, cwd, say)
    st = pack_status(data_folder)
    if st["found"]:
        say("Built: %s%s" % (describe(st), "" if rc == 0 else
                             " (some layers failed, see above; --update on get_offline_pack.py "
                             "fetches them again)"))
        return "built"
    say("WARNING: the pack was not built (exit %d, see above). The run goes on. %s" % (rc, NO_PACK))
    return "not built"


# ------------------------------------------------------------------ command line
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-folder", default=data_folder_default(),
                    help="suite data folder; the pack goes in its ND_Dam_Suite_Pack "
                         "(default $NDSUITE_DATA or ~/ND_Suite_Data)")
    ap.add_argument("--dams", default="", help="a dam list (CSV with dam_id, lat, lon): only their area")
    ap.add_argument("--only", default="", help="with --dams: comma separated dam ids")
    ap.add_argument("--pad-mi", type=float, default=PAD_MI,
                    help="with --dams: miles around each dam (default %g; Get data reads 15)" % PAD_MI)
    ap.add_argument("--bbox", default="", help="only this lon / lat box: west,south,east,north "
                                               "(write it with =, --bbox=-101.2,46.8,-100.8,47.2)")
    ap.add_argument("--zip", nargs="+", default=[], metavar="ZIP",
                    help="unpack a pack built before instead of building: zip files or https links")
    ap.add_argument("--update", action="store_true",
                    help="a pack is there: ask the builder to fetch its missing or failed layers")
    ap.add_argument("--refresh", action="store_true", help="with --update: fetch every layer again")
    ap.add_argument("--python", default="", help="the Python that runs the builder (default: one with GDAL)")
    ap.add_argument("--check", action="store_true", help="say what is there and stop")
    ap.add_argument("--dry-run", action="store_true", help="print the command and stop")
    args = ap.parse_args(argv)
    data = os.path.abspath(os.path.expanduser(args.data_folder))
    rows = read_dams(os.path.expanduser(args.dams), args.only) if args.dams else []
    if args.only and not args.dams:
        sys.exit("--only needs --dams (the list the ids are in)")
    box = None
    if args.bbox:
        try:
            box = tuple(float(v) for v in args.bbox.split(","))
            assert len(box) == 4 and box[0] < box[2] and box[1] < box[3]
        except (ValueError, AssertionError):
            sys.exit("--bbox needs west,south,east,north in degrees, e.g. --bbox=-101.2,46.8,-100.8,47.2")
    elif rows:
        box = dams_bbox(rows, args.pad_mi)
        if box is None:
            sys.exit("No dam in %s has a lat and lon." % args.dams)
        if is_statewide(box):
            print("The dams take in most of the State: building the whole State.")
            box = None
    st = find_pack(data)
    print(describe(st))
    if st["found"] and st["path"] != os.path.join(data, PACK):
        print("(chosen outside the data folder: $NDSUITE_PACK, or Offline pack in Suite data and "
              "servers. The plugins read that one, so none is built in %s.)" % data)
    if st["found"] and rows and st["bbox"]:
        out = not_covered(st, rows)
        print("It covers all %d dam(s)." % len(rows) if not out else
              "It does not cover %d of %d dam(s): %s" % (len(out), len(rows), ", ".join(out)))
    if args.check:
        return 0 if st["found"] else 1
    if args.zip:
        if args.dry_run:
            print("would unpack %d zip file(s) into %s" % (len(args.zip), os.path.join(data, PACK)))
            return 0
        print("Unpacked: " + describe(pack_status(os.path.dirname(install_zips(args.zip, data)))))
        return 0
    if st["found"] and st["path"] != os.path.join(data, PACK):
        return 0
    if st["found"] and not args.update:
        if box and not covers(st["bbox"], box):
            print("That pack does not take in the area asked for (%.2f, %.2f to %.2f, %.2f). A pack "
                  "is never replaced here: build the other area into another folder with "
                  "--data-folder, and name that folder to the run script." % box)
            return 2
        print("Nothing to do. --update fetches missing or failed layers again.")
        return 0
    got = find_builder()
    if not got:
        sys.exit("No pack builder found. It comes with the DamSafety Data plugin (or the ND Data "
                 "Updater). Looked in:\n  %s\nSet PACK_BUILDER to the QGIS plugins folder that "
                 "holds it." % "\n  ".join(plugin_folders()))
    py, gdal = find_python(args.python)
    if st["found"] and st["bbox"] and not box and not is_statewide(st["bbox"]):
        box = st["bbox"]                     # --update of an area pack keeps its area
    cmd, cwd = build_command(got[0], got[1], py, data, box, refresh=args.refresh)
    print("Builder: %s (%s)" % (got[1], "DamSafety Data" if got[0] == "dwr" else "ND suite"))
    print("Area: %s" % ("the whole State (hours, a few gigabytes)" if not box
                        else "%.2f, %.2f to %.2f, %.2f" % box))
    if not gdal:
        print("NOTE: %s has no GDAL, so the DWR layers and the land cover are left out. Name the "
              "QGIS Python with --python to get them." % py)
    if args.dry_run:
        print("$ (in %s) %s" % (cwd, " ".join(cmd)))
        return 0
    os.makedirs(data, exist_ok=True)
    rc = run_build(cmd, cwd)
    st = pack_status(data)
    print(describe(st))
    if not st["found"]:
        print("The pack was not written (exit %d). Run the same command again: finished layers "
              "are kept." % rc)
        return rc or 1
    if rc:
        print("The builder ended with exit %d (see above). When layers failed, the pack is usable "
              "without them and --update fetches them again." % rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
