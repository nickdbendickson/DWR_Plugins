"""BoannGrid delta-log: a provenance ledger of every terrain edit the conditioner makes, so the
run report can say which cells were changed and why, and no inferred value is ever silent.

Adapted from Nick's delta-log draft, with three corrections:
  * it is an honest SQLite ledger, not a GeoPackage: it carries no gpkg metadata, so it is named
    .sqlite and never claims a GeoPackage (the EDIT_CLASS GeoTIFF is the spatial product). This
    is the "plain SQLite is not a conformant GeoPackage" bug from the Phase-A review.
  * run_id is required, never a wall-clock default, so a run's provenance is tied to the run (a
    content hash), not to the moment the log was opened - the byte-reproducibility lesson. The
    clock for the audit timestamps is injectable so tests are deterministic.
  * counts come back structured, not as a string the summary re-parses.

The one hard invariant, enforced in the constructor path: a FLAGGED edit without a review_reason
raises. You cannot record an inferred culvert size or an ambiguous depression class without
stating the basis - the API refuses it ("assumptions are registered, never silent").

    from nd_breach_runner.core.metalterrain import delta_log as dl
    log = dl.DeltaLog(run_id=run_hash, engine="boanngrid")     # :memory: by default
    log.add(dl.Edit(dl.EditClass.CHANNEL_BURN, "BURN", evidence="NHD_FLOWLINE", n_cells=310,
                    dz_mean=-1.1, algo_ref="CHANNEL_BURN_V1"))
    log.flag(dl.Edit(dl.EditClass.CULVERT, "RATE_CULVERT", evidence="GEOMORPHIC_SIGNAL"),
             reason="culvert size inferred from drainage area; no inventory")
    print(log.summary())

    python3 -m nd_breach_runner.core.metalterrain.delta_log selftest
"""
import json
import sqlite3
from dataclasses import dataclass, field
from enum import IntEnum

SCHEMA = """
CREATE TABLE IF NOT EXISTS delta_log (
    fid            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    engine         TEXT NOT NULL,
    class          TEXT NOT NULL,
    action         TEXT NOT NULL,
    n_cells        INTEGER NOT NULL DEFAULT 0,
    dz_min         REAL, dz_mean REAL, dz_max REAL,
    spill_before   REAL, spill_after REAL, storage_vol REAL,
    params_json    TEXT,
    evidence       TEXT NOT NULL,
    evidence_ref   TEXT,
    algo_ref       TEXT,
    review_status  TEXT NOT NULL DEFAULT 'AUTO',   -- AUTO/REVIEWED/FLAGGED/REJECTED
    review_reason  TEXT,
    reviewed_by    TEXT,
    reviewed_at    TEXT,
    created_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_dl_run ON delta_log(run_id);
CREATE INDEX IF NOT EXISTS idx_dl_review ON delta_log(review_status);
"""


class EditClass(IntEnum):
    """EDIT_CLASS raster legend: the kind of edit each cell carries (0 = untouched)."""
    NONE = 0
    BREACH_ROAD_CULVERT = 1
    CHANNEL_BURN = 2
    FILLED = 3
    LAKE_RAISE = 4
    PRESERVED = 5
    BREAKLINE_ENFORCE = 6
    CROSSING_STITCH = 7
    CULVERT = 8
    CREST_WEIR = 9
    CHANNEL_PRISM = 10


CLASS_NAMES = {
    EditClass.NONE: "NONE", EditClass.BREACH_ROAD_CULVERT: "ROAD_FILL",
    EditClass.CHANNEL_BURN: "CHANNEL_BURN", EditClass.FILLED: "FILLED",
    EditClass.LAKE_RAISE: "LAKE_RAISE", EditClass.PRESERVED: "PRESERVED",
    EditClass.BREAKLINE_ENFORCE: "BREAKLINE", EditClass.CROSSING_STITCH: "CROSSING_STITCH",
    EditClass.CULVERT: "CULVERT", EditClass.CREST_WEIR: "CREST_WEIR",
    EditClass.CHANNEL_PRISM: "CHANNEL_PRISM",
}

# The action string for a crossing the conditioner REFUSED to carve because a frozen embankment /
# dam-crest cell lay on the carve path. It is a CROSSING_STITCH that did not happen, FLAGGED, and
# summary() lifts it into the Bottom line (see DeltaLog.freeze_refusals).
REFUSE_FREEZE_ACTION = "REFUSE_FREEZE"


class ExportRefused(RuntimeError):
    """A conditioned DEM still has unresolved freeze-refusals, so it must not be exported."""


