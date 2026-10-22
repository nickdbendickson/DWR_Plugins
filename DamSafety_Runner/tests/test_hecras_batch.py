"""3.10.66: Models = 'LISFLOOD-FP then HEC-RAS 2D' runs the HEC-RAS 2D stage in the batch
(ndbreach:batch) and the ndbreach CLI too, not only in 'Run breach model for one dam' (the
batch gave LISFLOOD-FP results without a word). Mocks only: no HEC-RAS, no Wine."""
import importlib
import json
import os
import shutil
import tempfile
import types

from nd_breach_runner.core import workflow
from nd_breach_runner.tests.registry import test

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


class _FakeEngine(object):
    class HecRasEngineError(Exception):
        pass

    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def run_stage(self, folder, final, writer, st, log):
        self.calls.append(folder)
        if self.fail:
            raise self.HecRasEngineError("Ras.exe exited 1")
        return {"final": {"depth": os.path.join(folder, "Depth (Max).tif")},
                "warnings": ["Final rasters are from HEC-RAS 2D"]}


def _res():
    return types.SimpleNamespace(final={"depth": "lisflood.tif"}, warnings=[],
                                 summary={"final_model": "LISFLOOD-FP"})


@test
def hecras_stage_shared_helper_adopts_the_final_engine():
    d = tempfile.mkdtemp(prefix="hec_batch_")
    old = workflow.adopt_final_engine
    seen = []
    try:
        workflow.adopt_final_engine = lambda folder, name, st, xy, log: (
            seen.append((folder, name)) or {"final_model": name})
        res, eng = _res(), _FakeEngine()
        workflow.run_hecras_stage(d, res, None, workflow.RunSettings(), (0, 0), None,
                                  engine=eng)
        assert eng.calls == [d]
        assert res.final["depth"].endswith("Depth (Max).tif")
        assert res.summary == {"final_model": "HEC-RAS 2D"} and seen == [(d, "HEC-RAS 2D")]
        assert res.warnings == ["Final rasters are from HEC-RAS 2D"]
        res = _res()
        try:
            workflow.run_hecras_stage(d, res, None, workflow.RunSettings(), (0, 0), None,
                                      engine=_FakeEngine(fail=True))
            assert False, "expected HecRasStageError"
        except workflow.HecRasStageError as e:
            assert "LISFLOOD-FP results are in the dam folder" in str(e)
            assert "Ras.exe exited 1" in str(e)
        assert res.summary["final_model"] == "LISFLOOD-FP"      # untouched on failure
    finally:
        workflow.adopt_final_engine = old
        shutil.rmtree(d, ignore_errors=True)


@test
def hecras_done_reads_the_stage_record():
    d = tempfile.mkdtemp(prefix="hec_done_")
    try:
        assert not workflow.hecras_done(d)
        os.makedirs(os.path.join(d, "_hecras"))
        rec = os.path.join(d, "_hecras", "hecras_stage.json")
        open(rec, "w").close()
        assert not workflow.hecras_done(d)                  # empty / unreadable
        with open(rec, "w") as f:
            json.dump({"engine": "hecras", "status": "timeout", "timeout": {}}, f)
        assert not workflow.hecras_done(d)                  # a timed-out stage is not done
        with open(rec, "w") as f:
            json.dump({"engine": "hecras", "results_hdf": "x.p01.hdf"}, f)
        assert workflow.hecras_done(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def batch_algorithm_runs_the_hecras_stage():
    """ndbreach:batch: the stage after run_dam (a failure is a 'failed' row, the batch goes
    on) and 'skip finished dams' needs the HEC-RAS stage record for this tier."""
    src = open(os.path.join(ROOT, "algorithms.py"), encoding="utf-8").read()
    batch = src.split("class BatchAlgorithm(", 1)[1].split("\nclass ", 1)[0]
    i_run = batch.index("res = workflow.run_dam(")
    i_hec = batch.index("hecras_tier_stage(folder, res, writer, st,")
    assert i_run < i_hec < batch.index("s = res.summary")
    assert "st.tier == workflow.TIER_HECRAS" in batch
    assert "hecras_done(folder)" in batch
    # the failure becomes the row's message (QgsProcessingException is caught per dam)
    assert "except HecRasTierError as e:\n                        raise QgsProcessingException" \
        in batch
    one = src.split("class RunDamAlgorithm(", 1)[1].split("\nclass ", 1)[0]
    assert "hecras_tier_stage(folder, res, writer, st, dam_xy, fb.log)" in one


@test
def cli_runs_the_hecras_stage_for_tier_hecras():
    cli = importlib.import_module("nd_breach_runner.scripts.ndbreach_cli")
    d = tempfile.mkdtemp(prefix="hec_cli_")
    old = workflow.run_hecras_stage
    calls = []
    try:
        workflow.run_hecras_stage = lambda *a, **k: calls.append(a)
        p = types.SimpleNamespace(st=workflow.RunSettings(), folder=d, dam_xy=(1, 2),
                                  setup=types.SimpleNamespace(writer=None))
        res = _res()
        res.summary["breach_occurs"] = True
        cli._hecras_stage(p, res, lambda m: None)
        assert calls == []                                       # LISFLOOD-FP only: nothing
        p.st.tier = workflow.TIER_HECRAS
        cli._hecras_stage(p, res, lambda m: None)
        assert len(calls) == 1 and calls[0][0] == d
        res.summary["breach_occurs"] = False
        cli._hecras_stage(p, res, lambda m: None)
        assert len(calls) == 1                                   # no breach: no stage
    finally:
        workflow.run_hecras_stage = old
        shutil.rmtree(d, ignore_errors=True)
    src = open(cli.__file__, encoding="utf-8").read()
    assert src.count("_hecras_stage(p, res, pr.log)") == 2       # run and batch
    assert "workflow.HecRasStageError) as e:" in src
