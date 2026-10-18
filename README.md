# DWR_Plugins

ND DWR dam-safety QGIS plugins (QGIS 3.34 LTR):
DamSafety_Setup, DamSafety_Screening (nddam), DamSafety_Runner (ndbreach), DamSafety_Hazard (ndhazard), DamSafety_Data.

- `docs/` holds the code audit (release 1.22), the full findings appendix, and the GUI simplification plan.
- Large data (terrain, model rasters, the ND_Dam_Suite_Pack) is kept out of this repo.

Roles: the Nightly Audit Engineer makes one small fix per area each night. SWE builds the larger features and tests. Grok Bot runs regression dams in QGIS and files issues.

## Archived: old nd_ plugins
`nd_claude_1.23/` (ND_Dam_Hazard_Helper 2.31.0, nd_breach_runner 3.21.0, nd_dam_screening 0.29.0) and `nd_installed/` (nd_data_updater 1.4.1) were removed from main. They are preserved on branch [`archive/nd-plugins`](https://github.com/nickdbendickson/DWR_Plugins/tree/archive/nd-plugins) and tag `archive-nd-plugins-20261015` (commit 18ae1f5). The DamSafety_* plugins replace them.

The top-level `nd_breach_runner/` and `nd_dam_screening/` folders are **kept**. They are test-only import aliases for DamSafety_Runner and DamSafety_Screening, because the tests import the plugins by their installed names.

## DamSafety_PMP (internal, experimental)
`DamSafety_PMP/` is the QGIS port of AWA's ND PMP Evaluation Tool (v0.3.0, provider `ndpmp`). **It's internal only, not validated against ArcGIS, and not for redistribution without written permission from the ND State Water Commission and AWA.** The AWA data isn't in the repo. See `DamSafety_PMP/README.md` for where to download it and for the hydrograph CSV order (`time_hr, flow_cfs, ...`) that the Runner reads.
