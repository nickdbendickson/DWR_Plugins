# Batch-chain correctness review (2026-10-24)

Adversarial static review of the batch-chain orchestration merged in PRs #156/#157/#158
(`core/chain.py`, `core/hecras_windows.py`, `algorithms_chain.py`, `chain_dialog.py`), on
`main` at `2b45b53`. Each finding was confirmed by a second pass that traced it in the actual
code. **Nothing has been changed** — this is a report. CI exercises none of these paths
(HEC-RAS, Wine, the HEC-RAS Controller COM, and Windows are all mocked), which is why they
survived to merge.

18 confirmed: **5 high, 10 medium, 3 low.**

## Systemic theme: "cannot verify results" is treated as "has results"
Findings 3, 5, 6, 8, 11, 14 share one root cause. `plan_has_results()` returns `None` when
`h5py` can't be imported (the module itself anticipates this), and every acceptance test uses
`check(path) is not False`, so `None` (unverifiable) is accepted. On a HEC-RAS/QGIS box without
`h5py`, a plan HDF that is **partial, geometry-only, or stale** passes as a finished compute and
gets handed to Hazard — a wrong/empty classification silently attributed to a dam. Fixing the
acceptance tests to require `is True` (treat unverifiable as not-finished → the dam waits for a
verifiable result) neutralizes most of this cluster at once.

## High

1. **`chain.py:698` `run_batch` — a write failure aborts the whole batch.** The per-dam
   `write_summary` (698) and final `save_json`/`write_summary` (718/719) aren't in the per-dam
   try/except (690-696). `os.replace` raises PermissionError (Windows: Summary.csv open in Excel
   to watch progress) or OSError (disk full) → unwinds the dam loop; every later dam is never
   run and never even gets a "failed" row. **Fix:** guard those writes (log + continue).

2. **`hecras_windows.py:256` `run_com` — COM compute has no timeout.** `Compute_CurrentPlan(..., True)`
   blocks with no watchdog; `NDBREACH_HECRAS_TIMEOUT_S` is honored only on the headless path. When
   headless times out and falls back to COM and COM hangs (modal dialog, stuck license), the batch
   stalls on that dam forever. **Fix:** run the COM compute under a timeout watchdog that force-quits
   RAS and raises, so the chain falls to the manual-wait path.

3. **`hecras_windows.py:181` `_has_results` — accepts results-less plan HDF.** `check(path) is not
   False` → a geometry-only `.p01.hdf` (preprocess wrote it, compute aborted) is accepted when
   h5py is absent. **Fix:** `return check(path) is True`.

4. **`algorithms_chain.py:438` `collect_dams` — dams silently dropped.** Features with blank/NULL
   ID, or a duplicate ID, are discarded with no warning and no "failed" row; they never appear in
   the summary/manifest even as requested. A dam the operator selected is never modeled and nothing
   says so. **Fix:** track and report blank/duplicate IDs like the existing `missing` handling.

5. **`hecras_windows.py:227` `run_headless` — promotes a partial RasUnsteady output.** Renames
   `.tmp.hdf → .p01.hdf` based only on "out lacks results," ignoring RasUnsteady's exit code `rc2`;
   a crashed/partial run's tmp becomes the canonical results file and (h5py absent) is returned as
   success. **Fix:** only promote when `rc2 == 0` and the tmp verifies; delete the partial otherwise;
   never leave a partial at the canonical name.

## Medium
6. **`chain.py:420` resume — unverifiable/growing plan accepted.** `find_finished_plan`/`wait_for_plan`
   accept `has_results is not False`, and `wait_for_plan` returns on first sight when `minutes<=0`,
   bypassing its size-stability guard → a still-being-written manual HDF is read truncated. **Fix:**
   treat `None` as "must prove stability" (require two equal-size polls) even when `minutes<=0`.
7. **`chain.py:212` `key_of_column` — override column silently dropped.** 3-part keys aren't
   case-normalized for stage tools, so `ndbreach.batch.method` ≠ canonical `...METHOD`; the override
   is dropped and preflight shows no error. **Fix:** normalize to `(p.lower(), t.lower(), k.upper())`
   and/or validate unknown keys in preflight.
