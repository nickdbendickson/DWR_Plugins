# CI

`.github/workflows/ci.yml` runs on every pull request and every push to `main`. The repository
is public, so GitHub-hosted runner minutes are free. (`anuga-omp-scaling.yml` is a separate,
manually started workflow; `release.yml` runs on a `v*` tag, see "Release" below.)

| Job | What it runs | Time |
|---|---|---|
| `fast-checks` | `tools/ci_checks.py` (damsuite drift and pin, shared/ui drift, versions and changelogs vs `main`, the 12 KB cap on each `metadata.txt` `changelog=`, no template value in a metadata.txt, files kept identical across plugins, zone and window colours, Qt6 enum spellings, compile, golden baseline), `tools/test_ci_checks.py`, the fingerprint nested-VRT test. No QGIS. | < 1 min |
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

Locally `tools/ci_checks.py` also runs the cross-fork parity check when an nd_hazard checkout is
at `/home/user/nd_hazard` (or `ND_HAZARD_ROOT`); CI has no such checkout and skips it.

An ANUGA env like CI's: `micromamba create -y -n anuga -c conda-forge python=3.12 anuga=4.0.1 scipy`.
Tests that change `NDBREACH_*` variables must restore them (a leak once broke `--models`).

## Changelogs: metadata.txt and docs/CHANGELOG.md

QGIS's Plugin Manager parses and renders the whole `changelog=` value of every installed plugin,
so the Runner's 149 KB / Hazard's 86 KB changelogs (2026-11-07) slowed it down for anyone with
the suite installed. Each `metadata.txt` now keeps its newest entries only (about 7; the gate
fails a `changelog=` over 12 KB) and the older entries live verbatim in `docs/CHANGELOG.md`
under `## Archived metadata changelog — <plugin>`. The "no changelog entry of the base is
missing" rule accepts an entry that left `metadata.txt` as long as its version string is still
in `docs/CHANGELOG.md` (a bullet title such as `**Runner 3.10.53: ...**`, or a line of the
plugin's archived section); an entry that is in neither still fails. To trim again: cut the
oldest entries out of `changelog=` and paste them, unchanged and newest first, at the top of the
plugin's archived section's code block (`tools/test_ci_checks.py` has the cases).

## Release: GitHub Release + QGIS plugin repository

`.github/workflows/release.yml` runs when a tag `v*` is pushed (`git tag v2026.11.07 && git push
origin v2026.11.07`), and by hand from the Actions tab (then it makes a *draft* release named
after the commit and publishes nothing to QGIS).

| Job | What it does |
|---|---|
| `lisflood-linux` | `ubuntu-22.04`: downloads LISFLOOD-FP 8.3 from Zenodo (md5 and sha256 checked, the same file CI's `model-tests` builds), runs `DamSafety_Runner/scripts/build_lisflood_linux.sh` with `LISFLOOD_STATIC=1` (static libstdc++ / libgcc / libgomp; glibc 2.35 or newer at run time). |
| `lisflood-macos` | `macos-14` (Apple Silicon): `DamSafety_Runner/scripts/build_lisflood_macos.sh` (Homebrew cmake + libomp), then `libomp.dylib` is copied next to the binary and the load command changed to `@executable_path/libomp.dylib` (`install_name_tool`, ad-hoc `codesign`) so it runs without Homebrew; checked by running `lisflood -version` with Homebrew's copy moved away. |
| `lisflood-windows` | `ubuntu-latest`: `DamSafety_Runner/scripts/build_lisflood_windows_cross.sh` cross-compiles with `g++-mingw-w64-x86-64-posix`, `-static` (libgomp, winpthread, libstdc++ and libgcc inside the .exe; `objdump -p` confirms no such DLL is imported). The MinGW edits (`_aligned_malloc` / `_aligned_free`) are part of `scripts/patch_lisflood.py`, so the Linux and macOS builds get them too, guarded by `_WIN32`. |
| `release` | `tools/build_release.py --runner-bin bin` zips the committed files of the tagged commit (no `tests/`, `__pycache__`, dotfiles), puts the three builds into the Runner zip under `DamSafety_Runner/bin/<platform>/` with a `README.txt`; attaches the zips, `SHA256SUMS`, each binary on its own (`lisflood-linux-x64`, `lisflood-windows-x64.exe`, `lisflood-macos-arm64.tar.gz`), the patched 8.3 source (`lisflood-8.3-ndbreach-source.zip`, the GPL-2.0 corresponding source) and `plugins.xml` to the GitHub Release (`softprops/action-gh-release`); writes `plugins.xml` with `tools/make_plugins_xml.py` and pushes it, with the plugin icons, to the root of the `gh-pages` branch (`peaceiris/actions-gh-pages`, `keep_files: true`, merged with the index already there). |

The Runner finds a bundled build on its own (`core/lisflood.bundled_exe`:
`bin/linux-x64/lisflood`, `bin/windows-x64/lisflood.exe`, `bin/macos-arm64/lisflood`,
`bin/macos-x64/lisflood` if one is ever built) after every installed LISFLOOD-FP and after the
suite / provider settings, never instead of them. `bin/` is git-ignored: the binaries exist only
in the release assets. A failed LISFLOOD-FP job stops the release.

**The QGIS plugin repository.** The index is served by GitHub Pages at
`https://nickdbendickson.github.io/dAmIt_Plugins/plugins.xml`, and its `download_url`s point at
the release assets. Two one-time steps for the repository owner:

1. *Settings → Pages → Build and deployment → Source: Deploy from a branch → Branch: `gh-pages`,
   folder `/ (root)`*. The branch is created by the first tagged run; until Pages is on, the URL
   answers 404 and QGIS reports the repository as unavailable (the release assets still install
   from ZIP).
2. Push the first `v*` tag.

Users then add the URL under *Plugins → Manage and Install Plugins → Settings → Add…* (with *Show
also experimental plugins* ticked) and get updates through the Plugin Manager. `plugins.xml`
holds one `<pyqgis_plugin>` per zip with the fields the installer reads (`version`,
`qgis_minimum_version` / `qgis_maximum_version`, `description`, `about`, `author_name`,
`download_url`, `file_name`, `experimental`, `update_date`, `tracker`, `repository`, ...), filled
from each `metadata.txt`; `tools/make_plugins_xml.py --merge` keeps the entries of plugins a later
release does not rebuild. `tools/mac/make_plugin_zips.py` stays the Mac installer's zip builder
(it keeps `.secrets.baseline` for a plugins.qgis.org upload; `tools/build_release.py` does not).

Local dry run of the release pieces (no binaries, no upload):

```bash
python3 tools/build_release.py --out dist            # dist/DamSafety_*_<ver>.zip + SHA256SUMS
python3 tools/make_plugins_xml.py --tag v0 --zips dist --out dist/plugins.xml
LISFLOOD_STATIC=1 bash DamSafety_Runner/scripts/build_lisflood_linux.sh /tmp/LISFLOOD-FP-8.3/source /tmp/lf   # static Linux build
```
