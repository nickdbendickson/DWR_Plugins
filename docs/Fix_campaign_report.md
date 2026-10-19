# Fix campaign — DWR_Plugins ↔ nd_hazard

**Date:** 2026-10-18
**Branch (both repos):** `claude/funny-goldberg-dg1x2s`
**Final HEADs:** DWR_Plugins `f983244`, nd_hazard `0e2484e` (both pushed, local == remote)
**Companion docs:** `docs/Audit_DWR_vs_nd_hazard.md` (the 54-finding comparison audit this campaign closed out)

---

## 1. What was asked

Three successive instructions drove this work:

1. *Audit the DWR plugins and compare them to the nd_hazard plugins.* → produced
   `docs/Audit_DWR_vs_nd_hazard.md` (54 confirmed/plausible findings, 6 refuted and
   excluded).
2. *Fix both sets of plugins.*
3. *Audit, compare, and optimize/fix issues. Do this 5 times. Look at the GUI changes of
   dam safety and provide feedback, and a similar display should be brought to nd_.*

So the campaign is **five iterated rounds** of audit → compare → fix across *both* repos,
plus a **review of the recent DamSafety GUI work** with the improved display **ported into
the nd_ plugins**. Every round committed and pushed to the branch above; no PRs were opened.

## 2. Method

- **Fan-out + adversarial verification.** Each round fanned work out across the plugin
  pairs, partitioned by **disjoint file ownership** so parallel edits never collided. Every
  candidate fix was handed to an independent skeptic who tried to **refute it by reading the
  same code** (refute-by-default). Only survivors were applied.
- **Re-audit each round.** The next round re-audited the previous round's own diffs. This
  caught two regressions *in my own fixes* (see §4, rounds 3–4) — the single most valuable
  property of the 5× loop.
