# Crest accuracy envelope (trust map)

**Scored 2026-11-05 on `main` 8bc4d20** (PR #75: the routed spread's pools settle into one sheet, `spread.ts
settlePools`; CHANGELOG 2026-11-05 "The routed flood draws as one sheet instead of a fan of wedges"), on top of #63
(demand-driven reach + exhausted-stretch cap) and #67 (spread ground beside each stretch + terrain-triggered MCT/MC
router). Layer scored: the routed volume spread (`score-routed.mjs --spread`, reach on), the layer the desk builds for
routed dams. Re-score when the spread, the reach, the router or a baseline record changes (see "Keeping this current").

Scored on trace-version 8 (`outline.ts TRACE_VERSION`; `outline.test.ts` fails with "re-score the envelope" when the
code's trace version moves past this line without the tables being re-scored).

Crest is a **screening** stand-in. The ND Breach Runner (LISFLOOD-FP 2-D) is the layer of record. This page says where
a Crest footprint is good enough to screen with and where it is not, so nobody has to guess.

## The trust map

| Terrain class | How Crest's own run tells you | Baseline dam | CSI 0.1 ft | CSI 1 ft | Confidence | Use |
|---|---|---|---|---|---|---|
| **Confined valley or closed basin** | flood ends in a basin or is contained in 0 reach rounds; no incomplete alert | DWR-3034 Wachter Ranch 11 | 0.84 | **0.77** | **High** | Screen with Crest. What it floods, floods (precision 96–97 % before the settle; re-measure the per-cell table below on the next full score). |
| **Incised valley, moderate release** | contained, no incomplete alert, not a basin | DWR-2865 Van Osting | 0.34 | **0.35** | **Medium** | Screen, but read the footprint as a *minimum*: recall about half, depth 1–2 ft shallow. A receptor just outside it is not cleared. |
| **Tiny pool, thin sheet** (release ≲ 10 ac-ft) | release in ac-ft is small; contained in 0 rounds | DWR-4031 Ward County | 0.43 | **0.14** | **Extent medium, depth low** | Use for "is anything in reach". Do not use the depth: only 8 ac of the Runner's flood is ≥ 1 ft deep, so one-foot depths are noise both ways (the settle moved this one from 0.17 to 0.14 on those 8 ac). |
| **Flat multi-channel plain, large release** (2-D sheet flow) | **incomplete-downstream alert** (reach hit its round cap with water stranded at the edge) | DWR-2801 Swan Buffalo 12 (Absaraka) | 0.33 | **0.23** | **Low** | **Run the Runner.** Crest still floods far downstream where the Runner is dry and the layer is incomplete, and the 2-D references disagree with each other too. Against all five references (before the settle): 0.19–0.25 at 1 ft. |

CSI columns are against LISFLOOD-FP, the Runner. 2801 also has ANUGA and DSS-WISE references (below).

**The one hard rule:** if the routed layer carries the *incomplete downstream* alert, the dam needs a Runner run. That
alert is the signal that separates the low-confidence class from the others on all four baselines.

**Per-dam triage uses these classes.** Crest's triage call (`src/lib/crest/triage.ts`, `CONFIDENCE_LABEL` /
`RECOMMENDATION_LABEL`) maps them to three confidence words: **High** = confined valley or closed basin, **Medium** =
open terrain (incised valley and tiny pool; read the footprint as a minimum, and on a tiny pool don't trust its
depths), **Low** = incomplete downstream. If a class name changes here, change those strings in the same PR. Design
and baseline results: project folder `crest-inundation/triage-2026-11-04/RESULTS.md`.

## Why 1 ft is the honest number

At 0.1 ft, a CSI mostly measures a thin sheet of water a few inches deep on a flat plain, and the refined 2-D models
do not agree with each other about that sheet. At 1 ft they mostly do. Scores between the 2-D references themselves on
DWR-2801, downstream half-plane only:

| Reference pair | CSI 0.1 ft | CSI 0.5 ft | CSI 1 ft | Source |
|---|---|---|---|---|
| LISFLOOD-FP (Oct 30, 30 m) vs ANUGA (30 m) | 0.46 | — | **0.58** | harness report `Crest_CSI_DWR-2801.docx`, 2026-11-03 |
| LISFLOOD-FP (baseline, 20 m) vs ANUGA | 0.49 | 0.50 | **0.51** | this page, on ANUGA's 30 m grid |
| ANUGA vs DSS-WISE (8 ft cells, unofficial) | 0.45 | 0.45 | **0.49** | this page, on ANUGA's 30 m grid |
| LISFLOOD-FP (baseline, 20 m) vs DSS-WISE | 0.34 | 0.35 | **0.43** | this page, on ANUGA's 30 m grid (on the 20 m Runner grid: 0.34 / 0.35 / 0.41) |

Agreement at 1 ft is as good or better than at 0.1 ft for every pair, never worse. Area is where they split most:
at 0.1 ft the three models wet 3,473–6,783 ac (a 2× range), at 1 ft 1,850–2,414 ac (1.3×).

So no single model is ground truth on a flat plain, and the 1 ft score is the one that is stable across references.
Quote the 1 ft CSI as the headline and the 0.1 ft CSI next to it, never the 0.1 ft one alone.

**Retire 0.35 as a floor.** The refined 2-D models of DWR-2801 agree with each other at only 0.34–0.49 at 0.1 ft. A fixed
0.35 bar would fail one accepted model against another. Judge Crest by class (table above) and, on flat plains,
against the spread between the references: Crest's 2801 score at 1 ft (0.19–0.25 across five references) is about
half of the model-to-model agreement (0.43–0.58). That gap is why the class is "Low", not the absolute number.

## Scores behind the map

All scores: `scripts/score-routed.mjs --spread --wet-ft <t>`, downstream of the breach only, cell by cell on the
reference grid. Recall = share of the reference flood Crest caught; precision = share of Crest's flood that is right;
area ratio = Crest acres / reference acres; bias = Crest − reference depth where both are wet.

### Against LISFLOOD-FP (ND Breach Runner, `baseline-2026-11-01`, 20 m)

CSI on `main` 8bc4d20 (2026-11-05, after the settle, trace-version 8; the figures the CHANGELOG entry of that day
reports), with the 34524e0 score (2026-11-04, before the settle) for the change:

| Dam | CSI 0.1 ft | CSI 1 ft | Before the settle (34524e0), 0.1 / 1 ft |
|---|---|---|---|
| DWR-3034 | 0.840 | **0.765** | 0.765 / 0.714 |
| DWR-2865 | 0.344 | **0.350** | 0.327 / 0.303 |
| DWR-4031 | 0.429 | **0.136** | 0.362 / 0.165 (1 ft: 8 ac of reference, so a few cells swing it) |
| DWR-2801 | 0.331 | **0.233** | 0.255 / 0.192 |

No dam changed class. The per-cell detail below (recall, precision, area ratio, depth bias) is the 34524e0 score; the
settle moves water, never adds it, so the area ratios and biases are expected to move little, but they have NOT been
re-measured. Re-measure them with the next full score and replace this table.

| Dam | Wet ≥ | Ref ac | Crest ac | CSI (34524e0) | Recall | Precision | Area ratio | Depth bias |
|---|---|---|---|---|---|---|---|---|
| DWR-3034 | 0.1 ft | 45 | 37 | 0.765 | 79 % | 97 % | 0.82 | −0.40 ft |
| | 1 ft | 42 | 32 | **0.714** | 74 % | 96 % | 0.77 | −0.25 ft |
| DWR-2865 | 0.1 ft | 256 | 225 | 0.327 | 46 % | 53 % | 0.88 | −0.77 ft |
| | 1 ft | 109 | 135 | **0.303** | 52 % | 42 % | 1.24 | −1.88 ft |
| DWR-4031 | 0.1 ft | 38 | 30 | 0.362 | 48 % | 60 % | 0.79 | +0.18 ft |
| | 1 ft | 8 | 12 | **0.165** | 36 % | 23 % | 1.57 | +0.62 ft |
| DWR-2801 | 0.1 ft | 6,144 | 4,802 | 0.255 | 36 % | 46 % | 0.78 | +0.42 ft |
| | 1 ft | 2,335 | 2,764 | **0.192** | 35 % | 30 % | 1.18 | +0.34 ft |

Change from 6bb9869 (before #67) to 34524e0, CSI 0.1 / 1 ft: 3034 0.701 / 0.633 → 0.765 / 0.714; 2865 0.295 / 0.311 →
0.327 / 0.303; 4031 0.354 / 0.101 → 0.362 / 0.165; 2801 0.227 / 0.175 → 0.255 / 0.192. No dam changed class.

### DWR-2801 against every reference on file

Scored on 34524e0 (before the settle); only R1 has been re-scored since (0.331 / 0.233 on 8bc4d20, table above).

| Reference | CSI 0.1 ft | CSI 1 ft | Notes |
|---|---|---|---|
| R1 LISFLOOD-FP, baseline-2026-11-01 (20 m; 6,144 ac wet ≥ 0.1 ft downstream) | 0.255 | 0.192 | the layer of record for these baselines |
| R1b LISFLOOD-FP, convergence run 10 m, 54 h, fixed domain (5,679 ac) | 0.276 | 0.224 | area ratio 0.97 / 1.17 |
| R1c LISFLOOD-FP, convergence run 5 m, 24 h clock (3,877 ac) | 0.239 | 0.209 | 24 h only, so its far tail is short; Crest 1.4× its area |
| R2 ANUGA (Oct 30, 30 m; 5,334 ac) | 0.247 | 0.187 | area ratio 1.07 / 1.47. Before #63 (56800ca, harness report): 0.15 / 0.19 |
| R3 DSS-WISE (unofficial, 8 ft cells; 3,165 ac) | 0.271 | 0.250 | recall 51–58 %, precision 33–34 %; Crest 1.6–1.7× its area |
| **Median of the five** | **0.255** | **0.209** | |

The Runner grid-convergence test (project folder `runner-convergence-2801-2026-11-03/`) found the 20 m baseline's
thin sheet is a grid artifact: 20 m cells average away the section-line road fills and ditches, so water leaks field
to field. At 10 m and 5 m the Runner's flood shrinks toward DSS-WISE. Crest scores a little better against the finer
runs, but not enough to change the class.

Two LISFLOOD-FP runs of 2801 exist and disagree: the 20 m baseline wets 6,948 ac, the Oct 30 30 m run wets 9,325 ac at
0.1 ft (+34 %, same model). That spread is itself thin-sheet extent, which is another reason to headline 1 ft.

### What the classes are reading

- **Confined / basin (3034):** the valley walls or a road fill decide the footprint; a level-pool spread gets that
  right. Stable across thresholds.
- **Incised, moderate (2865):** stable across thresholds (0.34/0.35) but recall is the weak side: Crest under-floods and
  runs 1–2 ft shallow. Its record is a stand-in (crest read from 3DEP, synthesized dam line), so its depth bias is soft.
- **Tiny pool (4031):** 6.9 ac-ft makes a 2.5 ft sheet. The extent at 0.1 ft is fair; at 1 ft there are only 8 ac to
  score, so a few cells swing the number. The TM 11 zone still agrees at 336 of 380 receptor points.
- **Flat plain (2801):** the reach fix (#63) put Crest's flood in the right place (Runner flood outside Crest's
  terrain fell from 2,334 ac to 1 ac). #67 then laid each stretch's water on the ground actually beside it, which
  thinned the flood: 58 % of Crest's wet area is ≥ 1 ft deep (was 79 %; the Runner's is 38 %), and precision at 1 ft
  rose from 26 % to 30 %. What is left is a routing-volume error, not layout: about 635 of 708 sections cannot carry the
  peak in their cut, so the wave attenuates too little and Crest floods 14–18 km downstream where the Runner is dry.
  The layer still ends incomplete (394 ac-ft stranded at the round cap).

## Limits of this envelope

- **Four dams.** One dam per class. The classes are the right shape but the numbers are not statistics; add baselines
  (another flat-plain dam and another incised valley first) before reading second decimals.
- **References are models, not observations.** No dam here has a surveyed flood. On flat plains the references
  disagree by up to ~2× in area.
- **Stand-in records** for 2865 and 3034 (crest from 3DEP at the dam point, dam line synthesized). 2801 and 4031 are
  surveyed.
- **The 2801 layer of record is a 20 m run that over-spreads** (convergence test). Scores against the 10 m and 5 m
  runs are given above; the baseline was not re-run at 10 m, so R1 is still the 20 m one.
- **Mask.** Downstream half-plane from the dam point along the record's bearing (107° for 2801). A badly wrong bearing
  on a bent outlet can drop or add cells.

## Keeping this current

Re-score all four dams at both thresholds whenever the spread, the reach, or a baseline record changes, and update the
tables here in the same PR. The guard: `outline.ts TRACE_VERSION` is bumped for every such change, and
`outline.test.ts` reads the "Scored on trace-version N" line at the top of this page and fails ("re-score the envelope")
when N is not the code's TRACE_VERSION, so the bump and the re-score land together:

```
node --experimental-strip-types scripts/score-routed.mjs --dam <record.json> --runner <dam folder or reference tif> \
  --spread --wet-ft 0.1 --json out-0.1.json
node --experimental-strip-types scripts/score-routed.mjs --dam <record.json> --runner <dam folder or reference tif> \
  --spread --wet-ft 1 --json out-1.json
```

The importer recognises a depth raster by its name: pass a lone ANUGA tif as `lisflood_hmax_ft.tif` (in its own
folder), a DSS-WISE one with `Hmax_ft` in its name.

Baseline records and LISFLOOD-FP rasters: project folder `crest-inundation/baseline-2026-11-01/<dam>/`. Score files
for this page and the model-to-model script: `crest-inundation/accuracy-envelope-2026-11-04/`. Cite a CSI only with
its reference, threshold, record, and scorer commit.

### Provenance

| Item | Value |
|---|---|
| Scorer | `nickdbendickson/crest` `main` 8bc4d20 (CSI, 2026-11-05) and 34524e0 (per-cell detail, 2026-11-04), `score-routed.mjs --spread` (reach on) |
| R1 LISFLOOD-FP 2801 | `baseline-2026-11-01/DWR-2801/Depth (Max).tif`, sha256 `e89a67b6…c64c8fe` |
| R1b / R1c LISFLOOD-FP 2801 | `runner-convergence-2801-2026-11-03/rasters/raw10_54h_depth_ft.tif`, `raw5_24h_depth_ft.tif` (EPSG:26914) |
| R3 DSS-WISE 2801 | `119062_Hmax_ft_upto_final.tif`, sha256 `25fd0d88…58ddc9a2` |
| R2 ANUGA 2801 | `DWR-2801_ANUGA_max_depth_ft_EPSG5070.tif` (Nick, 2026-11-04; 30 m; its downstream wet areas, 5,334 / 2,117 ac, match the harness report's `hz2210…/anuga_hmax_ft.tif` exactly), sha256 `9779c608…cd88f0c` |
| Records | `baseline-2026-11-01/<dam>/<dam>_record.json` |
