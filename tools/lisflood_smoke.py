#!/usr/bin/env python3
"""Start a LISFLOOD-FP build once, on this computer: one second on a 5 by 5 grid.

    python tools/lisflood_smoke.py path/to/lisflood[.exe]

Exit status 0 when the binary ran and wrote its result, 1 when it did not (the reason is
printed), 2 for a wrong command line. It is the check the Runner makes before every dam
(DamSafety_Runner/core/solve_gate.lisflood_smoke), so it needs no QGIS and no numpy.

release.yml runs it on a Windows runner: the Windows build is cross-compiled on Linux, where
it cannot be started, so before this check nobody ran it until a user did."""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(os.path.dirname(HERE), "DamSafety_Runner", "core", "solve_gate.py")


def load_gate(path=GATE):
    """solve_gate.py by its file (not through the plugin package, which imports QGIS)."""
    spec = importlib.util.spec_from_file_location("solve_gate_standalone", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    os.environ.pop("ND_DAM_PRE_RUN_SMOKE", None)      # a skipped check would pass anything
    hit = load_gate().lisflood_smoke(args[0])
    print("{}: {}{}".format(hit["status"], hit["detail"], "  " + hit["fix"] if hit["fix"] else ""))
    return 0 if hit["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