- **Validation under sandbox limits.** The sandbox cannot run the full suite: `scipy` aborts
  the import under numpy 2.5.3 (so `DamSafety_Runner`'s `run_tests` is gated), and
  `PyQt5.sip` is cpython-3.12-only (real-Qt tests need `python3.12`, not the default 3.13).
  Fixes were validated with `py_compile`, targeted unit tests against the fakes/`qgis_mock`
  harness, and offscreen `python3.12` for the real-Qt GUI paths. Where a fix depends on real
  QGIS/GDAL/LISFLOOD runtime behaviour, it is confirmed by end-to-end code reading plus the
  sibling repo's own documented real-world failures.

---

## 3. Scope of what changed

Both repos share a vendored `damsuite/` package and a common ancestor; `nd_hazard` is the
forward release line (`nd_*`, higher version numbers) and `DWR_Plugins` is the active-dev
monorepo where the same plugins were rebranded `nd_* → DamSafety_*` with versions reset
*downward*. They have diverged in **both** directions, so almost every fix had to be reasoned
per-repo: a bug present in one was often already fixed in the other, and a few fixes needed
porting *from* nd_hazard *into* DWR and vice-versa.

~90+ distinct findings were addressed across the two repos over the five rounds. The
high-impact ones:

| # | Area | Fix |
|---|---|---|
| H1/H2 | Screening GeoPackage writes | `disable_rtree` / `gpkg_sqlite` so merge-dams and move-breach-point `INSERT`/`UPDATE` don't raise `no such function: ST_IsEmpty` on a real GDAL-written GeoPackage |
| H4 | Life-loss count | removed `collapse_dwellings` (it zeroed occupants of buildings sharing one 911/street address — routine for ND mobile-home parks/apartments/farmsteads, under-counting PAR and dropping the TM-11 band) |
| H6 | Fine FV1 run | wired closed upstream edges through `workflow.py` into `refine.py`/`lisflood.py` (round 1 added the plumbing; round 3 found it was still dead code and connected the call site) |
| H7 | HEC-RAS export | units derived from the CRS instead of writing cfs numbers into an "SI Units" project (was ≈35.3× inflow overstatement) |
| H8 | DEM elevation | metre-DEM-in-feet-CRS cross-check + `z_unit_scale` restored (was a silent ~3.28× elevation error) |
| H14 | Data updater | Qt6 scoped-enum crashes fixed via `qtcompat.py` |
| M3 | Habitability | parcel-site model keyed on the `fema_parcel` flag (round 3 introduced it; round 4 caught that a family-filter vs flag mismatch could still under-count one dwelling, and fixed + regression-tested it) |
| M7 | Runner datum | DEM-estimate datum surfaced as `"DEM estimate"` in the report |
| N1 | nd Runner | restored baseline-matched `average` DEM resampling and exposed/recorded the resample choice (nd had regressed to `bilinear`, which it documented running floods 3–40% high) |

The remaining medium/low fixes are recorded in their commit messages (round 2's
`M6…M24`, `L1…L3`, `P1`, and the Setup/Data wiring) and in each repo's `CHANGELOG.md`.

---

## 4. Round-by-round

**Round 1 — high-severity.** DWR: Screening `H1,H2,H3,H9,H10,H11,H12,H13,L2`; Hazard
`H4,H5`; Runner `H6,H7,H8`; Data `H14`. nd_hazard: Screening `H13` self-test (it was
deleting a user WORK folder) and the Runner DEM-resampling/UTF-8 restore.

**Round 2 — medium/low.** DWR: Hazard `M1,M2,M4,M14`; Runner
`M6,M7,M8,M9,M11…M16,L1`; Screening `M17…M20`; Data `M21,M22,M23,L3,P1`; Setup
`M24,M4`. Also the Data pid-lock regression (a lock must not steal a *live* same-process
lock) and the `damsuite/FILES.json` hash refresh for `pack_convert.py`.

**Round 3 — re-audit of rounds 1–2, caught regression #1.** The H6 closed-edges fix from
round 1 was **dead code** — `workflow.py` never passed the edges to the fine FV1 run. Fixed
and wired. Also landed the M3 parcel-site habitability model, M2 incomplete-flag surfacing,
the Hazard Classify busy-guard, Screening `STORAGE_CHOICE` wiring + self-test temp isolation,
and the Setup status-dock release on unload.

**Round 4 — re-audit of round 3, caught regression #2.** The round-3 M3 parcel-site model
could still **under-count a dwelling** where the family-filter and the `fema_parcel` flag
disagreed. Fixed to key strictly on `fema_parcel`, with a regression test. Also fixed the
real 0.17.23 Screening bug where the results-tab summary wasn't the last log line.

**Round 5 — final parity audit + polish.** Confirmed the both-repos parity fixes are
code-consistent and closed four items: `ui_check_panel.py` Qt6 enum safety (would crash on a
PyQt6/QGIS 4 build), the Runner `write_final` gate so probabilistic runs honour the
per-run-rasters-off default (parity with nd), a stale datum test (`== "DEM"` →
`== "DEM estimate"`) left over from the M7 change, Screening Qt6 dialog enums + a dead
`if False` ternary removed, and nd Runner UTF-8 when reading `run_report.json`.

---

## 5. GUI review and port

**Reviewed** the recent DamSafety display work:

- **`DamSafety_Hazard` 2.18.32 — severity-ranked Check-inputs panel (`ui_check_panel.py`).**
  Strong. ERROR → red banner + disabled primary button with an explanatory tooltip; WARNING →
  collapsible amber rows that don't block the run; OK → one collapsed "N passed" line. The
  panel only renders; the dialog still logs every result. One defect found and fixed: it used
  unscoped Qt enums (`Qt.ToolButtonTextOnly`, `Qt.AlignTop`) and would crash on PyQt6.
- **`DamSafety_Screening` 0.17.23 — window shell** (Run/Review/Results tabs, pipeline strip,
  workspace row, settings behind the gear, folded log). Good. Two defects: an unscoped
  `QListWidget.MultiSelection` enum (Qt6 crash) and a dead `note if False else` ternary —
  both fixed.

**Ported into nd_** (preserving every nd_-only feature — the Priority tab, two-case flow,
resume, and progress bar):

1. The **Check-inputs panel** into `ND_Dam_Hazard_Helper` (commit `d95ef1d`), Qt6-safe from
   the start.
2. The **display shell** — pipeline strip, workspace header group, folded log — into
   `nd_dam_screening` (commit `d750412`), as an **additive** change.

New GUI tests accompany both sides: `DamSafety_Screening/tests/test_gui_qt6_enums.py` and
`nd_dam_screening/tests/test_ui_shell.py`.

**Deliberately deferred** (higher risk, needs a design call): the gear-Settings surface and
tab-collapse behaviour were not ported — they touch more of the dialog's state machine than a
safe additive port allows.

---

## 6. Verification at close

- DWR `test_v320` → **PASS** (exit 0), including the corrected datum assertion.
- DWR `ui_check_panel` → **PASS** on `python3.12` offscreen (real PyQt), confirming the Qt6
  enum fix.
- nd Runner UTF-8 `run_report.json` read → **PASS**.
- Both repos: working tree clean, branch == `origin/claude/funny-goldberg-dg1x2s`.

---

## 7. Deferred ledger — not done without a decision from you

These were found but **not** changed, because each is either a behaviour change in a
safety-critical tool or a pre-existing condition outside this campaign's "fix the audit
findings" scope. None is a blocker for the work above.

| Item | Why deferred |
|---|---|
| **M5 — port the built-in LISFLOOD engine into DWR** | A silent flood-engine substitution in a dam-safety tool is a behaviour change with real-world consequence; it needs an explicit go-ahead and a validation run against known baselines, not a quiet port. |
| **Perf — `read_results` triple-parses the `.mass` file; `move_xy` double-reads** | Real but non-correctness; a cleanup, not a fix. |
| **3DEP `EPSG:5070` fallback robustness** | The auto-DEM-refill fallback path could be hardened; needs a runtime-network test to validate, which the sandbox can't do. |
| **Pre-existing `test_popup` failures (lines 383/390)** | Introduced by main's 0.17.23 rework (menu/unload assertions), not by this campaign. Lines 327/334 were fixed as a side-effect of the dialog reorder; 383/390 remain and are flagged, not touched. |
| **Stale DWR tests to sync to nd_** | `test_nd_hazard.py:225` / `test_nd_hazard_adapter.py:189` (the "one name is enough" assertions) and the single-window popup assertions are stale against the current shell; a mechanical sync, deferred to avoid masking a real drift mid-campaign. |

My recommendation: **M5 should not be ported without a baseline-validated run** — of
everything on this list it is the only one with safety consequences, and the right way to do
it is a separate, explicitly-scoped task.

---

## 8. Final state

| | DWR_Plugins | nd_hazard |
|---|---|---|
| Branch | `claude/funny-goldberg-dg1x2s` | `claude/funny-goldberg-dg1x2s` |
| HEAD | `f983244` | `0e2484e` |
| Remote | in sync | in sync |
| Working tree | clean | clean |

All five rounds, the GUI review, and the GUI port are complete and pushed. The deferred
ledger (§7) is the remaining backlog and is yours to prioritise.

---

## 9. Follow-up (post-merge) — 2026-10-19

The §7 deferred ledger has since been **cleared**, grok's `origin/main` was merged in, and
three further fixes landed. Current branch HEADs: DWR `claude/funny-goldberg-dg1x2s` (merged),
nd_hazard same.

**§7 deferred items — now done:**

- **M5 built-in engine** — ported nd's numpy local-inertial engine into the Runner, but
  **strictly opt-in** (`'builtin'` keyword or the `NDBREACH_USE_BUILTIN` provider flag; the
  default still errors, so the approximation is never silently substituted for LISFLOOD-FP),
  **labeled on every surface** (report / summary / docx / manifest / log / rasters-tier), with
  the near-dam FV1 refine skipped-and-flagged and the limiter clip-volume surfaced. Three
  adversarial safety reviewers all required this form; a real-LISFLOOD-FP numeric parity run
  remains an **off-sandbox release gate** (see §10 P0).
- **Perf** — `read_results` now parses `.mass` once (was 3×); `move_xy` reads the breach-point
  GeoPackage once per UI move (both repos).
- **3DEP** — `fetch_3dep` hardened: bounded transport retry, pre-write TIFF-signature + size
  validation, atomic `.part`+`os.replace` (no junk tile on a bad/short answer).
- **test_popup** — fixed a real missing `home_action` in `DamSafety_Screening/plugin.py` and
  synced the stale menu/suite-home assertions; file green end-to-end.
- **Stale signing tests** — `test_nd_hazard.py` / `test_nd_hazard_adapter.py` brought in line
  with the one-signing-engineer model (verified against the code, not masking drift).

**Merged `origin/main` (grok's DamSafety updates):** +95 commits / 151 files (Runner 3.10.21,
Screening 0.17.25, Hazard 2.18.37, shared `ui_widgets`/`ui_fit`, the Setup wizard, the HEC-RAS
2D rework, reservoir-carve, gmsh mesh). 141 files auto-merged; 10 conflicts resolved
(grok's #77 `gpkg_write`, #78 county boxes and the `hecras_model` rework supersede the matching
campaign fixes, whose intent they preserve; the M5 labeling + perf fixes were combined in). An
audit of grok's new code (ui_widgets, reservoir-carve, gmsh gate, HEC-RAS engine, Setup wizard,
gpkg_write/handoff), refute-by-default, found **no correctness issues**.

