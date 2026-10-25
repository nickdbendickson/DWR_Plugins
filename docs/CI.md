# CI

`.github/workflows/ci.yml` runs on every pull request and every push to `main`. The repository
is public, so GitHub-hosted runner minutes are free. (`anuga-omp-scaling.yml` is a separate,
manually started workflow.)

| Job | What it runs | Time |
|---|---|---|
| `fast-checks` | `tools/ci_checks.py` (damsuite drift and pin, versions and changelogs vs `main`, compile, golden baseline), `tools/test_ci_checks.py`, the fingerprint nested-VRT test. No QGIS. | < 1 min |
| `plugin-suites` | Screening `run_all.py`, Runner fast suite, Hazard `run_all.py`, Hazard Lite, `tools/gui_lint.py`, the Runner wizard and batch-chain real-Qt scripts, pytest for suite dams and Setup, on QGIS from conda-forge. | ~19 min |
| `model-tests` | `python -m nd_breach_runner.tests.run_tests --models` on LISFLOOD-FP 8.3 built from the Zenodo source with `DamSafety_Runner/scripts/build_lisflood_linux.sh` (cached), and ANUGA 4.0.1 in its own conda env. | ~16 min |

Every job fails the run when it fails. A newer push to a PR cancels that PR's older run.

## Making the checks required (repository owner)

Settings > Rules > Rulesets > New branch ruleset: target the default branch, turn on
"Require status checks to pass", and add `fast-checks`, `plugin-suites` and `model-tests`.
Without a ruleset, GitHub lets a PR merge while its checks are red or have not run.

## When no job starts

A run whose jobs fail in a few seconds with no steps, annotated "The job was not started because
recent account payments have failed or your spending limit needs to be increased", is an account
billing lock, not a test failure. Fix the payment method under the account's
Settings > Billing and licensing, then re-run the jobs.

## Running CI locally

The cloud sessions have QGIS 3.34 with numpy 1.26 and scipy at `/usr/bin/python3.12`, which is
enough for all three jobs. From the repository root:

```bash
PY=/usr/bin/python3.12
export QT_QPA_PLATFORM=offscreen

# fast-checks (needs origin/main for the "versions only go up" checks)
git fetch --no-tags --depth=1 origin +refs/heads/main:refs/remotes/origin/main
CI_BASE_REF=origin/main $PY tools/ci_checks.py
$PY tools/test_ci_checks.py
$PY DamSafety_Hazard/tests/test_fingerprint_vrt.py

# plugin-suites
$PY DamSafety_Screening/tests/run_all.py
$PY -m nd_breach_runner.tests.run_tests      # nd_breach_runner/ is a shim beside DamSafety_Runner
$PY DamSafety_Hazard/tests/run_all.py
$PY DamSafety_HazardLite/tests/test_core.py
$PY tools/gui_lint.py
$PY DamSafety_Runner/tests/realqt_wizard.py
$PY DamSafety_Runner/tests/realqt_chain.py
$PY -c "import os, sys, pytest; rc = pytest.main(['-q', '-p', 'no:cacheprovider', 'DamSafety_Runner/tests/suite_dams', 'DamSafety_Setup/tests']); sys.stdout.flush(); os._exit(int(rc))"

# model-tests: this repo ships no LISFLOOD-FP binary, so build one (about 10 s) ...
curl -fsSL -o /tmp/lisflood.zip https://zenodo.org/api/records/13121119/files/LISFLOOD-FP-8.3.zip/content
echo "abaa2e41b58f6c7df4443274bf130d4a  /tmp/lisflood.zip" | md5sum -c -
unzip -q -o /tmp/lisflood.zip -d /tmp
bash DamSafety_Runner/scripts/build_lisflood_linux.sh /tmp/LISFLOOD-FP-8.3/source "$HOME/lisflood-nd"
# ... or copy nd_hazard's nd_breach_runner/bin/linux-x64/lisflood. Then:
NDBREACH_LISFLOOD="$HOME/lisflood-nd/lisflood" \
NDBREACH_ANUGA_PYTHON=/path/to/anuga/env/bin/python \
  $PY -m nd_breach_runner.tests.run_tests --models
```

An ANUGA env like CI's: `micromamba create -y -n anuga -c conda-forge python=3.12 anuga=4.0.1 scipy`.
Tests that change `NDBREACH_*` variables must restore them (a leak once broke `--models`).
