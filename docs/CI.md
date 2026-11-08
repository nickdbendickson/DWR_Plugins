# CI

`.github/workflows/ci.yml` runs on every pull request and every push to `main`. The repository
is public, so GitHub-hosted runner minutes are free. (`anuga-omp-scaling.yml` is a separate,
manually started workflow.)

| Job | What it runs | Time |
|---|---|---|
| `fast-checks` | `tools/ci_checks.py` (no vendored copy of the shared code: every plugin's `damsuite/` and shared window file is a shim onto `DamSafety_Core`; versions and changelogs vs `main`, files kept identical across plugins, zone and window colours, Qt6 enum spellings, compile, golden baseline), `tools/test_ci_checks.py`, the DamSafety Core alias / shim tests, the fingerprint nested-VRT test. No QGIS. | < 1 min |
| `plugin-suites` | Screening `run_all.py`, Runner fast suite, Hazard `run_all.py`, Hazard Lite, `tools/gui_lint.py`, the Runner wizard and batch-chain real-Qt scripts, pytest for suite dams and Setup, on QGIS from conda-forge. | 10-20 min |
| `model-tests` | `python -m nd_breach_runner.tests.run_tests --models` on LISFLOOD-FP 8.3 built from the Zenodo source with `DamSafety_Runner/scripts/build_lisflood_linux.sh` (cached), and ANUGA 4.0.1 in its own conda env. | 10-17 min |

Every job fails the run when it fails. A newer push to a PR cancels that PR's older run.

## Making the checks required (repository owner)

Settings > Rules > Rulesets > New branch ruleset: target the default branch, turn on
"Require status checks to pass", and add `fast-checks`, `plugin-suites` and `model-tests`.
Without a ruleset (or a classic branch protection rule) that requires them, GitHub lets a PR
merge while its checks are red or have not run.

## When no job starts

A run whose jobs fail in a few seconds with no steps, annotated "The job was not started because
recent account payments have failed or your spending limit needs to be increased", is an account
billing lock, not a test failure. Fix the payment method under the account's
Settings > Billing and licensing, then re-run the jobs.

## Running CI locally

The cloud sessions' `/usr/bin/python3.12` has QGIS 3.34 (Qt5), GDAL 3.8, numpy 1.26 and scipy.
As it is, it runs fast-checks and every plugin-suites command except the last, which needs pytest
(not installed; the venv below adds it). model-tests also needs an ANUGA env from micromamba or
conda, neither of which is installed; cmake and g++ for the LISFLOOD-FP build are. CI's conda
`qgis` is not pinned and resolves QGIS 4.x on Qt6 (4.2.3 with GDAL 3.13 in October 2026), so a
local pass on QGIS 3.34 is needed but not enough: the PR's own CI run is the Qt6 check. From the
repository root:

```bash
# a venv that keeps the system QGIS, GDAL, numpy and scipy, plus pytest
/usr/bin/python3.12 -m venv --system-site-packages "$HOME/ci-py"
"$HOME/ci-py/bin/pip" install -q pytest
PY="$HOME/ci-py/bin/python"
export QT_QPA_PLATFORM=offscreen

# fast-checks (needs origin/main for the "versions only go up" checks). CI fetches it with
# --depth=1 because its checkout is shallow anyway; in a full clone --depth=1 makes the clone
# shallow, and `git merge origin/main` then refuses ("unrelated histories"), so leave it out.
git fetch --no-tags origin +refs/heads/main:refs/remotes/origin/main
CI_BASE_REF=origin/main $PY tools/ci_checks.py
$PY tools/test_ci_checks.py
$PY DamSafety_Core/tests/test_alias.py
$PY DamSafety_Core/tests/test_ui_shim.py
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

# model-tests: this repo ships no LISFLOOD-FP binary, so build CI's 8.3 (about 30 s on 4 cores).
# nd_hazard's nd_breach_runner/bin/linux-x64/lisflood is LISFLOOD-FP 8.2 (glibc 2.38 or newer),
# not what CI runs, and builds can give different results, so build 8.3 to reproduce CI.
curl -fsSL -o /tmp/lisflood.zip https://zenodo.org/api/records/13121119/files/LISFLOOD-FP-8.3.zip/content
echo "abaa2e41b58f6c7df4443274bf130d4a  /tmp/lisflood.zip" | md5sum -c -
unzip -q -o /tmp/lisflood.zip -d /tmp
bash DamSafety_Runner/scripts/build_lisflood_linux.sh /tmp/LISFLOOD-FP-8.3/source "$HOME/lisflood-nd"
NDBREACH_LISFLOOD="$HOME/lisflood-nd/lisflood" \
NDBREACH_ANUGA_PYTHON=/path/to/anuga/env/bin/python \
  $PY -m nd_breach_runner.tests.run_tests --models
```

## Developers: shared code lives in DamSafety_Core

Since DamSafety Core 1.0.0 there is ONE copy of the code the plugins share:

- `DamSafety_Core/damsuite/` - the `damsuite` package (dam records, the REG_05.2026 curve table,
  the suite settings file, the offline pack reader and builder, the pre-solve gate, the version
  checks between the plugins, ...). Edit it here. Every other plugin's `damsuite/` folder holds
  only `__init__.py`, a two-line shim (`from DamSafety_Core import alias; alias.install(__name__)`)
  after which `<Plugin>.damsuite` and `<Plugin>.damsuite.<module>` are the very module objects of
  `DamSafety_Core.damsuite` (`DamSafety_Core/alias.py`, a `MetaPathFinder`). The plugins keep
  their relative imports (`from .damsuite import x`, `from ..damsuite.x import y`).
- `DamSafety_Core/ui/` - the shared window files (`tokens.py`, `ui_widgets.py`, `ui_fit.py`,
  `suite_settings.py`, `suite_settings_tools.py`, `suite_styles.py`, `suite_home.py`,
  `tool_dialog.py`) and `ui/styles/` (the QML layer styles). A plugin's copy of one of these
  names is a three-line shim: `import sys; from DamSafety_Core.ui import x as _m;
  sys.modules[__name__] = _m`. Setup's `suite_config.py` / `suite_fill.py` / `pack_convert.py`
  are the same shim onto `DamSafety_Core.damsuite`.
- `DamSafety_Core/damsuite/FILES.json` is the manifest `damsuite.selfcheck()` compares the
  installed files with (each plugin logs a warning at start-up when they differ). After editing
  anything under `DamSafety_Core/damsuite/`, run `python3 tools/damsuite_files.py`;
  `tools/ci_checks.py` fails on a stale manifest, on a vendored file inside a plugin's
  `damsuite/`, on a window file that is not the shim, and on a plugin whose `metadata.txt` lacks
  `plugin_dependencies=DamSafety Core`.
- Version bookkeeping: `DamSafety_Core/damsuite/versions.py` `RELEASE` must equal each plugin's
  `metadata.txt` version (Hazard, Runner, Screening), as before; bump the Core's own version in
  `DamSafety_Core/metadata.txt` when its content changes.
- Retired: `tools/sync_damsuite.py`, `tools/damsuite_pin.json`, `tools/tag_damsuite_pin.sh`,
  `tools/sync_ui.py`, `tools/test_sync_damsuite.py`, `shared/`. The nd_hazard fork (archived)
  still vendors its own copies; the shared pin it mirrored is no longer produced.
- Tests import the plugins as packages from the repo root, where `DamSafety_Core` is a sibling,
  so the shims resolve. In a QGIS profile the same holds: every plugin folder sits in
  `python/plugins/`. `DamSafety_Core/tests/test_alias.py` and `test_ui_shim.py` run in plain
  Python.

Locally `tools/ci_checks.py` also runs the cross-fork parity check when an nd_hazard checkout is
at `/home/user/nd_hazard` (or `ND_HAZARD_ROOT`); CI has no such checkout and skips it.

An ANUGA env like CI's: `micromamba create -y -n anuga -c conda-forge python=3.12 anuga=4.0.1 scipy`.
Tests that change `NDBREACH_*` variables must restore them (a leak once broke `--models`).