8. **`hecras_windows.py:227` — partial promotion persists for resume.** Same rename-ignores-`rc2`
   defect as #5, seen from the resume angle: the partial final-named HDF is later accepted by
   `find_finished_plan` as "manual"-finished. **Fix:** gate on `rc2==0` + positive results; remove
   partial.
9. **`hecras_windows.py:186` `_run` — timeout kills only the direct child.** `subprocess.run(timeout=)`
   kills `Ras.exe` but not its spawned compute engine; the orphan keeps writing the HDF and races the
   COM fallback / later polls. **Fix:** launch in a new process group / Job object and kill the whole
   tree on timeout.
10. **`hecras_windows.py:104` `timeout_s` — malformed value silently means "no limit."** A typo like
    `3600s`/`1h` → `float()` ValueError → `None` → no timeout. **Fix:** distinguish unset from invalid;
    warn/fail on invalid.
11. **`hecras_windows.py:207` `run_headless` — returns a stale pre-existing results HDF.** After a
    failed `Ras.exe -c` that wrote nothing, an older `.p01.hdf` passes `_has_results` and is returned
    as fresh. **Fix:** capture out's mtime before launch; accept only if newer (thread the caller's
    `since` through).
12. **`algorithms_chain.py:545` preflight — per-dam HEC_MODE overrides not validated.** "Check" uses
    the global mode only; a dam whose override needs an uninstalled stage passes Check, then fails at
    runtime. **Fix:** recompute needed stages per overridden dam in preflight.
13. **`chain_dialog.py:330` `dam_ids` — Check preview counts a 'NULL' dam.** Dialog guards only
    `v is None`, so a QVariant NULL stringifies to 'NULL' and is previewed; the run drops it → "5 dams"
    at Check becomes 4 processed. **Fix:** use the algorithm's `_id_text` normalizer.
14. **`hecras_windows.py:207` + `chain.py:420` — stale/partial result attribution (cluster).** The
    combination of the unverified-accepted rule and the no-freshness-gate short-circuit; same fixes as
    #3/#6/#11.
15. **`algorithms_chain.py:567` — two dam IDs collapse to one folder → cross-attribution.**
    `safe_name()` maps `Smith Dam #1` and `Smith Dam_1` to the same folder; with SKIP_DONE the second
    dam finds the first's stages "done" (matching fingerprint) and is reported with the first dam's
    hazard class. **Fix:** detect safe_name collisions (disambiguate or preflight ERROR); defense: in
    `run_dam`, drop loaded state whose stored dam id ≠ current.

## Low
16. **`hecras_windows.py:100` `methods` — unrecognized `NDBREACH_HECRAS_WIN_METHODS` silently reverts
    to default pair** (e.g. `comm` typo). **Fix:** warn when a set value yields no recognized token.
17. **`chain.py:249` `read_overrides` — override cells coerced by `_from_text`** turn `00123`→int 123
    (leading zeros lost) and `1.0`→float. **Fix:** keep the raw string; let each setting coerce by its
    kind.
18. **`algorithms_chain.py:564` — overrides matched by exact string, not canonical ID.** CSV `123.0`
    vs feature `_id_text`→`123` → override silently not applied (only a WARNING). **Fix:** canonicalize
    IDs on both sides; consider promoting the preflight WARNING to ERROR.

## Suggested fix order
- **First PR (highs + the h5py cluster):** #1, #3+#5+#6+#8+#11+#14 (the `is True` / freshness /
  `rc2` fixes), #2, #4, #15. These are the "wrong result attributed to a dam / dam silently skipped /
  batch aborts" class — the ones that matter for a safety tool. Add regression tests (the launcher is
  mockable the same way the existing `test_hecras_windows.py` mocks it).
- **Second PR (overrides + preflight robustness):** #7, #10, #12, #13, #16, #17, #18.
- #9 (process-tree kill) is Windows-specific and best verified on Nick's Mac+VMware HEC-RAS box.
