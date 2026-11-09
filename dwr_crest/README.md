# Crest

Desk screening of small embankment dams: breach hydrograph, schematic valley flood, receptor ranking
(ACER TM 11), and a datum-aware export. The engine is the pure `analyze()` in `src/lib/crest/engine.ts`.

Crest is a screen, not the layer of record. Where its flood footprint can be trusted and where a Breach Runner run is
needed is in [docs/accuracy-envelope.md](docs/accuracy-envelope.md).

Every export (flood strip and terrain GeoJSON / KML, `dam.json`, the batch CSV, the Report) carries two versions: the
Crest build that wrote it, `crest_version` = `<package.json version>+<short git SHA>` (`src/lib/crest/version.ts`, injected
by both Vite configs from `scripts/crest-version.mjs`; `+local` without git or under the tests), and the engine's method
version `engine_version` (`engine.ts ENGINE_VERSION`, bumped whenever `analyze()` changes its numbers). The terrain layer
adds `trace_version` (`outline.ts TRACE_VERSION`).

## Scripts

| Script | What it does |
|---|---|
| `npm run dev` | Vite dev server on :8080 |
| `npm run build` | Production build only (`vite build`). Never touches a database. |
| `npm run build:deploy` | `npm run build && npm run db:migrate`. This is what Vercel runs (`buildCommand` in `vercel.json`). |
| `npm run db:migrate` | Applies pending top-level `migrations/*.sql` to `DATABASE_URL` once each (recorded in `_migrations`); skips when `DATABASE_URL` is unset. |
| `npm test`, `npm run typecheck`, `npm run lint` | Tests, `tsc --noEmit`, ESLint |
| `npm run build:desktop` | Builds the desk into `crest-desktop/crest/web/` for the desktop app (committed; see below). |
| `npm run build:crest-html` | Builds the same desk into the one-file `Crest.html` at the repo root (committed). |
| `npm run test:desktop` | Headless smoke test of the desktop app (Python, no GUI). |
| `npm run validate:crest -- --runner <dam folder or zip> --crest <flood.geojson> [--receptors <points.csv>]` | Scores a Crest flood layer (strip or terrain outline export) against a Breach Runner / LISFLOOD-FP run: recall, precision, CSI, area ratio, depth bias, and depth / velocity / arrival at receptors (`src/lib/crest/validation.ts`). Run by hand. |
| `npm run data:nd-dams` | Rebuilds `src/lib/crest/data/nd_dams.json` from the NID (ND dam picker). Run only when asked. |
| `npm run batch:dams -- --out <dir> (--state \| --county Cass \| --watershed 09020205 \| --ids ND00199,DWR-4031) [--dem <1 m mosaic .vrt>]` | Batch screen from the command line (see **Batch screen** below); resumes from `<dir>/batch.jsonl`. |
| `npm run data:nd-structures -- --from data-raw/dwr_structures_<stamp>` | Rebuilds `src/lib/crest/data/nd_structures.json` (every DWR dam and dugout, with HUC8 watersheds) for the batch screen. Run only when asked. |
| `npm run data:nd-dams-dwr -- --from data-raw/crest_dwr_navd88_<stamp>` | Rebuilds `src/lib/crest/data/nd_dams_dwr.json` from a folder of DWR Dams and Dugouts CSVs already converted to NAVD 88. Run only when asked. |

### Deploys and migrations

The production migration runs on every Vercel deploy through `vercel.json` → `buildCommand: npm run build:deploy`
(`vercel build` uses the same setting). Until this change it was chained onto `npm run build` itself, so any local or
CI build with `DATABASE_URL` set would migrate. If you deploy some other way, for example `npm run build` then
`vercel deploy --prebuilt`, run `npm run db:migrate` as a separate step before switching traffic. Locally, without
`DATABASE_URL`, the PGLite fallback applies the same files at startup (`src/lib/db.ts`).

## Drawn area of interest (AOI)

A drawn area limits the schematic flood. The valley is laid from the breach in the downstream direction; the reach
stops where it leaves the area, the wet area is clipped to it, and receptors outside it are unresolved (one that would
rank higher is still named, so a known High is never hidden).

**Downstream direction.**

1. Dam line + reservoir point: the normal to the dam line's **chord** (the straight line from its first to its last
   point), on the side away from the reservoir point. The vertices in between are ignored. For a straight or gently
   curved dam that is the valley axis; for a strongly curved, L- or U-shaped dam the chord can point off-axis or even
   upstream. Draw just the straight stretch through the breach instead. (The real ND00199 embankment is an L whose
   chord normal points WNW while the flood runs SE; the sample uses its straight 131 m breach segment, ~130°.)
