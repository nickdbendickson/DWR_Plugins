# CI

`.github/workflows/ci.yml` runs on every pull request and every push to `main`. The repository
is public, so GitHub-hosted runner minutes are free. (`anuga-omp-scaling.yml` is a separate,
manually started workflow; `os-tests.yml` runs the suites on Windows and macOS before a release
and every Monday, see "Windows and macOS" below; `release.yml` runs on a `v*` tag, see "Release"
below.)

| Job | What it runs | Time |
|---|---|---|
| `fast-checks` | `tools/ci_checks.py` (damsuite drift and pin, shared/ui drift, versions and changelogs vs `main`, the 12 KB cap on each `metadata.txt` `changelog=`, no template value in a metadata.txt, files kept identical across plugins, zone and window colours, Qt6 enum spellings, compile, golden baseline), `tools/test_ci_checks.py`, the fingerprint nested-VRT test, and the Runner fast suite (`python3 -m nd_breach_runner.tests.run_tests`, stub Qt) on **numpy 2** + scipy. No QGIS, no GDAL: the GDAL-backed Runner tests skip there (see below). | 3-4 min |
| `plugin-suites` | Screening `run_all.py`, Runner fast suite, Hazard `run_all.py`, Hazard Lite, `tools/gui_lint.py` on a 1080p xvfb display (offscreen fallback), the Runner wizard and batch-chain real-Qt scripts, pytest for suite dams, Setup, Data and PMP, on the newest QGIS from conda-forge: **QGIS 4.x / Qt6 / numpy 2** (4.2.3, GDAL 3.13 in November 2026; nothing pinned). | 10-20 min |
| `plugin-suites-ltr` | The same suites in the official `qgis/qgis:release-3_34` container (the job's `container:`): **QGIS 3.34 LTR / Qt5 / GDAL 3.4 / numpy 1.x** on Ubuntu 22.04's system `python3`, pytest and scipy from pip, nothing from conda. This is the line `metadata.txt`'s `qgisMinimumVersion=3.28` promises and what the owner runs on macOS. | 10-20 min |
| `model-tests` | `python -m nd_breach_runner.tests.run_tests --models` on LISFLOOD-FP 8.3 built from the Zenodo source with `DamSafety_Runner/scripts/build_lisflood_linux.sh` (cached), and ANUGA 4.0.1 in its own conda env (QGIS from conda, `numpy<2`). | 10-17 min |

Every job fails the run when it fails. A newer push to a PR cancels that PR's older run.

## The matrix (audit CI matrix, 2026-11-07)

Three legs now cover what users run: Qt5 + numpy 1 (`plugin-suites-ltr`, the LTR line), Qt6 +
numpy 2 (`plugin-suites`, QGIS 4.x) and plain Python + numpy 2 without QGIS or GDAL
(`fast-checks`). Before this, every job pinned `numpy<2` and conda's unpinned `qgis` resolved to
4.x/Qt6 only, so neither the LTR the metadata promises nor numpy 2 was ever tested.

* **numpy 2 legs are red until `claude/reliability-engine-guards` merges.** `np.cross` on
  2-vectors raises under numpy 2 (`DamSafety_Runner/core/hecras_structures.py:212`,
  `core/hecras_crest.py:552/555`), so the three `hecras_crest_offcrest_*` Runner tests
  (`walk_stops_at_a_junction`, `round4_walk_and_pool_edges`, `round4_ridge_and_section_edges`)
  fail on `fast-checks` and `plugin-suites` until that branch's fix lands. Nothing else is
  expected to differ between the legs; a test that is red on one leg only is a real finding.
* **GDAL tests skip without osgeo.** `DamSafety_Runner/tests/registry.py` has `Skip` and
  `need_osgeo()`: a test (or the helper that builds its GeoTIFF) calls `need_osgeo()` first, the
  harness prints `skip  <name> (no GDAL (osgeo) in this Python)` and counts it in the summary
  (`N tests, F failed, S skipped`) instead of failing. On `fast-checks` 32 tests skip this way;
  on the QGIS legs they all run. A new GDAL-backed Runner test must call `need_osgeo()`.
* **GUI lint on a real display.** `docs/gui-grammar.md`'s "no page scrolls sideways" rule needs
  a 1920x1080 screen (offscreen Qt is 800x600 and the rule returns early). Both suite legs first
  probe `xvfb-run -s "-screen 0 1920x1080x24" env QT_QPA_PLATFORM=xcb` (apt `xvfb`; the LTR image
  already ships it) and run `tools/gui_lint.py` there; if the probe fails the lint runs offscreen
  as before and the step prints a warning annotation, so a broken xvfb never blocks a merge but
  is visible in the run.
* **The LTR image.** `qgis/qgis:release-3_34` is rebuilt by the QGIS project from the
  `release-3_34` branch (`.docker/qgis.dockerfile`: `qgis3-build-deps` on Ubuntu 22.04, Qt5,
  cmake prefix `/usr`, `PYTHONPATH` already points at `/usr/share/qgis/python`, xvfb and
  supervisor installed, Python 3.10, numpy from pip, GDAL's bindings from apt `python3-gdal`,
  which were built against numpy 1). The job pins `numpy<2` with pip so this stays the numpy 1
  leg whatever numpy a rebuild of the image resolved, then adds scipy and pytest with pip and
  `xvfb` with apt, nothing else. Should the tag be retired, point `image:` at a `final-3_34_*`
  point-release tag (same layout) or at the next LTR (`release-3_40`).
* **The LTR image's BLAS, and `OPENBLAS_CORETYPE: Haswell`.** The image's system numpy (1.21.5,
  from apt; the pip line leaves it in place) multiplies matrices through
  `libopenblas0-pthread 0.3.20`, which the image's alternatives put in front of the reference
  BLAS. That OpenBLAS chooses its kernels by CPU when it loads, and on an Intel CPU with
  AVX512_BF16 (Sapphire Rapids and later) it chooses "Cooperlake", whose matrix-matrix
  routines (dgemm, dtrsm, and LAPACK's solve on top of them) return wrong numbers past a
  modest size. Reading the 0.3.20 source and the library itself, that core calls a 4x8 kernel
  with the block sizes of a 16x2 one; upstream removed that special case in 0.3.21, and
  Ubuntu 22.04 still ships 0.3.20. Nothing raises, and the wrong numbers are the same on
  every run.
  From 2026-11-09 to 2026-11-13 this failed `plugin-suites-ltr` 11 times in 174, on
  eleven commits across nine branches, one of them only an Actions version bump: all 4 runs that landed on AWS-hosted
  runners and 7 of the 40 in Azure centralus, none in the other ten regions. It was always
  `reservoir_carve_terrain_wrapper_keeps_the_volume_at_any_cell` with
  `(40.0, 1426399.0234128712, 1110289.0878099161)`,
  `reservoir_carve_run_hook_writes_a_copy_and_leaves_the_source_dem`, and, once it existed,
  the Finder's `test_v2_clean_positives_trusted_marks_terrain` with `0.5 not greater than 0.9`.
  One commit failed on centralus and passed 36 minutes later on eastus with nothing else
  different. The same tests run against the library taken from the image give exactly those
  numbers on a CPU of that kind, and pass with `OPENBLAS_CORETYPE=Haswell`. No job log of that
  period names the CPU, so that the failing runners were of that kind is inferred from the
  numbers matching to the last digit.
  The job therefore sets `OPENBLAS_CORETYPE: Haswell` for every step (Haswell kernels are
  what an AMD runner gets anyway, and every x64 runner can run them; a forced core is not
  checked against the CPU, so `SkylakeX` is not safe on a runner without AVX-512, and one
  thread alone does not cure it). OpenBLAS reads the variable once,
  when it loads, so it must stay in the job's `env:` and cannot be set from a test.
  `tools/blas_selfcheck.py` runs at the end of the provision step, twice: with the setting
  removed, to print the CPU, the library and the kernels the runner would have picked and
  whether they are right (this never fails the job, and is the record that was missing), and
  as the job runs, where a wrong product stops the job before any suite. Do not loosen those
  three tests, and do not re-run a red job to get a different machine.
  **This is a CI setting, not a fix for users.** Someone running the plugins on Ubuntu 22.04
  with the system numpy and that OpenBLAS on such a CPU gets the same wrong products: a wrong
  reservoir carve and Finder scores that are all one value. A numpy whose OpenBLAS is 0.3.21
  or later is not affected (the other CI legs, on numpy 2, never failed these tests on the
  same runners). `python3 tools/blas_selfcheck.py` answers the question for any machine in
  under a second.

## Making the checks required (repository owner)

Settings > Rules > Rulesets > New branch ruleset: target the default branch, turn on
"Require status checks to pass", and add `fast-checks`, `plugin-suites`, `plugin-suites-ltr` and
`model-tests` (the Qt6 and the LTR legs are both required: a plugin that loads on one and not the
other is a release blocker).
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
local pass on QGIS 3.34 is the `plugin-suites-ltr` leg; the PR's own CI run is the Qt6 and the
numpy 2 check. The `plugin-suites-ltr` leg itself can be reproduced with Docker:
`docker run --rm -v "$PWD":/w -w /w qgis/qgis:release-3_34 bash -c 'pip3 install -q "numpy<2" scipy pytest && QT_QPA_PLATFORM=offscreen python3 -m nd_breach_runner.tests.run_tests'`.
From the repository root:

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
$PY tools/test_gui_lint.py
$PY DamSafety_Hazard/tests/test_fingerprint_vrt.py

# plugin-suites
$PY DamSafety_Screening/tests/run_all.py
$PY -m nd_breach_runner.tests.run_tests      # nd_breach_runner/ is a shim beside DamSafety_Runner
$PY DamSafety_Hazard/tests/run_all.py
$PY DamSafety_HazardLite/tests/test_core.py
$PY -m unittest DamSafety_Finder.tests.test_core DamSafety_Finder.tests.test_finder DamSafety_Finder.tests.test_qgis
$PY tools/gui_lint.py                        # offscreen: all rules but "no sideways scroll"
xvfb-run -a -s "-screen 0 1920x1080x24" env QT_QPA_PLATFORM=xcb $PY tools/gui_lint.py   # as CI
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

## Windows and macOS

`ci.yml` runs every job on Ubuntu. `.github/workflows/os-tests.yml` runs the suites of the
`plugin-suites` job on `windows-latest` and on `macos-latest` (Apple Silicon), with the newest
conda-forge QGIS (Qt6, numpy 2): the fast checks, Screening, the Runner fast suite, Hazard,
Hazard Lite, Finder, the GUI lint offscreen, the Runner wizard and batch chain, the Suite dam
data / Setup / Data pytest files and PMP. What it is for: a path with a backslash or a drive
letter, a file still open when it is deleted, a text file read in the Windows code page (the job
does not turn on Python's UTF-8 mode, as a user's QGIS does not), a program started without
its `.exe`.

| It runs | Why |
|---|---|
| before every release (`release.yml` calls it; the `release` job waits for it) | a release that fails on Windows or macOS is not published |
| every Monday, 09:23 UTC | a break is found within a week, not on release day |
| by hand: *Actions → Windows and macOS → Run workflow* | before tagging, or after a fix |
| on a pull request that changes `os-tests.yml` | the workflow itself is tested |

It is not a merge gate and should not be made a required check: it adds two jobs of 30 to 40
minutes, and a pull request that does not touch the workflow never starts it (a required check
that never starts blocks the merge). When the Monday run fails, GitHub mails the repository
owner; fix it on a branch and start the workflow by hand on that branch to see it pass.

Not covered on Windows and macOS: the Runner's `--models` suite (real LISFLOOD-FP and ANUGA
runs; Linux only, `model-tests`), the 1080p GUI lint (it needs xvfb) and the QGIS 3.34 LTR / Qt5
leg. The Windows LISFLOOD-FP build is started once in `release.yml` (`lisflood-windows-check`).

What the first runs found (13 November 2026), and what was done:

| Found | What it was | Done |
|---|---|---|
| The macOS job was cancelled at once: "The configuration 'macos-14-arm64-us-default' is not supported" | GitHub no longer offers the `macos-14` label | `os-tests.yml` uses `macos-latest`. **`release.yml`'s `lisflood-macos` job still names `macos-14` and would stop a release the same way.** It is left as it is on purpose: a newer runner bundles a newer Homebrew `libomp`, which raises the oldest macOS the bundled engine starts on. Decide the runner (and the oldest macOS to support) before the next tag. |
| Windows: `No module named 'qgis'` in every suite that uses the real QGIS | conda-forge's Windows package keeps QGIS's Python under `Library/python`, which Python does not search (its own start script sets the path, for `cmd`, not for the job's bash) | step "Make QGIS importable": `tools/ci_qgis_env.py`, first by writing `PYTHONPATH` (see the next row) and `QGIS_PREFIX_PATH` for the later steps |
| Windows, second run: still `No module named 'qgis'` | every step's shell activates the environment again, and QGIS's activation script puts its own folders in front of `PYTHONPATH` joined with `:`. Windows Python splits on `;`, so the first folder of the value set by the workflow was glued to that text | `tools/ci_qgis_env.py` writes a path file (`ci_qgis.pth`) into the environment's site-packages instead, and the step imports `qgis.core` to prove it |
| Windows: `damsuite/FILES.json is stale` | Git for Windows checked the `.py` files out with CR LF; the list holds the hashes of the files as zipped (LF) | `core.autocrlf false` before the checkout. A release zip is built on Linux and is not affected |
| Windows: the Runner's installer and Linux-build tests failed with "Windows Subsystem for Linux has no installed distributions" | they run `install_macos.sh` and `build_lisflood_linux.sh`; on Windows `bash` is WSL's launcher | those tests do not apply on Windows and say so (`bash_available()`); they run on Linux and macOS as before |
| Windows: two tests could not delete or rewrite a file | the test still held it open (a memory-mapped `.npy`, an open DEM); Linux allows that, Windows does not. The product code does not keep either open | the tests let go of the file first |
| Windows: the Runner suite ended part-way with exit status 127 and no message | **a real fault in the Runner.** Culvert prep (`core/culvert_prep.py`), when it straightens a culvert line the engineer accepted, read the old line's vertex count after it had replaced the line; the old line's memory is freed by then. Linux and macOS happened to read the right number, Windows ended the process. The first run's output was buffered and hid the test (`PYTHONUNBUFFERED=1` now) | the count is read before the line is replaced; `culvert_prep_limit_auto_applies_and_geometry_is_only_proposed` checks the note it goes into. No other place in the suite has this pattern |
| macOS: every suite ran; 5 of the Runner's 1,264 tests failed | none is a fault in the Runner. Two HEC-RAS stop tests counted a process group through `/proc` (macOS has none). One wrote `dwr_dams.gpkg` beside `DWR_dams.gpkg` (the same file where case is ignored). One lowered the stack limit to 8 MB first, which macOS refuses for this Python. One fixture had its cell boundaries exactly on the sampled stations, and which cell such a tie goes to differs on Apple Silicon | the tests count the group with `ps` where there is no `/proc`, use a separate folder, start from the limit as it is, and move the fixture 0.02 ft off the tie. Not checked on a Mac: HEC-RAS itself (there is no HEC-RAS for macOS) |
| macOS, third run: one Runner test left (`test_hecras_stack_limit.py`) | macOS also refuses to put a stack limit back once it was lowered | the test tolerates both refusals and compares with the limit read back. |
| Windows, third run (QGIS imports, every suite runs): 17 of 1,298 Runner tests, 6 of 84 Hazard scripts, 1 Setup test | **Four are faults a Windows user would meet** (changelog, Runner 3.20.5 / Hazard 2.25.4 / Core 1.5.1): a second download over a day file whose layer is still in the project stopped with `[WinError 5] Access is denied`; refreshing the sample dam with its terrain loaded said only `Access is denied`; a LISFLOOD-FP path that is no program said `[WinError 193] %1 is not a valid Win32 application`; the kill stage of a stop asked twice instead of killing. **The rest are tests that assumed Linux:** a path split on `/`, a file the test still had open when it removed its folder, `signal.SIGKILL`, a shell script as the stand-in LISFLOOD-FP, `lisflood.EXE` for `lisflood.exe` (the extension comes from `PATHEXT`), `//` (a network name on Windows), a layer source written with `/`, and settings saved with no organisation name (a native `QSettings` on Windows is the registry and keeps nothing then; the tests now keep an INI file in a temp folder) | fixed in the product and in the tests. Nothing is skipped except a Wine path check and the `//` check, which mean something else on Windows. |
| Windows, third run: windows measure wider (GUI lint: 8 windows "cannot shrink below" 852 to 1040 px, limit 800; the Classify page 1034 px in a 984 px view; the two docks over 420 px) | **not explained yet.** The offscreen platform on Windows may use another style or font than on Linux. `tools/gui_lint.py` now prints what it measured with (system, style, font, the width of 20 capital M, screen, scale) and says whether the width is a set minimum or what the contents need | open: read the next run's line before changing a window or a limit. |

## Release: GitHub Release + QGIS plugin repository

`.github/workflows/release.yml` runs when a tag `v*` is pushed (`git tag v2026.11.07 && git push
origin v2026.11.07`), and by hand from the Actions tab (then it makes a *draft* release named
after the commit and publishes nothing to QGIS).

| Job | What it does |
|---|---|
| `lisflood-linux` | `ubuntu-22.04`: downloads LISFLOOD-FP 8.3 from Zenodo (md5 and sha256 checked, the same file CI's `model-tests` builds), runs `DamSafety_Runner/scripts/build_lisflood_linux.sh` with `LISFLOOD_STATIC=1` (static libstdc++ / libgcc / libgomp; glibc 2.35 or newer at run time). |
| `lisflood-macos` | `macos-14` (Apple Silicon; **the label is no longer offered, see "Windows and macOS" above**): `DamSafety_Runner/scripts/build_lisflood_macos.sh` (Homebrew cmake + libomp), then `libomp.dylib` is copied next to the binary and the load command changed to `@executable_path/libomp.dylib` (`install_name_tool`, ad-hoc `codesign`) so it runs without Homebrew; checked by running `lisflood -version` with Homebrew's copy moved away. |
| `lisflood-windows` | `ubuntu-latest`: `DamSafety_Runner/scripts/build_lisflood_windows_cross.sh` cross-compiles with `g++-mingw-w64-x86-64-posix`, `-static` (libgomp, winpthread, libstdc++ and libgcc inside the .exe; `objdump -p` confirms no such DLL is imported). The MinGW edits (`_aligned_malloc` / `_aligned_free`) are part of `scripts/patch_lisflood.py`, so the Linux and macOS builds get them too, guarded by `_WIN32`. |
| `lisflood-windows-check` | `windows-latest`: starts the cross-compiled `lisflood.exe` once on a real Windows (`tools/lisflood_smoke.py`: one second on a 5 by 5 grid, the check the Runner makes before every dam). The build job runs on Linux and cannot start it. |
| `os-tests` | Calls `os-tests.yml`: the plugin suites on Windows and macOS (see "Windows and macOS" below). |
| `release` | Waits for every job above. `tools/build_release.py --runner-bin bin` zips the committed files of the tagged commit (no `tests/`, `__pycache__`, dotfiles), puts the three builds into the Runner zip under `DamSafety_Runner/bin/<platform>/` with a `README.txt`; attaches the zips, `SHA256SUMS`, each binary on its own (`lisflood-linux-x64`, `lisflood-windows-x64.exe`, `lisflood-macos-arm64.tar.gz`), the patched 8.3 source (`lisflood-8.3-ndbreach-source.zip`, the GPL-2.0 corresponding source) and `plugins.xml` to the GitHub Release (`softprops/action-gh-release`); writes `plugins.xml` with `tools/make_plugins_xml.py` and pushes it, with the plugin icons, to the root of the `gh-pages` branch (`peaceiris/actions-gh-pages`, `keep_files: true`, merged with the index already there). |

The Runner finds a bundled build on its own (`core/lisflood.bundled_exe`:
`bin/linux-x64/lisflood`, `bin/windows-x64/lisflood.exe`, `bin/macos-arm64/lisflood`,
`bin/macos-x64/lisflood` if one is ever built) after every installed LISFLOOD-FP and after the
suite / provider settings, never instead of them. `bin/` is git-ignored: the binaries exist only
in the release assets. A failed LISFLOOD-FP job stops the release. So does a Windows build that
does not start, and a suite that fails on Windows or macOS.

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
