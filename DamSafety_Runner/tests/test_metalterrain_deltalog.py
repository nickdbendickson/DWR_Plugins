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


if __name__ == "__main__":
    import sys
    fails = 0
    for fn in (deltalog_selftest_passes, deltalog_requires_run_id_and_reason,
               deltalog_summary_is_deterministic, deltalog_census_wiring_flags_pits_and_crossings,
               deltalog_review_clears_the_flag):
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
