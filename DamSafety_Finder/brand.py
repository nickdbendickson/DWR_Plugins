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