@dataclass
class Edit:
    class_: EditClass
    action: str
    evidence: str                   # NHD_FLOWLINE/ROAD_CROSSING/SURVEY_INVERT/INVENTORY/
                                    # GEOMORPHIC_SIGNAL/ASSUMED/DEFAULT
    n_cells: int = 0
    dz_min: float = None
    dz_mean: float = None
    dz_max: float = None
    spill_before: float = None
    spill_after: float = None
    storage_vol: float = None
    params: dict = None
    evidence_ref: str = None
    algo_ref: str = None
    fid: int = field(default=None, init=False)


class DeltaLog:
    """A run's terrain-edit ledger. `path` defaults to an in-memory database; pass a file path to
    persist. `run_id` is required (use the run's content hash, not a timestamp). `now` returns the
    audit timestamp string and is injectable for deterministic tests."""

    def __init__(self, run_id, path=":memory:", engine="boanngrid", now=None):
        if not run_id:
            raise ValueError("run_id is required (use the run's content hash, not a timestamp)")
        self.run_id = str(run_id)
        self.engine = engine
        self._now = now or _utc_now
        self._db = sqlite3.connect(str(path))
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")   # QGIS can read while a run is open
        self._db.executescript(SCHEMA)
        self._db.commit()

    # -- writes --
    def add(self, e, review_status="AUTO", review_reason=None):
        if review_status == "FLAGGED" and not review_reason:
            raise ValueError("FLAGGED edits require review_reason (nothing silent)")
        cur = self._db.execute(
            "INSERT INTO delta_log (run_id, engine, class, action, n_cells, dz_min, dz_mean,"
            " dz_max, spill_before, spill_after, storage_vol, params_json, evidence, evidence_ref,"
            " algo_ref, review_status, review_reason, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.run_id, self.engine, CLASS_NAMES[e.class_], e.action, e.n_cells, e.dz_min,
             e.dz_mean, e.dz_max, e.spill_before, e.spill_after, e.storage_vol,
             json.dumps(e.params, sort_keys=True) if e.params else None, e.evidence,
             e.evidence_ref, e.algo_ref, review_status, review_reason, self._now()))
        self._db.commit()
        e.fid = cur.lastrowid
        return e.fid

    def flag(self, e, reason):
        """Register an assumption / ambiguous edit for human review (reason required)."""
        return self.add(e, review_status="FLAGGED", review_reason=reason)

    def review(self, fid, verdict, reviewer, reason=None):
        if verdict not in ("REVIEWED", "REJECTED"):
            raise ValueError("verdict must be REVIEWED or REJECTED")
        self._db.execute("UPDATE delta_log SET review_status=?, review_reason=COALESCE(?,"
                         " review_reason), reviewed_by=?, reviewed_at=? WHERE fid=? AND run_id=?",
                         (verdict, reason, reviewer, self._now(), fid, self.run_id))
        self._db.commit()

    # -- reads --
    def counts_by_class(self):
        """{class_name: {"edits": n, "cells": n}} for this run."""
        rows = self._db.execute(
            "SELECT class, COUNT(*), COALESCE(SUM(n_cells),0) FROM delta_log WHERE run_id=?"
            " GROUP BY class ORDER BY class", (self.run_id,)).fetchall()
        return {r[0]: {"edits": int(r[1]), "cells": int(r[2])} for r in rows}

    def flagged(self):
        return self._db.execute(
            "SELECT fid, class, action, evidence, review_reason FROM delta_log WHERE run_id=?"
            " AND review_status='FLAGGED' ORDER BY fid", (self.run_id,)).fetchall()

    def freeze_refusals(self):
        """Rows (fid, class, action, evidence, review_reason) for crossings the conditioner refused
        to carve because a frozen embankment / dam-crest cell lay on the carve path. These are the
        dam-safety refusals that MUST reach the Bottom line of the report, never only the appendix:
        a sweep that leaves three dams uncarved cannot read as 'no interventions'."""
        return self._db.execute(
            "SELECT fid, class, action, evidence, review_reason FROM delta_log WHERE run_id=?"
            " AND action=? ORDER BY fid", (self.run_id, REFUSE_FREEZE_ACTION)).fetchall()

    def unresolved_freeze_refusals(self):
        """Freeze-refusals still FLAGGED. REVIEWED or REJECTED are resolved and no longer block
        export. The Bottom line of summary() is this list, not the historical one."""
        return self._db.execute(
            "SELECT fid, class, action, evidence, review_reason FROM delta_log WHERE run_id=?"
            " AND action=? AND review_status='FLAGGED' ORDER BY fid",
            (self.run_id, REFUSE_FREEZE_ACTION)).fetchall()

    def assert_export_allowed(self, acknowledge=None):
        """Refuse to export a conditioned DEM while freeze-refusals are unresolved.

        `acknowledge` must be a non-empty string. It is written onto each still-FLAGGED
        refusal as a REVIEWED reason (the override is in the ledger, not a silent flag).
        """
        rows = self.unresolved_freeze_refusals()
        if not rows:
            return
        note = str(acknowledge).strip() if acknowledge is not None else ""
        if note:
            for r in rows:
                self.review(r[0], "REVIEWED", reviewer="export-ack",
                            reason="export acknowledged: {}".format(note))
            return
        raise ExportRefused(
            "conditioned DEM export blocked: {} unresolved freeze-refusal(s) "
            "(delta-log Bottom line, fids {}). Review or reject each, or pass an explicit "
            "acknowledge string.".format(len(rows), ", ".join(str(r[0]) for r in rows)))

    def to_dataframe(self):
        import pandas as pd            # optional; notebook / QGIS inspection
        return pd.read_sql_query("SELECT * FROM delta_log WHERE run_id=?", self._db,
                                 params=(self.run_id,))

    # -- report (house style, as PR #401's peak-flow range section established) --
    def summary(self):
        counts = self.counts_by_class()
        n_edits = sum(v["edits"] for v in counts.values())
        n_cells = sum(v["cells"] for v in counts.values())
        out = ["# Terrain-edit delta log - run {}".format(self.run_id), "", "## Bottom line", ""]
        out.append("- {} interventions touching {:,} cells, each with a recorded reason and "
                   "evidence reference.".format(n_edits, n_cells) if n_edits
                   else "- No terrain edits recorded for this run.")
        # Dam-safety refusals belong in the Bottom line, not only the assumption register: a
        # whole-DEM sweep that silently left frozen embankments uncarved must not read as
        # "no interventions". This line is blocking - the run is not final until each is resolved.
        refusals = self.unresolved_freeze_refusals()
        if refusals:
            out.append("- **⚠ {} crossing(s) NOT carved: a frozen embankment / dam-crest cell "
                       "lies on the carve path (depression fid {}).** The conditioner refused them "
                       "(the dam-safety invariant); an operator must resolve each (real culvert? "
                       "mask wrong?) before this conditioned DEM is used.".format(
                           len(refusals), ", ".join(str(r[0]) for r in refusals)))
        out += ["", "## By class", ""]
        out += ["- {}: {} edits / {:,} cells".format(c, v["edits"], v["cells"])
                for c, v in sorted(counts.items())] or ["- (none)"]
        fl = self.flagged()
        out += ["", "## Flagged for review (assumption register)", ""]
        if fl:
            out += ["- #{} {} ({}): {} [evidence: {}]".format(r[0], r[1], r[2], r[4], r[3])
                    for r in fl]
            out += ["", "Each flagged item must be REVIEWED or REJECTED before the run report is "
                        "finalized."]
        else:
            out.append("- Nothing flagged - all interventions carry recorded evidence.")
        out += ["", "## Provenance", "",
                "- Every row carries algo_ref naming the cited algorithm and implementation that "
                "produced it; an edit without algorithm provenance is a defect, not a style choice."]
        return "\n".join(out)

    def export_edit_class_raster(self, edit_class, out_path, transform, acknowledge=None):
        """Write the per-cell EDIT_CLASS int16 raster (GDAL). Refused, before any file is
        written, while unresolved freeze-refusals remain — the raster review is not a substitute
        for the Bottom line. Returns False and skips, rather than failing a run, when GDAL is
        unavailable; the ledger stays the authoritative record."""
        self.assert_export_allowed(acknowledge=acknowledge)
        try:
            from osgeo import gdal
        except Exception:               # noqa: BLE001 - GDAL absent
            return False
        import numpy as np
        a = np.asarray(edit_class, dtype=np.int16)
        ny, nx = a.shape
        ds = gdal.GetDriverByName("GTiff").Create(str(out_path), nx, ny, 1, gdal.GDT_Int16)
        ds.SetGeoTransform(transform)
        b = ds.GetRasterBand(1)
        b.WriteArray(a)
        b.SetNoDataValue(0)
        b.FlushCache()
        ds = None
        return True

    def export_conditioned_dem(self, dem, out_path, dx=1.0, xll=0.0, yll=0.0, nodata=-9999.0,
                               acknowledge=None):
        """Write the conditioned DEM as an ESRI ASCII grid. Blocked while summary()'s Bottom
        line still lists unresolved FLAGGED freeze-refusals. `acknowledge` is the explicit
        override (recorded on each refusal); a missing or blank acknowledgment does not export."""
        self.assert_export_allowed(acknowledge=acknowledge)
        import numpy as np
        a = np.asarray(dem, dtype=np.float64)
        if a.ndim != 2:
            raise ValueError("conditioned DEM must be 2-D")
        ny, nx = a.shape
        out = np.where(np.isfinite(a), a, nodata)
        header = ("ncols {:d}\nnrows {:d}\nxllcorner {:.6f}\nyllcorner {:.6f}\n"
                  "cellsize {:.6f}\nNODATA_value {:g}".format(nx, ny, xll, yll, dx, nodata))
        np.savetxt(out_path, out, fmt="%.4f", header=header, comments="")
        return out_path

    def close(self):
        self._db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _utc_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# how a depression census (audit.depression_census) maps into the ledger: the class, the edit
