"""Update engine of both data updaters (damsuite.updatecore, suite 1.18.8): the ND Data Updater
(nd_hazard) and DamSafety Data (DWR_Plugins). Each plugin's core.py is a shim that IS this module
(sys.modules alias), so `from <plugin> import core` and the tests work as before. Edit it in
DWR_Plugins shared/damsuite only, then run tools/sync_damsuite.py.

Merged from both copies (1.18.8): DamSafety's own manifest name (structures_manifest.json, the
older manifest.json read once), the lock fix for a fresh lock of this same process (refused, not
stolen), the download's SHA-256 recorded beside it, OvertureSource and ArcGISFeatureSource; the
ND Updater's one-line status, the lock file that says why a run is refused and when it started,
OsmLayersSource (the five OSM_* pack layers from Geofabrik's .osm.pbf) and sources that update in
place (the offline pack).

Pure Python with no QGIS imports, so it can be tested outside QGIS. The plugin
supplies an HTTP client (network access through QGIS) and a Feedback object
(progress, cancel, log) and calls Updater.check_and_update() in a background task.

How an update works, for each source:
  1. Ask the source for its remote version without downloading the data
     (checksum sidecar file, HTTP ETag/Last-Modified, or latest git commit).
  2. If it matches the version recorded in structures_manifest.json (manifest.json before
     1.4.0), nothing is downloaded.
  3. Otherwise download into <data folder>/.staging, verify it, then swap it into
     place with directory renames. The working copy is replaced only after a
     complete, verified download, so an interrupted update never leaves
     half-written data behind.
  4. If the network is unavailable, the source is reported "offline" and the
     existing local copy stays in use.

1.4.0 (ND): the lock file records the pid of the process that holds it, and a lock whose process
is gone is cleared instead of blocking every later run. Feedback also carries a one-line `status`
message ("layer 12 of 29: ...") beside the percentage, so a long run can say what it is doing
without filling the log.
"""

import datetime
import hashlib
import json
import os
import shutil
import time
import uuid
import zipfile

# 1.4.0: Data's own manifest has its own name, so it never replaces the suite's manifest.json
# when Data downloads into the suite data folder. LEGACY_MANIFEST_NAME is read once (when the
# new one is missing) and never written or deleted.
MANIFEST_NAME = "structures_manifest.json"
LEGACY_MANIFEST_NAME = "manifest.json"
STAGING_NAME = ".staging"
LOCK_NAME = ".update.lock"
LOCK_STALE_SECONDS = 6 * 3600


class NetworkError(Exception):
    """The source could not be reached (offline, DNS failure, timeout, 5xx)."""


class UpdateError(Exception):
    """The source was reached but the data is missing or invalid."""


class Cancelled(Exception):
    """The user cancelled the update."""


class Feedback:
    """Progress, cancellation and logging hooks supplied by the caller."""

    def __init__(self, progress=None, is_cancelled=None, log=None, status=None):
        self._progress = progress
        self._status = status
        self._is_cancelled = is_cancelled
        self._log = log
        self._base = 0.0
        self._span = 100.0

    def set_range(self, start_pct, end_pct):
        self._base = start_pct
        self._span = end_pct - start_pct

    def fraction(self, value):
        """Report progress within the current source, 0.0-1.0."""
        if self._progress and value is not None:
            self._progress(self._base + self._span * max(0.0, min(1.0, value)))

    def check_cancelled(self):
        if self._is_cancelled and self._is_cancelled():
            raise Cancelled()

    def log(self, message):
        if self._log:
            self._log(message)

    def status(self, text):
        """One line on what is happening now (replaces the last one; not the log)."""
        if self._status and text:
            self._status(text)


def _now_iso(now):
    return datetime.datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")


def _iso_to_ts(value):
    try:
        return datetime.datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- the lock file


def _win_pid_alive(pid):
    """Is `pid` running, on Windows, without touching it.

    os.kill(pid, 0) must NOT be used on Windows: Python maps every signal other than
    CTRL_C_EVENT / CTRL_BREAK_EVENT to TerminateProcess, so "signal 0" would END the process in
    the lock file (the other QGIS mid-build). Ask the kernel through OpenProcess instead:
    ERROR_INVALID_PARAMETER (87) means no such pid (dead); ERROR_ACCESS_DENIED (5) means it
    exists but we may not query it (alive); otherwise read the exit code, where 259
    (STILL_ACTIVE) means running."""
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_INVALID_PARAMETER = 87
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return k32.GetLastError() != ERROR_INVALID_PARAMETER   # access-denied etc. -> exists
    try:
        code = wintypes.DWORD()
        if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True                        # could not read it, but the handle opened: it exists
    finally:
        k32.CloseHandle(handle)


