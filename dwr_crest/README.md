# Crest

Desk screening of small embankment dams: breach hydrograph, schematic valley flood, receptor ranking
(ACER TM 11), and a datum-aware export. The engine is the pure `analyze()` in `src/lib/crest/engine.ts`.

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
| `npm run data:nd-dams` | Rebuilds `src/lib/crest/data/nd_dams.json` from the NID (ND dam picker). Run only when asked. |
| `npm run data:nd-dams-dwr -- --from data-raw/Dams.zip` | Rebuilds `src/lib/crest/data/nd_dams_dwr.json` from a downloaded DWR Dams layer. Run only when asked. |

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
drawn from the dam line downstream is not lost to rounding.

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
  are entered: from the DEM (NAVD 88), typed, or, where the DWR record is in NGVD 29 or NAVD 88, with **Use DWR
  elevations (checked against the dam file)** after a confirm. NGVD 29 values are converted by VERTCON at the dam's own
  location (ND00199: 1070.0 / 1042.5 ft NGVD 29 -> 1071.09 / 1043.59 ft NAVD 88, H 27.5 ft). DWR elevations on a
  local or blank datum are relative only and used just as an H check.
- **Surface area** is never filled. The NID normal-pool area is shown as a reference and the DWR maximum-pool area has
  a **Use** button.
- **NID hazard** is shown in grey as reported to the NID (FEMA class). It is not used by the screen and is not the class
  of record.

**Data and sources.** Both bundles are offline, loaded on demand (no network use by the picker):

| Bundle | Source | Downloaded |
|---|---|---|
| `src/lib/crest/data/nd_dams.json` | U.S. Army Corps of Engineers, National Inventory of Dams, ND state CSV (`nid.sec.usace.army.mil`). No owner names (an NID owner can be a private person): an unnamed dam is labelled by its NID ID only, e.g. "(unnamed) · ND00971" / "Unnamed dam ND00971". | 2026-10-29 |
| `src/lib/crest/data/nd_dams_dwr.json` | North Dakota Department of Water Resources, Dams layer (`mapservice.dwr.nd.gov`). Numeric fields, the vertical datum code, and the county where it differs; no owner, contact, remark or permit fields. Provisional, provided as is; field meanings are inferred. | 2026-10-29 |

The source URL, download time, SHA-256 of the raw file and the attribution are stored in each bundle and shown in the
picker footer, the Report, the dam.json export and **About the dam data** at the bottom of the desk. Refresh only when
asked: `npm run data:nd-dams` (downloads the NID CSV; `--from file.csv` to use a saved one; it fails on a schema change
or if the dam count drops below 800 or more than 20% below the committed bundle, unless `--allow-shrink`), then `npm run data:nd-dams-dwr -- --from data-raw/Dams.zip`
(a DWR Dams shapefile zip saved by hand). `data-raw/` is git-ignored. Rebuild the desktop bundle and `Crest.html`
afterwards.

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

Needs **Python 3.10, 3.11 or 3.12** (3.12 recommended; 3.13 is not supported by the pinned GUI and PyInstaller stack).

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

### Linux
`pip install -r requirements.txt` pulls `pywebview[qt]` (PyQt6 + Qt WebEngine, about 500 MB), then `python run.py`.

### For developers
- After changing anything under `src/`, rebuild and commit the desktop bundle and the one-file desk:
  `npm run build:desktop && npm run build:crest-html`. `npm test` fails while `crest-desktop/crest/web/` or
  `Crest.html` is older than the sources.
- The Save dialog opens in the folder of the last export (remembered in `crest-settings.json` in the app's data folder).
- `npm run test:desktop` is a headless smoke test: it serves the bundle the way the app does and checks the
  served engine on DWR-2801 (H 27.50 ft, V 1,615.5 ac-ft, SA 154.5 ac: 28,671 cfs) and that nothing loads from a CDN.
