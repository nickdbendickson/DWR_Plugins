"""Bounded, contained zip extraction and bounded downloads (Core 1.5.0, security audit pass 3).

A zip that a tool downloads or that someone hands the user is checked before a byte of it is
written:

* every entry stays inside the destination (no '..', no absolute path, no drive letter, no
  symbolic link);
* the archive is not a "zip bomb": at most MAX_MEMBERS entries, a total unpacked size of at
  most `max_bytes`, and - for anything large - not compressed harder than MAX_RATIO to 1;
* the unpacked size fits on the disk with DISK_MARGIN_BYTES to spare.

The unpacked size comes from the zip's own directory. Python's zipfile never writes more than
that for an entry, so the directory cannot understate what lands on disk.

    check(zf, dest, max_bytes=...)        -> the total unpacked bytes, or ZipRefused
    extract(zf, dest, max_bytes=..., skip=None, cancel=None) -> the paths written
    copy_capped(read, write, max_bytes, what) -> bytes copied, or TooLarge

ZipRefused and TooLarge are ValueErrors, so callers that already report a bad archive as a
ValueError report these the same way. NDSUITE_ZIP_MAX_GB raises (or lowers) every size limit
for the rare real archive that is larger. Pure standard library.
"""
import os
import shutil
import stat

GB = 1024 ** 3
MAX_MEMBERS = 200000
MAX_RATIO = 1000                     # unpacked : packed, checked above RATIO_FLOOR_BYTES
RATIO_FLOOR_BYTES = 1 * GB           # small archives of very flat rasters compress hard
DISK_MARGIN_BYTES = 256 * 1024 ** 2
# what each kind of archive may unpack to
MAX_TILE_BYTES = 8 * GB              # one LiDAR tile zip (real ones: tens of MB)
MAX_DATASET_BYTES = 64 * GB          # one data set of the updater
MAX_PACKAGE_BYTES = 64 * GB          # a submittal / DSS-WISE package, an offline pack
MAX_DOWNLOAD_BYTES = 64 * GB         # one downloaded file, when the caller names no limit
ENV = "NDSUITE_ZIP_MAX_GB"


class ZipRefused(ValueError):
    """The archive is not extracted: an entry leaves the destination, or it is too large."""


class TooLarge(ValueError):
    """A download (or copy) went past its size limit and was stopped."""


def limit(default_bytes, environ=None):
    """`default_bytes`, or NDSUITE_ZIP_MAX_GB (a positive number of GiB) when that is set."""
    env = os.environ if environ is None else environ
    raw = str(env.get(ENV, "")).strip()
    try:
        gb = float(raw) if raw else 0.0
    except ValueError:
        gb = 0.0
    return int(gb * GB) if gb > 0 else int(default_bytes)


def _gb(n):
    return "{:,.1f} GB".format(n / float(GB)) if n >= GB else "{:,.0f} MB".format(
        n / 1024.0 ** 2)


def _is_link(member):
    return stat.S_ISLNK((member.external_attr >> 16) & 0xFFFF)


def unsafe_name(name):
    """Why a zip entry name cannot be extracted, or ''."""
    n = (name or "").replace("\\", "/")
    if not n or "\x00" in n:
        return "an empty or malformed name"
    if n.startswith("/") or (len(n) > 1 and n[1] == ":"):
        return "an absolute path"
    if ".." in n.split("/"):
        return "a path that climbs out of the folder ('..')"
    return ""


def free_bytes(folder):
    path = os.path.abspath(folder or ".")
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    try:
        return int(shutil.disk_usage(path).free)
    except (OSError, ValueError):
        return None


def check(zf, dest, max_bytes=MAX_PACKAGE_BYTES, skip=None, environ=None):
    """ZipRefused unless every entry of the open ZipFile `zf` (but those `skip(name)` drops)
    may be extracted into `dest`; returns the total unpacked bytes."""
    cap = limit(max_bytes, environ)
    root = os.path.realpath(dest)
    members = [m for m in zf.infolist() if not (skip and skip(m.filename))]
    if len(members) > MAX_MEMBERS:
        raise ZipRefused("the archive holds {:,} entries (limit {:,})".format(
            len(members), MAX_MEMBERS))
    total = packed = 0
    for m in members:
        why = unsafe_name(m.filename)
        if not why and _is_link(m):
            why = "a symbolic link"
        if not why:
            target = os.path.realpath(os.path.join(dest, m.filename))
            if target != root and not target.startswith(root + os.sep):
                why = "a path outside the destination"
        if why:
            raise ZipRefused("archive entry {!r} is {}".format(m.filename[:200], why))
        total += max(0, int(m.file_size))
        packed += max(0, int(m.compress_size))
    if total > cap:
        raise ZipRefused(
            "the archive unpacks to {} (limit {}); set {} to a larger number of GB if this "
            "archive is really that large".format(_gb(total), _gb(cap), ENV))
    if total > RATIO_FLOOR_BYTES and total > MAX_RATIO * max(packed, 1):
        raise ZipRefused(
            "the archive unpacks to {} from {} ({:,.0f} to 1): no real data set compresses "
            "that hard".format(_gb(total), _gb(packed), total / float(max(packed, 1))))
    free = free_bytes(dest)
    if free is not None and total + DISK_MARGIN_BYTES > free:
        raise ZipRefused(
            "not enough disk space to unpack the archive into {}: {} needed, {} free".format(
                dest, _gb(total + DISK_MARGIN_BYTES), _gb(free)))
    return total


def extract(zf, dest, max_bytes=MAX_PACKAGE_BYTES, skip=None, cancel=None, environ=None):
    """check(), then extract every entry (but those `skip(name)` drops) into `dest`.
    `cancel()` is called before each entry and may raise. Returns the paths written."""
    check(zf, dest, max_bytes, skip, environ)
    out = []
    for m in zf.infolist():
        if skip and skip(m.filename):
            continue
        if cancel is not None:
            cancel()
        out.append(zf.extract(m, dest))
    return out


def copy_capped(read, write, max_bytes=MAX_DOWNLOAD_BYTES, what="the download", block=1 << 20,
                environ=None):
    """Copy from read(n) to write(bytes) until read returns nothing; TooLarge as soon as more
    than the limit has arrived (the caller removes the partial file). Returns the bytes
    copied."""
    cap = limit(max_bytes, environ)
    n = 0
    while True:
        chunk = read(block)
        if not chunk:
            return n
        n += len(chunk)
        if n > cap:
            raise TooLarge(
                "{} is larger than {} and was stopped; set {} to a larger number of GB if it "
                "is really that large".format(what, _gb(cap), ENV))
        write(chunk)
