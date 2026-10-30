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
