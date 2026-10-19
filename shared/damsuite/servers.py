"""Raster servers the three plugins can use. No login. Selecting an offline
package adds these to the QGIS project; the vector layers come from the package.
"""

# kind is xyz, wms, or arcgis (MapServer / ImageServer export).
SERVERS = [
    {"id": "usgs_imagery", "name": "USGS Imagery Only", "kind": "xyz",
     "url": "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"},
    {"id": "usgs_imagery_wms", "name": "USGS Imagery WMS", "kind": "wms",
     "url": "https://basemap.nationalmap.gov/arcgis/services/USGSImageryOnly/MapServer/WMSServer"},
    {"id": "usgs_topo", "name": "USGS Topo WMS", "kind": "wms",
     "url": "https://basemap.nationalmap.gov/arcgis/services/USGSTopo/MapServer/WMSServer"},
    {"id": "usgs_3dep", "name": "USGS 3DEP Elevation", "kind": "arcgis",
     "url": "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer"},
    {"id": "usgs_nhd", "name": "USGS NHD MapServer", "kind": "arcgis",
     "url": "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer"},
    {"id": "esri_imagery", "name": "Esri World Imagery", "kind": "xyz",
     "url": "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"},
    {"id": "esri_topo", "name": "Esri World Topo", "kind": "xyz",
     "url": "https://services.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}"},
    {"id": "nd_hub", "name": "ND GIS Hub services", "kind": "arcgis",
     "url": "https://ndgishub.nd.gov/arcgis/rest/services"},
]

VECTOR_SOURCES = [
    {"layer": "dams", "name": "NID dams",
     "url": "https://geospatial.sec.usace.army.mil/dls/rest/services/NID/National_Inventory_of_Dams_Public_Service/FeatureServer/0"},
    {"layer": "flowlines", "name": "NHD flowlines",
     "url": "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/6"},
    {"layer": "waterbodies", "name": "NHD waterbodies",
     "url": "https://hydro.nationalmap.gov/arcgis/rest/services/nhd/MapServer/12"},
    {"layer": "roads", "name": "ND GIS Hub roads",
     "url": "https://services1.arcgis.com/GOcSXpzwBHyk2nog/arcgis/rest/services/NDGISHUBRoadsNG911/FeatureServer/0"},
    {"layer": "railroads", "name": "North American rail",
     "url": "https://services.arcgis.com/xOi1kZaI0eWDREZv/arcgis/rest/services/NTAD_North_American_Rail_Network_Lines/FeatureServer/0"},
    {"layer": "structures", "name": "USA Structures",
     "url": "https://services2.arcgis.com/FiaPA4ga0iQKduv3/arcgis/rest/services/USA_Structures_View/FeatureServer/0"},
    {"layer": "lifelines", "name": "ND address points",
     "url": "https://services1.arcgis.com/GOcSXpzwBHyk2nog/arcgis/rest/services/NDGISHUBSiteStructureAddressPoints/FeatureServer/0"},
]
