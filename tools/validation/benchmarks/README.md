# Boann published-benchmark gate

A small, self-contained regression gate for the Boann flood engine. It runs Boann's **CPU build**
on a handful of committed benchmark decks and checks the peak-depth field against a committed
reference. No network, no LISFLOOD-FP build, no GPU; the whole suite runs in a few minutes on one
core.

```
python3 tools/validation/benchmarks/benchmark_gate.py            # run every case, exit 1 on failure
python3 tools/validation/benchmarks/benchmark_gate.py --case ea3 # one case
python3 tools/validation/benchmarks/test_benchmark_gate.py       # self-test of the scoring logic
```

## Why

This guards the regression class that the 2026-11-24 **NoData wall bug** belonged to (PR #516,
Boann 6.0.0): a shock solver driving water into a NoData cell without limit, which a published
benchmark (EA test 5 at 10 m) exposed only after a merge. The two lake-at-rest cases catch that
class directly, and the dam-break / valley cases catch drift against LISFLOOD-FP and against a
frozen Boann reference. It also backs the "no existing result moves" claim on speed PRs.

## Cases

| case | solver(s) | what it checks | reference |
|---|---|---|---|
| `ea3` | FV1 | EA 2D benchmark test 3, dam break over a floodplain (5 m) | LISFLOOD-FP 8.3 FV1 on the same deck |
| `ea5_valley` | ACC | EA 2D benchmark test 5, valley flooding (50 m) | LISFLOOD-FP 8.3 ACC on the same deck |
| `humps_dambreak` | FV1 | three humps, dam break (LISFLOOD-FP test T03) | Boann 6.0.0 FV1, frozen (drift self-reference) |
| `humps_still` | ACC, FV1, DG2 | three humps, lake at rest (test T01): still water stays still | the deck's start surface |
| `nodata_lake` | FV1, FV2, DG2 | a lake at rest beside NoData must stay still (direct NoData-wall guard) | the deck's start surface |

Each case folder has `deck/` (the LISFLOOD-FP-style input files), `reference.max` (except the
lake-at-rest cases, which compare to their own start surface), and `case.json` (solvers,
tolerances, wet threshold, volume-error cap, and a note).

## Reference provenance

- **EA 2D benchmark decks** (tests 3 and 5): Neelz & Pender, *Benchmarking the latest generation of
  2D hydraulic modelling packages*, Environment Agency SC120002; input datasets on Zenodo
  `15256842`. The `reference.max` grids are LISFLOOD-FP 8.3 (Zenodo `13121119`) run on the same
  deck in the cloud, ACC on the CPU and FV1 on the CPU.
- **Three humps** (tests T01, T03): the cases shipped in the LISFLOOD-FP 8.3 distribution
  (`tests/data`). T03's reference is Boann's own FV1 output frozen on this deck, because the
  dam break over three hills is wave-dominated and any two codes differ by ~0.2 m in peak timing
  at a few cells; external agreement with LISFLOOD-FP is checked in the Mac/cloud kit instead.
- **nodata_lake**: a synthetic 20x20 deck (a 1 m lake with a NoData column and block), the same
  fixture as the `metalflood_shock_solvers_hold_a_lake_against_nodata` unit test.

## Notes

- GPU and CPU builds are **not** bit-identical (SIMD reduction order); this gate runs the CPU
  build and uses tolerances, like the engine's own tests.
- The full published suite (Ritter analytic, EA4, EA5 at 10 m, Carlisle, and the LISFLOOD-FP
  head-to-head) lives in the Mac/cloud kit at `/mnt/project-files/lisflood-benchmarks-2026-11-24/`
  and the report Doc; this gate is the subset small and fast enough to run on every PR.