# action, and whether it needs human review (a real pit or a blocked crossing does; filling DEM
# noise does not).
_CENSUS_MAP = {
    "noise": (EditClass.FILLED, "FILL", "DEFAULT", None),
    "fill": (EditClass.FILLED, "FILL", "DEFAULT", None),
    "pit": (EditClass.PRESERVED, "PRESERVE", "GEOMORPHIC_SIGNAL",
            "deep or large closed depression: confirm a real pit vs a DEM artifact"),
    "crossing": (EditClass.CROSSING_STITCH, "STITCH", "GEOMORPHIC_SIGNAL",
                 "a channel blocked by a linear fill: confirm the culvert / reconnection"),
}


def log_census(log, items, algo_ref="LEVELSET_CENSUS_V1+PD2002_FILL"):
    """Record a depression census (audit.depression_census items) in the ledger: one edit per
    depression, with pits and crossings FLAGGED (with a reason) so they reach human eyes, and
    filled artifacts AUTO. Returns the number of flagged edits."""
    flagged = 0
    for it in items:
        klass, action, evidence, reason = _CENSUS_MAP.get(
            it.get("klass", "fill"), _CENSUS_MAP["fill"])
        e = Edit(klass, action, evidence=evidence, n_cells=it["n_cells"],
                 dz_max=it["max_depth_m"], storage_vol=it["volume_m3"],
                 spill_after=it.get("spill_elev_m"), algo_ref=algo_ref,
                 evidence_ref="depression #{} at {}".format(it["id"], it["centroid"]))
        if reason:
            log.flag(e, reason=reason)
            flagged += 1
        else:
            log.add(e)
    return flagged


