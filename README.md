# DWR_Plugins

ND DWR dam-safety QGIS plugins (QGIS 3.34 LTR):
DamSafety_Setup, DamSafety_Screening (nddam), DamSafety_Runner (ndbreach), DamSafety_Hazard (ndhazard), DamSafety_Data.

- `docs/` holds the code audit (release 1.22), the full findings appendix, and the GUI simplification plan.
- Large data (terrain, model rasters, the ND_Dam_Suite_Pack) is kept out of this repo.

Roles: the Nightly Audit Engineer makes one small fix per area each night. SWE builds the larger features and tests. Grok Bot runs regression dams in QGIS and files issues.