**Three further fixes:**

- **Velocity peak sampled every step** in `builtin_lisflood.py` (was every 5th; it feeds the
  velocity-driven High zone of the ACER TM-11 chart — a silent low bias). Both repos.
- **Canonical `damsuite` master + drift gate** — `shared/damsuite/` is now the single source of
  truth; `tools/sync_damsuite.py` writes the vendored copies and regenerates `FILES.json`, and
  `--check` (with `tools/test_sync_damsuite.py`) fails the build on any drift. Ends the
  hand-regeneration of `FILES.json`. (DamSafety_Setup's trimmed mini-`damsuite` is intentionally
  excluded.)
- **Golden-baseline regression** for the deterministic built-in engine
  (`tests/test_golden_baseline.py` + `tests/data/golden/builtin_engine.json`, regen via
  `tools/regen_golden_builtin.py`) — freezes the regulatory-facing flood scalars so a
  coefficient / units / sign change can no longer pass the qualitative-only tests.

---

## 10. Roadmap — 33 recommendations (six-dimension review of the merged suite)

Prioritised P0 (do first) → P3; effort S/M/L/XL. Items already done above are marked **✅**.
The encouraging finding: the suite already carries strong defensibility *machinery*
(`sensitivity.py`, `probabilistic.py`, `verification.py`, `benchmark.py`, `provenance.py`,
signed/hashed ACER TM-11 charts) — most of the top work is **binding and gating** those pieces
into the deliverable, plus validating the engines against a real binary.

