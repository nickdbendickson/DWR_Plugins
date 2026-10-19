#!/usr/bin/env python3
"""Drift gate: the vendored damsuite copies must match shared/damsuite and carry a current
FILES.json. Pure stdlib; run in CI with `python3 tools/test_sync_damsuite.py` (exit 1 on drift).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sync_damsuite as s


def test_no_damsuite_drift():
    problems = s.check()
    assert not problems, "damsuite drift (run tools/sync_damsuite.py):\n" + "\n".join(problems)


def test_sync_is_idempotent():
    # a sync right now must make no change (the master already matches every vendored copy)
    changes = s.sync()
    assert not changes, "sync changed files when check() reported no drift: {}".format(changes)


if __name__ == "__main__":
    test_no_damsuite_drift()
    test_sync_is_idempotent()
    print("OK (damsuite in sync; sync idempotent)")