def pid_alive(pid):
    """Is the process that wrote the lock still running?

    On Linux and macOS signal 0 asks the kernel without touching the process. A PermissionError
    means the pid belongs to somebody else's process and therefore exists, so it counts as
    alive: clearing a lock on a guess is worse than asking the user to wait. On Windows
    os.kill(pid, 0) would kill that process, so a kernel query is used instead."""
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        return False
    if os.name == "nt":
        try:
            return _win_pid_alive(pid)
        except Exception:
            return True                    # cannot tell on Windows: treat as alive, never kill
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError, TypeError):
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def read_lock(path):
    """{"pid", "age", "text"} of a lock file, or {} when there is none.

    `pid` is None for a lock written by an older version (it held no pid) or one that cannot be
    read; `age` is its age in seconds."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read(200).strip()
        age = max(0.0, time.time() - os.path.getmtime(path))
    except OSError:
        return {}
    first = text.split()[0] if text else ""
    try:
        pid = int(first)
    except ValueError:
        pid = None
    return {"pid": pid, "age": age, "text": text}


def lock_decision(info, stale_seconds=LOCK_STALE_SECONDS, alive=pid_alive, own_pid=None):
    """("free" | "clear" | "refuse", why) for a lock file read by read_lock().

    "clear" means the lock is left over from a run that no longer exists (QGIS was killed, the
    machine was restarted) and can be removed: that is the fix for "every later run refuses to
    start". "refuse" is kept for a lock whose process really is still working, where removing it
    would let two builds write the same pack."""
    if not info:
        return "free", ""
    pid = info.get("pid")
    age = float(info.get("age") or 0.0)
    me = os.getpid() if own_pid is None else own_pid
    if pid is not None and pid == me:
        # M23 fix: a lock whose pid is THIS process is stale only once it is older than the stale
        # window. A fresh same-pid lock is a live concurrent update - a reloaded plugin or a
        # sibling updater sharing the QGIS process and data folder - and must be refused, not
        # stolen; stealing it let two updates race and clobber the staging folder.
        if age > stale_seconds:
            return "clear", (f"it was left behind by this process (pid {pid}) "
                             f"{age / 3600.0:.1f} hours ago")
        return "refuse", "another update is already running in this process"
    if pid is None:
        if age > stale_seconds:
            return "clear", f"it records no process and is {age / 3600.0:.1f} hours old"
        return "refuse", "another update is already running"
    if not alive(pid):
        return "clear", f"the process that made it (pid {pid}) is no longer running"
    if age > stale_seconds:
        return "clear", (f"pid {pid} is still running but the lock is {age / 3600.0:.1f} hours "
                         "old, longer than an update can take")
    return "refuse", f"another update is already running (process {pid})"


def file_hash(path, algorithm="md5"):
    h = hashlib.new(algorithm)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# audit R11 (2026-11-07): a download is checked against the free space of the folder it goes
# to before it starts (a full disk used to surface as a bare OSError half way, or not at all
# when the body was held in RAM), and is written to <file>.part, renamed when complete, so a
# half-written file is never taken for a finished one.
MIN_FREE_BYTES = 256 * 1024 * 1024          # headroom kept free whatever the download size
EXTRACT_FACTOR = 2.0                        # a zip that is extracted needs about this much more


def _mb(n):
    return "{:,.0f} MB".format(float(n) / (1024.0 * 1024.0))


def free_bytes(folder):
    """Free bytes on the file system of `folder` (the nearest existing parent), or None when
    it cannot be told (never raises)."""
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


def ensure_free_space(folder, need_bytes, what="the download"):
    """UpdateError, in plain words, when `folder`'s file system has less than `need_bytes` +
    MIN_FREE_BYTES free; the free space it found otherwise (None when unknown)."""
    free = free_bytes(folder)
    if free is None or need_bytes is None:
        return free
    need = int(need_bytes) + MIN_FREE_BYTES
    if free < need:
        raise UpdateError(
            "Not enough disk space for {} in {}: {} free, about {} needed ({} for the data "
            "plus {} kept free). Free some space or choose another data folder.".format(
                what, folder, _mb(free), _mb(need), _mb(need_bytes), _mb(MIN_FREE_BYTES)))
    return free


def remote_size(http, url, feedback=None):
    """The Content-Length the server announces for `url` (int), or None when it does not say
    or the client has no head(); a failure here never stops the download."""
    head = getattr(http, "head", None)
    if head is None:
        return None
    try:
        headers = head(url) or {}
    except Cancelled:
        raise
    except Exception as e:                       # noqa: BLE001 - size is a courtesy
        if feedback is not None:
            feedback.log("size of {} not known before the download ({})".format(url, e))
        return None
    # a Range request answered with 206 gives the full size after the slash
    rng = str(headers.get("content-range") or "")
    if "/" in rng and rng.rsplit("/", 1)[1].strip().isdigit():
        return int(rng.rsplit("/", 1)[1].strip())
    size = str(headers.get("content-length") or "").strip()
    return int(size) if size.isdigit() and int(size) > 1 else None


def download_file(http, url, path, feedback, extract=False, what=None):
    """http.download(url, ...) into `path` with the audit R11 guards: the free space of the
    folder is checked against the announced size first (x EXTRACT_FACTOR for a zip that is
    extracted there), the body goes to `path`.part and is renamed to `path` only when the
    client returned without error, and a left-over .part is removed. Returns the size the
    server announced (None when unknown)."""
    folder = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(folder, exist_ok=True)
    size = remote_size(http, url, feedback)
    name = what or os.path.basename(path)
    if size is not None:
        need = int(size * (EXTRACT_FACTOR if extract else 1.0))
        ensure_free_space(folder, need, "{} ({}{})".format(
            name, _mb(size), ", extracted" if extract else ""))
    part = path + ".part"
    try:
        http.download(url, part, feedback)
        if not os.path.isfile(part):
            raise UpdateError("the download of {} wrote nothing to {}".format(url, part))
        if size is not None and os.path.getsize(part) != size:
            raise NetworkError("download interrupted ({} of {} bytes)".format(
                os.path.getsize(part), size))
        os.replace(part, path)
    finally:
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
    return size


def extract_zip(zip_path, dest_dir, feedback):
    """Verify and extract a zip archive. Entries that escape dest_dir, symbolic links and an
    archive that unpacks to more than zipsafe.MAX_DATASET_BYTES (or more than the disk holds,
    or is compressed harder than any real data set: a "zip bomb") are refused before anything
    is written (zipsafe, security audit pass 3)."""
    from . import zipsafe
    try:
        with zipfile.ZipFile(zip_path) as zf:
            # the limits first: testzip() reads every entry in full, bomb or not
            try:
                zipsafe.check(zf, dest_dir, zipsafe.MAX_DATASET_BYTES)
            except zipsafe.ZipRefused as e:
                raise UpdateError(f"Downloaded archive was not unpacked: {e}") from e
            bad = zf.testzip()
            if bad is not None:
                raise UpdateError(f"Downloaded archive is corrupt (bad entry: {bad})")
            for member in zf.infolist():
                feedback.check_cancelled()
                zf.extract(member, dest_dir)
    except zipfile.BadZipFile as e:
        raise UpdateError(f"Downloaded file is not a valid zip archive: {e}") from e


# --------------------------------------------------------------------------- sources


class Source:
    """One dataset kept in its own sub-folder of the data folder."""

    id = ""
    name = ""
    description = ""
    folder = ""
    # Minimum hours between checks. 0 = check every time the updater runs.
    min_check_interval_hours = 0
    # Glob patterns (relative to the source folder) for layers to load in QGIS.
    layer_globs = ()
    # Substrings of layer names to add to the project from multi-layer files
    # (empty = add every layer). Used to add only buildings from the OSM GeoPackage.
    layer_filter = ()

    def remote_version(self, http, feedback):
        """Return the remote version string cheaply, or None if the data must be
        downloaded to tell whether it changed."""
        raise NotImplementedError

    def fetch(self, http, staging_dir, feedback, remote_version):
        """Download and prepare the data in staging_dir. Return the version string."""
        raise NotImplementedError


class HttpFileSource(Source):
    """A single file (usually a zip) at a fixed URL.

    version_mode:
      "md5"     - the server publishes <url>.md5; the download is verified against it.
      "headers" - use ETag, or Last-Modified plus Content-Length.
    """

    def __init__(self, id, name, url, filename, folder, version_mode="headers",
                 extract=True, layer_globs=(), description="", layer_filter=()):
        self.id = id
        self.name = name
        self.url = url
        self.filename = filename
        self.folder = folder
        self.version_mode = version_mode
        self.extract = extract
        self.layer_globs = tuple(layer_globs)
        self.layer_filter = tuple(layer_filter)
        self.description = description

    def remote_version(self, http, feedback):
        if self.version_mode == "md5":
            self._md5_missing = False
            try:
                text = http.get_bytes(self.url + ".md5").decode("utf-8", "replace").strip()
            except UpdateError as e:
                # The publisher's checksum file is not there (HTTP 404, as for Geofabrik's
                # *-free.gpkg.zip): fall back to the header version; fetch() records and logs
                # the download's SHA-256 instead.
                self._md5_missing = True
                feedback.log(f"{self.name}: no published checksum at {self.url}.md5 ({e}); "
                             "the SHA-256 of the download will be recorded instead")
            else:
                digest = text.split()[0].lower() if text else ""
                if len(digest) != 32 or any(c not in "0123456789abcdef" for c in digest):
                    raise UpdateError(f"Unexpected checksum file contents from {self.url}.md5")
                return "md5:" + digest
        headers = http.head(self.url)
        etag = headers.get("etag")
        if etag:
            return "etag:" + etag.strip()
        modified = headers.get("last-modified")
        if modified:
            return f"modified:{modified.strip()}|{headers.get('content-length', '')}"
        return None

    def fetch(self, http, staging_dir, feedback, remote_version):
        os.makedirs(staging_dir, exist_ok=True)
        path = os.path.join(staging_dir, self.filename)
        feedback.log(f"{self.name}: downloading {self.url}")
        download_file(http, self.url, path, feedback,           # audit R11: space, .part
                      extract=self.extract and self.filename.lower().endswith(".zip"),
                      what=self.name)
        feedback.check_cancelled()
        version = remote_version
        sha256 = file_hash(path, "sha256")
        feedback.log(f"{self.name}: SHA-256 of the download {sha256}")
        with open(os.path.join(staging_dir, self.filename + ".sha256"), "w", encoding="utf-8") as f:
            f.write(f"{sha256}  {self.filename}  {self.url}\n")
        if self.version_mode == "md5" and not getattr(self, "_md5_missing", False):
            actual = "md5:" + file_hash(path, "md5")
            if remote_version and actual != remote_version:
                raise UpdateError(f"{self.name}: checksum mismatch after download; not installed")
            version = actual
        elif version is None:
            version = "sha256:" + sha256
        if self.extract and self.filename.lower().endswith(".zip"):
            extract_zip(path, staging_dir, feedback)
            os.remove(path)
        return version


class GitHubRepoSource(Source):
    """Source code of a GitHub repository branch, tracked by latest commit."""

    min_check_interval_hours = 0

    def __init__(self, id, name, owner, repo, branch, folder, description="",
                 commit=None, zip_sha256=None):
        # commit: pin to this commit instead of following the branch; zip_sha256: the
        # expected SHA-256 of GitHub's archive of that commit (refused on mismatch).
        self.commit = commit
        self.zip_sha256 = zip_sha256
        self.id = id
        self.name = name
        self.owner = owner
        self.repo = repo
        self.branch = branch
        self.folder = folder
        self.description = description

    def remote_version(self, http, feedback):
        if self.commit:
            return "git:" + self.commit          # pinned: the branch is not followed
        url = f"https://api.github.com/repos/{self.owner}/{self.repo}/commits/{self.branch}"
        try:
            data = json.loads(http.get_bytes(url, {"Accept": "application/vnd.github+json"}))
            return "git:" + data["sha"]
        except (ValueError, KeyError, TypeError) as e:
            raise UpdateError(f"{self.name}: unexpected response from GitHub") from e

    def fetch(self, http, staging_dir, feedback, remote_version):
        os.makedirs(staging_dir, exist_ok=True)
        ref = remote_version[4:] if remote_version else self.branch
        url = f"https://codeload.github.com/{self.owner}/{self.repo}/zip/{ref}"
        zip_path = os.path.join(staging_dir, "source.zip")
        download_file(http, url, zip_path, feedback, extract=True, what=self.name)  # audit R11
        sha256 = file_hash(zip_path, "sha256")
        feedback.log(f"{self.name}: SHA-256 of {url} is {sha256}")
        if self.zip_sha256 and self.commit and ref == self.commit and sha256 != self.zip_sha256:
            raise UpdateError(f"{self.name}: SHA-256 mismatch for {self.owner}/{self.repo}@{ref} "
                              f"(got {sha256}, expected {self.zip_sha256}); not installed")
        unpack = os.path.join(staging_dir, "_unpack")
        extract_zip(zip_path, unpack, feedback)
        os.remove(zip_path)
        # GitHub archives wrap everything in one top-level folder; flatten it.
        entries = os.listdir(unpack)
        top = os.path.join(unpack, entries[0]) if len(entries) == 1 else unpack
        for name in os.listdir(top):
            shutil.move(os.path.join(top, name), os.path.join(staging_dir, name))
        shutil.rmtree(unpack, ignore_errors=True)
        with open(os.path.join(staging_dir, "COMMIT.txt"), "w", encoding="utf-8") as f:
            f.write(f"{self.owner}/{self.repo}@{ref}\n")
        return remote_version or ("git:" + ref)


class OsmLayersSource(Source):
    """The five OSM_* pack layers, built from Geofabrik's North Dakota .osm.pbf.

    The pbf is the same OpenStreetMap data as the 'OpenStreetMap (Geofabrik)' source's
    GeoPackage, but with every tag, which the GeoPackage drops (culverts, fords, trailer
    parks, substations). Change detection is Geofabrik's published MD5 of the pbf; the pbf is
    deleted after the layers are written.
    """

    URL = "https://download.geofabrik.de/north-america/us/north-dakota-latest.osm.pbf"

    def __init__(self, id, name, folder, description=""):
        self.id = id
        self.name = name
        self.folder = folder
        self.description = description
        self.layer_globs = ("*.gpkg",)
        self.config_key = "osm-layers-1"

    def remote_version(self, http, feedback):
        text = http.get_bytes(self.URL + ".md5").decode("utf-8", "replace").strip()
        digest = text.split()[0].lower() if text else ""
        if len(digest) != 32 or any(c not in "0123456789abcdef" for c in digest):
            raise UpdateError(f"Unexpected checksum file contents from {self.URL}.md5")
        return "md5:" + digest

    def fetch(self, http, staging_dir, feedback, remote_version):
        from . import osm_layers
        os.makedirs(staging_dir, exist_ok=True)
        pbf = os.path.join(staging_dir, "north-dakota-latest.osm.pbf")
        feedback.log(f"{self.name}: downloading {self.URL}")
        feedback.set_range(0, 30)
        download_file(http, self.URL, pbf, feedback, extract=True, what=self.name)  # audit R11
        feedback.check_cancelled()
        actual = "md5:" + file_hash(pbf, "md5")
        if remote_version and actual != remote_version:
            raise UpdateError(f"{self.name}: checksum mismatch after download; not installed")
        feedback.set_range(30, 95)
        counts = osm_layers.build_layers(
            pbf, staging_dir, tick=lambda n: feedback.check_cancelled(), say=feedback.log)
        os.remove(pbf)
        osm_layers.write_summary(staging_dir, counts, actual, self.URL)
        feedback.log(f"{self.name}: " + ", ".join(f"{k} {v:,}" for k, v in counts.items()))
        return actual


# North Dakota's 53 county FIPS codes: 38001, 38003, ... 38105.
ND_COUNTY_FIPS = tuple(f"38{n:03d}" for n in range(1, 106, 2))


# NSI versions served by the API: /nsiapi/<version>/structures. The unversioned
# /nsiapi/structures follows whatever USACE makes the default (nsi2026 since 2026),
# so the plugin always names the version it wants.
NSI_VERSIONS = {"nsi2026": "2026", "nsi2022": "2022"}
# What the NSI dataset holds: both versions merged (2026 wins where they overlap),
# or one version on its own.
NSI_MODES = {
    "merged": ("nsi2026", "nsi2022"),
    "nsi2026": ("nsi2026",),
    "nsi2022": ("nsi2022",),
}


# What the NSI API returns is checked before it replaces the inventory in use (Core 1.5.0,
# security audit pass 3). USACE publishes no checksum to verify a download against, so the
# checks are on the content: every structure is a point with real coordinates inside the box
# below, a response is not absurdly large, and a new inventory is not much smaller than the
# one it would replace (a cut-short or wrong answer would silently drop structures from
# every hazard classification). The SHA-256 of each response is kept in nsi_summary.json.
ND_NSI_BOUNDS = (-104.6, 45.4, -96.0, 49.5)      # west, south, east, north: ND with a margin
NSI_MAX_COUNTY_BYTES = 512 * 1024 * 1024         # one county, one version (real: < 60 MB)
NSI_MAX_BAD_SHARE = 0.01                         # malformed / out-of-box points tolerated
NSI_MAX_DROP_SHARE = 0.20                        # fewer structures than the inventory in use
NSI_ALLOW_DROP_ENV = "NDSUITE_NSI_ALLOW_DROP"    # 1 = accept a smaller inventory this once


def nsi_feature_ok(feat, bounds=None):
    """True for a GeoJSON point feature with finite coordinates (inside `bounds` when given:
    west, south, east, north) and a properties object."""
    if not isinstance(feat, dict):
        return False
    props = feat.get("properties")
    if props is not None and not isinstance(props, dict):
        return False
    geom = feat.get("geometry")
    if not isinstance(geom, dict) or geom.get("type") != "Point":
        return False
    xy = geom.get("coordinates")
    if not isinstance(xy, (list, tuple)) or len(xy) < 2:
        return False
    x, y = xy[0], xy[1]
    if isinstance(x, bool) or isinstance(y, bool) or \
            not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return False
    if x != x or y != y or abs(x) > 180.0 or abs(y) > 90.0:
        return False
    if bounds is not None:
        w, s, e, n = bounds
        if not (w <= x <= e and s <= y <= n):
            return False
    return True


class NsiApiSource(Source):
    """The published National Structure Inventory for North Dakota, from the NSI API.

    The API has no version header, so the counties are downloaded (at most every
    min_check_interval_hours) and a hash of the normalized features decides whether
    anything changed. mode "merged" downloads NSI 2026 and NSI 2022 and merges them
    county by county (nsi_merge.py: the 2026 point wins where both have a structure,
    2022-only structures are kept and flagged). A merger callable, when given,
    combines the county files into one GeoPackage (the plugin passes a GDAL-based
    merger).
    """

    API = "https://nsi.sec.usace.army.mil/nsiapi"

    def __init__(self, id, name, folder, county_fips=ND_COUNTY_FIPS, merger=None,
                 check_interval_hours=24 * 30, description="", mode="merged",
                 match_m=None):
        from . import nsi_merge
        if mode not in NSI_MODES:
            raise ValueError(f"unknown NSI mode {mode!r}")
        self.id = id
        self.name = name
        self.folder = folder
        self.county_fips = tuple(county_fips)
        self.merger = merger
        self.min_check_interval_hours = check_interval_hours
        self.description = description
        self.mode = mode
        self.match_m = float(match_m if match_m is not None else nsi_merge.DEFAULT_MATCH_M)
        self.layer_globs = ("nsi_nd.gpkg",) if merger else ("counties/*.geojson",)
        # A different mode or match distance needs a rebuild even inside the
        # check interval.
        self.config_key = mode + (f"|{self.match_m:g}m" if mode == "merged" else "")
        # the coordinate box applies to North Dakota counties (FIPS 38xxx) only
        self.bounds = ND_NSI_BOUNDS if all(str(f).startswith("38") for f in self.county_fips) \
            else None
        self._raw_sha256 = {}
        self._invalid = 0

    def remote_version(self, http, feedback):
        return None

    def _get_county(self, http, version, fips):
        raw = http.get_bytes(f"{self.API}/{version}/structures?fips={fips}&fmt=fc")
        if len(raw) > NSI_MAX_COUNTY_BYTES:
            raise UpdateError(f"{self.name}: county {fips} ({version}) returned "
                              f"{len(raw):,} bytes, far more than a county's structures")
        self._raw_sha256.setdefault(str(fips), {})[version] = hashlib.sha256(
            raw if isinstance(raw, bytes) else str(raw).encode("utf-8")).hexdigest()
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise UpdateError(f"{self.name}: county {fips} ({version}) returned invalid JSON") from e
        if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
            raise UpdateError(f"{self.name}: county {fips} ({version}) returned an "
                              "unexpected response")
        features = data.get("features") or []
        if not isinstance(features, list):
            raise UpdateError(f"{self.name}: county {fips} ({version}) returned an "
                              "unexpected response")
        # Core 1.5.0: only well-formed points inside the state's box are kept. A few bad
        # records are dropped and counted; more than NSI_MAX_BAD_SHARE means the answer is
        # not this county's inventory, and nothing is installed.
        good = [f for f in features if nsi_feature_ok(f, self.bounds)]
        bad = len(features) - len(good)
        if bad > max(5, NSI_MAX_BAD_SHARE * len(features)):
            raise UpdateError(
                f"{self.name}: county {fips} ({version}): {bad:,} of {len(features):,} "
                "structures are not points inside North Dakota; the response was not used "
                "and the inventory in use was kept")
        self._invalid += bad
        features = good
        # Hash features in a stable order so an identical inventory hashes identically.
        features.sort(key=lambda f: str((f.get("properties") or {}).get("fd_id", "")))
        return features

    def _previous_summary(self, staging_dir):
        """nsi_summary.json of the inventory in use (<data folder>/<folder>), or None."""
        live = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(staging_dir))),
                            self.folder, "nsi_summary.json")
        try:
            with open(live, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _check_not_shrunk(self, staging_dir, n_new, counties_new, environ=None):
        """UpdateError when the new inventory is much smaller than the one in use, of the
        same mode: fewer structures by more than NSI_MAX_DROP_SHARE, or a county that had
        structures and now has none. NDSUITE_NSI_ALLOW_DROP=1 accepts it."""
        env = os.environ if environ is None else environ
        if str(env.get(NSI_ALLOW_DROP_ENV, "")).strip().lower() in ("1", "true", "yes", "on"):
            return
        prev = self._previous_summary(staging_dir)
        if not prev or prev.get("mode") != self.mode:
            return
        n_old = prev.get("structures")
        if not isinstance(n_old, int):
            return                       # written before Core 1.5.0: nothing to compare with
        gone = sorted(set(prev.get("counties_with_structures") or []) - set(counties_new))
        if n_old > 0 and n_new < (1.0 - NSI_MAX_DROP_SHARE) * n_old:
            why = (f"the API returned {n_new:,} structures, {100.0 * (n_old - n_new) / n_old:.0f}"
                   f"% fewer than the {n_old:,} of the inventory in use")
        elif gone:
            why = (f"{len(gone)} count{'y' if len(gone) == 1 else 'ies'} with structures in "
                   f"the inventory in use came back empty (FIPS {', '.join(gone[:8])}"
                   f"{' ...' if len(gone) > 8 else ''})")
        else:
            return
        raise UpdateError(
            f"{self.name}: {why}. A cut-short answer would drop structures from every hazard "
            "classification, so the inventory in use was kept. If the smaller inventory is "
            f"real, set {NSI_ALLOW_DROP_ENV}=1 and update again.")

    def fetch(self, http, staging_dir, feedback, remote_version):
        from . import nsi_merge
        counties_dir = os.path.join(staging_dir, "counties")
        os.makedirs(counties_dir, exist_ok=True)
        versions = NSI_MODES[self.mode]
        digest = hashlib.sha256(self.config_key.encode())
        written = []
        totals = {}
        per_county = {}
        total = len(self.county_fips)
        self._raw_sha256, self._invalid = {}, 0
        n_structures, with_structures = 0, []
        for i, fips in enumerate(self.county_fips):
            by_version = {}
            for version in versions:
                feedback.check_cancelled()
                features = self._get_county(http, version, fips)
                digest.update(f"{fips}:{version}".encode())
                for feat in features:
                    digest.update(json.dumps(feat, sort_keys=True,
                                             separators=(",", ":")).encode())
                by_version[version] = features
            if self.mode == "merged":
                features, stats = nsi_merge.merge_versions(
                    by_version["nsi2022"], by_version["nsi2026"], match_m=self.match_m)
            else:
                features = by_version[versions[0]]
                for feat in features:
                    (feat.setdefault("properties", {}))["nsi_version"] = NSI_VERSIONS[versions[0]]
                stats = {"points_" + NSI_VERSIONS[versions[0]]: len(features)}
            by_version = None
            per_county[fips] = stats
            for k, v in stats.items():
                totals[k] = totals.get(k, 0) + v
            if features:
                path = os.path.join(counties_dir, f"{fips}.geojson")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump({"type": "FeatureCollection", "features": features}, f,
                              separators=(",", ":"))
                written.append(path)
                n_structures += len(features)
                with_structures.append(str(fips))
            features = None
            feedback.fraction((i + 1) / total * (0.8 if self.merger else 1.0))
        if not written:
            raise UpdateError(f"{self.name}: the API returned no structures")
        self._check_not_shrunk(staging_dir, n_structures, with_structures)
        with open(os.path.join(staging_dir, "nsi_summary.json"), "w", encoding="utf-8") as f:
            json.dump({"mode": self.mode, "versions": list(versions),
                       "match_m": self.match_m if self.mode == "merged" else None,
                       "totals": totals, "counties": per_county,
                       # Core 1.5.0: what the next update is compared with, and what was
                       # downloaded (SHA-256 of each API response, county by version)
                       "structures": n_structures,
                       "counties_with_structures": with_structures,
                       "invalid_dropped": self._invalid,
                       "response_sha256": self._raw_sha256}, f, indent=1, sort_keys=True)
        feedback.log(f"{self.name}: " + ", ".join(f"{k} {v:,}" for k, v in sorted(totals.items())))
        if self._invalid:
            feedback.log(f"{self.name}: {self._invalid:,} record(s) that were not points "
                         "inside North Dakota were left out")
        if self.merger:
            feedback.log(f"{self.name}: merging {len(written)} counties into nsi_nd.gpkg")
            self.merger(written, os.path.join(staging_dir, "nsi_nd.gpkg"))
            shutil.rmtree(counties_dir, ignore_errors=True)
        return "sha256:" + digest.hexdigest()


# --------------------------------------------------------------------------- updater


class Result:
    def __init__(self, source, status, message="", version=None):
        self.source_id = source.id
        self.name = source.name
        self.folder = source.folder
        self.status = status  # updated | current | skipped | offline | error | cancelled
        self.message = message
        self.version = version

    def __repr__(self):
        return f"Result({self.source_id!r}, {self.status!r}, {self.message!r})"


def _read_json_dict(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class Updater:
    def __init__(self, data_dir, sources, http, now=time.time):
        self.data_dir = data_dir
        self.sources = list(sources)
        self.http = http
        self.now = now

    # manifest -------------------------------------------------------------

    @property
    def manifest_path(self):
        return os.path.join(self.data_dir, MANIFEST_NAME)

    def load_manifest(self):
        if os.path.isfile(self.manifest_path):
            return _read_json_dict(self.manifest_path)
        # 1.3.x wrote manifest.json: take its per-source entries once; the next save writes
        # structures_manifest.json. The suite's manifest.json ("datasets", "updated", ...)
        # has no per-source dict entries, so nothing of it is taken.
        legacy = _read_json_dict(os.path.join(self.data_dir, LEGACY_MANIFEST_NAME))
        if legacy.get("format") or "datasets" in legacy:
            return {}
        return {k: v for k, v in legacy.items() if isinstance(v, dict)}

    def _save_manifest(self, manifest):
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, sort_keys=True)
        os.replace(tmp, self.manifest_path)

    def source_path(self, source):
        return os.path.join(self.data_dir, source.folder)

    # locking --------------------------------------------------------------

    def _acquire_lock(self, feedback=None):
        """(path, "") when the lock is ours, or (None, why) when another run holds it.

        A lock whose process is gone is cleared and said so once in the log, instead of refusing
        every run for six hours."""
        path = os.path.join(self.data_dir, LOCK_NAME)
        what, why = lock_decision(read_lock(path))
        if what == "clear":
            try:
                os.remove(path)
            except OSError:
                pass
            if feedback:
                feedback.log(f"Cleared the left-over lock file {path}: {why}")
        elif what == "refuse":
            return None, why
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return None, "another update started at the same moment"
        os.write(fd, f"{os.getpid()} {_now_iso(time.time())}".encode())
        os.close(fd)
        return path, ""

    # main entry -----------------------------------------------------------

    def check_and_update(self, feedback=None, force=False, only=None):
        """Check every source (or the ids in `only`) and install any changes.

        force=True ignores per-source minimum check intervals.
        """
        feedback = feedback or Feedback()
        os.makedirs(self.data_dir, exist_ok=True)
        lock, why = self._acquire_lock(feedback)
        if lock is None:
            raise UpdateError(f"Not started: {why} for this data folder. Wait for it to finish, "
                              f"or delete {os.path.join(self.data_dir, LOCK_NAME)} if you are "
                              "sure nothing is running.")
        try:
            shutil.rmtree(os.path.join(self.data_dir, STAGING_NAME), ignore_errors=True)
            sources = [s for s in self.sources if only is None or s.id in only]
            results = []
            for i, source in enumerate(sources):
                feedback.set_range(100.0 * i / len(sources), 100.0 * (i + 1) / len(sources))
                feedback.fraction(0.0)
                feedback.status(f"{source.name} ({i + 1} of {len(sources)})")
                result = self._update_one(source, feedback, force)
                results.append(result)
                feedback.log(f"{source.name}: {result.status}"
                             + (f" - {result.message}" if result.message else ""))
                if result.status == "cancelled":
                    break
            shutil.rmtree(os.path.join(self.data_dir, STAGING_NAME), ignore_errors=True)
            return results
        finally:
            try:
                os.remove(lock)
            except OSError:
                pass

    def _update_one(self, source, feedback, force):
        manifest = self.load_manifest()
        entry = manifest.get(source.id, {})
        target = self.source_path(source)
        have_local = os.path.isdir(target) and bool(os.listdir(target))
        now = self.now()

        config_key = getattr(source, "config_key", None)
        same_config = entry.get("config") == config_key
        if not force and have_local and same_config and source.min_check_interval_hours:
            last = _iso_to_ts(entry.get("checked_at"))
            if last is not None and now - last < source.min_check_interval_hours * 3600:
                days = (source.min_check_interval_hours * 3600 - (now - last)) / 86400
                return Result(source, "skipped", f"next check in {days:.0f} day(s)",
                              entry.get("version"))

        if getattr(source, "in_place", False):
            return self._update_in_place(source, feedback, force, entry, config_key, now)

        staging = os.path.join(self.data_dir, STAGING_NAME, f"{source.id}-{uuid.uuid4().hex[:8]}")
        try:
            feedback.check_cancelled()
            # audit R11: the staging folder's file system must have room at all before a
            # source starts (the per-download check knows the size; this one catches a full
            # disk before the first request)
            ensure_free_space(self.data_dir, 0, f"{source.name}")
            remote = source.remote_version(self.http, feedback)
            if (remote is not None and remote == entry.get("version") and have_local
                    and same_config):
                status = Result(source, "current", "", remote)
            else:
                version = source.fetch(self.http, staging, feedback, remote)
                feedback.check_cancelled()
                if version == entry.get("version") and have_local:
                    status = Result(source, "current", "", version)
                else:
                    self._swap(staging, target)
                    entry["version"] = version
                    entry["updated_at"] = _now_iso(now)
                    status = Result(source, "updated",
                                    "installed" if not have_local else "new data installed",
                                    version)
            entry["checked_at"] = _now_iso(now)
            entry.pop("last_error", None)
            if config_key is None:
                entry.pop("config", None)
            else:
                entry["config"] = config_key
        except Cancelled:
            return Result(source, "cancelled", "cancelled by user", entry.get("version"))
        except NetworkError as e:
            entry["last_error"] = f"offline: {e}"
            status = Result(source, "offline",
                            "using local copy" if have_local else f"not downloaded yet ({e})",
                            entry.get("version"))
        except Exception as e:  # noqa: BLE001 - one bad source must not stop the rest
            entry["last_error"] = str(e)
            status = Result(source, "error", str(e) or e.__class__.__name__, entry.get("version"))
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        entry["name"] = source.name
        entry["folder"] = source.folder
        manifest = self.load_manifest()
        manifest[source.id] = entry
        self._save_manifest(manifest)
        return status

    def _update_in_place(self, source, feedback, force, entry, config_key, now):
        """A source that builds into its own folder and resumes there (the offline pack): it is
        not staged and swapped, because a stopped build must keep the layers it finished."""
        target = self.source_path(source)
        try:
            feedback.check_cancelled()
            status, message, version = source.update(self.http, target, feedback, force, entry)
            result = Result(source, status, message, version or entry.get("version"))
            if status == "updated":
                entry["version"] = version
                entry["updated_at"] = _now_iso(now)
            entry["checked_at"] = _now_iso(now)
            entry.pop("last_error", None)
            entry["config"] = config_key
        except Cancelled:
            return Result(source, "cancelled", "cancelled by user - the finished layers are kept",
                          entry.get("version"))
        except NetworkError as e:
            entry["last_error"] = f"offline: {e}"
            result = Result(source, "offline", f"using the local copy ({e})", entry.get("version"))
        except Exception as e:  # noqa: BLE001 - one bad source must not stop the rest
            entry["last_error"] = str(e)
            result = Result(source, "error", str(e) or e.__class__.__name__, entry.get("version"))
        entry["name"] = source.name
        entry["folder"] = source.folder
        manifest = self.load_manifest()
        manifest[source.id] = entry
        self._save_manifest(manifest)
        return result

    @staticmethod
    def _swap(staging, target):
        """Replace target with staging using renames (same file system)."""
        backup = target + ".previous"
        shutil.rmtree(backup, ignore_errors=True)
        if os.path.exists(target):
            os.rename(target, backup)
        try:
            os.rename(staging, target)
        except OSError:
            if os.path.exists(backup) and not os.path.exists(target):
                os.rename(backup, target)
            raise
        shutil.rmtree(backup, ignore_errors=True)


class OvertureSource(Source):
    """Overture Maps buildings for a bounding box, downloaded and converted to GeoPackage.

    Uses the overturemaps CLI if installed (pip install overturemaps). Writes
    overture_buildings.gpkg so suite_fill and the offline pack can find it.
    """

    ND_BBOX = "-104.05,45.94,-96.55,49.00"  # west,south,east,north

    def __init__(self, id, name, folder, description="", check_interval_hours=24 * 30):
        self.id = id
        self.name = name
        self.folder = folder
        self.description = description
        # Overture publishes a new release about once a month; without a check interval the
        # whole bounding box was re-downloaded on every run. Skip a re-download until the
        # interval passes (overridable; force=True ignores it).
        self.min_check_interval_hours = check_interval_hours
        # Output filenames so "Add North Dakota Layers to Project" can find the layer.
        self.layer_globs = ("overture_buildings.gpkg", "overture_buildings.geojson")

    def remote_version(self, http, feedback):
        # Overture releases are dated; use a fixed check interval instead of a remote version.
        return None

    def fetch(self, http, staging_dir, feedback, remote_version):
        os.makedirs(staging_dir, exist_ok=True)
        out_geojson = os.path.join(staging_dir, "overture_buildings.geojson")
        out_gpkg = os.path.join(staging_dir, "overture_buildings.gpkg")

        feedback.log(f"{self.name}: downloading Overture buildings for ND bbox")
        # Prefer the overturemaps CLI
        import shutil
        import subprocess
        cli = shutil.which("overturemaps")
        if cli:
            cmd = [
                cli, "download",
                "--bbox", self.ND_BBOX,
                "-f", "geojson",
                "--type", "building",
                "-o", out_geojson,
            ]
            feedback.log(f"Running: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600, errors="replace")
            if result.returncode != 0:
                raise UpdateError(f"overturemaps CLI failed: {result.stderr[:500]}")
        else:
            raise UpdateError(
                "overturemaps CLI not found. Install with: pip install overturemaps\n"
                "Then re-run the update. The CLI downloads only the ND bounding box."
            )

        # Convert GeoJSON to GeoPackage if ogr2ogr / gdal is available
        if os.path.exists(out_geojson) and os.path.getsize(out_geojson) > 100:
            ogr = shutil.which("ogr2ogr")
            if ogr:
                feedback.log(f"{self.name}: converting to GeoPackage")
                subprocess.run(
                    [ogr, "-f", "GPKG", out_gpkg, out_geojson, "-nln", "overture_buildings"],
                    check=True, capture_output=True,
                )
                os.remove(out_geojson)
            else:
                # Keep the GeoJSON; suite_fill can still read it
                feedback.log(f"{self.name}: ogr2ogr not found, keeping GeoJSON")
        else:
            raise UpdateError(f"{self.name}: download produced no data")

        return "overture:" + datetime.datetime.now().strftime("%Y-%m-%d")


def arcgis_page_done(n, page, exceeded):
    """True when this FeatureServer page is the last one.

    A service that caps a page below the count we asked for (often 1,000) sets
    exceededTransferLimit. Stopping just because the page is shorter than 2,000
    would keep only the first page.
    """
    if n <= 0:
        return True
    if exceeded is True:
        return False
    if exceeded is False:
        return True
    return n < page

class ArcGISFeatureSource(Source):
    """Download a layer from an ArcGIS FeatureServer by paging queries.

    Writes a GeoJSON (and GeoPackage if ogr2ogr is available) named after the
    folder so suite_fill and the offline pack can find it.
    """

    def __init__(self, id, name, url, folder, where="1=1", out_fields="*",
                 description="", layer_name=None, check_interval_hours=24):
        self.id = id
        self.name = name
        self.url = url.rstrip("/")
        self.folder = folder
        self.where = where
        self.out_fields = out_fields
        self.description = description
        self.layer_name = layer_name or folder
        # Without a check interval the whole FeatureServer was paged and downloaded on every
        # run. Check at most this often (default daily; overridable; force=True ignores it).
        self.min_check_interval_hours = check_interval_hours
        # Output filenames so "Add North Dakota Layers to Project" can find the layer.
        self.layer_globs = (f"{self.layer_name}.gpkg", f"{self.layer_name}.ndjson")

    def remote_version(self, http, feedback):
        """A cheap change signal so an unchanged FeatureServer is skipped instead of paged and
        re-downloaded in full: the layer's last-edit timestamp and/or feature count (one HEAD-like
        metadata read plus a returnCountOnly query). Returns None when neither can be read, in
        which case fetch() downloads and records the feature count as the version. A real network
        failure propagates so the source is reported offline and the local copy is kept."""
        def _probe(url, params):
            try:
                data = http.get_json(url, params)
            except NetworkError:
                raise
            except Exception:
                return None
            return data if isinstance(data, dict) else None

        parts = []
        meta = _probe(self.url, {"f": "json"})
        if meta:
            editing = meta.get("editingInfo")
            if isinstance(editing, dict):
                last_edit = editing.get("lastEditDate") or editing.get("dataLastEditDate")
                if last_edit is not None:
                    parts.append(f"edit:{last_edit}")
        cnt = _probe(self.url + "/query",
                     {"where": self.where, "returnCountOnly": "true", "f": "json"})
        if cnt is not None and isinstance(cnt.get("count"), int):
            parts.append(f"count:{cnt['count']}")
        return "arcgis:" + "|".join(parts) if parts else None

    def fetch(self, http, staging_dir, feedback, remote_version):
        import json
        import os
        import time
        os.makedirs(staging_dir, exist_ok=True)
        out_ndjson = os.path.join(staging_dir, f"{self.layer_name}.ndjson")
        out_gpkg = os.path.join(staging_dir, f"{self.layer_name}.gpkg")

        feedback.log(f"{self.name}: querying ArcGIS FeatureServer")
        query_url = self.url + "/query"
        page = 2000
        try:
            meta = http.get_json(self.url, {"f": "json"})
            page = int(meta.get("maxRecordCount") or page)
        except Exception:
            page = 2000
        page = max(1, min(page, 2000))
        params_base = {
            "where": self.where,
            "outFields": self.out_fields,
            "returnGeometry": "true",
            "f": "geojson",
            "resultRecordCount": page,
        }
        offset = 0
        total = 0
        pages = 0
        prev_ids = None
        # Guard against a FeatureServer that ignores resultOffset while it keeps setting
        # exceededTransferLimit: it would hand back the same page forever and grow the NDJSON
        # without bound. 10,000 pages is far above any North Dakota layer (the largest is
        # ~440k points) yet still caps a runaway loop.
        max_pages = 10000
        with open(out_ndjson, "w", encoding="utf-8") as fh:
            while True:
                feedback.check_cancelled()
                params = dict(params_base)
                params["resultOffset"] = offset
                try:
                    data = http.get_json(query_url, params)
                except Exception as e:
                    raise UpdateError(f"ArcGIS query failed at offset {offset}: {e}")
                feats = data.get("features") or []
                n = len(feats)
                exceeded = data.get("exceededTransferLimit")
                if n == 0:
                    break
                # A server that ignores resultOffset returns the previous page unchanged; stop
                # before writing the same features a second time (feature ids are stable when
                # the layer has an OBJECTID, which these queries request).
                page_ids = tuple(f.get("id") for f in feats)
                if (prev_ids is not None and page_ids == prev_ids
                        and all(i is not None for i in page_ids)):
                    raise UpdateError(
                        f"{self.name}: the FeatureServer ignored resultOffset and returned the "
                        f"same {n} features again at offset {offset}; aborting so the download "
                        "does not loop and grow without bound")
                prev_ids = page_ids
                for feat in feats:
                    fh.write(json.dumps(feat) + "\n")
                total += n
                offset += n
                pages += 1
                feedback.log(f"{self.name}: {total} features")
                if arcgis_page_done(n, page, exceeded):
                    break
                if pages >= max_pages:
                    raise UpdateError(
                        f"{self.name}: stopped after {max_pages} pages ({total} features) to "
                        "avoid an unbounded download; the FeatureServer may be ignoring paging")
                time.sleep(0.2)

        if total == 0:
            raise UpdateError(f"{self.name}: no features returned")

        feedback.log(f"{self.name}: wrote {total} features to NDJSON")

        # Prefer GeoPackage if ogr2ogr is available
        import shutil
        import subprocess
        ogr = shutil.which("ogr2ogr")
        if ogr and os.path.exists(out_ndjson):
            feedback.log(f"{self.name}: converting to GeoPackage")
            # ogr2ogr can read GeoJSONSeq / NDJSON with the GeoJSONSeq driver
            subprocess.run(
                [ogr, "-f", "GPKG", out_gpkg, out_ndjson,
                 "-nln", self.layer_name, "-oo", "AUTODETECT_TYPE=YES"],
                check=True, capture_output=True,
            )
            os.remove(out_ndjson)
        else:
            # Fall back to a FeatureCollection GeoJSON (may be large)
            feedback.log(f"{self.name}: ogr2ogr not found; keeping NDJSON (GeoJSONSeq)")
            # Optionally write a small sidecar note
            with open(os.path.join(staging_dir, "README.txt"), "w", encoding="utf-8") as note:
                note.write(
                    f"{self.layer_name}.ndjson is a newline-delimited GeoJSON "
                    f"(GeoJSONSeq). Load with GDAL GeoJSONSeq driver or QGIS.\n"
                    f"Source: {self.url}\nFeatures: {total}\n"
                )

        # Reuse the cheap version signal from remote_version() so a later run that reads the
        # same signal detects "current" without paging the whole service again.
        return remote_version or f"arcgis-{total}"