### Safety & regulatory defensibility
| Pri | Eff | Recommendation |
|---|---|---|
| P0 | L | **Documented model-acceptance gate** against a pinned real patched LISFLOOD-FP build (+ its SHA): run the analytic suite, a built-in-vs-LISFLOOD-FP parity case and a real HEC-RAS benchmark with numeric thresholds, captured as a signed Validation Record. The engines (esp. the M5 built-in) have **never** been run against the reference. |
| P0 | M | **One signed, hash-bound Classification Record** binding hazard class ⊕ engine+binary SHA ⊕ input hashes ⊕ chart/rate-table signer ⊕ manifest digest, with a `verify` that refuses on any mismatch / stale input / built-in-engine. Pieces exist but are separate artifacts. |
| P1 | S | **✅ Fix peak-velocity under-sampling** in the built-in engine (done above). |
| P1 | M | **Propagate the screening-grade flag** (+ mass-error / clip-volume) onto the `Classification` itself — today log-only, so the deliverable carries no trace an unvalidated engine produced it. |
| P1 | M | **Harden ACER TM-11 digitization**: independent human check (shipped charts have digitizer == signer, `checked_by` blank), archived raw source + reproducible trace script, verification against the original Reclamation (1988) figures, and a digitization-sensitivity flag on borderline structures. |

