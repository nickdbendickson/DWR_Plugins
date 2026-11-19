"""H22a: the gmsh-only tests must report honestly, and CI must provision gmsh.

Without gmsh importable, each gmsh-only test raises registry.Skip, so run_tests counts it
as skipped instead of printing \"skip\" and returning (which main() counts as \"ok\") - that
hid gmsh regressions in CI. The model-tests CI job provisions python-gmsh so the gmsh tests
actually RUN there. No model runs here and no computed number is touched: this checks test
reporting and CI provisioning only."""
import os

from nd_breach_runner.tests.registry import test, Skip  # noqa: E402
import nd_breach_runner.tests.test_gmsh_mesher as gm  # noqa: E402
import nd_breach_runner.tests.test_gmsh_gate as gg  # noqa: E402
import nd_breach_runner.tests.test_gmsh_sizing as gs  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# the whole-test gmsh-only tests: no-ops without gmsh, so they must SKIP not silently pass
GMSH_ONLY = [
    gm.gmsh_mesh_meets_the_quality_gate_and_is_coarser_than_uniform,
    gm.gmsh_mesher_runs_as_a_script_and_reaches_the_job,
    gg.gmsh_culvert_area_option_refines_the_points_and_keeps_the_gate,
    gs.gmsh_sizing_options_make_fewer_triangles,
]


@test
def gmsh_only_tests_skip_without_gmsh_and_ci_provisions_it():
    # without gmsh each gmsh-only test must raise Skip (returning None would count as a pass)
    if not gm.HAVE_GMSH:
        for fn in GMSH_ONLY:
            try:
                fn()
            except Skip:
                continue
            raise AssertionError(
                "{} returned without raising Skip when gmsh is absent".format(fn.__name__))
    # the model-tests CI job's QGIS env (the numpy<2 leg) must install python-gmsh so the
    # gmsh tests RUN on the job that is supposed to have gmsh, instead of skipping everywhere
    ci = open(os.path.join(_ROOT, ".github", "workflows", "ci.yml")).read()
    env = [l for l in ci.splitlines() if "qgis" in l and "numpy<2" in l and "gdal" in l]
    assert len(env) == 1, env
    assert "python-gmsh" in env[0], env[0]
