# ND Data Updater (QGIS 3.28 plugin)

Keeps an offline copy of North Dakota structure and building-footprint data on
your Mac and downloads a new copy only when a source actually changes.

## Install

1. In QGIS: **Plugins > Manage and Install Plugins… > Install from ZIP**.
2. Choose `nd_data_updater.zip` and click **Install Plugin**.
3. The plugin appears under **Plugins > ND Data Updater** and as a toolbar button.

## Use

- **Check for Data Updates Now** (toolbar button): checks every dataset that is
  turned on. The first run downloads everything; later runs download only what
  changed. Progress shows in the task bar at the bottom of the QGIS window and
  can be cancelled there.
- **Add North Dakota Layers to Project**: adds the downloaded layers to a
  "North Dakota data" group.
- **Open Data Folder**: opens the folder in Finder.
- **Settings and Status…**: data folder, schedule, which datasets to keep, and
  when each was last updated and checked.

Where the data is stored (first that applies):

1. The folder you choose in **Settings and Status…**.
2. `<suite data folder>/structures/` when the dam suite's data folder is set
   (`ndsuite/data_folder`). A folder inside the offline pack (`ND_Dam_Suite_Pack`) or
   one that can't be written is skipped, with a message.
3. `~/Documents/ND Structure Data`.

If `~/Documents/ND Structure Data` already holds downloads and the suite's
`structures/` folder is empty, Data keeps using the old folder and shows a one-line
notice. To share the data with the suite, close QGIS and move that folder's contents
into `structures/`. Nothing is moved automatically.

## Datasets

| Dataset | Default | Folder | How changes are detected |
|---|---|---|---|
| National Structure Inventory (all 53 counties, NSI 2026 and 2022 merged, from the NSI API) | on | `nsi/` | Re-downloaded every 30 days (adjustable); replaced only if the structures changed |
| Microsoft building footprints (`NorthDakota.geojson`) | on | `microsoft_footprints/` | Server ETag / Last-Modified. Microsoft no longer updates this legacy release |
| OpenStreetMap from Geofabrik (GeoPackage; only the buildings layer is added to the project) | on | `osm/` | Geofabrik's published MD5; the download is verified against it |
| USA Structures (FEMA/ORNL) | off | `usa_structures/` | Needs a download link pasted into Settings. ETag / Last-Modified |
| nsi-generator source code (`USACE-NSI/nsi-generator`, main branch) | off | `nsi_generator_code/` | Latest commit |

The NSI counties are merged into one GeoPackage, `nsi/nsi_nd.gpkg`, using the
GDAL bundled with QGIS.

### NSI versions

The NSI API serves each release at its own address (`/nsiapi/nsi2026/...`,
`/nsiapi/nsi2022/...`); its unversioned address follows whatever USACE makes
the default, which became NSI 2026 in 2026. The plugin always asks for a named
version. **Settings > NSI version** chooses:

- **2026 and 2022 merged** (default). NSI point ids are not carried between
  versions, so structures are matched by location:
  - Every 2026 point is kept with its 2026 data.
  - A 2022 point with a 2026 point within the match distance (default 30 m) is
    the same structure, and the 2026 data wins. It is folded into the 2026
    point that matches best (nearest, preferring the same occupancy category:
    a 2022 house prefers the 2026 house over a slightly closer barn).
  - A 2022 point with no 2026 point nearby is kept with its 2022 data. Some are
    demolished or misplaced; keeping them is the conservative choice.
- **2026 only** or **2022 only**.

Extra fields on every point:

| Field | Meaning |
|---|---|
| `nsi_version` | `2026`, or `2022` for a structure only NSI 2022 has |
| `nsi22_fd_id`, `nsi22_occtype`, `nsi22_bldgtype` | The best-matching 2022 point (for a 2022-only point, its own values) |
| `nsi22_match_m` | Distance to that 2022 point, metres |
| `nsi22_count` | How many 2022 points were folded into this point (2022 often had several unit points on one apartment building) |
| `nsi22_mobile` | 1 if any folded 2022 point was a mobile home (occtype RES2 or bldgtype H). NSI 2026 dropped many of these. |

The one renamed field, 2022 `ground_elv_meters`, is written as the 2026
`grnd_elv_m`. `nsi/nsi_summary.json` lists point counts per county and in total.
Changing the version or the match distance rebuilds the NSI copy at the next check.

## Build offline pack… (1.5.0)

Data menu > **Build offline pack…** downloads every layer of the suite's offline pack and
writes it twice from the same download, into a folder you pick:

