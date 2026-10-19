# Flagged structures

Some inventory records are not a dam to breach. A flagged structure is listed in the suite's
`damsuite/structure_flags.py` (shipped byte-identical in Runner, Hazard and Screening) with the
structure class, the hazard class that stands, where that class comes from, the reason, the
evidence and the date. The plugins read the list; nothing writes it. It is not a setting
(`damsuite_settings.csv`) and it is not in the offline data pack.

Added in Runner 3.10.23, Hazard 2.18.38, Screening 0.17.26 (damsuite 1.17.2).

## The list

| DWR id | NID id | Name | Class | Hazard | Source | Date |
|---|---|---|---|---|---|---|
| DWR-3161 | ND00597 | Sanish Bay WPA Dam 1 | road fill over backwater | Low | DWR record | 2026-10-19 |

### DWR-3161 / ND00597 (Sanish Bay WPA Dam 1)

The "dam" is the 1995 ND-23 highway fill. A twin 10 x 9 ft box culvert with stoplogs runs under the
road (NBI structure 0023048401, 29 m from the dam point; year, culvert size and 40 ft top width
match the DWR record). The pool is the creek valley south of the road, and Lake Sakakawea
(Sanish Bay) is the downstream side to the north. Emptying the full 2,861 ac-ft into the lake
would raise it about 0.01 ft, so there is no breach flood to classify; the loss in a failure is the
highway. DWR's Low stands. The HIGH of the 3.10.16 rebaseline was a model artifact: the ND-23
"overtopped" there is the dam's own crest.

What was wrong on the suite's side:
- The Runner's 1844.44 ft top of dam is the 1966 ground from before the road fill (the 10 m USGS
  DEM, 1966 source; there is no lidar at the dam). The real crest is probably 1862-1870 ft.
- Screening's storage curve was one flat point at the lake surface.
- Screening picked south as downstream, which is backwards.

The DWR max pool (1868.6 ft, 2,861 ac-ft, 335 ac) fits the terrain within about 2 ft, so it is
the road top. The normal pool (62.5 ac-ft, 22.1 ac) fits about 1844.4 ft, not the recorded
1848 ft (170-220 ac-ft there on the DEM). Datum at the dam: NGVD29 to NAVD88 is +1.51 ft.

Open questions for DWR:
1. Does record 3161 mean the ND-23 road fill and its culvert (NBI 0023048401)?
2. Which datum are the record's elevations in (NGVD29 or NAVD88)?
3. What is the crest elevation of the road over the culvert?
4. The normal pool: 62.5 ac-ft / 22.1 ac fits about 1844.4 ft, not 1848 ft. Which is right?
5. Is the 1868.6 ft max pool the road top (overtopping), or a design level?
6. Are the stoplogs kept in? What is the stoplog crest ("20' + 7' stoplog struct")?
7. Is the dam point meant to be the culvert, and is the dam line the road centreline?
8. Do they want a breach analysis at all, or a road-embankment check instead?

## What the plugins do with a flagged structure

**Screening** (no override; the flag is the decision):
- Tool 1 keeps the dam and writes `struct_cls` (`road_fill_backwater`) and `flag_note`
  ("road fill over backwater; breach analysis not applicable; hazard Low (DWR record)"). These are
  new fields; no existing field changed.
- Tools 5, 6, 7 and 12 skip its breach: `brch_conf` = `flagged`, triage clear with the note,
  `sim_status` = `flagged`, not timed.
- 7b never sends it to the Runner (`rn_status` = "skipped: flagged structure ...").
- Tool 8 gives the hazard of record (`hz_class` Low, `hz_conf` `flagged`, the note in
  `hz_reason`) instead of a breach hazard. A flood raster of the dam, if any, is not used.
- Tool 10's pages and summary show that class and note; a flagged dam is not in the review queue
  as "simulation failed".

**Runner:**
- "Run breach model for one dam" and the sensitivity tool stop with the reason. The dam is matched
  by its Dam ID (DWR-3161 or 3161), the NID ID parameter, the DWR structure id parameter or the
  dam folder's dam.json NID id. The time estimate notes the flag and goes on.
- To run it anyway, for information only: tick "Run a flagged structure anyway"
  (`ALLOW_FLAGGED_STRUCTURE`, advanced), pass `--allow-flagged-structure` to `ndbreach_cli.py run`,
  or set the environment variable `ALLOW_FLAGGED_STRUCTURE=1`. The log and the run report then
  start with "FLAGGED STRUCTURE (run allowed ...)" and say the hazard of record stays Low.
- The batch tool and `ndbreach_cli.py batch` skip it: Status `flagged`, the reason in `Message`,
  and two new columns at the end of `Batch_Breach_Runs.csv`: `Structure_Flag` (the note) and
  `Hazard_Of_Record` ("Low (DWR record)"). The existing columns are unchanged. A flagged dam is
  skipped even if its folder already has `Depth (Max).tif`. `ALLOW_FLAGGED_STRUCTURE=1` in the
  environment runs it.

**Hazard:**
- A classification of a flagged dam (by the dam folder's name, the subject dam's NID ID, or the
  dam.json NID id) stops with the reason, and the run log records it. With
  `allow_flagged_structure` in the run settings or `ALLOW_FLAGGED_STRUCTURE=1` it runs, and the log
  and the DATA SOURCES of the summary say the hazard of record stays Low and the computed class is
  for information only.

**Dam suite home** (all three plugins): the dam folder's row shows class Low, next action "none",
and "Flagged structure (DWR-3161 / ND00597): road fill over backwater; breach analysis not
applicable; hazard Low (DWR record). No breach run or classification." A class recorded earlier
by a breach run is named as not used.

## How to add a flagged structure

1. Edit `DamSafety_Runner/damsuite/structure_flags.py`: append a dict to `FLAGS` with every key of
   `REQUIRED`: `dwr_id` ("DWR-<structure id>"), `nid_id`, `name`, `structure_class` (a key of
   `CLASS_LABELS`; add a label there for a new kind), `hazard` (Low, Significant or High),
   `hazard_source`, `breach_analysis` ("not applicable"), `reason`, `evidence`, `date`,
   `decided_by`. `followup` is optional.
2. Copy the file byte-identical to `DamSafety_Hazard/damsuite/` and `DamSafety_Screening/damsuite/`.
3. Re-stamp the three `damsuite/FILES.json` (sha256 of each file); `damsuite.selfcheck()` must
   return `[]`.
4. Run the tests: `structure_flags.problems()` must be `[]` (complete entries, no shared ids).
5. Bump the three plugins' versions (and `versions.py` `RELEASE` in all three copies) and note the
   entry in the changelog.

An entry needs an engineer's decision; the evidence should name the records it rests on. Removing
an entry makes the dam a normal dam again in all three plugins.
