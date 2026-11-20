"""BoannGrid delta-log (core/metalterrain/delta_log.py): the provenance ledger's invariants - a
FLAGGED edit needs a reason, run_id is required, the summary renders in house style and is
deterministic given a fixed clock - and the wiring that records a depression census, with pits and
crossings flagged for review. Pure stdlib (no GPU, no scipy) except the census wiring, which uses
the fill CPU build.
"""
import os

import numpy as np

from nd_breach_runner.core.metalterrain import delta_log as DL
from nd_breach_runner.core.metalterrain import audit as A
from nd_breach_runner.core.metalterrain import cases
from nd_breach_runner.core.metalflood import backends as mfb
from nd_breach_runner.tests.registry import Skip, test


@test
def deltalog_selftest_passes():
    """The module's own selftest (schema, lifecycle, guard, summary, persistence) passes."""
    assert DL.selftest() == 0


@test
def deltalog_requires_run_id_and_reason():
    """run_id is required (no wall-clock default), and a FLAGGED edit without a reason raises -
    the 'nothing silent' invariant as a hard guard."""
    try:
        DL.DeltaLog(run_id=None)
        raise AssertionError("a log with no run_id was accepted")
    except ValueError:
        pass
    log = DL.DeltaLog(run_id="R1")
    try:
        log.add(DL.Edit(DL.EditClass.CULVERT, "RATE_CULVERT", evidence="ASSUMED"),
                review_status="FLAGGED")
        raise AssertionError("FLAGGED without a reason was accepted")
    except ValueError:
        pass
    log.close()


@test
def deltalog_summary_is_deterministic():
    """Same run_id, same edits, a fixed clock -> byte-identical summary (the report is
    reproducible; no wall-clock leaks into it)."""
    clock = lambda: "FIXED"                                     # noqa: E731
    def build():
        log = DL.DeltaLog(run_id="RUN", now=clock)
        log.add(DL.Edit(DL.EditClass.CHANNEL_BURN, "BURN", evidence="NHD_FLOWLINE", n_cells=12,
                        dz_mean=-0.5, algo_ref="CHANNEL_BURN_V1"))
        log.flag(DL.Edit(DL.EditClass.CROSSING_STITCH, "STITCH", evidence="GEOMORPHIC_SIGNAL",
                         n_cells=3, algo_ref="STITCH_V1"), reason="blocked channel at crossing 2")
        s = log.summary()
        log.close()
        return s
    assert build() == build(), "the delta-log summary is not reproducible"


@test
def deltalog_census_wiring_flags_pits_and_crossings():
    """Recording a depression census: a real pit is PRESERVED and FLAGGED (with a reason), DEM
    noise is FILLED and AUTO - so the flagged count equals the pits+crossings, each with a basis."""
    if os.name == "nt" or not mfb.find_compiler():
        raise Skip("no C++ compiler for the fill CPU build")
    z = cases.nodata_ringed_pit(n=64, depth=8.0)
    _labels, items = A.depression_census(z, cell_m=10.0, backend="cpu")
    log = DL.DeltaLog(run_id="CENSUS")
    flagged = DL.log_census(log, items)
    want = sum(1 for it in items if it["klass"] in ("pit", "crossing"))
    assert flagged == want, (flagged, want)
    assert len(log.flagged()) == flagged
    # every flagged row carries a reason (the guard would have raised otherwise)
    assert all(r[4] for r in log.flagged()), "a flagged edit has no reason"
    s = log.summary()
    assert "Provenance" in s and "run CENSUS" in s
    log.close()


@test
def deltalog_review_clears_the_flag():
    """A flagged edit, once REVIEWED, drops off the review list."""
    log = DL.DeltaLog(run_id="REV")
    fid = log.flag(DL.Edit(DL.EditClass.CULVERT, "RATE_CULVERT", evidence="GEOMORPHIC_SIGNAL",
                           algo_ref="HDS5_V1"), reason="inferred from drainage area")
    assert len(log.flagged()) == 1
    log.review(fid, verdict="REVIEWED", reviewer="nick", reason="checked")
    assert len(log.flagged()) == 0
    log.close()