2. No dam line: the bearing from the reservoir point to the breach.
3. Neither:
   - breach inside the area: the reach is cut at the area's nearest edge in any direction (the shortest it could be)
     and the wet area is cut by distance only, with a warning;
   - breach outside the area (or on its edge): the area is **not applied**, since it cannot be put upstream or
     downstream; the flood and every receptor are screened in full and a warning says why.

**Breach on the outline.** A breach within 1 m of the outline (on an edge or a corner) counts as inside, so an area
drawn from the dam line downstream is not lost to rounding. The same 1 m tolerance applies on both sides of every
edge along the valley, so the flow line counts as inside when it runs on or less than 1 m beside the outline, and
gaps under 2 m (a slot between two parts or a hole less than 2 m across the valley) are closed; a gap of 2 m or
more ends the reach. Cross-sections use the same test, which adds about 0.01 ac.

**Upstream warning.** If more than half of the area's filled outline (holes ignored) lies upstream of the breach, a
warning asks you to check the drawing.

## Basemap

The map uses key-free tiles: **USGS The National Map Topo** by default (public domain, with contours and streams),
with USGS Imagery + Topo and OpenStreetMap in the layer switcher. Tiles are the desk's only network use. To use
another XYZ tile server, set `VITE_CREST_TILE_URL` (with `{z}`, `{x}` and `{y}`) and `VITE_CREST_TILE_ATTRIBUTION` at
build time; it becomes the default layer (`src/lib/crest/basemap.ts`). The desktop bundle and `Crest.html` pick it up
when rebuilt with the variable set.

## North Dakota dam picker (NID, with DWR values)

On the Record step, **Fill from the NID** picks a county and then a dam (type a name, other name such as "Absaraka",
or NID ID such as `ND00199` / `199`). After a confirm ("Apply to this record" or "New record from NID"), it fills the
name, NID ID, county, year, location, height, length, storage and drainage area, each tagged "from NID" (or "from
DWR"), "edited (NID: x)" with a revert, or "not in NID: enter a value". **Unlink NID** drops the link and the tags.

- **Dams:** the NID's 948 North Dakota main dams (Federal ID equal to NID ID; associated structures such as
  `ND00145S001` are left out). ND00182 is listed under Clay (as the NID has it), with a note that its point lies in
  Cass Co., ND.
- **Storage V** is the NID Storage, with a chip saying whether it is the NID Max Storage or the Normal Storage (no max
  reported, with a warning). Over 500,000 ac-ft gets a note that the breach equations are out of range.
- **Height H** is the DWR maximum dam height where the dam joins the DWR file (830 dams, "from DWR"), else the NID
  Height (whole feet). Warnings W-H1 and W-H2 flag an NID height that looks structural, and an H that disagrees with
  crest minus breach bottom (tolerance max(1 ft, 5% of it), +0.5 ft for a rounded NID height).
- **Elevations are never filled from the NID** (it has none). The crest, breach bottom and datum stay blank, an amber
  banner asks for them, Next is disabled and the engine returns *Review (elevations missing)* with no flood until they
  are entered: typed from a DEM (NAVD 88; Crest does not sample the DEM, so this does not set the DEM crest and the
  DEM-vs-crest check still applies), typed in the fields, or, where the DWR record is in NGVD 29 or NAVD 88, with
  **Use DWR elevations (checked against the dam file)** after a confirm. The offer is always NAVD 88, straight from the
  bundle: NGVD 29 values were converted once, before the build, by per-point NOAA VERTCON and are labelled "converted
  from NGVD29 (+x.xxx ft, VERTCON)" (ND00199: 1070.0 / 1042.5 ft NGVD 29 -> 1071.09 / 1043.59 ft NAVD 88, +1.089 ft,
  H 27.5 ft); the stated value, datum and shift are kept as provenance and nothing is converted twice. Values the
  conversion QC held (outside 700-3,600 ft under an NGVD 29 / NAVD 88 label: ND01032) are never offered. DWR
  elevations on a local or blank datum are relative only and used just as an H check; `drawdown_e` is treated as
  local and not carried. 91 of the 839 joined dams have a NAVD 88 offer.
- **DWR dugout record (reference only).** Where a DWR Dugouts record has the dam's NID ID (108 dams), a grey box shows
  its depth (ft), height (ft), freeboard (ft), surface area (ac) and storage (ac-ft). It never fills a field and does
  not change the screen.
- **Surface area** is never filled. The NID normal-pool area is shown as a reference and the DWR maximum-pool area has
  a **Use** button.
- **NID hazard** is shown in grey as reported to the NID (FEMA class). It is not used by the screen and is not the class
  of record.

