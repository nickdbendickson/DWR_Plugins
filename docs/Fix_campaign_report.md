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
