"""Dataset definitions for North Dakota."""

from .core import GitHubRepoSource, HttpFileSource, NsiApiSource, OvertureSource, ArcGISFeatureSource

MICROSOFT_URL = ("https://minedbuildings.z5.web.core.windows.net/legacy/"
                 "usbuildings-v2/NorthDakota.geojson.zip")
GEOFABRIK_URL = "https://download.geofabrik.de/north-america/us/north-dakota-latest-free.gpkg.zip"
# NSI generator pinned to one reviewed commit (main as of 2026-10-14, committed 2026-09-15) and
# the SHA-256 of GitHub's zip of that commit. To move to a newer commit, review it and update both.
NSI_GENERATOR_COMMIT = "87f768783302e51852e38bb622ebd35fe17eee59"
NSI_GENERATOR_ZIP_SHA256 = "26eedf5bad62b0f47438f58ce12e2689fc2070ca29cdea58e9f7a9d32bdd0098"
# Overture Maps buildings (conflated OSM + Microsoft + others, with height)
# Downloaded via overturemaps CLI or S3; ND bbox: -104.05,45.94,-96.55,49.00
OVERTURE_NOTE = "Overture buildings (ND bbox) via overturemaps download --type=building"

# id -> (display name, description, enabled by default)
SOURCE_INFO = {
    "nsi": ("National Structure Inventory (NSI)",
            "The USACE structure inventory for all 53 North Dakota counties, from the NSI "
            "API. By default NSI 2026 and NSI 2022 are merged into one layer: the 2026 point "
            "wins where both versions have a structure, and 2022-only structures are kept "
            "and flagged. Checked at most every N days (set below).", True),
    "microsoft": ("Microsoft building footprints",
                  "Statewide NorthDakota.geojson (about 568,000 footprints). "
                  "Microsoft no longer updates this legacy release.", True),
    "osm": ("OpenStreetMap (Geofabrik)",
            "Statewide OpenStreetMap GeoPackage, rebuilt daily by Geofabrik. "
            "Changes detected via ETag / Last-Modified headers.", True),
    "usa_structures": ("USA Structures (FEMA/ORNL)",
                       "Needs a download link: paste the North Dakota zip link from "
                       "FEMA's USA Structures page.", False),
    "nsi_generator": ("nsi-generator source code",
                      "Latest code on the main branch of USACE-NSI/nsi-generator.", False),
    "overture": ("Overture Maps Buildings",
                 "Conflated building footprints (OSM + Microsoft + others) with height and type. "
                 "Downloaded for the North Dakota bounding box. Layer name: overture_buildings.", False),
    "address_points": ("ND 911 Site/Structure Address Points",
                       "NENA SiteStructure Address Points from the ND GIS Hub "
                       "(438k+ points). Layer name: address_points. "
                       "Used by Hazard for household verification when no NSI/outline is present.", True),
}


def build_sources(config, merger=None):
    """config keys: enabled (dict id->bool), usa_structures_url, nsi_interval_days,
    nsi_mode ("merged", "nsi2026", "nsi2022"), nsi_match_m."""
    enabled = config.get("enabled", {})

    def on(source_id):
        return enabled.get(source_id, SOURCE_INFO[source_id][2])

    sources = []
    if on("nsi"):
        sources.append(NsiApiSource(
            "nsi", SOURCE_INFO["nsi"][0], "nsi", merger=merger,
            check_interval_hours=24 * int(config.get("nsi_interval_days", 30)),
            description=SOURCE_INFO["nsi"][1], mode=config.get("nsi_mode") or "merged",
            match_m=config.get("nsi_match_m")))
    if on("microsoft"):
        sources.append(HttpFileSource(
            "microsoft", SOURCE_INFO["microsoft"][0], MICROSOFT_URL,
            "NorthDakota.geojson.zip", "microsoft_footprints", version_mode="headers",
            layer_globs=("*.geojson",), description=SOURCE_INFO["microsoft"][1]))
    if on("osm"):
        sources.append(HttpFileSource(
            "osm", SOURCE_INFO["osm"][0], GEOFABRIK_URL,
            "north-dakota-latest-free.gpkg.zip", "osm", version_mode="headers",
            layer_globs=("**/*.gpkg",), layer_filter=("buildings",),
            description=SOURCE_INFO["osm"][1]))
    usa_url = (config.get("usa_structures_url") or "").strip()
    if on("usa_structures") and usa_url:
        filename = usa_url.split("?")[0].rstrip("/").rsplit("/", 1)[-1] or "usa_structures.zip"
        sources.append(HttpFileSource(
            "usa_structures", SOURCE_INFO["usa_structures"][0], usa_url, filename,
            "usa_structures", version_mode="headers",
            extract=filename.lower().endswith(".zip"),
            layer_globs=("**/*.gdb", "**/*.shp", "**/*.gpkg"),
            description=SOURCE_INFO["usa_structures"][1]))
    if on("nsi_generator"):
        sources.append(GitHubRepoSource(
            "nsi_generator", SOURCE_INFO["nsi_generator"][0], "USACE-NSI", "nsi-generator",
            "main", "nsi_generator_code", description=SOURCE_INFO["nsi_generator"][1],
            commit=NSI_GENERATOR_COMMIT, zip_sha256=NSI_GENERATOR_ZIP_SHA256))
    if on("overture"):
        sources.append(OvertureSource(
            "overture", SOURCE_INFO["overture"][0], "overture_buildings",
            description=SOURCE_INFO["overture"][1]))
    if on("address_points"):
        sources.append(ArcGISFeatureSource(
            "address_points", SOURCE_INFO["address_points"][0],
            "https://services1.arcgis.com/GOcSXpzwBHyk2nog/ArcGIS/rest/services/"
            "NDGISHUBSiteStructureAddressPoints/FeatureServer/0",
            "address_points",
            where="1=1",
            out_fields="OBJECTID,ADDRESS,Add_Number,St_Name,St_PosTyp,County,Lat,Long,"
                       "Site_NGUID,Placement,Post_Code,Post_Comm",
            description=SOURCE_INFO["address_points"][1],
            layer_name="address_points"))
    return sources
