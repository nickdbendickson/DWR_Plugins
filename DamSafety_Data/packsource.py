"""1.7.0: DamSafety Data keeps its offline pack up to date, the way the ND Data Updater does.

One source, "pack", in the updater's list (sources.py). It looks after the pack that
"Build offline pack..." made (pack_build.py) in the folder chosen there: each check reads the
pack's manifest, builds the layers that are missing or not complete, fetches again the layers
older than the refresh interval (default 30 days), and leaves what is current alone. Both
formats are rewritten from the same download (ND_Dam_Suite_Pack and nd_offline_pack), only the
layers that changed in format 2. A layer that cannot be fetched keeps its old copy (the work
database keeps it, and pack_build lists it as failed).

What it does not do:
  - start a pack: the first build takes hours and asks for the DWR / HIFLD / land cover files,
    so it stays in the Build offline pack window. Without a pack built there this source is
    not in the list (sources.pack_source returns None).
  - fetch over a local file: layers built from the user's DWR or HIFLD files are rebuilt only
    when missing (from the folders last used in the build window), never by age, so a DWR
    GeoPackage is not replaced by the MapService download on day 31.
  - touch a pack it did not build (pack_build.check_target refuses one without its mark).

The pack the plugins read is updated in place. That is the point of this source, and it is safe
for the same reason it is in the ND Data Updater: every layer file is written beside the old one
and renamed over it (gpkgpack.write_layer, pack_build.write_format1), so a reader sees the old
file or the new one. pack_build's own lock keeps the window and this source from building the
same folder at once.

No QGIS imports here.
"""
import datetime
import os

from . import core

FOLDER = "ND_Dam_Suite_Pack"
LOCAL_SOURCES = ("ND DWR file", "HIFLD file supplied")     # manifest "source" of local layers
DEFAULT_REFRESH_DAYS = 30


def pack_builder():
    from . import pack_build
    return pack_build


def built_here(out_parent, builder=None):
    """The ND_Dam_Suite_Pack folder in out_parent when Build offline pack made it, else ""."""
    if not out_parent:
        return ""
    B = builder or pack_builder()
    p2 = os.path.join(os.path.abspath(os.path.expanduser(out_parent)), B.PACK2)
    man = B._read_json(os.path.join(p2, "manifest.json"))
    return p2 if isinstance(man, dict) and man.get(B.MARK) else ""


