# HEC-RAS 6.6 on Linux: Wine drivers

`core/hecras_engine.py` uses these steps. They're **not** used by the LISFLOOD-FP or ANUGA engines.

- `terrain.cs`, `mesh.cs`, `drv.cs` are small C# programs that call HEC's RasMapperLib: terrain HDF, mesh, `complete`/`assoc`/`props`. `drv.exe complete` only completes an **existing** geometry HDF: on a new project it prints OK and writes nothing. Build them with any C# compiler against .NET Framework 4.8. Copy the `.exe` files **into the HEC-RAS folder** inside the Wine prefix (next to `RasMapperLib.dll` and `GDAL/`).
- One-time prefix setup: Wine 10, 64-bit prefix, `winetricks dotnet48`, then the HEC-RAS 6.6 Windows files. After that run `Ras.exe /regserver` and `RegAsm /codebase RasMapperLib.dll`, and register the VB6 OCX files from the installer archive with `regsvr32`.
- Linux solver: HEC's `Linux_RAS_v66.zip` (https://www.hec.usace.army.mil/software/hec-ras/downloads/Linux_RAS_v66.zip). Set `NDBREACH_HECRAS_LINUX_DIR` to the unzipped folder.
- Wine needs an X display (`NDBREACH_HECRAS_DISPLAY`, default `:77`). Start an Xvfb yourself.
- **HEC's Terms and Conditions (TCU):** Wine doesn't keep the acceptance, and the engine never clicks it. Accepting is the user's decision. A user who has accepted can set `NDBREACH_HECRAS_TCU_HOOK` to their own script, for example one that clicks "I agree" with xdotool. The engine starts that script next to `Ras.exe` and says so in the log.
- `muncie_check.py` reproduces the chain on HEC's Muncie example (about 3 minutes) and compares max WSE against HEC's run inputs.

## Step order (`hecras_engine.preprocess`)

Proven on Absaraka (DWR-2801, 2026-10-18) for a project written by `build_model`, which has no geometry HDF yet:

1. `terrain.exe`: `Terrain/Terrain.hdf`, registered in the `.rasmap`.
2. `Ras.exe -c`, pass 1: writes `<name>.g01.hdf` with the 2D mesh from the text geometry (65,739 cells, about 14 s). The engine stops it as soon as the HDF has cells. Skipped when the HDF already has them, as in HEC's examples.
3. `drv.exe assoc` (SetGeometryAssociation): about 4 s.
4. `drv.exe props` (2D property tables): about 22 s. Must print `tables complete`.
5. `Ras.exe -c`, pass 2: `.b01`, `.x01` and `.p01.tmp.hdf` (about 39 s), then the Linux `RasUnsteady`.

After each step the engine opens the geometry HDF read-only (h5py) and fails with a clear message, even on exit code 0, when the expected output is missing: no file, no 2D cells, no associated terrain, or no `Cells Volume Elevation` / `Faces Area Elevation` tables. A `<name>.p01.data_errors.txt` from Ras.exe is quoted in the error.

The plan's adaptive time step needs Min Courant < 0.5 x Max Courant (HEC-RAS data error otherwise). The defaults are max 1 / min 0.45 (diffusion wave: 2 / 0.9).