def log_reconnect(log, records, cell_m=1.0, algo_ref="CROSSING_RECONNECT_V1+PD2002_FILL"):
    """Record `burn.reconnect_crossings` records in the ledger: each carved crossing as an AUTO
    CROSSING_STITCH edit, and each freeze-refusal (burn.freeze_refusals) as a FLAGGED CROSSING_STITCH
    with action REFUSE_FREEZE and a reason naming the frozen cells, so it reaches summary()'s Bottom
    line. Returns the number of freeze-refusals (the aggregate count the caller must not drop)."""
    refused = 0
    for r in records:
        ev = "crossing #{} pour {} -> exit {}".format(r["id"], r["pour"], r["exit"])
        if r.get("refused") == "freeze":
            cells = r.get("frozen_cells", [])
            e = Edit(EditClass.CROSSING_STITCH, REFUSE_FREEZE_ACTION, evidence="FREEZE_MASK",
                     n_cells=int(r.get("barrier_cells", 0)), spill_before=r.get("spill_before_m"),
                     algo_ref=algo_ref, evidence_ref=ev,
                     params={"frozen_cells": [list(c) for c in cells],
                             "accum_cells": r.get("accum_cells")})
            log.flag(e, reason="carve path crosses {} frozen (embankment / dam-crest) cell(s); "
                               "refused by the dam-safety invariant - resolve before use "
                               "(real culvert? mask wrong?)".format(len(cells)))
            refused += 1
        else:
            e = Edit(EditClass.CROSSING_STITCH, "STITCH", evidence="GEOMORPHIC_SIGNAL",
                     n_cells=int(r.get("barrier_cells", 0)), spill_before=r.get("spill_before_m"),
                     spill_after=r.get("floor_elev_m"), algo_ref=algo_ref, evidence_ref=ev,
                     params={"accum_cells": r.get("accum_cells")})
            log.add(e)
    return refused


