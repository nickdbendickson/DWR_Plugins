# DamSafety plugins for QGIS

Eight QGIS plugins (QGIS 3.28 LTR and later, QGIS 4 / Qt6 included) for dam-break hazard
classification under the North Dakota Dam Safety Standards (REG_05.2026) and USBR ACER TM 11.
They install separately and work together through a shared suite data folder.

| Plugin | Folder | What it does |
|---|---|---|
| DamSafety Setup | `DamSafety_Setup` | Start here for the DamSafety plugins: choose the suite data folder, then continue in DamSafety Runner's dam wizard. Also the suite setup / status window and the status dock. |
| DamSafety Data | `DamSafety_Data` | Keeps an offline copy of North Dakota structure and building-footprint data on this computer and downloads new versions when the sources change; builds the suite's offline pack and downloads NAIP imagery per dam. |
| DamSafety Runner | `DamSafety_Runner` | Popup window and Processing tools for dam breach flood modelling with LISFLOOD-FP and ANUGA: sunny-day and PMF scenarios with incremental results, sensitivity, cascades, nested grids, probabilistic outputs, outlet works, consequences, animation, a submittal report, a run manifest and ND Section 5.2 hazard classification with the ACER TM 11 charts. |
| DamSafety Hazard | `DamSafety_Hazard` | Consolidates dam hazard data and generates ACER-11 hazard classifications (Class 1 = High / Red, 2 = Judgment / Yellow, 3 = Low / Green) from depth, velocity and arrival rasters against structures, address points and building outlines. |
| DamSafety Screening | `DamSafety_Screening` | Batch screening of North Dakota dams, and of ponds and dugouts found in a water-body layer with LiDAR: inventory prep, terrain caching, storage estimation, breach hydrographs, quick triage, 2D flood simulation, hazard classification and hand-off of chosen dams to the Runner. Output is for prioritization only. |
| DamSafety PMP (prototype) | `DamSafety_PMP` | Internal prototype port of the North Dakota statewide PMP Evaluation Tool (AWA, 2021) to QGIS Processing, plus a StreamStats basin tool. Not validated against ArcGIS; the PMP study data is not included. |
| DamSafety Hazard Lite | `DamSafety_HazardLite` | One Processing tool: hmax / vmax at 911-addressed structures, roads (NDDOT AADT) and railroads, and the ND REG_05.2026 Section 5.2 hazard class, from breach depth and velocity rasters. Screening aid. |
| DamSafety Finder | `DamSafety_Finder` | Finds structures that are not on the lists: fills across streams in the LiDAR (dams, dry dams, road fills), ponds and dugouts in a water-body layer, and road / rail stream crossings (culverts). Each is marked listed, unsure or new against the DWR, NID and bridge / culvert layers you give; the not-listed dams and ponds are also written for Screening tool 1. Screening aid. |

Install order: **Setup → Data → Runner → Hazard**. Screening, PMP, Hazard Lite and Finder are optional
and can be added at any time. Each plugin checks the versions of the others it works with at
start-up (`shared/damsuite/versions.py`) and says what it needs.

## Installing

Three routes; all give the same plugin folders.

**1. QGIS plugin repository (recommended: updates show up in the Plugin Manager).**
In QGIS: *Plugins → Manage and Install Plugins… → Settings → Add…*, name it `DamSafety` and give
the URL

    https://nickdbendickson.github.io/dAmIt_Plugins/plugins.xml

Tick *Show also experimental plugins* (Runner, Screening, PMP, Hazard Lite and Finder are marked
experimental), then install the plugins from the *All* / *Not installed* list in the order above.
The index is written by the release workflow (`.github/workflows/release.yml`) at every `v*` tag
and points at the zips of that GitHub Release. It exists once the first release has run and the
repository owner has turned GitHub Pages on, once: *Settings → Pages → Build and deployment →
Deploy from a branch → `gh-pages` / root* (docs/CI.md, "Release").

**2. Install from ZIP.** Download `DamSafety_<Plugin>_<version>.zip` for each plugin from
[GitHub Releases](https://github.com/nickdbendickson/dAmIt_Plugins/releases) (check the file
against `SHA256SUMS`), then *Plugins → Manage and Install Plugins… → Install from ZIP*, one zip at
a time, in the order above. A zip built from a checkout (`python3 tools/build_release.py --out dist`)
installs the same way but carries no LISFLOOD-FP binary.

