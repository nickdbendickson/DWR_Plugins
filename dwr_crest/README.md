# Crest

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
- After changing anything under `src/`, rebuild and commit the desktop bundle: `npm run build:desktop`.
  `npm test` fails while `crest-desktop/crest/web/` is older than the sources.
- `npm run test:desktop` is a headless smoke test: it serves the bundle the way the app does and checks the
  served engine on DWR-2801 (H 27.50 ft, V 1,615.5 ac-ft, SA 154.5 ac: 28,671 cfs) and that nothing loads from a CDN.