def log_prism(log, channels, refusals=(), cell_m=1.0,
              algo_ref="CHANNEL_PRISM_V1+BIEGER2015", evidence="GEOMORPHIC_SIGNAL",
              evidence_ref=None):
    """Record `prism.carve_channels` output in the ledger: each carved flowline as an AUTO
    CHANNEL_PRISM edit (its cells, drainage-area and depth range, dz statistics), and each refusal
    as a FLAGGED CHANNEL_PRISM with action REFUSE_FREEZE (the same action the crossing refusals use,
    so it reaches summary()'s Bottom line and blocks export through the existing dam-safety gate).
    `evidence` defaults to GEOMORPHIC_SIGNAL (a DEM-traced flowline); pass NHD_FLOWLINE with an
    `evidence_ref` when the flowline came from a real 3DHP / NHD vector. Returns the number of
    refusals (the aggregate the caller must not drop)."""
    refused = 0
    for r in refusals:
        cells = r.get("frozen_cells", [])
        e = Edit(EditClass.CHANNEL_PRISM, REFUSE_FREEZE_ACTION, evidence="FREEZE_MASK",
                 n_cells=int(r.get("n_cells", 0)), algo_ref=algo_ref,
                 evidence_ref="flowline {} -> {}".format(r.get("head"), r.get("outlet")),
                 params={"frozen_cells": [list(c) for c in cells],
                         "da_max_km2": r.get("da_max_km2")})
        log.flag(e, reason="channel flowline centreline crosses {} frozen (embankment / dam-crest) "
                           "cell(s); refused by the dam-safety invariant - resolve before use "
                           "(real culvert / channel? mask wrong?)".format(len(cells)))
        refused += 1
    for ch in channels:
        e = Edit(EditClass.CHANNEL_PRISM, "CARVE", evidence=evidence,
                 n_cells=int(ch.get("n_cells", 0)), dz_min=ch.get("dz_min_m"),
                 dz_mean=ch.get("dz_mean_m"), dz_max=ch.get("dz_max_m"), algo_ref=algo_ref,
                 evidence_ref=evidence_ref or "flowline {} -> {}".format(ch.get("head"),
                                                                         ch.get("outlet")),
                 params={"n_reaches": ch.get("n_reaches"), "da_min_km2": ch.get("da_min_km2"),
                         "da_max_km2": ch.get("da_max_km2"), "width_max_m": ch.get("width_max_m"),
                         "depth_max_m": ch.get("depth_max_m"), "cell_m": cell_m})
        log.add(e)
    return refused


def selftest():
    """Round-trip on synthetic edits: schema, add/flag/review, counts, flagged listing, summary
    render, the FLAGGED-without-reason guard, and persistence across reopen. Does NOT prove GDAL
    export or QGIS render."""
    import tempfile
    import os
    clock = iter("t{}".format(i) for i in range(100)).__next__
    print("[1/5] add + counts")
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "test.sqlite")
        log = DeltaLog(run_id="SELFTEST", path=p, now=clock)
        log.add(Edit(EditClass.CHANNEL_BURN, "BURN", evidence="NHD_FLOWLINE", n_cells=310,
                     dz_mean=-1.1, algo_ref="CHANNEL_BURN_V1"))
        log.add(Edit(EditClass.PRESERVED, "PRESERVE", evidence="DEFAULT", n_cells=1850,
                     algo_ref="LEVELSET_CENSUS_V1"))
        c = log.counts_by_class()
        assert c["CHANNEL_BURN"]["cells"] == 310 and "PRESERVED" in c, c
        print("[2/5] FLAGGED needs a reason")
        try:
            log.flag(Edit(EditClass.CULVERT, "RATE_CULVERT", evidence="GEOMORPHIC_SIGNAL"),
                     reason=None)
            raise AssertionError("FLAGGED without reason was accepted")
        except ValueError:
            pass
        print("[3/5] flag -> review lifecycle")
        cid = log.flag(Edit(EditClass.CULVERT, "RATE_CULVERT", evidence="GEOMORPHIC_SIGNAL",
                            params={"diameter_m": 0.9}, algo_ref="HDS5_V1"),
                       reason="culvert size inferred from drainage area; no inventory")
        assert len(log.flagged()) == 1
        log.review(cid, verdict="REVIEWED", reviewer="nbendickson", reason="checked vs county std")
        assert len(log.flagged()) == 0
        print("[4/5] summary house style")
        s = log.summary()
        for needle in ("Bottom line", "By class", "Flagged for review", "assumption register",
                       "Provenance", "CHANNEL_BURN"):
            assert needle in s, needle
        print("[5/5] persistence across reopen")
        final = log.counts_by_class()                 # after every edit, including the culvert
        log.close()
        log2 = DeltaLog(run_id="SELFTEST", path=p)
        assert log2.counts_by_class() == final
        log2.close()
    print("selftest VERDICT: PASS (synthetic edits only)")
    return 0


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        raise SystemExit(selftest())
    print(__doc__)
