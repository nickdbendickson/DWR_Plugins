"""What differs between DamSafety Finder and DamIT Finder: names, the Processing provider id and
the hints about which layers to give. Every other file of the Finder is the same in both
repositories (DamSafety_Finder/ in dAmIt_Plugins, damit_finder/ in damit) except the docstring
of core/warp_opts.py (here byte-identical to the Runner's and Screening's) and the README."""
NAME = "DamSafety Finder"
PROVIDER_ID = "ndfind"
KNOWN_EXAMPLES = ("from the offline pack: DWR dams, DWR dugouts, Unknown Structures, NID dams; for "
                  "crossings NBI bridges, NDDOT bridges and centre-line pipes, USGS stream crossings, "
                  "SARP / ABI crossings")
EMB_HELP = ("Layers: the statewide 1 m VRT or a county DEM; the pack's 3DHP_flowlines (or "
            "NHD_flowlines), roads (NG9-1-1) and railroads (NARN). Output: 'embankments' (every fill, "
            "status listed / unsure / new against the known structures), 'screening_dams' (the fills "
            "that hold water and are not listed, in the DWR export field names, status 'Candidate': "
            "DamSafety Screening tool 1 reads them; set its status filter to Candidate) and a review "
            "hillshade lit from all round.")
POND_HELP = ("The same pond engine as DamSafety Screening tool 16 (core/ponds.py, kept identical by "
             "CI), with every pond checked against the DWR dams, dugouts and NID: tool 16 screens "
             "ponds, this tool tells you which are not on a list. 'screening_ponds' holds the "
             "not-listed ponds that could release water, in the DWR export field names.")
CROSS_HELP = ("Known structures for crossings: NBI bridges (pack NBI_bridges; item 43B 19 = culvert), "
              "NDDOT bridge inventory, NDDOT centre-line pipes, USGS_stream_crossings, SARP / ABI road "
              "crossings. The Runner's culvert guesses (CULVERT_BURN_GUESSES) use the same road x "
              "stream idea per dam; this tool lists them for a county or the state.")
# Image services with a near-infrared band for the water outlines: (label, ImageServer address,
# green band, near-infrared band). The ND GIS Hub 2021-2024 statewide NAIP is natural colour only
# (no near-infrared), so it cannot be used. Colour-infrared services are NIR, red, green.
_HUB = "https://ndgishub.nd.gov/arcgis/rest/services/Imagery/{}/ImageServer"
IMAGE_SERVICES = [
    ("ND GIS Hub 2018 aerial, 30 cm, 4-band (Jul-Oct 2018)", _HUB.format("AerialImage_ND_2018_30cm"), 2, 4),
    ("ND GIS Hub 2020 NAIP colour-infrared, 60 cm", _HUB.format("AerialImage_ND_2020_CIR"), 3, 1),
    ("ND GIS Hub 2019 NAIP colour-infrared, 60 cm", _HUB.format("AerialImage_ND_2019_CIR"), 3, 1),
    ("ND GIS Hub 2017 NAIP colour-infrared, 60 cm", _HUB.format("AerialImage_ND_2017_CIR"), 3, 1),
]
# 'Add evidence to fills': the default imagery for the water history, one per line (YEAR | source |
# bands). The ND GIS Hub has statewide NAIP colour-infrared 2009-2020 (NIR, red, green) and the
# 2018 4-band 30 cm (red, green, blue, NIR); its 2021-2024 NAIP is natural colour only.
EVIDENCE_IMAGES = "\n".join(
    ["{0} | {1} | nir=1 red=2 green=3".format(y, _HUB.format("AerialImage_ND_{}_CIR".format(y)))
     for y in (2009, 2010, 2012, 2014, 2015, 2016, 2017, 2019, 2020)]
    + ["2018 | {} | red=1 green=2 blue=3 nir=4".format(_HUB.format("AerialImage_ND_2018_30cm"))])
EVIDENCE_IMAGES_HELP = ("The default list is every statewide ND GIS Hub image year with a near-infrared "
                        "band (2009-2020): one small download per fill per year.")
WATER_HELP = ("Layers: the pack's 3DHP_waterbodies (or NHD / NWI) as the mapped water bodies. The "
              "'water' polygons with mapped = no are the ponds no layer has: give them to 'Find ponds "
              "and dugouts' as its water bodies to ask the LiDAR whether each is held above the ground.")
# Build-the-water-mosaic tool: the output CRS of the per-tile count / elevation rasters. DamSafety
# builds for North Dakota (EPSG:26914, UTM 14N, the DWR 1 m terrain grid); DamIT builds for the
# whole U.S. (EPSG:5070, CONUS Albers) so a tile from any state lands on one grid.
MOSAIC_EPSG = 26914
MOSAIC_REGION = "North Dakota (UTM 14N)"
MOSAIC_HELP = ("Point it at a folder of classified LiDAR tiles (USGS 3DEP .las/.laz) and it writes "
               "the water_count.vrt and water_elev.vrt that 'Find open water' reads with its "
               "'Prebuilt class-9 water-count raster' input. Build it once per area; the open-water "
               "tool then reads the mosaic a tile at a time, so a county or the whole state is in "
               "reach without re-reading the raw point cloud. Tiles whose CRS is not in the file "
               "need their EPSG. Dry land and the gaps between tiles read back as no water.")
OSM_HELP = ("Use the Geofabrik North Dakota extract the offline pack's OpenStreetMap step downloads "
            "(north-dakota-latest.osm.pbf). The pack's OSM_crossings layer (culverts, fords, rail "
            "bridges) can be given to the crossings tool as it is; this tool adds the dams, weirs and "
            "dikes. Give the output to the other tools as a known-structure layer.")
# The fill score model shipped with the plugin (core/score.py; learned from listed dams on USGS 1 m
# LiDAR in 7 ND regions). The Find fills tool uses it when no other model is given.
DEFAULT_MODEL = "models/nd_fill_score_statewide_2026-11.json"