class DataPackSource(core.Source):
    """See the module text. `in_place`: built straight into the pack folder (a stopped build
    resumes there), not staged and swapped like the other sources."""

    in_place = True
    min_check_interval_hours = 24

    def __init__(self, id, name, out_parent, refresh_days=DEFAULT_REFRESH_DAYS, description="",
                 dwr_folder=None, hifld_folder=None, builder=None, now=None):
        self.id = id
        self.name = name
        self.description = description
        self.out_parent = os.path.abspath(os.path.expanduser(out_parent))
        self._builder = builder
        # absolute: Updater.source_path joins it to the data folder, which keeps it as it is
        self.folder = os.path.join(self.out_parent, self.builder.PACK2)
        self.refresh_days = max(1, int(refresh_days or DEFAULT_REFRESH_DAYS))
        self.dwr_folder = dwr_folder or None
        self.hifld_folder = hifld_folder or None
        self._now = now or datetime.date.today
        self.layer_globs = ()                 # dozens of layers: Setup adds the ones it needs
        self.config_key = "{}d".format(self.refresh_days)
        self.failed = {}

    @property
    def builder(self):
        if self._builder is None:
            self._builder = pack_builder()
        return self._builder

    def remote_version(self, http, feedback):
        return None

    def fetch(self, http, staging_dir, feedback, remote_version):  # pragma: no cover
        raise NotImplementedError("the pack source builds in place (update())")

    # ------------------------------------------------------------ what to do
    def _manifest(self):
        m = self.builder._read_json(os.path.join(self.folder, "manifest.json"))
        return m if isinstance(m, dict) else {}

    def _age_days(self, fetched):
        try:
            day = datetime.date.fromisoformat(str(fetched or "")[:10])
        except ValueError:
            return None
        return (self._now() - day).days

    def plan(self):
        """(missing, stale, why): layer names to build, names to fetch again, one line."""
        B = self.builder
        man = self._manifest()
        sets = man.get("datasets") or {}
        rasters = man.get("rasters") or {}
        missing, stale = [], []
        for t in B.registry(self.dwr_folder, self.hifld_folder):
            name, kind = t["name"], t["kind"]
            if kind == "none":
                continue                      # cannot be fetched (listed as left out)
            if kind == "nlcd":
                if name not in rasters:
                    missing.append(name)
                continue                      # the 2021 land cover does not change
            names = [c[0] for c in t["spec"]["choices"]] if kind == "first_of" else [name]
            have = [sets[n] for n in names if isinstance(sets.get(n), dict)]
            if not have:
                missing.append(name)
                continue
            e = have[0]
            if kind in ("local_gpkg", "ogr") or \
                    str(e.get("source") or "").startswith(LOCAL_SOURCES):
                continue                      # the user's own file: never fetched by age
            age = self._age_days(e.get("fetched"))
            if e.get("status") not in ("complete", "not available") or age is None or \
                    age > self.refresh_days:
                stale.append(name)
        why = []
        if missing:
            why.append("{} layer(s) not in the pack yet".format(len(missing)))
        if stale:
            why.append("{} layer(s) older than {} days or incomplete".format(
                len(stale), self.refresh_days))
        return missing, stale, "; ".join(why)

    # ---------------------------------------------------------------- update
    def update(self, http, target, feedback, force, entry):
        """Refresh the pack. Returns (status, message, version)."""
        B = self.builder
        if not built_here(self.out_parent, B):
            raise core.UpdateError("No offline pack built in {} yet: use Build offline pack… "
                                   "first; after that it is kept up to date here.".format(
                                       self.out_parent))
        missing, stale, why = self.plan()
        if not missing and not stale:
            return "current", "", entry.get("version") or "built"
        feedback.log("{}: {}".format(self.name, why))

        def progress(pct, msg=""):
            feedback.fraction(pct / 100.0)
            if msg:
                feedback.status(msg)

        def cancel():
            try:
                feedback.check_cancelled()
            except core.Cancelled:
                return True
            return False

        kw = dict(dwr_folder=self.dwr_folder, hifld_folder=self.hifld_folder,
                  # the plugins may be reading this pack: that is what is being kept up to date
                  # (layer files are replaced by rename; check_target still refuses a pack that
                  # was not built here)
                  active=(), progress=progress, log=feedback.log, cancel=cancel)
        failed = {}
        try:
            r = None
            if missing:
                r = B.build(self.out_parent, only=missing, refresh=False, **kw)
                failed.update(r.get("failed") or {})
            if stale:
                r = B.build(self.out_parent, only=stale, refresh=True, **kw)
                failed.update(r.get("failed") or {})
        except B.BP.BuildCancelled as e:
            raise core.Cancelled() from e
        except B.PackBuildError as e:
            raise core.UpdateError(str(e)) from e
        self.failed = failed
        if failed and set(failed) >= set(missing + stale):
            raise core.NetworkError("no layer could be fetched ({}); the pack is unchanged".format(
                "; ".join("{}: {}".format(k, v) for k, v in sorted(failed.items())[:3])))
        man = (r or {}).get("manifest2") or self._manifest()
        n = len(man.get("datasets") or {}) + len(man.get("rasters") or {})
        msg = "{} layers, both formats".format(n)
        if failed:
            msg += "; not fetched (old copy kept): " + ", ".join(sorted(failed))
        version = "built:{}:{}".format(self._now().isoformat(), n)
        return "updated", msg, version
