"""3.20.6: a resumed run whose second ANUGA pass (the wider mesh) had started computed the first
pass again from t = 0 before it went on with the second (the Lake Delhi 2010 validation, run in
six-hour legs, spent every leg on the first pass). workflow.anuga_passes keeps the first pass's
result and a resumed run uses it.

No ANUGA: run_pass is a stand-in that writes anuga_result.npz the way the real one does."""
import os
import shutil
import tempfile

import numpy as np

from nd_breach_runner.tests.registry import test


class _State:
    """workflow._State's interface: get() answers only on a resumed run."""

    def __init__(self, data=None, resumed=False):
        self.data = dict(data or {})
        self.resumed = resumed

    def get(self, k, default=None):
        return self.data.get(k, default) if self.resumed else default

    def save(self, **kw):
        self.data.update(kw)


class _Ast:
    buffer_m = 500.0


class _Env:
    def __init__(self, edge_ft, tag):
        self.stats = {"max_edge_depth_ft": edge_ft, "tag": tag}
        self.notes = ["note " + tag]
        self.depth = type("G", (), {"data": np.full((3, 3), 1.0)})()


def _runner(workdir, edges, calls):
    """run_pass stand-in: pass k ends with edges[k] ft at the mesh edge. A reused pass reads
    the tag back from anuga_result.npz (as the real run reads its stats from the file)."""
    def run_pass(k, flooded, reuse):
        res = os.path.join(workdir, "anuga_result.npz")
        calls.append((k, bool(reuse)))
        if reuse:
            with np.load(res) as r:
                tag = str(r["tag"])
        else:
            tag = "pass{}-computed".format(k + 1)
            np.savez(res, tag=np.array(tag))
        return _Env(edges[k], tag), "outer{}".format(k), "fine{}".format(k)
    return run_pass


def _passes(workdir, state, edges, calls, ast=None):
    from nd_breach_runner.core import workflow as wf
    log = []
    out = wf.anuga_passes(_runner(workdir, edges, calls), state, workdir, ast or _Ast(),
                          np.zeros((3, 3), bool), 0.5, log.append)
    return out, log


@test
def anuga_second_pass_resumes_without_computing_the_first_again():
    from nd_breach_runner.core import workflow as wf
    d = tempfile.mkdtemp(prefix="ndbr_passes_")
    try:
        # first session: pass 1 reaches the edge, pass 2 starts and is stopped part way
        state, calls = _State(), []
        ast = _Ast()

        def stopped(k, flooded, reuse):
            if k == 1:
                raise RuntimeError("stopped")
            return _runner(d, [3.0, 0.0], calls)(k, flooded, reuse)
        try:
            wf.anuga_passes(stopped, state, d, ast, np.zeros((3, 3), bool), 0.5, lambda m: None)
        except RuntimeError:
            pass
        assert os.path.isfile(os.path.join(d, wf.ANUGA_PASS1_RESULT))
        assert state.data["anuga_pass1"]["buffer_m"] == 500.0, state.data
        # the second pass overwrites anuga_result.npz in the shared folder before it is stopped
        np.savez(os.path.join(d, "anuga_result.npz"), tag=np.array("pass2-partial"))

        # resumed session: pass 1 is read back, pass 2 runs
        calls = []
        ast2 = _Ast()
        (env, outer, fine, runs, warns), log = _passes(
            d, _State(state.data, resumed=True), [3.0, 0.2], calls, ast2)
        assert calls == [(0, True), (1, False)], calls
        assert runs[0]["stats"]["tag"] == "pass1-computed", runs
        assert runs[0]["buffer_m"] == 500.0 and runs[1]["buffer_m"] == 1000.0, runs
        assert ast2.buffer_m == 1000.0
        assert env.stats["tag"] == "pass2-computed" and outer == "outer1"
        assert any("finished in an earlier session" in m for m in log), log
        assert warns == ["note pass1-computed", "note pass2-computed"], warns
    finally:
        shutil.rmtree(d, True)


@test
def anuga_passes_unchanged_without_a_kept_first_pass():
    d = tempfile.mkdtemp(prefix="ndbr_passes_")
    try:
        # one pass that stays inside the mesh: nothing kept, nothing recorded
        state, calls = _State(), []
        (env, _o, _f, runs, warns), _log = _passes(d, state, [0.1, 0.0], calls)
        assert calls == [(0, False)] and len(runs) == 1 and "anuga_pass1" not in state.data
        # a resumed run whose state names a first pass but whose kept file is gone computes it
        calls = []
        (env, _o, _f, runs, _w), _log = _passes(
            d, _State({"anuga_pass1": {"buffer_m": 500.0}}, resumed=True), [3.0, 4.0], calls)
        assert calls == [(0, False), (1, False)], calls
        # both passes at the edge: the second pass's warning, as before
        assert any("reaches the mesh boundary 4.0 ft" in w for w in _w), _w
        # a finished ANUGA (anuga_done) keeps the old behaviour: pass 1 reused from the result
        calls = []
        _passes(d, _State({"anuga_done": True}, resumed=True), [0.1, 0.0], calls)
        assert calls == [(0, True)], calls
    finally:
        shutil.rmtree(d, True)
