#!/usr/bin/env python3
"""Cross-fork safety-core parity gate.

DamSafety_* (this repo) and nd_* (the nd_hazard repo) are two forks of the same dam-safety
suite. For the result to be defensible, the files that decide HOW a dam is classified and how
its provenance is fingerprinted must be byte-identical across the forks, so a dam classified
under one fork is provably comparable to one classified under the other.

This checks the reconciled safety core:
  * damsuite/chain.py     - the model->classification fingerprint + run-manifest comparison
  * damsuite/solvegate.py - the pre-solve safety gate
  * <plugin>/fingerprint.py - the content fingerprints of the files a run reads (a plugin-root
                              file the shared pin cannot cover, so it needs this cross-fork gate)
against the nd_hazard checkout. It also confirms each fork's own vendored copies agree.

NOT checked (intentionally fork-specific): damsuite/versions.py is branding + per-fork release
numbers (nd_* folder names and the 2.3x/3.2x line vs DamSafety_* and the reset line); forcing it
identical would erase the brands. Its shared LOGIC (version_of / the compat matrix) can converge
later, but its data must not.

Because the forks live in separate repos, this runs where BOTH are checked out (a combined CI
job or locally), not in single-repo CI. The within-fork drift gate is tools/sync_damsuite.py.

    python3 tools/check_fork_parity.py --nd /path/to/nd_hazard      # exit 1 on any drift
"""
import argparse
import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASTER = os.path.join(ROOT, "shared", "damsuite")
SAFETY_CORE = ("chain.py", "solvegate.py")
DWR_FULL = ("DamSafety_Hazard", "DamSafety_Runner", "DamSafety_Screening")
ND_PLUGINS = ("ND_Dam_Hazard_Helper", "nd_breach_runner", "nd_dam_screening", "nd_data_updater")

# plugin-root files (OUTSIDE damsuite/, so the pin cannot cover them) that must also be
# byte-identical across the forks because they fingerprint a run's provenance - the same
# "how its provenance is fingerprinted" category as the safety core. {label: (DamSafety, nd)}.
PLUGIN_ROOT_SHARED = {
    "fingerprint.py": ("DamSafety_Hazard/fingerprint.py", "ND_Dam_Hazard_Helper/fingerprint.py"),
}


def _sha(path):
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _copies(repo_root, plugins, name):
    """{plugin: sha} for a safety-core file across a fork's vendored copies."""
    out = {}
    for p in plugins:
        out[p] = _sha(os.path.join(repo_root, p, "damsuite", name))
    return out


def check(nd_root):
    problems = []
    if not os.path.isdir(nd_root):
        return ["nd_hazard checkout not found: {}".format(nd_root)]
    for name in SAFETY_CORE:
        master = _sha(os.path.join(MASTER, name))
        if master is None:
            problems.append("DamSafety master missing: shared/damsuite/{}".format(name))
            continue
        # DamSafety vendored copies must match the master (also covered by sync_damsuite --check)
        for p, sha in _copies(ROOT, DWR_FULL, name).items():
            if sha != master:
                problems.append("DamSafety {}/damsuite/{} != master".format(p, name))
        # nd copies must be internally consistent AND equal to the master
        nd = _copies(nd_root, ND_PLUGINS, name)
        distinct = set(v for v in nd.values() if v is not None)
        if len(distinct) > 1:
            problems.append("nd_hazard copies of {} disagree with each other".format(name))
        for p, sha in nd.items():
            if sha is None:
                problems.append("nd_hazard {}/damsuite/{} missing".format(p, name))
            elif sha != master:
                problems.append("nd_hazard {}/damsuite/{} != DamSafety master (forks diverge "
                                "in the safety core)".format(p, name))
    # plugin-root provenance files (outside damsuite/, so the pin cannot cover them)
    for label, (dwr_rel, nd_rel) in PLUGIN_ROOT_SHARED.items():
        a = _sha(os.path.join(ROOT, dwr_rel))
        b = _sha(os.path.join(nd_root, nd_rel))
        if a is None:
            problems.append("DamSafety {} missing".format(dwr_rel))
        elif b is None:
            problems.append("nd_hazard {} missing".format(nd_rel))
        elif a != b:
            problems.append("{} differs across forks ({} != {}) - a provenance-fingerprint file "
                            "must stay byte-identical".format(label, dwr_rel, nd_rel))
    return problems


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--nd", default=os.environ.get("ND_HAZARD_ROOT", "/home/user/nd_hazard"),
                    help="path to the nd_hazard checkout")
    args = ap.parse_args(argv)
    problems = check(args.nd)
    if problems:
        print("SAFETY-CORE FORK DRIFT:")
        for p in problems:
            print("  - " + p)
        return 1
    print("Safety core (chain.py, solvegate.py) and fingerprint.py are identical across DamSafety "
          "and nd_hazard.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