def _refuse(log, run_ok=True):
    fid = log.flag(DL.Edit(DL.EditClass.CROSSING_STITCH, DL.REFUSE_FREEZE_ACTION,
                           evidence="FREEZE_MASK", n_cells=2, algo_ref="CROSSING_RECONNECT_V1"),
                   reason="carve path crosses a frozen embankment cell")
    return fid


@test
def deltalog_unresolved_freeze_refusal_blocks_export():
    """A conditioned DEM with an unresolved FLAGGED freeze-refusal is not exported. Reviewing
    it, or passing an explicit acknowledgment (which is written into the ledger), allows the
    export. The EDIT_CLASS raster is under the same gate, not a way around it."""
    import os
    import tempfile
    log = DL.DeltaLog(run_id="FREEZE")
    fid = _refuse(log)
    s = log.summary()
    assert "NOT carved" in s.split("## By class")[0]
    dem = np.array([[1.0, 2.0], [3.0, 4.0]])
    try:
        log.export_conditioned_dem(dem, os.path.join(tempfile.gettempdir(), "should-not-write.asc"))
        raise AssertionError("export was allowed with an unresolved freeze-refusal")
    except DL.ExportRefused:
        pass
    try:
        log.export_edit_class_raster(np.zeros((2, 2), np.int16), "no.tif", (0, 1, 0, 0, 0, -1))
        raise AssertionError("EDIT_CLASS export bypassed the freeze-refusal gate")
    except DL.ExportRefused:
        pass
    # a blank acknowledgment is not an acknowledgment
    try:
        log.export_conditioned_dem(dem, "no.asc", acknowledge="   ")
        raise AssertionError("a blank acknowledgment exported the DEM")
    except DL.ExportRefused:
        pass
    # explicit acknowledgment is recorded, then the Bottom line is clear and the file exists
    d = tempfile.mkdtemp()
    try:
        path = log.export_conditioned_dem(dem, os.path.join(d, "cond.asc"), dx=10.0,
                                          acknowledge="operator confirmed the mask is wrong")
        assert os.path.isfile(path)
        assert log.unresolved_freeze_refusals() == []
        assert "NOT carved" not in log.summary().split("## By class")[0]
        row = log.freeze_refusals()
        assert len(row) == 1 and row[0][0] == fid
        assert "acknowledged" in (row[0][4] or "")
    finally:
        import shutil
        shutil.rmtree(d, True)
    log.close()

    # reviewing, rather than acknowledging at export time, also clears the gate
    log = DL.DeltaLog(run_id="FREEZE2")
    fid = _refuse(log)
    log.review(fid, verdict="REVIEWED", reviewer="nick", reason="real culvert, mask lifted")
    assert "NOT carved" not in log.summary().split("## By class")[0]
    d = tempfile.mkdtemp()
    try:
        path = log.export_conditioned_dem(dem, os.path.join(d, "cond.asc"))
        assert os.path.isfile(path)
    finally:
        import shutil
        shutil.rmtree(d, True)
    log.close()


if __name__ == "__main__":
    import sys
    fails = 0
    for fn in (deltalog_selftest_passes, deltalog_requires_run_id_and_reason,
               deltalog_summary_is_deterministic, deltalog_census_wiring_flags_pits_and_crossings,
               deltalog_review_clears_the_flag,
               deltalog_unresolved_freeze_refusal_blocks_export):
        try:
            fn()
            print("ok    ", fn.__name__)
        except Skip as e:
            print("skip  ", fn.__name__, "({})".format(e))
        except Exception:
            import traceback
            fails += 1
            print("FAIL  ", fn.__name__)
            traceback.print_exc()
    sys.exit(1 if fails else 0)