**Data and sources.** Both bundles are offline, loaded on demand (no network use by the picker):

| Bundle | Source | Downloaded |
|---|---|---|
| `src/lib/crest/data/nd_dams.json` | U.S. Army Corps of Engineers, National Inventory of Dams, ND state CSV (`nid.sec.usace.army.mil`). No owner names (an NID owner can be a private person): an unnamed dam is labelled by its NID ID only, e.g. "(unnamed) · ND00971" / "Unnamed dam ND00971". | 2026-10-29 |
| `src/lib/crest/data/nd_dams_dwr.json` | North Dakota Department of Water Resources, Dams and Dugouts layers (`mapservice.dwr.nd.gov`). Dams: numeric fields, the stated vertical datum code, NAVD 88 values (NGVD 29 converted with NOAA VERTCON before the build; the stated value and shift kept), held-field names, and the county where it differs. Dugouts: five numeric reference fields for dugouts whose NID ID is a main dam. No owner, name, contact, designer, remark or permit fields. Provisional, provided as is; field meanings are inferred. | 2026-10-30 |

The source URL, download time, SHA-256 of the raw file and the attribution are stored in each bundle and shown in the
picker footer, the Report, the dam.json export and **About the dam data** at the bottom of the desk. Past 180 days the
picker footer and About the dam data show an amber "Inventory is N months old" line naming the refresh command, the
Report's footer and the batch CSV's comment rows carry each bundle's pull date and age (`src/lib/crest/data-provenance.ts`).
Refresh only when asked: `npm run data:nd-dams` (downloads the NID CSV; `--from file.csv` to use a saved one; it fails on a schema change
or if the dam count drops below 800 or more than 20% below the committed bundle, unless `--allow-shrink`), then `npm run data:nd-dams-dwr -- --from data-raw/crest_dwr_navd88_<stamp> --pulled <UTC time>`
(a folder with `Dams_navd88.csv` and `Dugouts_navd88.csv`, converted to NAVD 88 by per-point VERTCON outside the app,
plus `raw/` and `grids/` `SHA256SUMS.txt`; the build checks each NAVD 88 value against stated + shift and refuses a
local value or a second conversion). `data-raw/` is git-ignored. Rebuild the desktop bundle and `Crest.html`
afterwards.

## Batch screen (many structures at once)

**Batch** (top right) runs many structures through the same path one dam takes on the desk: the breach, the inundation
layer the flood-size gate picks, the 911 address points inside it (with 3DEP pads), the screen and the triage
(`src/lib/crest/batch.ts`). The result is a Runner worklist, *Needs a Runner run* first, then *Uncertain*, then
*Clearly trivial*, with a CSV and **Open in desk** on each row. It is a screen to order work, not a class.

- **Pick by:** structures one by one (name, NID ID, `DWR-`, `DUG-` or `UNK-` number), one or more counties, one or
  more USGS HUC8 watersheds, or statewide. **Include** NID dams, DWR dams, DWR dugouts and DWR unknown structures;
  **Status** filters the DWR records (default: NID, Intact, Dugout, Pending, Under Construction, Unknown; Breached,
  Location inconclusive, Submerged and Inconclusive are listed but off).