### Correctness & robustness (grok's new engines)
| Pri | Eff | Recommendation |
|---|---|---|
| P1 | M | **HEC-RAS domain truncation**: the 2D area has one "DS Normal Depth" outflow arc; every other perimeter cell is a closed wall. Compute and surface a boundary-wetting metric so wall-ponding / silent extent truncation is detected. |
| P1 | M | **HEC-RAS run.log gating**: parse RasUnsteady volume accounting + max-cell WS error and reject on them, not just on the "Finished Unsteady Flow Simulation" string. |
| P1 | M | **End-to-end golden test for the new engines** against a real HEC-RAS HDF (the Muncie example is already manually proven). |
| P2 | M | **HEC-RAS normal-depth outflow**: validate and record the placement + energy slope (currently a single hard-clamped scalar). |
| P2 | S | **gmsh mesher**: add an area-coverage / watertightness check to the quality gate (today only min-angle / min-area). |
| P2 | M | **reservoir-carve**: require a dam-line wall or lake clip before an unbounded carve; gate on carve quality. |

### Architecture & maintainability
| Pri | Eff | Recommendation |
|---|---|---|
| P0 | M | **✅ Canonical `damsuite` master + sync tool + drift gate** (done above). |
| P0 | L | **Converge the DWR ↔ nd_ forks, starting with the safety core** — `chain.py` (model→class fingerprint), `solvegate.py` (pre-solve gate), `versions.py` differ, so a dam classified under one fork isn't comparable to the other. Pick one product + a thin branding skin, or extract `damsuite` to its own pinned-tag repo both vendor from. |
| P1 | S | **Wire `damsuite.selfcheck()` into plugin startup / the Setup health dock** — it has no non-test caller today. |
| P1 | M | **Make the Screening→Runner handoff contract Runner-owned and runtime-checked** (today `handoff.py` mirrors the Runner's param/enum assumptions by hand → silent drift, e.g. a 20 m screen re-running at 10 m). |
| P1 | M | **CI as the merge gate** on py3.12 (drift-check + handoff-contract + version consistency). |
| P2 | S | **Eliminate the second `damsuite` flavor in Setup**; single-source the version table. |

### Testing & CI
| Pri | Eff | Recommendation |
|---|---|---|
| P0 | M | **CI pipeline** running every plugin's fast suite on each PR (no CI exists today). |
| P0 | M | **✅ Golden-baseline regression for flood numbers** (done above for the engine; extend to derived class/LoL next). |
| P1 | L | **Periodic real-engine parity job** (built-in vs patched LISFLOOD-FP, + ANUGA/HEC-RAS) — scheduled, not per-PR. |
| P1 | S | **Pin numpy/scipy/QGIS/GDAL** and make the scipy gate fail loudly (today it silently disables the Runner suite under numpy 2.5.3). |
| P1 | M | **Unify the package import name** (`nd_breach_runner` vs the `DamSafety_Runner` dir) and wire the built-in engine into the automated fast suite. |
| P2 | L | **Headless QGIS-container job** for the real-Qt shells and the real GDAL self-test. |

### UX & GUI
| Pri | Eff | Recommendation |
|---|---|---|
| P0 | M | **Wire the shared `CheckPanel` into Runner & Screening pre-flight** — it exists in `ui_widgets.py` but only Hazard uses it; the two tools that launch multi-hour runs still gate with one-shot modal warnings. |
| P1 | M | **Show (and let the user choose) the actual engine + output units** on the Runner/Screening workspace (now that there are 4 engines incl. the HEC-RAS tier and the built-in). |
| P1 | M | **Converge the forked `ui_fit.py`** and extend window/state persistence to every dialog. |
| P1 | M | **Make the Setup wizard / health reflect the 4-engine reality** (report the built-in as always-available screening-only; the real runtime constraints). |
| P2 | M | **Give Screening a shared "which-step-am-I" strip** (`StageStrip`) and align error surfacing across the suite. |

### Features
| Pri | Eff | Recommendation |
|---|---|---|
| P0 | L | **Observed-event / reference calibration loop** — compose `benchmark.py` + `sensitivity.py` into a workflow that demonstrates the model reproduces a known event (the strongest defensibility argument a submittal can carry). |
| P0 | M | **Crash-safe portfolio batch** — append per-dam results + a resumable queue + a portfolio roll-up ("which dams are High / which moved"); today the summary is all-or-nothing. |
| P1 | S | **Stamp input-data vintage** (NSI / 911 / AADT versions) onto the face of every LoL / EAP / hazard output. |
| P1 | L | **Finish HEC-RAS 2D to production** — chain self-test, headless TCU, golden regression, batch tier (it self-labels "preliminary"). |
| P2 | M | **Promote the uncertainty envelope** (`probabilistic.py` / `sensitivity.py` P10/P50/P90) to a first-class, EAP-ready output. |

---

## 11. Fork convergence — slice 1 done (safety core)

Started the P0 **DWR ↔ nd_ fork convergence** with its safe first slice: the safety core.

- **Key finding (reassuring):** the `chain.py` functions that decide classification
  comparability — `_canonical`, `digest_of`, `manifest_ok`, `compare_rasters`,
  `find_runner_manifest`, `chain_summary`, `_norm_role`, `_entries`, `_where` — were **already
  byte-identical across both forks**. So the forks already fingerprint and classify comparably;
  the only `chain.py` divergence was DamSafety's transparent persistent **disk cache** (perf,
  not safety).