**3. Mac installer.** `tools/mac/` is a self-contained package for an Apple Silicon Mac: conda
QGIS 3.34 in its own profile, ANUGA, LISFLOOD-FP built on the Mac from the official 8.3 source
with the Runner's patch, and the plugin zips (`tools/mac/README_MAC.md`).

## First run

1. **Suite data folder.** *Plugins → Dam safety → Start here…* (DamSafety Setup) asks for the
   folder with the suite's layers and scans it as you type. The folder is either the offline
   pack (`ND_Dam_Suite_Pack`, read only) or a folder of your own layer files; the DWR dams layer
   is required, the others only add detail. Results never go into this folder: they go to the
   Dams folder (default `~/DamSafety/Dams`).
2. **Offline pack.** DamSafety Data's *Build offline pack…* downloads every pack layer
   (structures, roads, railroads, lifelines, hydro, DWR dams and dugouts, counties, HIFLD, ND DEQ,
   barriers, NLCD land cover) into a folder you pick and keeps it up to date; or copy a pack you
   were given. `docs/USER_GUIDE.md` has the pack formats and the pack check.
   **New to the suite?** Open *Plugins → Dam safety → Dam suite home…* and click *Try the
   sample dam…*: it sets up one real dam with its terrain and opens the breach Runner on it.
3. **LISFLOOD-FP** (the Runner's flood engine; a build with the arrival-depth patch is required,
   an unpatched one is refused).
   - *Windows, Linux x64, macOS Apple Silicon:* the Runner zip of a GitHub Release carries a
     patched LISFLOOD-FP 8.3 under `DamSafety_Runner/bin/<platform>/`; the Runner finds it on its
     own when nothing else is set. The same binaries are attached to the release on their own.
   - *Linux (build it yourself):* `cmake`, `g++`, `python3`
     (`sudo apt install cmake g++ python3`), the LISFLOOD-FP 8.3 source from Zenodo
     (record 13121119), then `bash DamSafety_Runner/scripts/build_lisflood_linux.sh <source> <install folder>`
     or the *Build LISFLOOD-FP…* button of the Suite status dock (DamSafety Setup), whose Log shows
     the output and which remembers the executable on success.
   - *macOS:* Xcode command line tools and Homebrew (`cmake`, `libomp`), then
     `bash DamSafety_Runner/scripts/build_lisflood_macos.sh <source> <install folder>`; or the
     release asset `lisflood-macos-arm64.tar.gz` (the binary with `libomp.dylib` beside it); or the
     Mac installer, which builds it. Intel Macs build it with the same script (no release build).
   - *Windows (build it yourself):* cross-compile on Linux with MinGW-w64
     (`DamSafety_Runner/scripts/build_lisflood_windows_cross.sh`), or apply
     `scripts/patch_lisflood.py` and build with CMake and Visual Studio.
   Set another build in *Processing → Options → Providers → DamSafety Runner* or in DamSafety
   Setup's *Suite setup / status…*; *Check the model installation* (the Runner window's Tools tab, or the
   Processing Toolbox) confirms the patch is active.
4. **ANUGA** (optional second engine): its own conda environment
   (`DamSafety_Runner/scripts/create_anuga_env.sh`, or `micromamba create -n anuga -c conda-forge
   python=3.12 anuga=4.0.1 scipy`), then its Python interpreter in the Runner's provider settings.
5. **Run a dam.** *Plugins → Dam safety → Run a dam…* (Runner): the wizard picks the dam, the
   DEM and the scenario and opens the run tool; the results dock and the Hazard hand-off follow.

## Documentation

- [docs/USER_GUIDE.md](docs/USER_GUIDE.md): the work-folder layout, settings, the offline pack,
  the tools and how-tos.
- [docs/CHANGELOG.md](docs/CHANGELOG.md): every change, newest first, plus the archived
  `metadata.txt` changelogs of each plugin.
- [docs/CI.md](docs/CI.md): the merge gate, the per-plugin suites, running CI locally, the
  release workflow and the QGIS plugin repository.
- [docs/gui-grammar.md](docs/gui-grammar.md): what a DamSafety window looks like and the lint
  that keeps it so.
- Each plugin folder has its own README with the tool-by-tool detail.

Repository: https://github.com/nickdbendickson/dAmIt_Plugins (issues:
https://github.com/nickdbendickson/dAmIt_Plugins/issues). LISFLOOD-FP is GPL-2.0 (University of
Bristol); the release ships the patched source it was built from.
