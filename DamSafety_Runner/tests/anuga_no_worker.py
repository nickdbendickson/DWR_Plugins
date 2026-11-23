"""Tests that call anuga_model.run() only up to the job file.

They used to rely on sys.executable having no anuga, so the worker failed right after the job file
was written. Where the test interpreter does have ANUGA (a conda Python on a Mac with the suite
installed), the worker ran for real, and on success run() removes anuga_elevation_fine.npy ("the job
only needs it once"), which failed anuga_job_gets_the_fine_bed_only_when_a_finer_source_is_given
(repo audit 2026-11-23, finding 3). worker_fails() stands in for _launch with a worker that exits 1
without starting, so these tests behave the same with or without ANUGA installed."""
import contextlib
import os

from nd_breach_runner.core import anuga_model as AM


class _Failed:
    returncode = 1


def _no_worker(st, job, result, workdir, *args, **kwargs):
    log_path = os.path.join(workdir, "anuga.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("test stub: the ANUGA worker was not started\n")
    return _Failed(), log_path


@contextlib.contextmanager
def worker_fails():
    real = AM._launch
    AM._launch = _no_worker
    try:
        yield
    finally:
        AM._launch = real
