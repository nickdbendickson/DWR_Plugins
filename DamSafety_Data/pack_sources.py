# -*- coding: utf-8 -*-
"""Additional data sets for the ND offline pack.

Layer names written into the pack must match what Screening, Runner, and
Hazard expect via suite_fill / suiteexport:

  structures        -> NSI_structures (or nsi_nd)
  structures_usa    -> USA_Structures
  overture_buildings -> Overture Maps buildings (new)
  microsoft_footprints -> Microsoft building footprints (already in Data plugin)
  address_points    -> ND 911 Site/Structure Address Points (NENA)

3DHP and Aquatic Barriers are additional hydrography / barrier layers.
"""

# 3D Hydrography Program (in addition to NHD)
THREEDHP_BASE = "https://hydro.nationalmap.gov/arcgis/rest/services/3DHP_all/MapServer"
THREEDHP_FLOWLINE_URL = f"{THREEDHP_BASE}/50"
THREEDHP_WATERBODY_URL = f"{THREEDHP_BASE}/60"

# Aquatic Barriers FeatureServer
AQUATIC_BARRIERS_FS = (
    "https://services.arcgis.com/QVENGdaPbd4LUkLV/ArcGIS/rest/services/"
    "Aquatic_Barrier_Prioritization_Dams_Crossings_Indicators_05242024/FeatureServer/0"
)

# Overture Maps buildings (S3 GeoParquet; use overturemaps CLI for ND bbox)
OVERTURE_S3 = "s3://overturemaps-us-west-2/release/"
OVERTURE_BUILDINGS_TYPE = "building"
ND_BBOX = (-104.0489, 45.9351, -96.5543, 49.0000)

# Layer names the other plugins already accept (suite_fill):
ACCEPTED_STRUCTURE_LAYERS = (
    "nsi_structures", "nsi_nd", "nsi",
    "structures_usa", "usa_structures",
    "overture_buildings",   # new
    "microsoft_footprints", # already produced by Data plugin
)