- **DWR Unknown Structures** (from Nick's DWR export, 2026-11-05; that layer is not on the DWR map service): about
  3,600 of the 3,770 sit on exactly the point of a DWR Dams record with the same height and storage under a new DWR
  number (DWR has moved them into the Dams layer; e.g. UNK-2400 is DWR-16431), and six on an NID dam. Those are not
  listed twice: the UNK- number is shown in brackets on that record and finds it. The other 144 are listed as unknown
  structures.
- **The record is auto-derived.** Height and storage come from the inventory (NID dams as the picker fills them; DWR
  `max_dam_he` / `max_pool_v`; dugout `max_height` / `max_storag`). The crest is the DWR NAVD 88 elevation where the
  picker would offer it, else the highest 3DEP cell within 30 m of the inventory point; streambed = crest − H; the
  downstream direction points at the lowest cell within 500 m, and a dam line is synthesized across it; then the
  drainage is traced from the toe and the line is re-laid across the chord to the trace 4 km down the channel, so the
  straight strip points down the valley the water takes (kept when the trace is under 1 km or turns under 5°). New
  records default to piping. The record
  says so (*Batch record (auto-derived …)*) and its crest provenance is "DEM". Check the point, crest and direction
  before relying on a row.
- **Needs data:** a structure with no height, storage or location is listed and skipped. Statewide (default filters)
  that is about 4,400 of 9,700, mostly small DWR dams and dugouts with no height or storage on file.
- **Scale:** about 4–25 s a structure, mostly the 3DEP download, one structure per spare core at a time (at most four;
  the builds run in Web Workers, so the tab stays live). A county is minutes to under an hour; statewide (~5,300
  runnable) is hours. Only the small result rows are kept in browser storage (IndexedDB), so **Resume** works after a
  reload; the finished records (for Open in desk, without their terrain layer) stay in memory.
- **Command line:** `npm run batch:dams -- --out batch-out --state` runs the same thing in Node, appends each row to
  `batch-out/batch.jsonl` (one fsynced line per structure; start it again to resume, and a last line cut short by a
  kill is dropped with a warning and that structure runs again, while a corrupt line anywhere else stops the run and
  names the line) and writes `batch-out/batch.csv`. `--list` prints the selection and the time estimate. **Resume keeps
  different rows on the two sides, on purpose:** the command line keeps every row in `batch.jsonl` and writes them all
  to the CSV, whatever the current `--county`/`--ids` selection (the file is the record of a long run that may be
  resumed in pieces); the desk's Resume keeps only the rows whose structure is in the selection on screen, so its table
  and CSV always match what is picked. The CSV's first rows are comments: the disclaimer, the Crest build and engine
  versions, and the age of each inventory bundle. With `--dem /path/ND_1m_EPSG26914.vrt` the terrain comes from a local raster through
  `gdalwarp` (on PATH, or QGIS's copy on macOS; `--gdalwarp <path>` to name it) instead of 3DEP: no per-dam download
  (about 1.5 s a small dam instead of 7 s), and the crest is read on a 2 m grid, so it lands on the embankment top.
  The VRT must be in metres (`--dem-feet` for a feet DEM); mosaic gaps are NoData. The 911 address points still come
  from the ND GIS Hub.
- **Local DEM in the desktop app:** under **Terrain** (Batch screen and the Flood step), *Use a local DEM file (.vrt,
  .tif)…* opens a file dialog; pick e.g. `ND_1m_EPSG26914.vrt`. The choice is saved (`crest-settings.json`,
  `dem_path`) and every terrain read on the desk then comes from it through `gdalwarp`, a window around each dam only
  (`GET /api/dem`, `crest-desktop/crest/localdem.py`): the inundation layer, the batch, the breach point and the crest
  read at a new dam. *Back to USGS 3DEP* undoes it. A web page cannot open a file by path, so the web desk stays on 3DEP.

**Dam record helpers.**

- **Fill from DWR dams, dugouts** (Record step, under the NID picker): search any DWR dam or dugout by number, county or
  watershed and make a new record from it, the way the batch screen does (height, storage, area and length from DWR;
  crest from the terrain at the point; dam line, reservoir and breach point from the terrain).
- **Pick the dam's location on the map** (Record step): click the embankment to set latitude and longitude for a new
  dam; *Read the ground here* sets the DEM crest from the highest ground within 30 m of the point.
- **Breach point** (Flood step): *Pick breach point automatically* puts it where the ground at the downstream toe is
  lowest along the dam line (the old channel crossing; a dam line and reservoir point are laid from the terrain when
  missing, `src/lib/crest/breach-point.ts`), or *Click to place breach point* / *reservoir point* and click the map.

**Data.** `src/lib/crest/data/nd_structures.json` holds every DWR Dams record that is not an NID main dam (the NID entry
stands for it) and every DWR Dugouts record, keyed `DWR-<structure>` / `DUG-<structure>`, except status Removed / Not
Built: numbers, county, status, DWR hazard and purpose, and the USGS WBD HUC8 watershed by point-in-polygon. It also
gives each NID dam its HUC8. No owner, name, designer, remark or permit fields (DWR dams are labelled by number). DWR's
own `hu_sub_bas` names are not used: they mix two WBD vintages ("Maple" and "Maple River"). Refresh only when asked:
download Dams and Dugouts from the mapservice.dwr.nd.gov download form (*Full data set*, NAD83 geographic, shapefile),
`ogr2ogr -f CSV Dams.csv Dams.shp` (and Dugouts), put both CSVs and a `SHA256SUMS.txt` for the zips in
`data-raw/dwr_structures_<stamp>/`, then `npm run data:nd-structures -- --from data-raw/dwr_structures_<stamp> --pulled
<UTC>` (it downloads the WBD HUC8 polygons once into that folder). Rebuild the desktop bundle and `Crest.html`
afterwards.

## Judgment-zone homes (ND rule)

A home in the ACER TM 11 judgment zone screens **ND Medium by rule** (Nick, 10/30/2026): it is not counted as probable
loss of life and is not flagged High. Homes in the high zone stay High, and TM 11 lives in jeopardy is unchanged. The
old rule is kept as an option, off by default: on the desk, **Judgment zone** -> *Count as probable loss of life (High,
old option)* (`judgmentAsProbable: true` in the dam record) screens such a home High (flagged: judgment zone). A saved
record that chose it keeps it; a record without the setting screens Medium.

## Crest.html (one file)

`Crest.html` is the whole desk in a single file that opens in any browser, with no install and no server: the same
`src/` desk and engine as the web app and the desktop app, inlined by `npm run build:crest-html`. Do not edit it by
hand. `npm test` fails while it is older than the sources, so rebuild and commit it with the desktop bundle.

## Desktop app (Windows and macOS)

`crest-desktop/` opens the same Crest desk as the web app in a native window (pywebview). It is not a second
engine: it shows the static build of the web desk in `crest-desktop/crest/web/`, which is committed, so you do
not need Node to run it. Everything is bundled (Leaflet, leaflet-draw, icons, styles); only the basemap tiles
need the network. Your work is saved on the computer (the same autosave as the browser). Export buttons open a
Save dialog.

Needs **Python 3.10 or newer** to open the desk (3.14 is fine). Building `Crest.app` still needs 3.10–3.12; PyInstaller fails on 3.13 and newer.

### Windows 10/11
1. Install Python 3.12 from python.org (tick "Add python.exe to PATH") and Git.
2. Make sure the Microsoft Edge WebView2 Runtime is installed. It is built into Windows 11; on Windows 10 get it from Microsoft.
3. In PowerShell:
   ```
   git clone https://github.com/nickdbendickson/crest ; cd crest\crest-desktop
   py -3.12 -m venv .venv ; .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python run.py
   ```
4. Optional EXE: `pyinstaller crest.spec` gives `dist\Crest\Crest.exe`.

### macOS
1. Install Python 3.12 (python.org or `brew install python@3.12`) and Git.
2. In Terminal:
   ```
   git clone https://github.com/nickdbendickson/crest && cd crest/crest-desktop
   python3.12 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   python run.py
   ```
3. Optional app: `sh build_mac.sh` gives `dist/Crest.app`. It is unsigned, so the first time right-click it and choose Open.

The Report step can hand the open dam to the QGIS plugins already installed on this Mac. **Get layers** either uses the ND offline pack both plugin sets already share (`NDSUITE_DATA`, or `ndsuite/data_folder` in `~/.ndsuite/config.json`) or downloads a 3DEP DEM and NHD streams and waterbodies for the drawn area. **Write the dam folder** writes `<work>/dams/<id>/` (`dam.json`, the drawn line, breach point, reservoir point, downstream area, both hydrographs, the LISFLOOD `.par` / `.bci` / `.bdy`, and the flood strip) and does not start a model. **Write folder and run screening** does that write, runs `nddam:preflight` (tool 17), and starts `nddam:runchain` only when that preflight says READY. A FAIL stops there; a new `CrestDams` folder is expected to fail until it has the screening DEM and layers. **Run the flood model** is separate. Pick LISFLOOD-FP, LISFLOOD then ANUGA, LISFLOOD then HEC-RAS 2D, or the built-in screening engine. It needs a DEM (`dem.tif` in the work folder, the DEM field, the pack DEM, or `CREST_DEM`) and runs one dam: `ndbreach:rundam`, or `ndbreach_cli.py run` when both Point X and Point Y are filled in the DEM's coordinates. It does not start `ndbreach:batch`. The drawn line is longitude and latitude, so Crest does not send it as `DAM_LINES` and does not set `line_xy`. The flood strip is straight down the valley at the breach (the dam segment nearest the breach, not the end-to-end chord, and not later bends in the stream). The log is on the Report step. `Crest.html` has no local server, so those buttons work from `python run.py`, not from the one-file page.

### Linux
`pip install -r requirements.txt` pulls `pywebview[qt]` (PyQt6 + Qt WebEngine, about 500 MB), then `python run.py`.

### For developers
- After changing anything under `src/`, rebuild and commit the desktop bundle and the one-file desk:
  `npm run build:desktop && npm run build:crest-html`. `npm test` fails while `crest-desktop/crest/web/` or
  `Crest.html` is older than the sources.
- The Save dialog opens in the folder of the last export (remembered in `crest-settings.json` in the app's data folder).
- `npm run test:desktop` is a headless smoke test: it serves the bundle the way the app does and checks the
  served engine on DWR-2801 (H 27.50 ft, V 1,615.5 ac-ft, SA 154.5 ac: 28,671 cfs) and that nothing loads from a CDN.