| Folder | Format | What reads it |
|---|---|---|
| `ND_Dam_Suite_Pack/` | 2: one GeoPackage per layer (structures, roads, railroads, lifelines, hydro, dwr, hifld, barriers, boundaries, landcover), `manifest.json`, `LAYERS.txt` | Setup, Hazard, Runner, Screening |
| `nd_offline_pack/nd_offline_pack.sqlite` + `manifest.json` | 1: every vector layer in one file (no raster; the manifest's `rasters_not_included` says so) | older plugin versions |
| `ND_Dam_Suite_Pack_build/` | the work file and downloads | resume only; delete after the build if you like |

- **All pack layers** (default) or tick the ones you want. Area: all of North Dakota, a small
  test box, or your own box.
- Optional local files: a DWR folder (needed for `DWR_unknown_structures`, which the DWR
  MapService does not publish), a HIFLD folder, an NLCD GeoTIFF.
- Run it again to finish a stopped build: finished layers are kept ("Download everything
  again" fetches all). Each manifest entry has the source URL, date, count and sha256.
- It refuses a folder that is a pack, holds a pack it did not build, or holds the pack the
  plugins read now: an existing pack is never written into.
- **Sources (PR #221):**
  - **HIFLD** (fire / EMS stations, local EOCs, local law enforcement, wastewater plants): the
    Data Rescue Project's copies (portal.datarescueproject.org) kept on DataLumos / ICPSR.
    DataLumos needs a free ICPSR login and its Terms of Use, so the builder does not download
    them: download each layer (GeoJSON or shapefile zip) from its DataLumos project page
    (fire 239161, EOC 239489, law enforcement 239493, wastewater 239290) into one folder and
    pick it as the HIFLD folder. Features outside North Dakota are dropped. Without a file the
    layer is left out (the manifest's `left_out` names the page). `--hifld-mirrors` uses the
    old FEMA / Esri mirrors instead.
  - **No HPMS.** Traffic (AADT) comes from `NDDOT_traffic_counts` and `ND_county_roads`.
  - **3DHP replaces NHD**, keeping the names `NHD_flowlines` / `NHD_waterbodies` (the Hazard /
    Runner culvert tools and Setup look for those names). The USGS 3DHP map service
    (`3DHP_all/MapServer` 50 flowlines, 60 waterbodies) is read and `gnis_name`, `gnis_id`,
    `ftype`, `fcode` are added; the 3DHP fields stay. When 3DHP fails, the NHD service is used
    and the manifest says why (`hydro_source`, `fallback_reason`).
  - **SARP National Aquatic Barrier Inventory** (`barriers/`): `ABI_dams` and
    `ABI_small_barriers_surveyed` from the public API (`tool.aquaticbarriers.org/api/v1/public/
    <dams|barriers>/state?id=ND`); `ABI_road_crossings_unsurveyed` from the national
    road-crossings download (about 600 MB, cached for a week), ND rows with
    Surveyed = "not likely". Each has a `network` field (on-network / off-network). Terms: credit
    NACC, SARP and NFHP; the manifests hold the citation, terms and data version.
- Each manifest entry also has the source file's sha256 and, in the work folder,
  `provenance.json` keeps them for resumed builds.
- NLCD 2021 comes from the MRLC WCS; when that is down, from the Esri Annual NLCD image
  service (year 2021, same class codes); the manifest names the source used.
- Command line: `python3 -m DamSafety_Data.pack_build --out <folder> [--bbox w,s,e,n]
  [--only NAME,...] [--hifld-folder DIR] [--hifld-mirrors] [--list] [--verify]`.

## Download imagery for these dams… (1.5.0)

Data menu > **Download imagery for these dams…** fetches NAIP aerial imagery around each dam
of a dams layer (all of it, the selected dams, or a list of dam ids) and writes one file per
dam into `<output folder>/imagery/`:

- `<dam_id>_naip_<year>.tif`: Cloud Optimized GeoTIFF, JPEG (YCbCr, quality 85) with internal
  overviews, natural colour (NAIP bands 1-3), in the dam's NAD83 / UTM zone (EPSG 26913 or
  26914). The dam id comes from `dam_id`, `nidid`, `structure_id`, … or the field you pick.
- `manifest.json`: per dam the exportImage URL and request, the NAIP year and image names,
  resolution, box, CRS, size and sha256.
- Source: the USGS National Map NAIP Imagery ImageServer
  (`imagery.nationalmap.gov/arcgis/rest/services/USGSNAIPImagery/ImageServer`; USDA FPAC NAIP,
  0.6 m in North Dakota). Only the newest NAIP year on the dam's box is used.
- Buffer: a radius, 2.5 km by default (a 5 km square). Resolution: 0.6 m (native), 1 m or
  2 m. The box is fetched in pieces of at most 2000 × 2000 px (the server allows 4000).
- The window shows the size before downloading. Measured at Bismarck (NAIP 2023, 0.6 m,
  2.5 km): about 60 MB downloaded and 24 MB on disk per dam, about 5 minutes per dam.
- Run again to resume: a dam whose file matches the manifest (same sha256 and settings) is
  kept, and pieces already downloaded are reused. The output folder may not be (inside) a
  pack, and nothing goes into the sqlite.
- Command line: `python3 -m DamSafety_Data.imagery --dams <file> --out <folder> [--ids A,B]
  [--id-field F] [--radius 2500] [--res 0.6|1|2] [--transfer jpeg|lossless] [--estimate]`.

## When it checks

- About 20 seconds after QGIS starts, then hourly while QGIS is open, but never
  more often than **Check at most every** (default 24 hours).
- Automatic checks stay quiet unless something was downloaded or went wrong.
- Nothing runs while QGIS is closed.

## Offline behaviour

The plugin always works from the local copies. If there is no connection, the
check reports "offline" and nothing is changed. Updates are downloaded to a
hidden `.staging` folder, verified (checksum, complete size, valid zip), and only
then swapped into place, so an interrupted download never damages the data you
have. Layers already in your project are reloaded after an update.

`structures_manifest.json` in the data folder (`manifest.json` before 1.4.0, read
once and left in place) records the version, last update and last
check for each dataset. It has its own name so it never replaces the suite's
`manifest.json` in the suite data folder.

## Notes

- Network access uses QGIS's own settings (**Settings > Options > Network**),
  including any proxy.
- A full first download is roughly 0.5-1 GB.
- The nsi-generator code expects USA Structures as per-county shapefiles; FEMA
  publishes a statewide geodatabase, so convert it before feeding it to that tool.