- **Reconciled to byte-identical across forks:** `damsuite/chain.py` and `damsuite/solvegate.py`.
  nd adopted DamSafety's superset `chain.py` (adds the disk cache) plus the one-line **FP.1
  guard** in nd's `fingerprint.py` (`use_disk_cache=False` on evaluation keys — preserves nd's
  fresh-hash behavior; required so a raster rewritten in place is still seen as changed).
  `solvegate.py` picked up DamSafety's UTF-8/encoding hardening. nd's four `FILES.json`
  regenerated; all four nd `damsuite.selfcheck()` clean.
- **Intentionally left fork-specific:** `damsuite/versions.py` is branding + per-fork release
  numbers (`nd_*` folder names and the 2.3x/3.2x line vs `DamSafety_*` and the reset line);
  forcing it identical would erase the brands. Its shared *logic* (`version_of`, the compat
  matrix) can converge later; its data must not.
- **New gate:** `tools/check_fork_parity.py` asserts `chain.py` + `solvegate.py` are identical
  across the forks (and that each fork's vendored copies agree). It runs where both repos are
  checked out (a combined CI job or locally) and is wired into `tools/ci_checks.py` as a step
  that skips gracefully when the nd checkout is absent. Within-fork drift stays covered by
  `tools/sync_damsuite.py`.

**Remaining convergence work** (larger slices, still P0-adjacent): a true single-source shared
`damsuite` both forks vendor from a pinned tag (so the cross-fork gate runs in normal CI, not
only a combined checkout); converging the coupled non-core files that drifted (e.g. the newer
`fingerprint.py` nested-VRT handling); and deciding the end-state (one product + a branding skin,
vs. an extracted shared package).

## 12. Fork convergence — slice 2 done (shared pin + coupled-file convergence)

Delivered the next slice: a single shared `damsuite` source both forks vendor from a **pinned
tag**, so the parity gate runs in **normal per-repo CI** (no combined checkout, no network), plus
convergence of the genuinely-drifted coupled files. Opened as PRs in both repos for review — not
merged. The end-state decision (one product + a branding skin vs. an extracted shared package) is
**still yours**; nothing here forecloses it.

- **The pin (`tools/damsuite_pin.json`, committed byte-identical in both repos).** A content
  fingerprint of the canonical master (`DWR_Plugins/shared/damsuite/`, 35 modules): `sha256` of
  every module, a `fork_specific` list, a tag label `damsuite-pin-v1`, and a `pin_sha256` over the
  content-checked modules. Because it is committed into both repos, each repo's CI verifies its own
  vendored copies against it **offline** — the explicit "normal CI, not only a combined checkout"
  requirement. It keys on **content, not `SUITE_VERSION`** (the forks run inverted version lines —
  nd 1.18.0 vs DamSafety 1.17.3 — so a version key would be meaningless).
- **Drift was far smaller than feared.** Of the 27 modules both forks ship, **23 were already
  byte-identical**; only `__init__.py`, `versions.py`, `suite_home.py` and `offline_pack.py`
  differed.
- **Converged (nd adopted DamSafety's newer version), as separate commits:**
  - `fingerprint.py` (plugin-level) — the nested-VRT handling: follows nested VRTs with cycle
    detection, treats a `<SourceFilename>` with no `relativeToVRT` as relative (GDAL's default —
    nd's old regex silently dropped those sources), unescapes HTML entities, and marks a truncated
    source list incomplete (FP.2). Depends only on `chain` (already identical) + stdlib, so the
    swap is self-contained; now byte-identical across forks. A new QGIS-free regression test
    (`tests/test_fingerprint_vrt.py`, added to **both** forks) locks the behaviour in (fail-before
    on the old code, pass-after).
  - `damsuite/offline_pack.py` — `encoding="utf-8"` on two `.write_test` probes (the campaign's
    UTF-8 hardening); converged across all four nd copies, each `FILES.json` refreshed. Now
    byte-identical across forks.
- **Documented, NOT converged (surfaced for the end-state decision):**
  - `fork_specific` (present in both forks, content legitimately per-fork): `__init__.py` (module
    manifest + `SUITE_VERSION`), `versions.py` (branding + releases), and `suite_home.py` —
    DamSafety couples it to the DamSafety-only `structure_flags` module, so nd ships the same file
    minus that feature until the `structure_flags` port is decided. The pin does not compare their
    content across forks (each fork's own CI still keeps its copies internally consistent).
  - `ND_NOT_VENDORED` — the 8 DamSafety-only modules nd does not carry (`health`, `layout`,
    `pack_convert`, `record`, `servers`, `structure_flags`, `suite_config`, `suite_fill`). Listed
    explicitly so CI does not flag them as missing while the gap stays visible.
- **Tooling / gates.** DamSafety's `tools/sync_damsuite.py` gained `--write-pin` and a
  `pin == master` check folded into `--check`; `tools/ci_checks.py` runs the pin check (and now
  covers `DamSafety_PMP`). nd gained a `tools/` dir it never had: a pin-based `sync_damsuite.py`
  (`--check` against the pin; `--from <DamSafety>/shared/damsuite` to re-vendor) and a QGIS-free
  `tools/ci_checks.py`, plus its **first `.github/workflows/ci.yml`**. Both gates were shown to
  fail on injected drift and pass clean.
- **The tag.** `tools/tag_damsuite_pin.sh` cuts the annotated `damsuite-pin-v1` tag at the pinned
  commit (and only creates it locally unless `--push` is given). Cut it right after the pin PR
  merges so it names a durable commit; bump the label (`--write-pin --pin damsuite-pin-v2`) on the
  next content change.

**Residual, honest limits.** Keeping the two repos' `tools/damsuite_pin.json` identical is a
release-time step (or the combined-checkout `check_fork_parity.py` job can assert it) — no single
repo's CI can prove the *other* repo's pin matches without reaching across repos. A true single
source (the extracted-package end-state) would remove that bridge entirely; that is the decision
left to you.

**Verification (adversarial, Oct 2026).** A 10-agent review of this slice refuted 13 of 15 raised
findings; the 2 survivors are now fixed: nd's `--check` validates its own `pin_sha256` (catches a
corrupt/hand-edited pin), and `check_fork_parity.py` now gates `fingerprint.py` cross-fork identity.

## 13. Recommendations round — dead code, de-dup, split assessment (2026-10-19)

Acting on five recommendations: (1) dead-code sweep, (2) continue de-duplication, (3) split
`pipeline.py`/`structures.py` only with real-QGIS coverage, (4) leave damsuite vendoring alone, and
(5) no byte minification. Backstop in this environment: `py_compile` of every source, `pyflakes`
(0 undefined names), and the QGIS-free suites — the real-QGIS suites cannot run here.

### 13.1 Dead-code sweep (pyflakes + vulture) — done
- **Removed 122 unused imports** across both forks (autoflake, name-aware), preserving the `# noqa`
  availability probes, the `__init__` re-exports and partial imports; `damsuite/` was left untouched
  (rec 4). Count matched an independently-derived safe list exactly.
- **Removed a latent bug:** `DamSafety_Runner/core/workflow.py` called an undefined `feedback` inside
  a swallowed `try/except`, so the push never ran (the warning is already in the `warnings` list;
  nd never had the block). Removing it is no-behaviour-change and converges with nd.
- **vulture** at high confidence surfaced only unused *parameters* (part of signatures) — not removed.
- **Flagged, not changed (needs your eyes):** `DamSafety_Runner/core/cascade.py` sets a local
  `failed = True` that is never read; the `CascadeResult.failed` field may never be propagated — a
  possible missing-failure-status bug in the cascade path.

### 13.2 De-duplication — the safe, verified subset applied; the rest a prioritized backlog
A de-dup discovery pass (5 subsystem finders + adversarial equivalence verification) found 7
provably-equivalent "unify now" cases and 31 "report only". **Applied** (behaviour-preserving,
verified): the unit-conversion factors in `lisflood.py` read_results → `units.FT/ACRE/CFS/ACFT`
(both forks; golden baseline reproduces identical numbers), the three `0.3048` ft↔m literals in
`conditioning.py` → `units.FT` (DWR only — nd lacks that module), and a `crest_rule.py`
byte-identical-across-plugins CI assertion.

**Backlog (not applied here), highest value first:**
- **Structure-outcome decision tokens** (`OUT_PROBABLE`/`OUT_JUDGMENT`/`OUT_ECONOMIC`/`OUT_DRY`):
  produced once by `structures.structure_outcome` but re-declared/literal in ~11 Hazard modules
  (robustness, ensemble, rulecompare, portfolio, watch, traveltime, history, quickcreep, formfill).
  They drive High/Medium/Low, so a drift is a real misclassification risk. Provably literal-equal
  today → define in `constants.py` (QGIS-free) and re-export. **Needs the real-QGIS classify/
  ensemble suite to validate**, so deferred to an environment that can run it. Caveats for whoever
  does it: keep the `_DRY` membership tuples' extra members (`""`, `None`, quickcreep's `"NULL"`);
  `ensemble` also inlines `"Not inundated"`.
- **Same-file helper extractions** (low risk, low drift-danger): `dem_download.py` `to_5070`/
  `_box_2266` → one `_box_in(epsg,…)`; `gdal_io.py` the two north-up-bounds expressions → a
  `bounds_from_gt` helper (NOT the *windowed* one in `rasimport.py`); the duplicate `ALBERS =
  "EPSG:5070"` in Screening `_common.py` vs `terrain_cache.py`.
- **Runner↔Screening breach-formula family** (Froehlich 1995/2008, Von Thun–Gillette, MLM/L-M,
  TR-60, weir coefficients): parallel implementations. Candidates for a shared breach module — but
  **Screening's are deliberately simplified**, so this is a human design decision, not an auto-unify.
- **Do NOT unify (deliberate differences):** `G = 9.80665` (Runner) vs `G = 9.81` (Screening); the
  TM-11 danger-zone logic (canonical in `damsuite/tm11_curves.py`, off-limits); the hazard-class
  severity tiers (canonical in `damsuite/priority.py`).
- **Unit-constant sprawl in Screening** (~15 files restate `AF_M3`/`FT`/`ACRE`): candidate for one
  Screening-wide units module (or vendoring `damsuite/units.py`), left for the end-state decision.

### 13.3 Split `pipeline.py` / `structures.py` — assessed, correctly deferred (rec 3)
Both import `qgis.core` at module scope, and their coverage (`test_lumo_real*`,
`test_m1m2_classify_nd`, `test_flagged_structures`, …; 19 of 27 Hazard test files import qgis)
runs **only with real QGIS**, which this environment lacks. Per rec 3, **not split here**. Proposed
themed split (the files already carry `# --- inputs --` / `# --- scenarios --` section markers):
pull the product-reuse / evaluation-key machinery (`_layer_sig`…`_evaluate`), the input collection
(`_collect_inputs`…`_collect_facilities`), and the scenario runner (`_run_scenario`…`_result_items`)
into themed submodules, leaving `pipeline.py` as the `Run` orchestrator. Execute it on a branch
where the `lumo_real` suite can confirm before/after parity.

### 13.4 Recs 4 & 5
Rec 4 (leave damsuite vendoring alone): honored — the pin slice is identical-copy-plus-assertion
with no build step, and neither the dead-code nor de-dup work touched a vendored `damsuite/` file.
Rec 5 (no minification): not done, by design.
